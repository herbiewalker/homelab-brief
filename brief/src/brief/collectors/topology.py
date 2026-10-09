"""Write what a collector discovered into the network map tables (topo_node, topo_edge).

Shared by the collectors so every one records a node the same way: `first_seen` is set once (the
new_infrastructure detector reads it) and `last_seen` moves on every sighting.
"""

import json


def upsert_node(db, node_id: str, kind: str, label: str, parent: str | None, attrs: dict, now: str) -> None:
    db.execute(
        "INSERT INTO topo_node (node_id, kind, label, parent, system_id, source, attrs_json, first_seen, "
        "last_seen) VALUES (?, ?, ?, ?, NULL, 'discovered', ?, ?, ?) "
        "ON CONFLICT (node_id) DO UPDATE SET kind = excluded.kind, label = excluded.label, "
        "parent = excluded.parent, attrs_json = excluded.attrs_json, last_seen = excluded.last_seen",
        (node_id, kind, label, parent, json.dumps(attrs), now, now),
    )


def upsert_edge(db, src: str, dst: str, kind: str, now: str, attrs: dict | None = None) -> None:
    """kind: uplink, hosts, depends, or power."""
    db.execute(
        "INSERT INTO topo_edge (src, dst, kind, attrs_json, source, last_seen) "
        "VALUES (?, ?, ?, ?, 'discovered', ?) "
        "ON CONFLICT (src, dst, kind) DO UPDATE SET attrs_json = excluded.attrs_json, "
        "last_seen = excluded.last_seen",
        (src, dst, kind, json.dumps(attrs) if attrs else None, now),
    )
