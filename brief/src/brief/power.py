"""Power and energy per system, from the readings the ha_statestream collector stores (power_reading).

A reading is one entity at one hour: `watts` for a power entity (an instantaneous figure), `kwh` for an energy
entity (a meter that only counts up, which Home Assistant calls total_increasing). Energy over a period is
worked out here, not stored:

  - a system with any energy meter uses the meters: the sum of the increases between consecutive readings, and
    a reading lower than the one before is a meter that restarted, so its new value counts
  - otherwise the power readings are integrated: each hourly sample stands for one hour

`power_drift` is the detector: a system's average draw over the last day against its own earlier days.
"""

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from brief.detectors import Finding
from brief.store import iso

HOUR = timedelta(hours=1)


def floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def energy_kwh(db, system: str, start: datetime, end: datetime) -> float | None:
    """kWh used by `system` in (start, end], or None when there is nothing to base it on."""
    s, e = iso(start), iso(end)
    rows = db.execute(
        "SELECT ts, entity, watts, kwh FROM power_reading "
        "WHERE system_id = ? AND ts > ? AND ts <= ? ORDER BY ts",
        (system, s, e),
    ).fetchall()
    meters = [r for r in rows if r["kwh"] is not None]
    has_meter = (
        bool(meters)
        or db.execute(
            "SELECT 1 FROM power_reading WHERE system_id = ? AND kwh IS NOT NULL AND ts <= ? LIMIT 1",
            (system, s),
        ).fetchone()
    )
    if has_meter:
        total, seen = 0.0, False
        for entity in {r["entity"] for r in meters}:
            before = db.execute(
                "SELECT kwh FROM power_reading "
                "WHERE system_id = ? AND entity = ? AND kwh IS NOT NULL AND ts <= ? "
                "ORDER BY ts DESC LIMIT 1",
                (system, entity, s),
            ).fetchone()
            previous = before["kwh"] if before else None
            for r in (m for m in meters if m["entity"] == entity):
                if previous is not None:
                    total += r["kwh"] - previous if r["kwh"] >= previous else r["kwh"]
                    seen = True
                previous = r["kwh"]
        return round(total, 3) if seen else None
    samples = [r["watts"] for r in rows if r["watts"] is not None]
    return round(sum(samples) / 1000, 3) if samples else None


def hourly_watts(db, system: str, start: datetime, end: datetime) -> list[float]:
    """The system's total draw for each hour in (start, end] that has power readings."""
    return [
        r["total"]
        for r in db.execute(
            "SELECT ts, SUM(watts) AS total FROM power_reading WHERE system_id = ? AND watts IS NOT NULL "
            "AND ts > ? AND ts <= ? GROUP BY ts ORDER BY ts",
            (system, iso(start), iso(end)),
        )
    ]


def systems_with_power(db, since: datetime) -> list[str]:
    return [
        r["system_id"]
        for r in db.execute(
            "SELECT DISTINCT system_id FROM power_reading WHERE system_id IS NOT NULL AND ts > ? ORDER BY 1",
            (iso(since),),
        )
    ]


def summary(db, labels: dict[str, str], now: datetime) -> list[dict]:
    """For the daily brief: each system's latest draw and its energy in the last 24 hours."""
    out = []
    for system in systems_with_power(db, now - 26 * HOUR):
        latest = db.execute(
            "SELECT ts, SUM(watts) AS total FROM power_reading WHERE system_id = ? AND watts IS NOT NULL "
            "AND ts <= ? GROUP BY ts ORDER BY ts DESC LIMIT 1",
            (system, iso(now)),
        ).fetchone()
        out.append(
            {
                "system": system,
                "label": labels.get(system, system),
                "watts": round(latest["total"], 1) if latest else None,
                "kwh_24h": energy_kwh(db, system, now - 24 * HOUR, now),
            }
        )
    return out


def weekly(db, labels: dict[str, str], week_start: datetime) -> list[dict]:
    """For the weekly report: energy this week and the week before, and this week's average draw."""
    end = week_start + timedelta(days=7)
    prev_start = week_start - timedelta(days=7)
    out = []
    for system in systems_with_power(db, prev_start):
        this = energy_kwh(db, system, week_start, end)
        before = energy_kwh(db, system, prev_start, week_start)
        watts = hourly_watts(db, system, week_start, end)
        if this is None and before is None:
            continue
        out.append(
            {
                "system": system,
                "label": labels.get(system, system),
                "kwh": this,
                "kwh_before": before,
                "avg_w": round(statistics.fmean(watts), 1) if watts else None,
            }
        )
    return out


@dataclass(frozen=True)
class DrawRow:
    system: str
    label: str
    recent: tuple[float, ...]  # hourly total watts over the recent window
    baseline: tuple[float, ...]  # hourly total watts over the days before it


def power_drift(
    rows: list[DrawRow],
    recent_hours: int = 24,
    baseline_days: int = 14,
    min_increase_pct: float = 25,
    min_increase_w: float = 10,
    min_samples: int = 48,
) -> list[Finding]:
    """A system drawing well above its own usual power. An increase only: a system that draws less is not a
    problem the product needs to raise. Needs enough hours in both windows, a percentage rise, and a rise in
    watts big enough to matter (so a 3 W device doubling is not news)."""
    out = []
    for r in rows:
        if len(r.recent) < max(1, recent_hours // 2) or len(r.baseline) < min_samples:
            continue
        now_w, usual = statistics.fmean(r.recent), statistics.fmean(r.baseline)
        if usual <= 0:
            continue
        rise = now_w - usual
        pct = rise / usual * 100
        if rise < min_increase_w or pct < min_increase_pct:
            continue
        out.append(
            Finding(
                id=f"power_drift:{r.system}",
                kind="power_drift",
                title=(
                    f"{r.label} is drawing {now_w:.0f} W on average, {pct:.0f}% above its usual {usual:.0f} W"
                ),
                system=r.system,
                value=round(pct, 1),
                detail={
                    "system_name": r.label,
                    "watts": round(now_w),
                    "baseline_watts": round(usual),
                    "increase_pct": round(pct),
                    "recent_hours": recent_hours,
                    "baseline_days": baseline_days,
                },
            )
        )
    return sorted(out, key=lambda f: -(f.value or 0))


def draw_rows(
    db, labels: dict[str, str], now: datetime, recent_hours: int, baseline_days: int
) -> list[DrawRow]:
    recent_start = now - recent_hours * HOUR
    base_start = recent_start - timedelta(days=baseline_days)
    out = []
    for system in systems_with_power(db, base_start):
        out.append(
            DrawRow(
                system,
                labels.get(system, system),
                tuple(hourly_watts(db, system, recent_start, now)),
                tuple(hourly_watts(db, system, base_start, recent_start)),
            )
        )
    return out
