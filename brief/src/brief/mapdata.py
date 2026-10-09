"""The network map document (`map.json`): discovered nodes merged with the manual topology.

docs/guide/07-network-map.md. Pure data work: `build_map` takes plain rows and dicts and returns the
document the interactive page, the static SVG, and the draw.io export all draw from. The rows come from
`topo_node` / `topo_edge` (collectors write them), `topology.yaml` (manual entries win), `status.json`
(node colour, badges), the client table (one count node per VLAN, never one per device by default), and the
saved default layout.

Privacy: devices with the `ignore` profile never appear; clients are counted per VLAN. The document
describes a network, so it is served on the LAN like the rest of the views and never committed.
"""

import json
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from brief.store import iso

SCHEMA = 1
LAYERS = ("network", "services", "power")
SERVICE_KINDS = {"container", "vm", "service"}
POWER_KINDS = {"ups"}
EDGE_LAYER = {"uplink": "network", "hosts": "services", "depends": "services", "power": "power"}
NEW_DAYS = 7  # a discovered node keeps its "new" badge this long
SETTLE_HOURS = 24  # nodes first seen within this window of the very first sighting are the initial scan
CLIENT_DAYS = 30  # a client counts toward its VLAN group if seen this recently
MAX_CLIENT_NODES = 200  # `group_clients: each` stops here so a busy network cannot flood the page
MAX_LAYOUT_NODES = 2000
LAYOUT_LIMIT = 1_000_000  # coordinates are plain numbers within this bound
STATE_RANK = {"red": 3, "yellow": 2, "unknown": 1, "green": 0}


def layer_of_node(kind: str) -> str:
    if kind in SERVICE_KINDS:
        return "services"
    if kind in POWER_KINDS:
        return "power"
    return "network"


def _load_attrs(text: str | None) -> dict:
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _state_of(attrs: dict, system_state: str | None) -> str:
    if attrs.get("online") is False:
        return "red"
    return system_state or "unknown"


def build_map(
    nodes: Iterable[Mapping],
    edges: Iterable[Mapping],
    topology: Mapping,
    cfg: Mapping,
    system_states: Mapping[str, dict],
    finding_counts: Mapping[str, int],
    pending_firmware: set[str],
    clients: Iterable[Mapping],
    layout: Mapping[str, Mapping],
    now: datetime,
    run_ok: bool = True,
) -> dict:
    """Merge everything into one document. `system_states` maps system id to {"state", "label"}."""
    nodes = [dict(n) for n in nodes]
    by_label = {}
    for sid, info in system_states.items():
        by_label[sid.lower()] = sid
        if info.get("label"):
            by_label[str(info["label"]).lower()] = sid

    first = [n["first_seen"] for n in nodes if n.get("first_seen") and n.get("source") != "manual"]
    baseline = min(first) if first else None
    new_after = iso(datetime.fromisoformat(baseline) + timedelta(hours=SETTLE_HOURS)) if baseline else None
    newest = iso(now - timedelta(days=NEW_DAYS))

    out: dict[str, dict] = {}
    for n in nodes:
        attrs = _load_attrs(n.get("attrs_json"))
        sid = n.get("system_id") or by_label.get(str(n.get("label") or "").lower())
        out[n["node_id"]] = {
            "id": n["node_id"],
            "kind": n.get("kind") or "other",
            "label": n.get("label") or n["node_id"],
            "parent": n.get("parent"),
            "system": sid,
            "source": n.get("source") or "discovered",
            "attrs": attrs,
            "first_seen": n.get("first_seen"),
            "last_seen": n.get("last_seen"),
            "badges": [],
        }
        fs = n.get("first_seen")
        if n.get("source") != "manual" and fs and new_after and fs > new_after and fs >= newest:
            out[n["node_id"]]["badges"].append("new")
        if n["node_id"] in pending_firmware:
            out[n["node_id"]]["badges"].append("update")

    # manual topology: nodes and overrides win over what a collector saw
    for item in topology.get("nodes") or []:
        node_id = str(item["id"])
        attrs = {k: v for k, v in item.items() if k not in ("id", "kind", "label", "parent", "system")}
        base = out.get(node_id, {"id": node_id, "badges": [], "first_seen": None, "last_seen": None})
        base.update(
            kind=str(item.get("kind") or base.get("kind") or "other"),
            label=str(item.get("label") or base.get("label") or node_id),
            parent=item.get("parent") or base.get("parent"),
            system=item.get("system") or base.get("system"),
            source="manual",
            attrs={**base.get("attrs", {}), **attrs},
        )
        out[node_id] = base
    hidden: set[str] = set()
    for item in topology.get("overrides") or []:
        node = out.get(str(item["id"]))
        if node is None:
            continue
        if item.get("label"):
            node["label"] = str(item["label"])
        if item.get("system"):
            node["system"] = str(item["system"])
        if item.get("hide"):
            hidden.add(node["id"])

    if cfg.get("show_containers") is False:
        hidden |= {i for i, n in out.items() if n["kind"] == "container"}
    if cfg.get("show_vms") is False:
        hidden |= {i for i, n in out.items() if n["kind"] == "vm"}
    # hiding a parent hides what sits inside it
    changed = True
    while changed:
        changed = False
        for i, n in out.items():
            if i not in hidden and n.get("parent") in hidden:
                hidden.add(i)
                changed = True
    for i in hidden:
        out.pop(i, None)

    for n in out.values():
        if n.get("parent") not in out:
            n["parent"] = None
        n["layer"] = layer_of_node(n["kind"])
    # colour: own system, else the server it sits in
    for n in out.values():
        holder = n
        info = system_states.get(holder.get("system") or "") or system_states.get(
            (out.get(n["parent"]) or {}).get("system") or ""
        )
        n["state"] = _state_of(n["attrs"], (info or {}).get("state") if run_ok else "unknown")
        sid = n.get("system") or (out.get(n["parent"]) or {}).get("system")
        count = finding_counts.get(sid or "", 0)
        if count and n["kind"] not in SERVICE_KINDS:
            n["badges"].append(f"findings:{count}")

    links = []
    seen_edges = set()
    for e in edges:
        key = (e["src"], e["dst"], e["kind"])
        if key in seen_edges or e["src"] not in out or e["dst"] not in out:
            continue
        seen_edges.add(key)
        links.append(
            {
                "src": e["src"],
                "dst": e["dst"],
                "kind": e["kind"],
                "layer": EDGE_LAYER.get(e["kind"], "network"),
                "label": _load_attrs(e.get("attrs_json")).get("label"),
                "source": e.get("source") or "discovered",
            }
        )
    for item in topology.get("edges") or []:
        key = (str(item["from"]), str(item["to"]), str(item.get("kind") or "uplink"))
        if key in seen_edges or key[0] not in out or key[1] not in out:
            continue
        seen_edges.add(key)
        links.append(
            {
                "src": key[0],
                "dst": key[1],
                "kind": key[2],
                "layer": EDGE_LAYER.get(key[2], "network"),
                "label": item.get("label"),
                "source": "manual",
            }
        )

    _add_clients(out, links, clients, str(cfg.get("group_clients") or "by_vlan"))

    saved = {k: v for k, v in layout.items() if k in out}
    return {
        "schema": SCHEMA,
        "generated_at": iso(now),
        "run_ok": run_ok,
        "layers": [x for x in (cfg.get("layers") or LAYERS) if x in LAYERS],
        "refresh_s": int(cfg.get("refresh_s") or 300),
        "auto_layout": str(cfg.get("auto_layout") or "cose"),
        "nodes": sorted(out.values(), key=lambda n: n["id"]),
        "edges": links,
        "layout": saved,
    }


def _add_clients(out: dict, links: list, clients: Iterable[Mapping], mode: str) -> None:
    """Clients as one count node per VLAN (default) or one node each (capped)."""
    if mode == "none":
        return
    gateway = next(
        (n["id"] for n in sorted(out.values(), key=lambda n: n["id"]) if n["kind"] == "gateway"), None
    )
    rows = list(clients)
    if mode == "each":
        for row in rows[:MAX_CLIENT_NODES]:
            cid = f"client:{row['mac']}"
            out[cid] = _client_node(cid, row.get("label") or row["mac"], "other", {}, "network")
            if gateway:
                links.append(_client_edge(cid, gateway))
        return
    groups: dict[str, int] = {}
    for row in rows:
        vlan = str(row.get("vlan") or "").strip() or "untagged"
        groups[vlan] = groups.get(vlan, 0) + 1
    for vlan, count in sorted(groups.items()):
        cid = f"clients:{vlan}"
        label = f"VLAN {vlan}" if vlan != "untagged" else "Clients"
        out[cid] = _client_node(
            cid, f"{label} · {count}", "client_group", {"count": count, "vlan": vlan}, "network"
        )
        if gateway:
            links.append(_client_edge(cid, gateway))


def _client_node(node_id: str, label: str, kind: str, attrs: dict, layer: str) -> dict:
    return {
        "id": node_id,
        "kind": kind,
        "label": label,
        "parent": None,
        "system": None,
        "source": "discovered",
        "attrs": attrs,
        "first_seen": None,
        "last_seen": None,
        "badges": [],
        "layer": layer,
        "state": "unknown",
    }


def _client_edge(node_id: str, gateway: str) -> dict:
    return {
        "src": node_id,
        "dst": gateway,
        "kind": "uplink",
        "layer": "network",
        "label": None,
        "source": "discovered",
    }


# --------------------------------------------------------------------------- reading the sources


def read_map(store, topology: Mapping, cfg: Mapping, status: Mapping, systems, devices, layout_path: Path,
             now: datetime | None = None) -> dict:  # fmt: skip
    """Collect the rows and build the document. Reads only; nothing here writes."""
    now = now or datetime.now(UTC)
    states = {s["id"]: s for s in status.get("systems") or [] if s.get("id")}
    for s in getattr(systems, "systems", []):
        states.setdefault(s.id, {"id": s.id, "state": "unknown"})
        states[s.id] = {**states[s.id], "label": s.label}
    counts: dict[str, int] = {}
    for story in status.get("stories") or []:
        if story.get("system"):
            counts[story["system"]] = counts.get(story["system"], 0) + 1
    since = iso(now - timedelta(days=CLIENT_DAYS))
    with store.read() as db:
        nodes = [dict(r) for r in db.execute("SELECT * FROM topo_node")]
        edges = [dict(r) for r in db.execute("SELECT * FROM topo_edge")]
        pending = {
            r["item_id"]
            for r in db.execute("SELECT item_id FROM inventory WHERE available_version IS NOT NULL")
        }
        clients = []
        if cfg.get("group_clients", "by_vlan") != "none":
            for r in db.execute(
                "SELECT mac, hostname, vendor, vlan FROM client WHERE last_seen >= ?", (since,)
            ):
                if devices.profile_of(r["mac"]) == "ignore":
                    continue
                clients.append(
                    {
                        "mac": r["mac"],
                        "vlan": r["vlan"],
                        "label": devices.label(r["mac"], r["hostname"], r["vendor"]),
                    }
                )
    return build_map(
        nodes, edges, topology, cfg, states, counts, pending, clients, load_layout(layout_path), now,
        run_ok=bool(status.get("run_ok", True)),
    )  # fmt: skip


# --------------------------------------------------------------------------- the saved default layout


def clean_layout(data) -> dict[str, dict[str, float]]:
    """Validate a layout upload: {node id: {x, y}} with plain bounded numbers. Anything else is refused."""
    if not isinstance(data, dict):
        raise ValueError("A layout is a JSON object of node ids to positions.")
    if len(data) > MAX_LAYOUT_NODES:
        raise ValueError(f"A layout holds at most {MAX_LAYOUT_NODES} nodes.")
    out = {}
    for key, pos in data.items():
        if not isinstance(key, str) or not 0 < len(key) <= 200:
            raise ValueError("A node id in the layout is not valid.")
        if not isinstance(pos, dict):
            raise ValueError(f"Position for {key!r} must have x and y.")
        x, y = pos.get("x"), pos.get("y")
        for v in (x, y):
            if isinstance(v, bool) or not isinstance(v, int | float) or abs(v) > LAYOUT_LIMIT or v != v:
                raise ValueError(f"Position for {key!r} must be plain numbers.")
        out[key] = {"x": round(float(x), 1), "y": round(float(y), 1)}
    return out


def load_layout(path: Path) -> dict:
    try:
        return clean_layout(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def save_layout(path: Path, layout: Mapping) -> dict:
    """Write the default layout atomically; returns what was stored."""
    cleaned = clean_layout(dict(layout))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cleaned, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return cleaned
