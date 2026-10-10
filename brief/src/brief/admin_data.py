"""What the admin pages show, as plain data (no web code), so each page's content is easy to test.

Everything is read from the database and the status document; the only things this module ever writes
are alert acknowledgements and snoozes, and those go through brief.alerts (which audits them).
"""

import re
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
        unmapped = db.execute("SELECT COUNT(*) FROM host WHERE known = 0").fetchone()[0]
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
        "unmapped_senders": unmapped,
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
    store: Store,
    user: str = "",
    text: str = "",
    before: int | None = None,
    limit: int = AUDIT_PAGE,
    kind: str = "",
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
    if kind and re.fullmatch(r"[a-z_]{1,30}", kind):
        where.append("action LIKE ? ESCAPE '\\'")
        args.append(kind.replace("_", "\\_") + ".%")
    sql = "SELECT rowid AS id, ts, user, action, detail FROM audit_log"
    sql += (" WHERE " + " AND ".join(where)) if where else ""
    with store.read() as db:
        rows = [dict(r) for r in db.execute(sql + " ORDER BY rowid DESC LIMIT ?", (*args, limit + 1))]
        users = [r["user"] for r in db.execute("SELECT DISTINCT user FROM audit_log ORDER BY user LIMIT 50")]
        kinds = [
            r["k"]
            for r in db.execute(
                "SELECT DISTINCT substr(action, 1, instr(action, '.') - 1) AS k FROM audit_log "
                "WHERE instr(action, '.') > 1 ORDER BY k LIMIT 40"
            )
            if re.fullmatch(r"[a-z_]{1,30}", r["k"] or "")
        ]
    more = len(rows) > limit
    rows = rows[:limit]
    return {"rows": rows, "next": rows[-1]["id"] if more else None, "users": users, "kinds": kinds}


# ---------------------------------------------------------------- the strip at the top of every admin page


def shell_status(store: Store, out_dir: Path) -> tuple[list[dict], dict]:
    """(status chips, counts) for the top bar and the side menu. Cheap (one small file and a few counts), and it
    never raises: a page must render even if the status is unreadable."""
    from brief.outputs import read_json

    chips: list[dict] = []
    counts = {"alerts": 0, "proposals": 0, "collectors_failing": 0}
    try:
        status = read_json(out_dir / "latest" / "status.json") or {}
        if status:
            ok = bool(status.get("run_ok"))
            chips.append(
                {"kind": "ok" if ok else "bad", "text": "Run ok" if ok else "No data today", "href": "/admin"}
            )
            silent = int((status.get("pipeline") or {}).get("hosts_silent") or 0)
            if silent:
                chips.append(
                    {
                        "kind": "warn",
                        "text": f"{silent} silent host{'s' if silent != 1 else ''}",
                        "href": "/admin/sources",
                    }
                )
            failing = [c for c in (status.get("collectors") or {}).values() if not c.get("ok")]
            counts["collectors_failing"] = len(failing)
            if failing:
                chips.append(
                    {
                        "kind": "bad",
                        "text": f"{len(failing)} collector{'s' if len(failing) != 1 else ''} failing",
                        "href": "/admin/collectors",
                    }
                )
        with store.read() as db:
            counts["alerts"] = db.execute(
                "SELECT COUNT(*) FROM alert WHERE state IN ('open', 'snoozed', 'acked')"
            ).fetchone()[0]
            counts["proposals"] = db.execute(
                "SELECT COUNT(*) FROM proposal WHERE status = 'pending'"
            ).fetchone()[0]
        if counts["proposals"]:
            chips.append(
                {
                    "kind": "info",
                    "text": f"{counts['proposals']} proposal{'s' if counts['proposals'] != 1 else ''} waiting",
                    "href": "/admin/ai#proposals",
                }
            )
    except Exception:  # noqa: BLE001 (a status strip is never worth an error page)
        pass
    return chips, counts


# ---------------------------------------------------------------- list pages (Task 35)

LEVEL_RANK = {"critical": 4, "error": 3, "warning": 2, "info": 1}
ALERT_SORTS = ("opened", "state", "level", "title", "pushed")
ALERT_STATES = ("open", "acked", "snoozed")


def alerts_view(store: Store, now: datetime, params: dict) -> dict:
    """The alert list for the Findings page: filtered by state, level, and text, sorted, and paged. The
    parameters come from the address, so each is checked against its allowed values before it is used."""
    from brief import listing

    state = listing.choice(str(params.get("state", "")), ALERT_STATES, "")
    q = str(params.get("q", "")).strip()[: listing.MAX_Q]
    every = alert_rows(store, now)
    levels = sorted({r["level"] for r in every if r["level"]}, key=lambda x: -LEVEL_RANK.get(x, 0))
    level = listing.choice(str(params.get("level", "")), levels, "")
    sort = listing.choice(str(params.get("sort", "")), ALERT_SORTS, "opened")
    direction = listing.choice(str(params.get("dir", "")), ("asc", "desc"), "desc")
    view = listing.Listing(
        "/admin/findings",
        {"state": state, "level": level, "q": q, "sort": sort, "dir": direction, "page": params.get("page")},
        {"sort": "opened", "dir": "desc", "page": "1"},
    )
    rows = [
        r
        for r in every
        if (not state or r["state"] == state)
        and (not level or r["level"] == level)
        and (not q or q.lower() in f"{r['title']} {r['kind']}".lower())
    ]
    keys = {
        "opened": lambda r: r["opened"] or "",
        "state": lambda r: ALERT_STATES.index(r["state"]) if r["state"] in ALERT_STATES else 9,
        "level": lambda r: LEVEL_RANK.get(r["level"], 0),
        "title": lambda r: (r["title"] or "").lower(),
        "pushed": lambda r: r["pushed"] or 0,
    }
    rows.sort(key=keys[sort], reverse=direction == "desc")
    page = listing.paginate(rows, listing.number(params.get("page")))
    return {"view": view, "page": page, "levels": levels, "states": ALERT_STATES, "total_open": len(every)}


SOURCE_SORTS = ("host", "system", "last_seen", "events_24h", "events_hour")
SOURCE_FILTERS = ("unknown", "silent", "known")


def sources_view(rows: list[dict], params: dict) -> dict:
    """Senders filtered by kind and text, sorted, and paged. With no sort chosen, senders that need naming come
    first, then the busiest, which is the order that helps a new install most."""
    from brief import listing

    flt = listing.choice(str(params.get("show", "")), SOURCE_FILTERS, "")
    q = str(params.get("q", "")).strip()[: listing.MAX_Q]
    sort = listing.choice(str(params.get("sort", "")), SOURCE_SORTS, "")
    direction = listing.choice(str(params.get("dir", "")), ("asc", "desc"), "desc")
    view = listing.Listing(
        "/admin/sources",
        {"show": flt, "q": q, "sort": sort, "dir": direction if sort else "", "page": params.get("page")},
        {"dir": "desc", "page": "1"},
    )
    shown = [
        r
        for r in rows
        if (
            not flt
            or (flt == "unknown" and not r["known"])
            or (flt == "silent" and r["silent"])
            or (flt == "known" and r["known"])
        )
        and (not q or q.lower() in f"{r['host']} {r['system'] or ''}".lower())
    ]
    if sort:
        key = {
            "host": lambda r: r["host"].lower(),
            "system": lambda r: (r["system"] or "").lower(),
            "last_seen": lambda r: r["last_seen"] or "",
            "events_24h": lambda r: r["events_24h"],
            "events_hour": lambda r: r["events_hour"],
        }[sort]
        shown.sort(key=key, reverse=direction == "desc")
    return {
        "view": view,
        "page": listing.paginate(shown, listing.number(params.get("page")), 50),
        "total": len(rows),
        "unknown": sum(1 for r in rows if not r["known"]),
        "silent": sum(1 for r in rows if r["silent"]),
        "filters": SOURCE_FILTERS,
    }
