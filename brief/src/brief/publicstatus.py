"""What the public status page (`/`) shows, as plain data: one overall state, a row per system with its
30-day bar, the recent incident feed, and a few pipeline numbers.

Read-only and login-free like `/status.json`, which carries the same facts. "Operational" is only ever said
when the last run was ok; a failed run says so instead, because a quiet-looking page with no data behind it
is the worst answer.
"""

from datetime import datetime

from brief import overviewdata, systemhistory
from brief.store import Store

LEVELS = {"ok": "All systems operational", "warn": "Some systems need a look", "bad": "A system is down"}


def overall(status: dict) -> dict:
    """{kind, title, note}: the banner. Never 'operational' when the data behind it is missing."""
    systems = status.get("systems") or []
    red = [s for s in systems if s.get("state") == "red"]
    yellow = [s for s in systems if s.get("state") == "yellow"]
    silent = int((status.get("pipeline") or {}).get("hosts_silent") or 0)
    failing = [n for n, c in (status.get("collectors") or {}).items() if not c.get("ok")]
    if not status.get("run_ok"):
        return {
            "kind": "bad",
            "title": "No data today: the status cannot be confirmed",
            "note": status.get("headline") or "",
        }
    if red:
        n = len(red)
        return {
            "kind": "bad",
            "title": f"{n} system{'s' if n != 1 else ''} down",
            "note": status.get("headline") or "",
        }
    if yellow or silent or failing:
        parts = []
        if yellow:
            parts.append(f"{len(yellow)} degraded")
        if silent:
            parts.append(f"{silent} silent host{'s' if silent != 1 else ''}")
        if failing:
            parts.append(f"{len(failing)} collector{'s' if len(failing) != 1 else ''} failing")
        return {"kind": "warn", "title": LEVELS["warn"], "note": ", ".join(parts).capitalize() + "."}
    return {"kind": "ok", "title": LEVELS["ok"], "note": status.get("headline") or ""}


def build(store: Store, status: dict, health: dict, now: datetime) -> dict:
    tiles = overviewdata.system_tiles(status)
    return {
        "banner": overall(status),
        "components": overviewdata.components(store, tiles, now),
        "timeline": overviewdata.safe_db(lambda: systemhistory.timeline(store, now, limit=8), []),
        "events": overviewdata.safe_db(lambda: systemhistory.events_by_hour(store, now), None),
        "pipeline": status.get("pipeline") or {},
        "learning": status.get("learning") or {},
        "health": health,
        "generated": status.get("generated_at"),
    }
