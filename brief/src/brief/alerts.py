"""The alert lifecycle on a real database: load the alerts, run `lifecycle.step`, save, and send.

The order matters. The decisions are saved BEFORE anything is sent, so a crash between the two can at
worst lose one push (a repeat comes later), never send one twice. Sending is best effort: a channel
that is down (no MQTT broker yet) just means `delivered` stays 0 in the `push` table, and nothing is
retried, because a late "new" would be stale news.

Feedback (`ack`, `snooze_24h`, `snooze_7d`, from Home Assistant buttons or `brief ack`) is applied the
moment it arrives, and always recorded in the `feedback` table and the audit log.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from brief import grouping, lifecycle
from brief.jsonlog import log
from brief.lifecycle import Alert, Item, Notification, Policy, Result
from brief.routing import Priorities
from brief.store import Store, iso

COUNTED_STATES = ("new", "repeat", "escalated")
MAX_HEADLINE_CHARS = 280
HEADLINE_NAMES = 3


class Notifier(Protocol):
    def send_alert(self, payload: dict) -> bool: ...
    def send_headline(self, text: str) -> bool: ...


class LogNotifier:
    """The channel when nothing else is configured: the decision is logged and kept in the push table."""

    def send_alert(self, payload: dict) -> bool:
        log("alert", state=payload.get("state"), level=payload.get("level"), kind=payload.get("kind"))
        return False

    def send_headline(self, text: str) -> bool:
        log("headline", length=len(text))
        return False


# ---------------------------------------------------------------- database <-> engine


def _alert(row) -> Alert:
    def num(v):
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    return Alert(
        finding_id=row["finding_id"],
        kind=row["kind"] or "",
        title=row["title"] or "",
        level=row["level"] or "info",
        route=row["route"] or "push_now",
        urgent=bool(row["urgent"]),
        state=row["state"],
        opened=row["opened"],
        first_sent=row["first_sent"],
        last_sent=row["last_sent"],
        last_value=num(row["last_value"]),
        snooze_until=row["snooze_until"],
        pushed=row["pushed"],
        pending_headline=bool(row["pending_headline"]),
        pending_state=row["pending_state"],
        quiet_runs=row["quiet_runs"],
        rule=row["rule"],
    )


def load_book(store: Store, include_resolved: bool = True) -> dict[str, Alert]:
    with store.read() as db:
        sql = "SELECT * FROM alert" + ("" if include_resolved else " WHERE state != 'resolved'")
        return {r["finding_id"]: _alert(r) for r in db.execute(sql)}


def pushes_today(store: Store, now: datetime) -> int:
    """Non-urgent notifications already sent today (UTC), which is what the daily budget counts."""
    with store.read() as db:
        marks = ",".join("?" * len(COUNTED_STATES))
        return db.execute(
            "SELECT COUNT(*) FROM push WHERE ts >= ? AND channel = 'alert' AND urgent = 0 "
            f"AND state IN ({marks})",
            (f"{now:%Y-%m-%d}T00:00:00Z", *COUNTED_STATES),
        ).fetchone()[0]


def _fields(a: Alert) -> dict:
    return {
        "kind": a.kind,
        "rule": a.rule,
        "level": a.level,
        "state": a.state,
        "first_sent": a.first_sent,
        "last_sent": a.last_sent,
        "last_value": None if a.last_value is None else repr(a.last_value),
        "snooze_until": a.snooze_until,
        "title": a.title,
        "route": a.route,
        "urgent": int(a.urgent),
        "opened": a.opened,
        "pushed": a.pushed,
        "pending_headline": int(a.pending_headline),
        "pending_state": a.pending_state,
        "quiet_runs": a.quiet_runs,
    }


def save(store: Store, before: dict[str, Alert], result: Result, now: datetime) -> None:
    """Write what changed and log the notifications, in one transaction."""
    with store.write() as tx:
        for fid, alert in result.book.items():
            if before.get(fid) != alert:
                tx.upsert_alert(fid, **_fields(alert))
        for n in result.notifications:
            tx.add_push(n.finding_id, n.state, n.channel, n.level, n.title, n.urgent, n.at or now)


def mark_delivered(store: Store, notifications: list[Notification]) -> None:
    with store.write() as tx:
        for n in notifications:
            tx.db.execute(
                "UPDATE push SET delivered = 1 WHERE finding_id = ? AND ts = ? AND state = ? AND channel = ?",
                (n.finding_id, n.at, n.state, n.channel),
            )


# ---------------------------------------------------------------- stories to items, headline text


def items_from_stories(stories: list[grouping.Story]) -> list[Item]:
    from brief.digest import host_file

    out = []
    for s in stories:
        route = lifecycle_route(s)
        body = [s.title, *(f"biggest: {t}" for t in s.top)]
        if not s.grouped:
            body += [
                f"{k.replace('_', ' ')}: {v}" for k, v in s.detail.items() if isinstance(v, (str, int, float))
            ][:4]
        detail = f"hosts/{host_file(s.host)}" if s.host else None
        value = s.value if s.escalates else None  # a size that only grows with time is not "worse"
        out.append(Item(s.id, s.kind, s.title, route, value, s.system, s.host, tuple(body), detail))
    return out


def lifecycle_route(s: grouping.Story):
    from brief.routing import Route

    return Route(s.rank, s.route, s.urgent)


def headline_text(base: str, notifications: list[Notification]) -> str:
    """The daily headline with the items listed in it: "<headline> Also: a; b (+2 more)"."""
    listed = sorted(
        (n for n in notifications if n.channel == "headline"),
        key=lambda n: (-lifecycle.LEVEL_RANK.get(n.level, 0), n.title),
    )  # the more serious first
    if not listed:
        return base
    names = [n.title if n.state != "escalated" else f"{n.title} (worse)" for n in listed[:HEADLINE_NAMES]]
    text = f"{base} Also: {'; '.join(names)}"
    if len(listed) > HEADLINE_NAMES:
        text += f" (+{len(listed) - HEADLINE_NAMES} more)"
    return text if len(text) <= MAX_HEADLINE_CHARS else text[: MAX_HEADLINE_CHARS - 3] + "..."


# ---------------------------------------------------------------- the run


def run_lifecycle(
    store: Store,
    priorities: Priorities,
    stories: list[grouping.Story],
    evaluated: set[str],
    now: datetime,
    headline_run: bool = False,
    base_headline: str | None = None,
    notifier: Notifier | None = None,
) -> Result:
    """Decide, save, then send. `stories` are this run's stories for the kinds in `evaluated`."""
    policy = Policy.from_priorities(priorities)
    before = load_book(store)
    result = lifecycle.step(
        before, items_from_stories(stories), evaluated, now, policy, pushes_today(store, now), headline_run
    )
    save(store, before, result, now)
    notifier = notifier or LogNotifier()
    delivered = []
    for n in result.notifications:
        if n.channel == "alert" and notifier.send_alert(n.payload()):
            delivered.append(n)
    if headline_run and base_headline is not None:
        text = headline_text(base_headline, result.notifications)
        if notifier.send_headline(text):
            delivered += [n for n in result.notifications if n.channel == "headline"]
    if delivered:
        mark_delivered(store, delivered)
    if result.held:
        log("alerts_held", reason="quiet hours", count=len(result.held))
    if result.folded:
        log("alerts_folded", reason="daily budget", count=len(result.folded))
    return result


# ---------------------------------------------------------------- feedback


def apply_feedback(
    store: Store, finding_id: str, action: str, now: datetime | None = None, policy: Policy | None = None,
    source: str = "mqtt",
) -> str | None:  # fmt: skip
    """Apply an ack or snooze. Returns what happened, or None if it meant nothing. Always recorded."""
    now = now or datetime.now(UTC)
    policy = policy or Policy.from_priorities(Priorities.load())
    with store.read() as db:
        row = db.execute("SELECT * FROM alert WHERE finding_id = ?", (finding_id,)).fetchone()
    alert = _alert(row) if row else None
    outcome = lifecycle.apply_feedback(alert, action, now, policy)
    with store.write() as tx:
        tx.add_feedback(finding_id, action, now)
        if outcome and alert is not None:
            tx.upsert_alert(finding_id, state=alert.state, snooze_until=alert.snooze_until)
            tx.audit(source, f"alert.{action}", f"{finding_id}: {outcome}")
    log("feedback", action=action, outcome=outcome or "ignored", source=source)
    return outcome


def drain_feedback(
    store: Store, messages: Callable[[], tuple[str, str] | None], now: datetime | None = None
) -> int:
    """Apply every queued (finding_id, action) message; returns how many were applied."""
    applied = 0
    while (msg := messages()) is not None:
        if apply_feedback(store, msg[0], msg[1], now):
            applied += 1
    return applied


def describe(a: Alert) -> str:
    until = f", until {a.snooze_until}" if a.state == "snoozed" and a.snooze_until else ""
    sent = f", last sent {a.last_sent}" if a.last_sent else ", not sent yet"
    return f"{a.finding_id}  [{a.state}{until}] {a.level} {a.title}{sent}"


def timestamp(now: datetime) -> str:
    return iso(now)
