"""History the status pages draw: a 30-day state bar per system, 24-hour trend lines, and the incident
timeline.

The worker samples each system's state once an hour (`record`); the pages read it back. A day nobody
recorded is "none" (shown grey), never green: the bars must not claim a quiet network on a day the
container was off.
Read-only apart from `record`, which writes one tiny row per system per day.
"""

from datetime import datetime, timedelta

from brief.store import Store, iso

DAYS = 30
RANK = {"unknown": 0, "green": 1, "yellow": 2, "red": 3}
STATE_WORD = {
    "green": "operational",
    "yellow": "degraded",
    "red": "down",
    "unknown": "unknown",
    "none": "no data",
}
BAR_KIND = {"green": "ok", "yellow": "warn", "red": "bad", "unknown": "off", "none": "none"}


def record(store: Store, status: dict, now: datetime) -> int:
    """Keep the worst state each system showed today. Returns how many systems were noted."""
    day = f"{now:%Y-%m-%d}"
    rows = [
        (s["id"], s.get("state") if s.get("state") in RANK else "unknown")
        for s in status.get("systems") or []
        if s.get("id")
    ]
    if not rows or not status.get("run_ok", True):
        # A failed run says nothing about the systems; leave the day alone rather than record guesses.
        return 0
    with store.write() as tx:
        for system_id, state in rows:
            old = tx.db.execute(
                "SELECT worst, samples FROM system_day WHERE day = ? AND system_id = ?", (day, system_id)
            ).fetchone()
            if old is None:
                tx.db.execute(
                    "INSERT INTO system_day (day, system_id, worst, samples) VALUES (?, ?, ?, 1)",
                    (day, system_id, state),
                )
            else:
                worst = state if RANK[state] > RANK.get(old["worst"], 0) else old["worst"]
                tx.db.execute(
                    "UPDATE system_day SET worst = ?, samples = samples + 1 WHERE day = ? AND system_id = ?",
                    (worst, day, system_id),
                )
        tx.db.execute("DELETE FROM system_day WHERE day < ?", (f"{now - timedelta(days=400):%Y-%m-%d}",))
    return len(rows)


def bars(store: Store, now: datetime, days: int = DAYS) -> dict[str, list[dict]]:
    """system id -> `days` cells, oldest first: {day, state, kind, word}. Missing days are state "none"."""
    first = (now - timedelta(days=days - 1)).date()
    wanted = [(first + timedelta(days=i)).isoformat() for i in range(days)]
    with store.read() as db:
        rows = db.execute(
            "SELECT day, system_id, worst FROM system_day WHERE day >= ?", (wanted[0],)
        ).fetchall()
    by_system: dict[str, dict[str, str]] = {}
    for r in rows:
        by_system.setdefault(r["system_id"], {})[r["day"]] = r["worst"]
    out = {}
    for system_id, seen in by_system.items():
        out[system_id] = [_cell(day, seen.get(day, "none")) for day in wanted]
    return out


def empty_bar(now: datetime, days: int = DAYS) -> list[dict]:
    first = (now - timedelta(days=days - 1)).date()
    return [_cell((first + timedelta(days=i)).isoformat(), "none") for i in range(days)]


def _cell(day: str, state: str) -> dict:
    return {
        "day": day,
        "state": state,
        "kind": BAR_KIND.get(state, "off"),
        "word": STATE_WORD.get(state, state),
    }


def uptime_pct(cells: list[dict]) -> float | None:
    """Share of recorded days that were fully green. None when nothing was recorded."""
    seen = [c for c in cells if c["state"] != "none"]
    if not seen:
        return None
    return round(100.0 * sum(1 for c in seen if c["state"] == "green") / len(seen), 1)


def events_by_hour(store: Store, now: datetime, hours: int = 24) -> list[float]:
    """Events counted per hour for the last `hours` hours, oldest first (zero where nothing arrived)."""
    start = (now - timedelta(hours=hours - 1)).replace(minute=0, second=0, microsecond=0)
    keys = [start + timedelta(hours=i) for i in range(hours)]
    low = f"{start:%Y-%m-%d}{start.hour:02d}"
    with store.read() as db:
        rows = db.execute(
            "SELECT day || printf('%02d', hour) AS k, SUM(n) AS n FROM hourly "
            "WHERE day || printf('%02d', hour) >= ? GROUP BY k",
            (low,),
        ).fetchall()
    got = {r["k"]: float(r["n"]) for r in rows}
    return [got.get(f"{k:%Y-%m-%d}{k.hour:02d}", 0.0) for k in keys]


def metric_series(
    store: Store, metric_id: str, now: datetime, hours: int = 24, points: int = 24
) -> list[float]:
    """A metric's samples for the last `hours` hours, thinned to at most `points` values, oldest first."""
    low = iso(now - timedelta(hours=hours))
    with store.read() as db:
        rows = db.execute(
            "SELECT value FROM metric WHERE metric_id = ? AND ts >= ? AND value IS NOT NULL ORDER BY ts",
            (metric_id, low),
        ).fetchall()
    values = [float(r["value"]) for r in rows]
    if len(values) <= points:
        return values
    step = len(values) / points
    chunks = [values[int(i * step) : max(int(i * step) + 1, int((i + 1) * step))] for i in range(points)]
    return [sum(c) / len(c) for c in chunks]


def timeline(store: Store, now: datetime, limit: int = 12, days: int = 14) -> list[dict]:
    """Recent notification events as an incident feed: opened, got worse, repeated, or resolved."""
    low = iso(now - timedelta(days=days))
    with store.read() as db:
        rows = db.execute(
            "SELECT ts, finding_id, state, level, title FROM push WHERE ts >= ? AND channel = 'alert' "
            "ORDER BY ts DESC LIMIT ?",
            (low, limit),
        ).fetchall()
    words = {"new": "Opened", "repeat": "Still open", "escalated": "Got worse", "resolved": "Resolved"}
    kinds = {"new": "warn", "repeat": "off", "escalated": "bad", "resolved": "ok"}
    return [
        {
            "ts": r["ts"],
            "finding_id": r["finding_id"],
            "state": r["state"],
            "word": words.get(r["state"], r["state"]),
            "kind": kinds.get(r["state"], "off"),
            "title": r["title"] or r["finding_id"],
        }
        for r in rows
    ]
