"""status.json (schema 1) and the numbers behind the MQTT sensors.

Everything here is derived from the baseline database, so it needs no collector. The same numbers
feed `/status.json`, the file the worker writes to /out/latest, and the MQTT sensors, so the three
can never disagree. All times are UTC. When the pipeline is unhealthy the file says so
(`run_ok: false` and a "no data" headline) instead of reporting a quiet network.

Silent hosts come from the absence detector (brief.detectors.absence): a host that has sent before and
has not sent for longer than `gap_multiple` times its learned gap (thresholds.absence in playbook.yaml,
never below `min_minutes` or above `max_hours`; `max_hours` until a gap is learned). Only hosts of
systems with `expected: true` count.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from brief import detect, detectors, health
from brief.learning import learning_status
from brief.store import Store, iso
from brief.systems import Systems

SCHEMA = 1
MAX_LISTED_SILENT = 10

# The registry ids this module can compute. The MQTT publisher only offers these.
DERIVED_METRICS = ("events_24h", "new_signatures_24h", "hosts_silent")

silent_limit = detectors.silent_limit  # the absence rule lives in brief.detectors


def absence_thresholds(env=None) -> dict:
    """thresholds.absence from playbook.yaml, falling back to the defaults on a missing or bad file."""
    return detect.thresholds(env)["absence"]


def learning_days(env=None) -> int:
    return detect.thresholds(env)["learning"]["days"]


def derive(store: Store, systems: Systems, rules: dict, now: datetime | None = None) -> dict:
    """events_24h, new_signatures_24h, hosts_silent, plus the list behind the silent count."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=24)
    key = f"{cutoff:%Y-%m-%d}{cutoff.hour:02d}"  # compares like (day, hour) pairs: 2026-10-0313
    expected = {s.id for s in systems.systems if s.expected}
    with store.read() as db:
        counted = db.execute(
            "SELECT COALESCE(SUM(n), 0) FROM hourly WHERE day || printf('%02d', hour) >= ?", (key,)
        ).fetchone()[0]
        suppressed = db.execute(
            "SELECT COALESCE(SUM(n), 0) FROM suppressed WHERE day || printf('%02d', hour) >= ?", (key,)
        ).fetchone()[0]
        new_signatures = db.execute(
            "SELECT COUNT(*) FROM signature WHERE first_seen >= ?", (iso(cutoff),)
        ).fetchone()[0]
    hosts = detect.host_rows(store)
    silent = [
        {"host": f.host, "system": f.system, "last_seen": f.detail["last_seen"]}
        for f in detectors.absence(hosts, now, rules, expected)
    ]
    return {
        "events_24h": int(counted + suppressed),
        "new_signatures_24h": int(new_signatures),
        "hosts_silent": len(silent),
        "silent": silent,
        "hosts_checked": sum(1 for h in hosts if h.known and h.system in expected),
    }


def headline(run_ok: bool, problems: list[str], numbers: dict, learning: dict) -> str:
    if not run_ok:
        return "No data: " + ("; ".join(problems) if problems else "the pipeline is not healthy")
    parts = [f"{numbers['events_24h']:,} events in 24 hours"]
    if numbers["new_signatures_24h"]:
        parts.append(f"{numbers['new_signatures_24h']} new signatures")
    if numbers["hosts_silent"]:
        parts.append(f"{numbers['hosts_silent']} silent hosts")
    text = ", ".join(parts)
    return f"{text} (learning, day {learning['day']} of {learning['of']})" if learning["active"] else text


def build_status(
    store: Store,
    systems: Systems,
    syslog_port: int,
    interval: float,
    uptime_s: float,
    env=None,
    now: datetime | None = None,
    network: dict | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    report = health.check_health(store, syslog_port, interval, uptime_s, now)
    run_ok = report["status"] == "ok"
    problems = [
        f"{name} {c.get('state') or c.get('error') or 'failing'}"
        for name, c in report["checks"].items()
        if not c["ok"]
    ]
    learning = {"active": True, "day": 0, "of": learning_days(env)}
    try:
        with store.read() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'live_since'").fetchone()
        learning = learning_status(row["value"] if row else None, now, learning["of"])
        numbers = derive(store, systems, absence_thresholds(env), now)
    except Exception as exc:  # the database is unreadable: say so, never invent numbers
        run_ok = False
        problems.append(f"database {type(exc).__name__}")
        numbers = {
            "events_24h": 0,
            "new_signatures_24h": 0,
            "hosts_silent": 0,
            "silent": [],
            "hosts_checked": 0,
        }
    silent = numbers.pop("silent")
    return {
        "schema": SCHEMA,
        "generated_at": iso(now),
        "run_ok": run_ok,
        "learning": learning,
        "headline": headline(run_ok, problems, numbers, learning),
        "pipeline": {
            "events_24h": numbers["events_24h"],
            "new_signatures_24h": numbers["new_signatures_24h"],
            "hosts_silent": numbers["hosts_silent"],
            "hosts_checked": numbers["hosts_checked"],
        },
        "silent_hosts": silent[:MAX_LISTED_SILENT],
        "network": network,
    }


def metric_values(status: dict) -> dict[str, int]:
    """The sensor values, or nothing at all when the run failed (the sensors then expire instead of
    showing a calm network)."""
    if not status["run_ok"]:
        return {}
    return {k: status["pipeline"][k] for k in DERIVED_METRICS}


def write_status(status: dict, out_dir: Path) -> Path:
    """Write out_dir/latest/status.json atomically (a tablet never reads half a file)."""
    folder = Path(out_dir) / "latest"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "status.json"
    tmp = folder / "status.json.tmp"
    tmp.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target
