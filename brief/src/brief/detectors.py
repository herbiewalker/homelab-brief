"""The detectors: pure functions from database rows to findings.

Nothing here touches the database or the clock; `brief.detect` gathers the rows and passes `now`.
That keeps every rule testable with a handful of literal rows.

Rules (current values live in brief/playbook.yaml; docs/guide/03-brief.md has the table):

  novelty      a signature first seen in the last 24 hours, or seen again after a long quiet spell
  deviation    a host's count for one signature in the last 24 hours against the median of the
               previous days' totals, flagged above median + k x MAD (the MAD is floored at 1)
  off_hours    a signature that is active at other times appearing in an hour of the day where it has
               not appeared in the previous days
  absence      a host quiet for longer than its normal gap (hourly)
  clock_skew   a source whose event times drift from receive time by more than a few minutes

Novelty, deviation, and off_hours need a learned baseline, so they say nothing during the learning
period. Absence and clock_skew do not depend on a baseline (absence falls back to a day when no gap
has been learned yet), so they run from the start.

Analysis windows are rolling: "the last 24 hours" and the 24-hour windows before it, never calendar
days, which avoids day-boundary and daylight-saving bugs.
"""

import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from brief.store import iso, parse_time

SEVERITY_RANK = {
    "emerg": 0,
    "alert": 1,
    "crit": 2,
    "err": 3,
    "warning": 4,
    "notice": 5,
    "info": 6,
    "debug": 7,
}
STORY_RANK = SEVERITY_RANK["warning"]  # warning and worse become stories; the rest are only counted


@dataclass(frozen=True)
class Finding:
    id: str  # stable: the same condition keeps the same id from run to run (alert dedupe uses it)
    kind: str
    title: str
    system: str | None = None
    host: str | None = None
    sig_id: int | None = None
    story: bool = True  # False: counted in the pipeline summary but not worth a line of its own
    value: float | None = None  # a size for ranking and for "got worse" (a count, seconds of drift)
    detail: dict = field(default_factory=dict)
    # False when growth is not "worse": absence grows by the hour, but a longer silence is the same problem
    escalates: bool = True


# ---------------------------------------------------------------- novelty


@dataclass(frozen=True)
class SignatureRow:
    sig_id: int
    app: str | None
    template: str | None
    first_seen: str
    last_seen: str
    prev_last_seen: str | None
    ignored: bool
    worst: str  # the worst severity name seen for it in the window ("info" when unknown)
    hosts: tuple[str, ...] = ()  # the busiest senders in the window
    systems: tuple[str, ...] = ()
    returned_at: str | None = None  # first activity after prev_last_seen (the end of the quiet spell)
    host_hours_in_gap: int | None = None  # hours its busiest host logged anything while it was away
    prior_days: int | None = None  # distinct days it was seen on before the quiet spell


def novelty(
    rows: Iterable[SignatureRow],
    now: datetime,
    learning: bool,
    quiet_days: int = 7,
    window_hours: int = 24,
    min_host_hours: int = 3,
    min_prior_days: int = 5,
) -> list[Finding]:
    """min_prior_days: a signature only "returned" if it used to be regular (seen on at least this many
    days before the quiet spell); one that shows up every few weeks is always "coming back".
    min_host_hours: a signature only "returned" if its host kept logging other things (at least
    this many active hours) while the signature was away. If the host itself was silent, or
    collection was down, the host came back, not the signature."""
    if learning:
        return []
    since = _iso(now - timedelta(hours=window_hours))
    out = []
    for r in rows:
        if r.ignored:
            continue
        new = r.first_seen >= since
        # it came back inside the window after at least quiet_days of nothing. `returned_at` (the
        # first activity after prev_last_seen) is what keeps a signature that returned days ago and
        # has been active since from being reported again every day.
        returned = (
            not new
            and r.prev_last_seen is not None
            and r.returned_at is not None
            and r.returned_at >= since
            and _parse(r.returned_at) - _parse(r.prev_last_seen) >= timedelta(days=quiet_days)
            and (r.host_hours_in_gap is None or r.host_hours_in_gap >= min_host_hours)
            and (r.prior_days is None or r.prior_days >= min_prior_days)
        )
        if not (new or returned):
            continue
        what = "New" if new else "Returned after a quiet spell"
        out.append(
            Finding(
                id=f"novelty-{r.sig_id}",
                kind="novelty",
                title=f"{what}: {short_label(r.app, r.template)}",
                system=r.systems[0] if r.systems else None,
                host=r.hosts[0] if r.hosts else None,
                sig_id=r.sig_id,
                story=SEVERITY_RANK.get(r.worst, 6) <= STORY_RANK,
                detail={
                    "state": "new" if new else "returned",
                    "worst_severity": r.worst,
                    "first_seen": r.first_seen,
                    "previous_last_seen": r.prev_last_seen,
                    "hosts": list(r.hosts),
                },
            )
        )
    return out


# ---------------------------------------------------------------- deviation


@dataclass(frozen=True)
class SeriesRow:
    host: str
    sig_id: int
    system: str | None
    today: int  # count in the last 24 hours
    history: tuple[int, ...]  # counts in each of the previous 24-hour windows (zeros included)
    label: str = ""


def mad(values: list[int]) -> float:
    median = statistics.median(values)
    return statistics.median([abs(v - median) for v in values])


def deviation(
    rows: Iterable[SeriesRow],
    learning: bool,
    min_count: int = 20,
    mad_multiple: float = 4,
    min_history_days: int = 3,
    min_ratio: float = 3,
) -> list[Finding]:
    """The history holds one count per earlier window in which the host was logging at all, zeros
    included for the signature, so an outage does not make everything "usually zero". min_ratio asks
    for a real multiple of the median as well, because a chatty signature's MAD is wide but its
    swings of two times are routine."""
    if learning:
        return []
    out = []
    for r in rows:
        if r.today < min_count or len(r.history) < min_history_days:
            continue
        median = statistics.median(r.history)
        limit = median + mad_multiple * max(mad(list(r.history)), 1.0)
        if r.today <= limit or (median > 0 and r.today < min_ratio * median):
            continue
        out.append(
            Finding(
                id=f"deviation-{r.host}-{r.sig_id}",
                kind="deviation",
                title=f"{r.label or f'signature {r.sig_id}'}: {r.today:,} in 24h, usually about {median:g}",
                system=r.system,
                host=r.host,
                sig_id=r.sig_id,
                value=float(r.today),
                detail={"today": r.today, "median": median, "limit": round(limit, 1), "days": len(r.history)},
            )
        )
    return out


# ---------------------------------------------------------------- off hours


@dataclass(frozen=True)
class HourRow:
    host: str
    sig_id: int
    system: str | None
    hour: int  # hour of day, UTC
    count: int  # in the last 24 hours
    history_hours: frozenset[int]  # hours of day it appeared in over the previous days
    label: str = ""
    history_days: int = 0  # distinct days it appeared on in the history
    history_events: int = 0  # how many times it appeared in the history


def off_hours(
    rows: Iterable[HourRow],
    learning: bool,
    min_count: int = 3,
    min_history_days: int = 3,
    min_history_events: int = 10,
    max_usual_hours: int = 8,
) -> list[Finding]:
    """An hour is only "unusual" for a signature with a real pattern: enough history (days and
    events) to call an hour unusual, and concentrated in few hours of the day. A signature that
    turns up at any hour (a random fault, a wandering client) has no hour that is unusual."""
    if learning:
        return []
    out = []
    for r in rows:
        # no history at all means a new signature, which novelty reports
        if r.count < min_count or not r.history_hours or r.hour in r.history_hours:
            continue
        if r.history_days < min_history_days or r.history_events < min_history_events:
            continue
        if len(r.history_hours) > max_usual_hours:
            continue
        out.append(
            Finding(
                id=f"off_hours-{r.host}-{r.sig_id}-{r.hour:02d}",
                kind="off_hours",
                title=f"{r.label or f'signature {r.sig_id}'} at {r.hour:02d}:00 UTC, an unusual hour",
                system=r.system,
                host=r.host,
                sig_id=r.sig_id,
                value=float(r.count),
                detail={"hour_utc": r.hour, "count": r.count, "usual_hours": sorted(r.history_hours)},
            )
        )
    return out


# ---------------------------------------------------------------- absence


@dataclass(frozen=True)
class HostRow:
    host: str
    system: str | None
    known: bool
    last_seen: str
    expected_gap_min: int | None = None
    clock_skew_s: float | None = None
    skew_updated: str | None = None


def silent_limit(expected_gap_min: int | None, rules: dict) -> timedelta:
    """gap_multiple x the learned gap, never below min_minutes or above max_hours; max_hours until a
    gap is learned, so a sparse source does not flap."""
    ceiling = rules["max_hours"] * 60
    if not expected_gap_min:
        return timedelta(minutes=ceiling)
    return timedelta(
        minutes=min(max(rules["gap_multiple"] * expected_gap_min, rules["min_minutes"]), ceiling)
    )


def absence(
    hosts: Iterable[HostRow], now: datetime, rules: dict, expected_systems: set[str]
) -> list[Finding]:
    out = []
    for h in hosts:
        if not h.known or h.system not in expected_systems:
            continue
        quiet = now - _parse(h.last_seen)
        if quiet <= silent_limit(h.expected_gap_min, rules):
            continue
        minutes = int(quiet.total_seconds() // 60)
        out.append(
            Finding(
                id=f"absence-{h.host}",
                kind="absence",
                title=f"{h.host} has been silent for {_duration(minutes)}",
                system=h.system,
                host=h.host,
                value=float(minutes),
                detail={
                    "last_seen": h.last_seen,
                    "silent_minutes": minutes,
                    "expected_gap_min": h.expected_gap_min,
                },
                escalates=False,
            )
        )
    return sorted(out, key=lambda f: f.detail["last_seen"])


# ---------------------------------------------------------------- clock skew


def clock_skew(
    hosts: Iterable[HostRow], now: datetime, max_minutes: float = 3, fresh_hours: int = 24
) -> list[Finding]:
    out = []
    fresh = _iso(now - timedelta(hours=fresh_hours))
    for h in hosts:
        if h.clock_skew_s is None or h.skew_updated is None or h.skew_updated < fresh:
            continue  # no recent measurement (a silent source is the absence detector's job)
        if abs(h.clock_skew_s) <= max_minutes * 60:
            continue
        minutes = h.clock_skew_s / 60
        out.append(
            Finding(
                id=f"clock_skew-{h.host}",
                kind="clock_skew",
                title=f"{h.host}'s clock is {abs(minutes):.0f} min {'ahead' if minutes > 0 else 'behind'}",
                system=h.system,
                host=h.host,
                value=abs(h.clock_skew_s),
                detail={"skew_seconds": round(h.clock_skew_s, 1), "measured": h.skew_updated},
            )
        )
    return out


# ---------------------------------------------------------------- helpers


def expected_gap(active_hours: list[int], min_hours_seen: int = 24, percentile: float = 0.95) -> int | None:
    """A host's normal quiet gap in minutes, from the hours (any consistent hour index) it was active.

    The gap between consecutive active hours is the resolution available, so a host that is active
    every hour has a gap of 60 minutes. None until the host has been active in enough hours."""
    hours = sorted(set(active_hours))
    if len(hours) < min_hours_seen:
        return None
    gaps = sorted(b - a for a, b in zip(hours, hours[1:], strict=False))
    index = min(len(gaps) - 1, int(percentile * len(gaps)))
    return gaps[index] * 60


_STAMP = re.compile(r"^(?:<NUM>[-:./ ]+){2,}<NUM>\s*(?:<\w>:?)?\s*")  # a masked date and time at the start
_CALL = re.compile(r"^(?:\[[^\]]+\])?[\w.\-]*\(\):\s*")  # "[TAG].function():" or "mod.function():"
_PAIR = re.compile(r'"(\w+)"\s*:\s*"([^"]*)"')
_WILD_RUN = re.compile(r"(?:<\*>\s*){2,}")
_JSON_KEYS = 3


def tidy_template(template: str) -> str:
    """A log template made readable for a person: no masked timestamp or `[TAG].function():` prefix, a
    JSON payload shown as its first few values, and runs of wildcards folded into one."""
    text = template.strip()
    text = _STAMP.sub("", text)
    text = _CALL.sub("", text)
    if text.startswith("{"):
        values = [v for _, v in _PAIR.findall(text) if v and "<" not in v]
        if values:
            text = ", ".join(values[:_JSON_KEYS])
    return _WILD_RUN.sub("<*> ", text).strip() or template.strip()


def short_label(app: str | None, template: str | None, limit: int = 110) -> str:
    body = tidy_template(template) if template else "(no template)"
    text = f"{app + ': ' if app else ''}{body}"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _duration(minutes: int) -> str:
    if minutes < 120:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // 1440} days"


_iso = iso
_parse = parse_time
