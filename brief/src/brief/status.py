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

from brief import detect, detectors, health, selfmon
from brief.collectors import framework
from brief.learning import learning_status
from brief.store import Store, iso, parse_time
from brief.systems import Systems

SCHEMA = 1
MAX_LISTED_SILENT = 10
SELF_KEYS = (
    "memory_mb",
    "memory_limit_mb",
    "archive_mb",
    "archive_free_gb",
    "archive_free_pct",
    "storage_days_left",
    "events_last_hour",
)


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


def extra_numbers(store: Store, now: datetime, state_dir: Path | str | None = None) -> dict:
    """parse_failed_24h, ingest_lag_s and db_mb: the pipeline numbers that are not about hosts or events."""
    cutoff = iso(now - timedelta(hours=24))
    with store.read() as db:
        failed = db.execute(
            "SELECT COALESCE(SUM(value), 0) FROM metric WHERE metric_id = 'ingest.parse_failed' AND ts >= ?",
            (cutoff,),
        ).fetchone()[0]
        lag = db.execute(
            "SELECT value FROM metric WHERE metric_id = 'ingest.lag_seconds' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    path = Path(state_dir or os.environ.get("BRIEF_STATE", "/state")) / "baseline.db"
    size = round(path.stat().st_size / 1e6, 1) if path.exists() else None
    return {
        "parse_failed_24h": int(failed),
        "ingest_lag_s": int(lag["value"]) if lag else None,
        "db_mb": size,
        **selfmon.numbers(store, now),
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
    previous: dict | None = None,
    state_dir: Path | str | None = None,
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
    try:
        extras = extra_numbers(store, now, state_dir)
    except Exception:  # the database is unreadable (said above); these numbers just stay empty
        extras = {"parse_failed_24h": None, "ingest_lag_s": None, "db_mb": None, **dict.fromkeys(SELF_KEYS)}
    live = {
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
            **extras,
        },
        "silent_hosts": silent[:MAX_LISTED_SILENT],
        "network": network,
        "collectors": collector_block(store, env),
    }
    return merge_daily(live, previous, now)


def collector_block(store: Store, env=None) -> dict:
    """Each enabled collector's last outcome and numbers; empty when none is on or the file cannot be read."""
    try:
        return framework.status_block(store, env)
    except Exception:  # a collector problem never costs the status its other numbers
        return {}


def write_status(status: dict, out_dir: Path) -> Path:
    """Write out_dir/latest/status.json atomically (a tablet never reads half a file)."""
    folder = Path(out_dir) / "latest"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "status.json"
    tmp = folder / "status.json.tmp"
    tmp.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


# ---------------------------------------------------------------- the daily run's part of status.json

DAILY_FIELDS = ("systems", "stories", "developing", "quiet")
DAILY_STALE_HOURS = 36  # a daily run older than this means the daily run has stopped


def learning_status_for(store: Store, now: datetime, env=None) -> dict:
    days = learning_days(env)
    with store.read() as db:
        row = db.execute("SELECT value FROM meta WHERE key = 'live_since'").fetchone()
    return learning_status(row["value"] if row else None, now, days)


def status_document(d, network: dict | None = None) -> dict:
    """The complete schema-1 status.json for a daily run (`d` is a brief.digest.Digest)."""
    silent = [
        {"host": f.host, "system": f.system, "last_seen": f.detail.get("last_seen")}
        for f in d.findings
        if f.kind == "absence"
    ][:MAX_LISTED_SILENT]
    p = d.pipeline
    return {
        "schema": SCHEMA,
        "generated_at": iso(d.generated_at),
        "daily_generated_at": iso(d.generated_at),
        "run_ok": d.run_ok,
        "learning": d.learning,
        "headline": d.headline,
        **(d.to_status() if d.run_ok else {k: [] for k in DAILY_FIELDS}),
        "pipeline": {
            k: p.get(k)
            for k in (
                "events_24h",
                "new_signatures_24h",
                "hosts_silent",
                "hosts_checked",
                "parse_failed_24h",
                "ingest_lag_s",
                "db_mb",
                *SELF_KEYS,
            )
        },
        "silent_hosts": silent,
        "network": network,
    }


def merge_daily(live: dict, previous: dict | None, now: datetime) -> dict:
    """The hourly status (live health and pipeline numbers) with the last daily run's stories carried
    over. If the daily run failed or has gone stale the result says so and is not run_ok, rather than
    keeping yesterday's calm headline."""
    if not previous or not previous.get("daily_generated_at"):
        return live  # no daily run yet (a fresh install): only the live numbers exist
    out = {**live, "daily_generated_at": previous["daily_generated_at"]}
    for key in DAILY_FIELDS:
        out[key] = previous.get(key, [])
    age = now - parse_time(previous["daily_generated_at"])
    if age > timedelta(hours=DAILY_STALE_HOURS):
        out["run_ok"] = False
        out["headline"] = (
            f"No data today: the daily run has not completed for {int(age.total_seconds() // 3600)} hours"
        )
    elif not previous.get("run_ok", True):
        out["run_ok"] = False
        out["headline"] = previous.get("headline") or "No data today"
    elif live["run_ok"]:
        out["headline"] = previous.get("headline", live["headline"])
    # hosts that went silent since the daily run turn their system red
    silent_systems = {s["system"] for s in live.get("silent_hosts", []) if s.get("system")}
    out["systems"] = [
        {
            **s,
            "state": "red",
            "summary": "silent host" if s["summary"] == "no findings" else s["summary"] + ", silent host",
        }
        if s["id"] in silent_systems and s["state"] != "red"
        else s
        for s in out["systems"]
    ]
    return out


def health_problems(
    store: Store, syslog_port: int, interval: float, uptime_s: float, now: datetime
) -> list[str]:
    """What is wrong with the pipeline right now, in words (empty when it is healthy)."""
    report = health.check_health(store, syslog_port, interval, uptime_s, now)
    return [
        f"{name} {c.get('state') or c.get('error') or 'failing'}"
        for name, c in report["checks"].items()
        if not c["ok"]
    ]
