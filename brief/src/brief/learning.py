"""The learning period: baselines are collected, but novelty and deviation findings stay quiet.

It counts from the first LIVE event (an imported backfill does not start it), so a fresh install
shows "learning, day N of 7" for a week. A backfill gives the baselines a head start, and the
length in days is a setting (thresholds.learning.days in playbook.yaml).
"""

from datetime import UTC, datetime

from brief.store import parse_time

DEFAULT_DAYS = 7


def learning_status(live_since: str | None, now: datetime | None = None, days: int = DEFAULT_DAYS) -> dict:
    """{"active": bool, "day": int, "of": int}; day is 0 before any live data has arrived."""
    now = now or datetime.now(UTC)
    if not live_since:
        return {"active": True, "day": 0, "of": days}
    elapsed = now - parse_time(live_since)
    day = max(1, elapsed.days + 1)
    return {"active": day <= days, "day": day, "of": days}
