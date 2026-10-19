"""One system, in detail: its state, its 30-day bar, and every device in it by name.

A device is a sender of syslog. Its name comes, in this order, from a name the admin gave it, from the UniFi
collector (the device's name in the UniFi application, matched by the MAC address in its syslog header), from
the hostname the device puts in its own header, or else its address. Everything else a device shows (model,
firmware, uplink, online, update available) is whatever the collector and the headers know; a column with
nothing behind it is left out rather than guessed.

Read-only. The one write, a name for a device, is `Tx.set_host_alias` through the admin route.
"""

import json
from datetime import datetime

from brief import admin_data, systemhistory
from brief.collectors.unifi import mac12
from brief.store import Store

KIND_WORD = {"gateway": "Gateway", "switch": "Switch", "ap": "Access point", "pdu": "PDU", "device": "Device"}


def pretty_mac(mac: str | None) -> str:
    digits = mac12(mac)
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2)) if digits else ""


def _rows(db) -> dict:
    """host -> what is known besides its counts: its names and the facts in its header."""
    hosts = {}
    for r in db.execute(
        "SELECT h.host, h.name, h.first_seen, h.clock_skew_s, a.alias, d.mac, d.model, d.firmware "
        "FROM host h LEFT JOIN host_alias a ON a.host = h.host LEFT JOIN device_header d ON d.host = h.host"
    ):
        hosts[r["host"]] = dict(r)
    return hosts


def devices(store: Store, status: dict, system_id: str, now: datetime) -> list[dict]:
    counts = [r for r in admin_data.sources(store, status, now) if r["system"] == system_id]
    with store.read() as db:
        extra = _rows(db)
        nodes = {
            r["node_id"]: {
                "kind": r["kind"],
                "label": r["label"],
                "attrs": json.loads(r["attrs_json"] or "{}"),
            }
            for r in db.execute(
                "SELECT node_id, kind, label, attrs_json FROM topo_node WHERE node_id LIKE 'unifi:%'"
            )
        }
        uplinks = {
            r["src"]: r["dst"] for r in db.execute("SELECT src, dst FROM topo_edge WHERE kind = 'uplink'")
        }
        available = {
            r["item_id"]: r["available_version"]
            for r in db.execute("SELECT item_id, available_version FROM inventory")
        }
    out = []
    for row in counts:
        host = row["host"]
        info = extra.get(host, {})
        mac = mac12(info.get("mac"))
        node = nodes.get(f"unifi:{mac}") if mac else None
        attrs = node["attrs"] if node else {}
        if info.get("alias"):
            name, source = info["alias"], "you"
        elif node and node["label"]:
            name, source = node["label"], "UniFi"
        elif info.get("name"):
            name, source = info["name"], "the device"
        else:
            name, source = host, "its address"
        uplink = nodes.get(uplinks.get(f"unifi:{mac}", "")) if mac else None
        kind = node["kind"] if node else ""
        out.append(
            {
                "host": host,
                "name": name,
                "name_source": source,
                "alias": info.get("alias") or "",
                "header_name": info.get("name") or "",
                "kind": kind,
                "kind_word": KIND_WORD.get(kind, ""),
                "model": attrs.get("model") or info.get("model") or "",
                "firmware": attrs.get("firmware") or info.get("firmware") or "",
                "mac": pretty_mac(mac),
                "online": attrs.get("online") if node else None,
                "uplink": uplink["label"] if uplink else "",
                "update_to": available.get(f"unifi:{mac}") if mac else None,
                "silent": row["silent"],
                "last_seen": row["last_seen"],
                "last_seen_text": row["last_seen_text"],
                "first_seen": info.get("first_seen") or "",
                "events_24h": row["events_24h"],
                "events_hour": row["events_hour"],
                "gap_min": row["gap_min"],
                "skew_s": info.get("clock_skew_s"),
                "in_unifi": node is not None,
            }
        )
    # silent first (they need a look), then the busiest
    return sorted(out, key=lambda d: (not d["silent"], -d["events_24h"], d["name"].lower()))


def detail(store: Store, systems, status: dict, system_id: str, now: datetime) -> dict | None:
    cfg = next((s for s in getattr(systems, "systems", []) if s.id == system_id), None)
    live = next((s for s in status.get("systems") or [] if s.get("id") == system_id), None)
    if cfg is None and live is None:
        return None
    state = (live or {}).get("state") or "unknown"
    cells = systemhistory.bars(store, now).get(system_id) or systemhistory.empty_bar(now)
    found = devices(store, status, system_id, now)
    alerts = [
        a
        for a in admin_data.alert_rows(store, now)
        if any(dev["host"] in (a["title"] or "") or dev["host"] in a["id"] for dev in found)
    ]
    return {
        "id": system_id,
        "label": (cfg.label if cfg else None) or (live or {}).get("name") or system_id,
        "kind": cfg.kind if cfg else "",
        "state": state,
        "state_word": systemhistory.STATE_WORD.get(state, state),
        "kind_class": systemhistory.BAR_KIND.get(state, "off"),
        "summary": (live or {}).get("summary") or "",
        "cells": cells,
        "uptime": systemhistory.uptime_pct(cells),
        "devices": found,
        "silent": sum(1 for dev in found if dev["silent"]),
        "events_24h": sum(dev["events_24h"] for dev in found),
        "unifi": sum(1 for dev in found if dev["in_unifi"]),
        "alerts": alerts,
    }
