"""What the Overview page shows, as plain data: the headline and its one fix, health cards, system tiles,
top alerts, what changed, and the setup checklist.

Read-only. Everything comes from the status document, the database and the config files; a part that cannot be
read leaves its own card out (or marked unknown) and never costs the page its other parts.
"""

import os
from collections.abc import Mapping
from datetime import datetime, timedelta

from brief import admin_data, ai
from brief.collectors import framework
from brief.config import ConfigError, config_file, load_mapping
from brief.store import Store, iso
from brief.systems import Systems

TOP_ALERTS = 5
STATE_RANK = {"red": 0, "yellow": 1, "unknown": 2, "green": 3}
STATE_KIND = {"red": "bad", "yellow": "warn", "green": "ok", "unknown": ""}


def headline_action(status: dict, health: dict, config_errors: dict, checklist_open: bool) -> dict | None:
    """The one thing that fixes the biggest problem, or None when nothing is wrong."""
    checks = health.get("checks") or {}
    if config_errors:
        name = next(iter(config_errors))
        return {
            "text": f"Fix {name}",
            "href": f"/admin/config/{name}",
            "why": "a configuration file cannot be used",
        }
    if not (checks.get("receiver") or {}).get("ok", True):
        return {
            "text": "Check the syslog port",
            "href": "/admin/network",
            "why": "nothing is listening for logs",
        }
    if not (checks.get("worker") or {}).get("ok", True):
        return {"text": "See the audit log", "href": "/admin/audit", "why": "the worker has stopped cycling"}
    if not (checks.get("database") or {}).get("ok", True):
        return {"text": "Back up and restart", "href": "/admin/backups", "why": "the database cannot be read"}
    pipeline = status.get("pipeline") or {}
    if not status.get("run_ok"):
        if not pipeline.get("events_24h"):
            return {
                "text": "Repoint a source",
                "href": "/admin/network",
                "why": "no logs arrived in 24 hours",
            }
        return {"text": "Open the sources", "href": "/admin/sources", "why": "the last run did not finish"}
    if pipeline.get("hosts_silent"):
        return {"text": "See silent hosts", "href": "/admin/sources", "why": "a host has stopped sending"}
    failing = [n for n, c in (status.get("collectors") or {}).items() if not c.get("ok")]
    if failing:
        return {"text": "Open the collectors", "href": "/admin/collectors", "why": f"{failing[0]} is failing"}
    return None


def hero(status: dict, action: dict | None) -> dict:
    run_ok = bool(status.get("run_ok"))
    if not run_ok:
        kind, glyph = "bad", "alert-circle"
    elif action:
        kind, glyph = "warn", "alert"
    else:
        kind, glyph = "ok", "check-circle"
    return {
        "kind": kind,
        "glyph": glyph,
        "title": status.get("headline") or "No status yet",
        "note": "" if run_ok else "Nothing below is a statement that the network is fine.",
        "action": action,
    }


def _card(label, value, sub="", kind="", href="", glyph="") -> dict:
    return {"label": label, "value": value, "sub": sub, "kind": kind, "href": href, "glyph": glyph}


def cards(data: dict, counts: dict) -> list[dict]:
    p = data["status"].get("pipeline") or {}
    out = []
    events = p.get("events_24h") or 0
    lag = p.get("ingest_lag_s")
    silent = p.get("hosts_silent") or 0
    out.append(
        _card(
            "Pipeline",
            f"{events:,}",
            f"events in 24 h, lag {lag} s" if lag is not None else "events in 24 h",
            "bad" if not events else ("warn" if silent or (lag or 0) > 300 else "ok"),
            "/admin/sources",
            "activity",
        )
    )
    out.append(
        _card(
            "Silent hosts",
            f"{silent} of {p.get('hosts_checked') or 0}",
            "expected hosts that stopped sending" if silent else "all expected hosts are sending",
            "warn" if silent else "ok",
            "/admin/sources",
            "server",
        )
    )
    days = p.get("storage_days_left")
    free = p.get("archive_free_gb")
    out.append(
        _card(
            "Storage",
            f"{days} days" if days is not None else "ok",
            (
                f"{free} GB free, archive {p.get('archive_mb')} MB"
                if free is not None
                else "not enough history yet"
            ),
            "warn" if days is not None and days < 30 else "",
            "/admin/backups",
            "database",
        )
    )
    mem, limit = p.get("memory_mb"), p.get("memory_limit_mb")
    pct = round(100 * mem / limit) if mem is not None and limit else None
    out.append(
        _card(
            "Memory",
            f"{mem} MB" if mem is not None else "-",
            f"{pct}% of {limit} MB" if pct is not None else "container limit unknown",
            "bad" if pct is not None and pct >= 90 else ("warn" if pct is not None and pct >= 75 else ""),
            glyph="box",
        )
    )
    collectors = data["status"].get("collectors") or {}
    if collectors:
        failing = [n for n, c in collectors.items() if not c.get("ok")]
        out.append(
            _card(
                "Collectors",
                f"{len(collectors) - len(failing)} of {len(collectors)}",
                f"{failing[0]} failing" if failing else "all reporting",
                "bad" if failing else "ok",
                "/admin/collectors",
                "plug",
            )
        )
    else:
        out.append(
            _card("Collectors", "off", "add a server to get device data", "", "/admin/collectors", "plug")
        )
    out.append(
        _card(
            "AI and agents",
            "on" if data.get("ai_enabled") else "off",
            f"{data['ai_calls_today']} calls in 24 h" if data.get("ai_enabled") else "optional",
            "",
            "/admin/ai",
            "sparkles",
        )
    )
    out.append(
        _card(
            "Alerts",
            data["open_alerts"],
            "open, acknowledged or snoozed",
            "warn" if data["open_alerts"] else "ok",
            "/admin/findings",
            "bell",
        )
    )
    out.append(
        _card(
            "Proposals",
            counts.get("proposals", 0),
            "waiting for your review" if counts.get("proposals") else "nothing waiting",
            "" if not counts.get("proposals") else "warn",
            "/admin/ai#proposals",
            "diff",
        )
    )
    return out


def system_tiles(status: dict) -> list[dict]:
    tiles = [
        {
            "id": s.get("id"),
            "name": s.get("name") or s.get("id"),
            "state": s.get("state") or "unknown",
            "kind": STATE_KIND.get(s.get("state") or "unknown", ""),
            "summary": s.get("summary") or "",
        }
        for s in status.get("systems") or []
    ]
    return sorted(tiles, key=lambda t: (STATE_RANK.get(t["state"], 2), t["name"] or ""))


def changes(store: Store, status: dict, now: datetime) -> list[dict]:
    """A short list of what is different from yesterday. Empty means nothing moved."""
    out = []
    p = status.get("pipeline") or {}
    since = iso(now - timedelta(days=1))
    with store.read() as db:
        opened = db.execute("SELECT COUNT(*) FROM alert WHERE opened >= ?", (since,)).fetchone()[0]
        new_hosts = db.execute("SELECT COUNT(*) FROM host WHERE first_seen >= ?", (since,)).fetchone()[0]
    if opened:
        out.append(
            {
                "glyph": "bell",
                "text": f"{opened} alert{'s' if opened != 1 else ''} opened",
                "href": "/admin/findings",
            }
        )
    if p.get("new_signatures_24h"):
        n = p["new_signatures_24h"]
        out.append(
            {
                "glyph": "file-text",
                "text": f"{n} new log pattern{'s' if n != 1 else ''}",
                "href": "/admin/signatures",
            }
        )
    if new_hosts:
        out.append(
            {
                "glyph": "server",
                "text": f"{new_hosts} new sender{'s' if new_hosts != 1 else ''}",
                "href": "/admin/sources",
            }
        )
    for d in (status.get("developing") or [])[:3]:
        out.append(
            {
                "glyph": "activity",
                "text": f"{d.get('id')} is {d.get('trend', 'developing')} ({d.get('days')} days)",
                "href": "/admin/findings",
            }
        )
    return out


def _safe(fn, default):
    try:
        return fn()
    except (ConfigError, OSError, ValueError, TypeError, KeyError):
        return default


def checklist(store: Store, status: dict, data: dict, env: Mapping[str, str] | None = None) -> list[dict]:
    """The setup steps of a new install, each done or one click away."""
    env = os.environ if env is None else env
    p = status.get("pipeline") or {}
    systems = _safe(lambda: Systems.load(config_file("systems.yaml", env)).systems, [])
    notify_on = bool((env.get("MQTT_HOST") or "").strip()) or bool(
        _safe(
            lambda: (load_mapping(config_file("notify.yaml", env)).get("apprise") or {}).get("enabled"), False
        )
    )
    collectors_on = _safe(lambda: bool(framework.enabled(env)), False)
    ai_on = _safe(lambda: ai.load_settings(env).enabled, False)
    steps = [
        {"key": "account", "title": "Create the admin account", "done": True, "href": "/admin/account"},
        {
            "key": "sources",
            "title": "Point a device at this container",
            "hint": "Send one device's syslog here, then repoint the rest.",
            "done": bool(p.get("events_24h")),
            "href": "/admin/network",
        },
        {
            "key": "systems",
            "title": "Name your systems",
            "hint": "Group senders into systems so they get a tile, a sensor and silence alerts.",
            "done": bool(systems) and not data.get("unmapped_senders"),
            "href": "/admin/wizard",
        },
        {
            "key": "collectors",
            "title": "Turn on a collector",
            "hint": "Optional: pull device data from Unraid, UniFi and others.",
            "done": collectors_on,
            "href": "/admin/collectors",
            "optional": True,
        },
        {
            "key": "notifications",
            "title": "Choose where alerts go",
            "hint": "Optional: a phone notification or Home Assistant.",
            "done": notify_on,
            "href": "/admin/config/notify.yaml",
            "optional": True,
        },
        {
            "key": "ai",
            "title": "Connect a model",
            "hint": "Optional: plain-language explanations and agents.",
            "done": ai_on,
            "href": "/admin/ai",
            "optional": True,
        },
    ]
    return steps


def build(
    store: Store, out_dir, status: dict, health: dict, state_dir, now: datetime, counts: dict, env=None
) -> dict:
    data = admin_data.overview(store, out_dir, status, health, state_dir, now)
    env = os.environ if env is None else env
    data["ai_enabled"] = _safe(lambda: ai.load_settings(env).enabled, False)
    steps = checklist(store, status, data, env)
    required_open = [s for s in steps if not s["done"] and not s.get("optional")]
    action = headline_action(status, health, data["config_errors"], bool(required_open))
    data["hero"] = hero(status, action)
    data["cards"] = cards(data, counts)
    data["systems"] = system_tiles(status)
    data["changes"] = changes(store, status, now)
    data["alerts"] = admin_data.alert_rows(store, now)[:TOP_ALERTS]
    data["alerts_total"] = data["open_alerts"]
    data["checklist"] = steps
    data["setup_open"] = len([s for s in steps if not s["done"]])
    data["setup_required_open"] = len(required_open)
    data["stories"] = (status.get("stories") or [])[:TOP_ALERTS]
    return data
