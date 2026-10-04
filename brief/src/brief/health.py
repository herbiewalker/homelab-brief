"""Health: is the database readable, is the worker cycling, is the receiver accepting lines?

Docker's health check and the status page both use this, so a crashed worker or Vector no longer
leaves the container looking healthy. The worker proves it is alive by recording an
ingest.cycle_seconds metric every cycle, so no extra heartbeat is needed.
"""

import socket
from datetime import UTC, datetime

from brief.store import Store, parse_time


def receiver_ok(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def check_health(
    store: Store, syslog_port: int, interval: float, uptime_s: float, now: datetime | None = None
) -> dict:
    now = now or datetime.now(UTC)
    checks: dict[str, dict] = {}
    try:
        with store.read() as db:
            db.execute("SELECT 1").fetchone()
            last = db.execute(
                "SELECT ts FROM metric WHERE metric_id = 'ingest.cycle_seconds' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        checks["database"] = {"ok": True}
    except Exception as exc:  # report, don't raise: this is the health check
        checks["database"] = {"ok": False, "error": type(exc).__name__}
        last = None

    allowed = max(3 * interval, 120.0)
    if last is not None:
        age = (now - parse_time(last["ts"])).total_seconds()
        checks["worker"] = {
            "ok": age <= allowed,
            "state": "running" if age <= allowed else "stale",
            "last_cycle_s": int(age),
        }
    elif uptime_s < allowed:
        checks["worker"] = {"ok": True, "state": "starting"}
    else:
        checks["worker"] = {"ok": False, "state": "stale"}

    up = receiver_ok(syslog_port)
    checks["receiver"] = {"ok": up, "state": "listening" if up else "down"}
    return {"status": "ok" if all(c["ok"] for c in checks.values()) else "degraded", "checks": checks}
