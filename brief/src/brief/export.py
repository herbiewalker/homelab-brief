"""`brief export`: a history file for "what's normal?" questions.

One CSV per host and signature, one row per day for the last N days (zeros included, so a gap is
visible): events that day, how many hours it was active, its busiest hour (UTC), the count in that
hour, and the worst severity. Small enough to hand to an assistant or open in a spreadsheet.

  /out/export/<host>-<sig_id>.csv
"""

import csv
import io
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from brief.digest import host_file
from brief.store import Store

COLUMNS = ("day", "events", "hours_active", "peak_hour_utc", "peak_events", "worst_severity")
SEVERITIES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")


def series(store: Store, host: str, sig_id: int, today: date, days: int = 30) -> list[list]:
    first = today - timedelta(days=days - 1)
    per_day: dict[str, dict[int, int]] = {}
    worst: dict[str, int] = {}
    with store.read() as db:
        for r in db.execute(
            "SELECT day, hour, severity, SUM(n) AS n FROM hourly WHERE host = ? AND sig_id = ? "
            "AND day BETWEEN ? AND ? GROUP BY day, hour, severity",
            (host, sig_id, first.isoformat(), today.isoformat()),
        ):
            per_day.setdefault(r["day"], {})
            per_day[r["day"]][r["hour"]] = per_day[r["day"]].get(r["hour"], 0) + r["n"]
            rank = SEVERITIES.index(r["severity"]) if r["severity"] in SEVERITIES else 7
            worst[r["day"]] = min(worst.get(r["day"], 7), rank)
    rows = []
    for i in range(days):
        day = (first + timedelta(days=i)).isoformat()
        hours = per_day.get(day, {})
        if hours:
            peak_hour = min(hours, key=lambda h: (-hours[h], h))
            rows.append(
                [day, sum(hours.values()), len(hours), peak_hour, hours[peak_hour], SEVERITIES[worst[day]]]
            )
        else:
            rows.append([day, 0, 0, "", 0, ""])
    return rows


def to_csv(rows: list[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(rows)
    return buf.getvalue()


def write_export(
    store: Store, out_dir: Path, host: str, sig_id: int, today: date | None = None, days: int = 30
) -> Path:
    today = today or datetime.now(UTC).date()
    path = Path(out_dir) / "export" / f"{host_file(host)[:-3]}-{sig_id}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(to_csv(series(store, host, sig_id, today, days)), encoding="utf-8", newline="")
    tmp.replace(path)
    return path


def top_signatures(store: Store, host: str, today: date, days: int, limit: int) -> list[int]:
    first = today - timedelta(days=days - 1)
    with store.read() as db:
        return [
            r["sig_id"]
            for r in db.execute(
                "SELECT sig_id, SUM(n) AS n FROM hourly WHERE host = ? AND day BETWEEN ? AND ? "
                "GROUP BY sig_id ORDER BY n DESC, sig_id LIMIT ?",
                (host, first.isoformat(), today.isoformat(), limit),
            )
        ]
