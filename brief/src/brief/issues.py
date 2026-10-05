"""state.json: developing issues carried from one daily run to the next.

An issue is a finding id that keeps showing up. It is "developing" once it has been open on more than
one day, with a trend (growing, shrinking, same) from the size it was reported at, and it is dropped
after `QUIET_RUNS_TO_RESOLVE` daily runs without it, so a problem that went away does not linger.
Running the daily job twice on one date updates the numbers but does not count the day twice.

The state is plain data in and out (no files here), so it is easy to test.
"""

from datetime import date

from brief.detectors import Finding

SCHEMA = 1
QUIET_RUNS_TO_RESOLVE = 3
TREND_MARGIN = 0.15  # a change of less than 15% is "same"


def _trend(before: float | None, now: float | None) -> str:
    if before is None or now is None or before == 0:
        return "same"
    change = (now - before) / before
    if change > TREND_MARGIN:
        return "growing"
    if change < -TREND_MARGIN:
        return "shrinking"
    return "same"


def empty() -> dict:
    return {"schema": SCHEMA, "last_run": None, "issues": {}}


def update(prev: dict | None, findings: list[Finding], day: date) -> dict:
    """The new state after a daily run on `day` that reported `findings`."""
    prev = prev if isinstance(prev, dict) and prev.get("schema") == SCHEMA else empty()
    today = day.isoformat()
    rerun = prev.get("last_run") == today  # the same date again: refresh, but do not count it twice
    old_issues = prev.get("issues") or {}
    present = {f.id: f for f in findings}
    issues: dict[str, dict] = {}
    for fid, old in old_issues.items():
        if fid not in present:
            quiet = old.get("quiet_runs", 0) + (0 if rerun else 1)
            if quiet < QUIET_RUNS_TO_RESOLVE:
                issues[fid] = {**old, "quiet_runs": quiet}
    for fid, f in present.items():
        old = old_issues.get(fid)
        if old is None:
            days, first, trend = 1, today, "same"
        elif rerun:
            days, first, trend = old["days"], old["first_seen"], old.get("trend", "same")
        else:
            days, first, trend = old["days"] + 1, old["first_seen"], _trend(old.get("value"), f.value)
            if old.get("quiet_runs", 0):  # it went away for a while and came back: a fresh run of days
                days, first, trend = 1, today, "same"
        issues[fid] = {
            "kind": f.kind,
            "title": f.title,
            "system": f.system,
            "host": f.host,
            "first_seen": first,
            "last_seen": today,
            "days": days,
            "trend": trend,
            "value": f.value,
            "quiet_runs": 0,
        }
    return {"schema": SCHEMA, "last_run": today, "issues": issues}


def developing(state: dict, min_days: int = 2) -> list[dict]:
    """Issues open on at least `min_days` days and still open now, longest first."""
    out = []
    for fid, i in (state.get("issues") or {}).items():
        if i.get("quiet_runs", 0) == 0 and i.get("days", 0) >= min_days:
            out.append({"id": fid, "days": i["days"], "trend": i.get("trend", "same"), "kind": i["kind"],
                        "title": i["title"], "system": i.get("system")})  # fmt: skip
    return sorted(out, key=lambda d: (-d["days"], d["id"]))
