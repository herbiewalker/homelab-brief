"""The alert lifecycle: when a story becomes a push, and when it stays quiet.

A lasting problem must not nag. Every story whose route sends notifications (`push_now`, and
`daily_headline`) goes through this, once per run:

  new        the first time it is notified
  repeat     still open after `renotify_hours`
  escalated  it got worse (its level went up, or its size grew by `escalate_factor`), whatever the interval
  resolved   a notified story that stayed away for `resolve_after_quiet_runs` runs

Around that:

  quiet hours   a non-urgent push waits until the quiet hours end (it stays "new", so it still goes out)
  daily budget  non-urgent pushes beyond `daily_push_budget` a day fold into the next daily headline
  ack / snooze  an acknowledged or snoozed story drops to the digest until it gets worse (a snooze also
                ends by itself)
  urgent        bypasses quiet hours and the budget

`step` is pure: it takes the alerts as they were, this run's stories, and the time, and returns the
alerts as they are now plus the notifications to send. It reads no clock and no database, so the live
worker and `brief replay` run exactly the same code. Times in the config (quiet hours) are UTC.

Resolution is per kind: a run that did not evaluate a kind (the hourly run only looks at absence)
neither confirms nor clears its alerts.
"""

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta

from brief.routing import Priorities, Route
from brief.store import iso, parse_time

STATES = ("open", "acked", "snoozed", "resolved")
LEVEL_RANK = {"info": 0, "notice": 1, "warning": 2, "critical": 3}
SNOOZE = re.compile(r"^snooze_(\d+)([hd])$")


def level_for(route: Route) -> str:
    if route.urgent:
        return "critical"
    return {"push_now": "warning", "daily_headline": "notice"}.get(route.route, "info")


@dataclass(frozen=True)
class Policy:
    renotify_hours: float = 24
    escalate_on_worse: bool = True
    escalate_factor: float = 1.5
    escalate_min_minutes: float = 60
    send_resolved: bool = True
    resolve_after_quiet_runs: int = 3
    daily_push_budget: int = 5
    quiet_start: time | None = None
    quiet_end: time | None = None
    snooze_options_h: tuple[int, ...] = (24, 168)

    @classmethod
    def from_priorities(cls, p: Priorities) -> "Policy":
        life, quiet = p.lifecycle, p.quiet_hours
        default = cls()
        return cls(
            renotify_hours=float(life.get("renotify_hours", default.renotify_hours)),
            escalate_on_worse=bool(life.get("escalate_on_worse", default.escalate_on_worse)),
            escalate_factor=float(life.get("escalate_factor", default.escalate_factor)),
            escalate_min_minutes=float(life.get("escalate_min_minutes", default.escalate_min_minutes)),
            send_resolved=bool(life.get("send_resolved", default.send_resolved)),
            resolve_after_quiet_runs=int(
                life.get("resolve_after_quiet_runs", default.resolve_after_quiet_runs)
            ),
            daily_push_budget=int(life.get("daily_push_budget", default.daily_push_budget)),
            quiet_start=_clock(quiet.get("start")),
            quiet_end=_clock(quiet.get("end")),
            snooze_options_h=tuple(int(h) for h in life.get("snooze_options_h", default.snooze_options_h)),
        )


def _clock(text) -> time | None:
    try:
        hh, mm = str(text).split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError, AttributeError):
        return None


def in_quiet_hours(now: datetime, start: time | None, end: time | None) -> bool:
    """True inside [start, end), which may wrap midnight (22:00 to 06:30)."""
    if start is None or end is None or start == end:
        return False
    t = now.timetz().replace(tzinfo=None)
    return start <= t < end if start < end else (t >= start or t < end)


@dataclass
class Item:
    """One story to track: what the lifecycle needs from a digest story."""

    id: str
    kind: str
    title: str
    route: Route
    value: float | None = None
    system: str | None = None
    host: str | None = None
    body: tuple[str, ...] = ()
    detail: str | None = None


@dataclass
class Alert:
    finding_id: str
    kind: str = ""
    title: str = ""
    level: str = "info"
    route: str = "push_now"
    urgent: bool = False
    state: str = "open"
    opened: str | None = None
    first_sent: str | None = None
    last_sent: str | None = None
    last_value: float | None = None
    snooze_until: str | None = None
    pushed: int = 0  # how many times it was notified
    pending_headline: bool = False  # waiting for the next daily headline (budget fold or a headline route)
    pending_state: str | None = None  # what it would have been (new, repeat, escalated)
    quiet_runs: int = 0
    rule: str | None = None


@dataclass
class Notification:
    finding_id: str
    state: str  # new | repeat | escalated | resolved
    channel: str  # alert (its own push) | headline (listed in the daily headline)
    level: str
    title: str
    kind: str = ""
    urgent: bool = False
    system: str | None = None
    host: str | None = None
    body: tuple[str, ...] = ()
    detail: str | None = None
    at: str = ""
    actions: tuple[str, ...] = ("ack", "snooze_24h", "snooze_7d")

    def payload(self) -> dict:
        """The JSON that goes to homelab/brief/alert (docs/guide/04-publish.md)."""
        return {
            "finding_id": self.finding_id,
            "state": self.state,
            "level": self.level,
            "title": self.title,
            "body": "\n".join(self.body),
            "kind": self.kind,
            "system": self.system,
            "host": self.host,
            "urgent": self.urgent,
            "detail": self.detail,
            "at": self.at,
            "actions": list(self.actions),
        }


@dataclass
class Result:
    book: dict[str, Alert]
    notifications: list[Notification] = field(default_factory=list)
    pushes_today: int = 0
    held: list[str] = field(default_factory=list)  # waiting for quiet hours to end
    folded: list[str] = field(default_factory=list)  # over budget, waiting for the daily headline


def _worse(a: Alert, item: Item, level: str, policy: Policy) -> bool:
    if not policy.escalate_on_worse or not a.pushed:
        return False
    if LEVEL_RANK.get(level, 0) > LEVEL_RANK.get(a.level, 0):
        return True
    before, now = a.last_value, item.value
    if before is None or now is None:
        return False
    return now >= before * policy.escalate_factor if before > 0 else now > 0


def _since(then: str | None, now: datetime) -> timedelta | None:
    return None if not then else now - parse_time(then)


def step(
    book: dict[str, Alert],
    items: list[Item],
    evaluated: set[str],
    now: datetime,
    policy: Policy,
    pushes_today: int = 0,
    headline_run: bool = False,
) -> Result:
    """One run of the lifecycle. `items` are this run's stories for the kinds in `evaluated`;
    `headline_run` is true for the daily run, which is when a headline is sent."""
    new_book = {k: replace(v) for k, v in book.items()}
    out = Result(new_book, [], pushes_today)
    stamp = iso(now)
    quiet = in_quiet_hours(now, policy.quiet_start, policy.quiet_end)
    present = set()

    for item in sorted((i for i in items if i.route.route in ("push_now", "daily_headline")),
                       key=lambda i: (-i.route.rank, i.id)):  # fmt: skip
        present.add(item.id)
        level = level_for(item.route)
        a = new_book.get(item.id)
        if a is None or a.state == "resolved":
            a = new_book[item.id] = Alert(item.id, opened=stamp)
        a.kind, a.title, a.route, a.urgent = item.kind, item.title, item.route.route, item.route.urgent
        a.quiet_runs = 0
        worse = _worse(a, item, level, policy)
        state = None
        if a.state in ("acked", "snoozed"):
            ended = a.state == "snoozed" and a.snooze_until and now >= parse_time(a.snooze_until)
            if worse or ended:
                a.state, a.snooze_until = "open", None
                state = "escalated" if worse else ("repeat" if a.pushed else "new")
        else:
            gap = _since(a.last_sent, now)
            if not a.pushed:
                state = "new"
            elif worse and (gap is None or gap >= timedelta(minutes=policy.escalate_min_minutes)):
                state = "escalated"
            elif gap is None or gap >= timedelta(hours=policy.renotify_hours):
                state = "repeat"
        if state is None:
            if item.route.route == "daily_headline" and not a.pushed:
                a.pending_headline, a.pending_state = True, a.pending_state or "new"
            a.level = level if not a.pushed else a.level
            continue
        channel = "alert" if item.route.route == "push_now" else "headline"
        if channel == "alert" and a.pending_headline and not item.route.urgent:
            continue  # already folded: it goes out with the daily headline, not on its own
        if channel == "alert" and not item.route.urgent:
            if quiet:
                out.held.append(item.id)  # stays unsent; it goes out as soon as the quiet hours end
                continue
            if out.pushes_today >= policy.daily_push_budget:
                a.pending_headline, a.pending_state = True, state
                out.folded.append(item.id)
                continue
        if channel == "headline" and not headline_run:
            a.pending_headline, a.pending_state = True, state  # listed in the next daily headline
            continue
        _send(out, a, item, state, channel, level, stamp)

    if headline_run:  # alerts that were folded or held for the headline go out with it now
        for a in sorted(new_book.values(), key=lambda x: x.finding_id):
            if a.pending_headline and a.finding_id in present and a.state == "open":
                item = next(i for i in items if i.id == a.finding_id)
                _send(out, a, item, a.pending_state or "new", "headline", level_for(item.route), stamp)

    for a in new_book.values():
        if a.finding_id in present or a.state == "resolved" or a.kind not in evaluated:
            continue
        a.quiet_runs += 1
        if a.quiet_runs < policy.resolve_after_quiet_runs:
            continue
        if a.pushed and policy.send_resolved:
            if quiet and not a.urgent:
                a.quiet_runs = policy.resolve_after_quiet_runs  # say so after the quiet hours
                continue
            out.notifications.append(
                Notification(
                    a.finding_id,
                    "resolved",
                    "alert",
                    a.level,
                    f"Resolved: {a.title}",
                    a.kind,
                    a.urgent,
                    at=stamp,
                )  # fmt: skip
            )
        a.state, a.pending_headline, a.pending_state = "resolved", False, None
    return out


def _send(out: Result, a: Alert, item: Item, state: str, channel: str, level: str, stamp: str) -> None:
    a.pushed += 1
    a.first_sent = a.first_sent or stamp
    a.last_sent, a.last_value, a.level = stamp, item.value, level
    a.pending_headline, a.pending_state, a.state = False, None, "open"
    if channel == "alert" and not item.route.urgent and state != "resolved":
        out.pushes_today += 1
    out.notifications.append(
        Notification(
            item.id,
            state,
            channel,
            level,
            item.title,
            item.kind,
            item.route.urgent,
            item.system,
            item.host,
            item.body,
            item.detail,
            stamp,
        )  # fmt: skip
    )


# ---------------------------------------------------------------- feedback


def apply_feedback(a: Alert | None, action: str, now: datetime, policy: Policy) -> str | None:
    """Apply an ack or snooze from Home Assistant to an alert. Returns what happened, or None when
    the feedback means nothing (an unknown alert, an alert already resolved, a snooze length that is
    not allowed)."""
    if a is None or a.state == "resolved":
        return None
    if action == "ack":
        a.state, a.snooze_until = "acked", None
        return "acked"
    m = SNOOZE.match(action)
    if m:
        hours = int(m.group(1)) * (24 if m.group(2) == "d" else 1)
        if hours not in policy.snooze_options_h:
            return None
        a.state, a.snooze_until = "snoozed", iso(now + timedelta(hours=hours))
        return f"snoozed {hours}h"
    return None
