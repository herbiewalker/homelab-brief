"""What the admin pages show, as plain data (no web code), so each page's content is easy to test.

Everything is read from the database and the status document; the only things this module ever writes
are alert acknowledgements and snoozes, and those go through brief.alerts (which audits them).
"""

from datetime import datetime, timedelta
from pathlib import Path

from brief import alerts, configguard, selfmon
from brief.store import Store, iso, parse_time

AUDIT_PAGE = 100
MAX_AUDIT_FILTER = 80


def ago(text: str | None, now: datetime) -> str:
    """'3 min ago', '2 h ago', '4 days ago' for a stored UTC time; 'never' when there is none."""
    if not text:
        return "never"
    try:
        seconds = int((now - parse_time(text)).total_seconds())
    except (ValueError, TypeError):
        return "unknown"
    if seconds < 0:
        return "just now"
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{seconds // 86400} days ago"


# ---------------------------------------------------------------- overview


def last_runs(store: Store, out_dir: Path, status: dict, now: datetime) -> list[dict]:
    """When each scheduled job last ran, and whether it worked."""
    with store.read() as db:
        meta = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM meta")}
    hourly = None
    try:
        hourly = datetime.fromtimestamp((out_dir / "latest" / "status.json").stat().st_mtime, tz=now.tzinfo)
    except OSError:
        pass
    weekly_dir = out_dir / "weekly"
    reports = sorted(weekly_dir.glob("*.md")) if weekly_dir.exists() else []
    daily = status.get("daily_generated_at")
    return [
        {"job": "Hourly status", "when": iso(hourly) if hourly else None, "ok": status.get("run_ok")},
        {"job": "Daily brief", "when": daily, "ok": status.get("run_ok") if daily else None},
        {
            "job": "Weekly report",
            "when": None,
            "ok": bool(reports) or None,
            "note": reports[-1].name if reports else meta.get("last_weekly_key"),
        },
    ]


def overview(store: Store, out_dir: Path, status: dict, health: dict, state_dir: Path, now: datetime) -> dict:
    rates = selfmon.source_rates(store, now)
    top = sorted(rates.items(), key=lambda kv: -kv[1])[:8]
    with store.read() as db:
        open_alerts = db.execute("SELECT COUNT(*) FROM alert WHERE state != 'resolved'").fetchone()[0]
        ai_today = db.execute(
            "SELECT COUNT(*) FROM ai_call WHERE ts >= ?", (iso(now - timedelta(days=1)),)
        ).fetchone()[0]
    return {
        "status": status,
        "health": health,
        "runs": [
            {**r, "when_text": ago(r["when"], now) if r["when"] else "-"}
            for r in last_runs(store, out_dir, status, now)
        ],
        "config_errors": configguard.read_errors(state_dir),
        "open_alerts": open_alerts,
        "ai_calls_today": ai_today,
        "top_sources": top,
    }


# ---------------------------------------------------------------- sources


def sources(store: Store, status: dict, now: datetime) -> list[dict]:
    """Every sender: system, last line, events in 24 hours and the last hour, and whether it is silent."""
    low = now - timedelta(hours=24)
    key = f"{low:%Y-%m-%d}{low.hour:02d}"
    rates = selfmon.source_rates(store, now)
    silent = {s["host"] for s in status.get("silent_hosts") or []}
    with store.read() as db:
        rows = db.execute(
            "SELECT h.host, h.system_id, h.known, h.last_seen, h.expected_gap_min, "
            "COALESCE((SELECT SUM(n) FROM hourly x WHERE x.host = h.host "
            "AND x.day || printf('%02d', x.hour) >= ?), 0) "
            "+ COALESCE((SELECT SUM(n) FROM suppressed s WHERE s.host = h.host "
            "AND s.day || printf('%02d', s.hour) >= ?), 0) AS n24 "
            "FROM host h ORDER BY h.known ASC, n24 DESC, h.host",
            (key, key),
        ).fetchall()
    return [
        {
            "host": r["host"],
            "system": r["system_id"],
            "known": bool(r["known"]),
            "last_seen": r["last_seen"],
            "last_seen_text": ago(r["last_seen"], now),
            "gap_min": r["expected_gap_min"],
            "events_24h": int(r["n24"]),
            "events_hour": rates.get(r["host"], 0),
            "silent": r["host"] in silent,
        }
        for r in rows
    ]


# ---------------------------------------------------------------- alerts


def alert_rows(store: Store, now: datetime) -> list[dict]:
    """Alerts that are open, acknowledged or snoozed (resolved ones are history, not a to-do list)."""
    book = alerts.load_book(store, include_resolved=False)
    return [
        {
            "id": a.finding_id,
            "title": a.title or a.finding_id,
            "kind": a.kind,
            "level": a.level,
            "route": a.route,
            "state": a.state,
            "opened": a.opened,
            "opened_text": ago(a.opened, now),
            "snooze_until": a.snooze_until,
            "pushed": a.pushed,
        }
        for a in sorted(book.values(), key=lambda a: (a.state != "open", a.opened or ""), reverse=False)
    ]


def alert_row(store: Store, finding_id: str, now: datetime) -> dict | None:
    return next((r for r in alert_rows(store, now) if r["id"] == finding_id), None)


# ---------------------------------------------------------------- audit log


def audit(
    store: Store, user: str = "", text: str = "", before: int | None = None, limit: int = AUDIT_PAGE
) -> dict:
    """Newest first. `before` is the rowid of the last row of the previous page."""
    where, args = [], []
    if user:
        where.append("user = ?")
        args.append(user[:MAX_AUDIT_FILTER])
    if text:
        where.append("(action LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\')")
        like = (
            "%" + text[:MAX_AUDIT_FILTER].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        args += [like, like]
    if before:
        where.append("rowid < ?")
        args.append(before)
    sql = "SELECT rowid AS id, ts, user, action, detail FROM audit_log"
    sql += (" WHERE " + " AND ".join(where)) if where else ""
    with store.read() as db:
        rows = [dict(r) for r in db.execute(sql + " ORDER BY rowid DESC LIMIT ?", (*args, limit + 1))]
        users = [r["user"] for r in db.execute("SELECT DISTINCT user FROM audit_log ORDER BY user LIMIT 50")]
    more = len(rows) > limit
    rows = rows[:limit]
    return {"rows": rows, "next": rows[-1]["id"] if more else None, "users": users}
