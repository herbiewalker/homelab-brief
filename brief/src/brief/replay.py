"""`brief replay`: run the detectors over stored history, as of each past moment, without writing.

For every run in the range it asks the detectors the question they would have been asked then (the
daily detectors once a day at `--at`, absence every hour), then runs the alert lifecycle over the
stories exactly as the live worker does (same code, `lifecycle.step`, held in memory), and reports what
was NEW, what RESOLVED, and what would have been sent: pushes (new, repeat, escalated, resolved), items
for the daily headline, and what quiet hours held back or the daily budget folded. Use it after every
change to playbook.yaml or priorities.yaml to see the effect in minutes instead of waiting days.

Every input is as of the replayed moment (see detect.py), so a replay reproduces what a live run
said. What a replay cannot reproduce is clock skew (measured live only) and "no data today" alerts
(pipeline health is not stored), and it uses today's system mapping, ignore rules, and learned host
gaps. It reads only: nothing is sent or stored, and acknowledgements cannot be replayed.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from brief import alerts, detect, grouping, lifecycle
from brief.detectors import Finding
from brief.lifecycle import Alert, Notification
from brief.routing import Priorities
from brief.store import Store
from brief.systems import Systems

REPLAY_DAILY = tuple(k for k in detect.DAILY if k != "clock_skew")


@dataclass
class Run:
    at: datetime
    kinds: tuple[str, ...]
    learning: bool
    findings: list[Finding]
    new: list[Finding]
    resolved: list[str]
    open_count: int
    notifications: list[Notification] = field(default_factory=list)
    held: list[str] = field(default_factory=list)  # waiting for quiet hours to end
    folded: list[str] = field(default_factory=list)  # over the daily budget


@dataclass
class Replay:
    start: date
    end: date
    at: time
    hourly: bool
    runs: list[Run] = field(default_factory=list)
    priorities: Priorities = field(default_factory=Priorities)

    def pushes(self, run: Run) -> list[Notification]:
        """The notifications that would have been pushed on their own (not resolved, not headline items)."""
        return [n for n in run.notifications if n.channel == "alert" and n.state != "resolved"]


def schedule(
    start: date, end: date, at: time = time(6, 0), hourly: bool = True
) -> list[tuple[datetime, tuple]]:
    """(moment, detector kinds) for each run, in order. The daily run also checks absence."""
    runs = []
    day = start
    while day <= end:
        hours = range(24) if hourly else (at.hour,)
        for h in hours:
            if h == at.hour:
                runs.append((datetime.combine(day, at, tzinfo=UTC), (*REPLAY_DAILY, "absence")))
            else:
                runs.append((datetime.combine(day, time(h, 0), tzinfo=UTC), ("absence",)))
        day += timedelta(days=1)
    return sorted(runs)


def replay(
    store: Store,
    systems: Systems,
    start: date,
    end: date,
    at: time = time(6, 0),
    hourly: bool = True,
    env=None,
    ignore_learning: bool = False,
    progress=None,
    with_lifecycle: bool = True,
) -> Replay:
    if end < start:
        raise ValueError("--to is before --from")
    priorities = Priorities.load(env)
    out = Replay(start, end, at, hourly, priorities=priorities)
    policy = lifecycle.Policy.from_priorities(priorities)
    labels = {s.id: s.label for s in systems.systems}
    activity = detect.HostActivity.load(store)
    open_now: dict[str, dict[str, Finding]] = {}  # per kind: finding id -> finding, as of its last run
    book: dict[str, Alert] = {}
    pushes_today, today = 0, None
    plan = schedule(start, end, at, hourly)
    for i, (moment, kinds) in enumerate(plan):
        result = detect.run(store, systems, moment, env, kinds, ignore_learning, activity)
        current: dict[str, dict[str, Finding]] = {k: {} for k in kinds}
        for f in result["findings"]:
            current.setdefault(f.kind, {})[f.id] = f
        new, resolved = [], []
        for kind in kinds:
            before = open_now.get(kind, {})
            new += [f for fid, f in current.get(kind, {}).items() if fid not in before]
            resolved += [fid for fid in before if fid not in current.get(kind, {})]
            open_now[kind] = current.get(kind, {})
        flat = [f for group in open_now.values() for f in group.values()]
        run = Run(moment, kinds, result["learning"], flat, new, sorted(resolved), len(flat))
        if with_lifecycle:
            if moment.date() != today:  # the daily budget starts over each UTC day
                today, pushes_today = moment.date(), 0
            evaluated = [f for kind in kinds for f in current.get(kind, {}).values()]
            stories = grouping.build_stories(evaluated, priorities, labels)
            step = lifecycle.step(
                book,
                alerts.items_from_stories(stories),
                set(kinds),
                moment,
                policy,
                pushes_today,
                headline_run="novelty" in kinds,
            )
            book, pushes_today = step.book, step.pushes_today
            run.notifications, run.held, run.folded = step.notifications, step.held, step.folded
        out.runs.append(run)
        if progress:
            progress(i + 1, len(plan), moment)
    return out


# ---------------------------------------------------------------- output


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def summary(rep: Replay) -> dict:
    new_by_kind = Counter(f.kind for run in rep.runs for f in run.new)
    sent = [n for run in rep.runs for n in run.notifications]
    pushes = [(run.at, n) for run in rep.runs for n in rep.pushes(run)]
    return {
        "runs": len(rep.runs),
        "new_total": sum(new_by_kind.values()),
        "new_by_kind": dict(new_by_kind),
        "resolved_total": sum(len(run.resolved) for run in rep.runs),
        "would_push": len(pushes),
        "notifications_by_state": dict(Counter(f"{n.channel}:{n.state}" for n in sent)),
        "held_runs": sum(1 for run in rep.runs if run.held),
        "folded_total": sum(len(run.folded) for run in rep.runs),
        "pushes": [
            {"at": _stamp(at), "id": n.finding_id, "state": n.state, "title": n.title} for at, n in pushes
        ],
    }


def as_dict(rep: Replay) -> dict:
    return {
        "from": rep.start.isoformat(),
        "to": rep.end.isoformat(),
        "daily_run_utc": rep.at.strftime("%H:%M"),
        "hourly_absence": rep.hourly,
        "summary": summary(rep),
        "runs": [
            {
                "at": _stamp(run.at),
                "kinds": list(run.kinds),
                "learning": run.learning,
                "open": run.open_count,
                "new": [
                    {**_plain(f), "rank": r.rank, "route": r.route} for f, r in rep.priorities.ranked(run.new)
                ],
                "resolved": run.resolved,
                "notifications": [
                    {
                        "id": n.finding_id,
                        "state": n.state,
                        "channel": n.channel,
                        "level": n.level,
                        "title": n.title,
                    }
                    for n in run.notifications
                ],
                "held": run.held,
                "folded": run.folded,
            }
            for run in rep.runs
            if run.new or run.resolved or run.notifications or run.held or run.folded
        ],
    }


def _plain(f: Finding) -> dict:
    return {
        "id": f.id,
        "kind": f.kind,
        "title": f.title,
        "system": f.system,
        "host": f.host,
        "story": f.story,
        "value": f.value,
        "detail": f.detail,
    }


def format_text(rep: Replay, verbose: bool = False, per_run: int = 6) -> list[str]:
    lines = [
        f"Replay {rep.start} to {rep.end}: daily detectors at {rep.at:%H:%M} UTC"
        + (", absence every hour" if rep.hourly else ", absence at the daily run")
        + ". Read-only: nothing was sent or stored.",
    ]
    learning = [run for run in rep.runs if run.learning and "novelty" in run.kinds]
    if learning:
        lines.append(
            f"The learning period was active for {len(learning)} daily runs "
            "(novelty, deviation and off-hours were quiet)."
        )
    current_day = None
    for run in rep.runs:
        if not (run.new or run.resolved or run.notifications or run.held or run.folded):
            continue
        if run.at.date() != current_day:
            current_day = run.at.date()
            lines.append(f"\n-- {current_day} --")
        lines.append(
            f"{run.at:%H:%M}  {len(run.new)} new, {len(run.resolved)} resolved  ({run.open_count} open)"
        )
        ranked = rep.priorities.ranked(run.new)
        for f, r in ranked if verbose else ranked[:per_run]:
            count_only = "" if f.story else " (count only)"
            lines.append(
                f"    NEW   {r.route:<14} r{r.rank:<3} {f.kind:<10} {f.title}{count_only}"
                + (f"  [{f.system}]" if f.system else "")
            )
        if not verbose and len(ranked) > per_run:
            lines.append(f"    ... and {len(ranked) - per_run} more (use --verbose)")
        for fid in run.resolved[: (None if verbose else per_run)]:
            lines.append(f"    CLEARED  {fid}")
        for n in run.notifications:
            where = "PUSH" if n.channel == "alert" else "HEADLINE"
            lines.append(f"    {where:<8} {n.state:<9} {n.level:<8} {n.title}")
        if run.held:
            lines.append(f"    held for the end of quiet hours: {len(run.held)}")
        if run.folded:
            lines.append(f"    over the daily budget, folded into the headline: {len(run.folded)}")
    s = summary(rep)
    lines += [
        "",
        f"{s['runs']} runs: {s['new_total']} new findings, {s['resolved_total']} cleared, "
        f"{s['would_push']} would have been pushed.",
    ]
    for kind, n in sorted(s["new_by_kind"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {kind:<12} {n}")
    for key, n in sorted(s["notifications_by_state"].items()):
        lines.append(f"  sent {key}: {n}")
    if s["held_runs"] or s["folded_total"]:
        lines.append(
            f"  held by quiet hours in {s['held_runs']} runs; folded by the budget: {s['folded_total']}"
        )
    for p in s["pushes"]:
        lines.append(f"  PUSH {p['at']}  {p['state']}: {p['title']}")
    lines.append("Acknowledgements and snoozes cannot be replayed; a live run applies them.")
    return lines
