"""Task 25: the network map document, the static drawings, and the pages and endpoints that serve them."""

import json
import re
from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree

import pytest
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)

from brief import mapdata, mapsvg
from brief.collectors import topology
from brief.store import iso

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
CFG = {"enabled": True, "layers": ["network", "services", "power"], "group_clients": "by_vlan"}
STATES = {"nas": {"state": "red", "label": "Storage"}, "gw": {"state": "green", "label": "Gateway"}}


def node(
    node_id, kind="switch", label=None, parent=None, first=None, source="discovered", attrs=None, system=None
):
    return {
        "node_id": node_id, "kind": kind, "label": label or node_id, "parent": parent, "system_id": system,
        "source": source, "attrs_json": json.dumps(attrs or {}), "first_seen": first, "last_seen": first,
    }  # fmt: skip


def edge(src, dst, kind="uplink"):
    return {"src": src, "dst": dst, "kind": kind, "attrs_json": None, "source": "discovered"}


def build(nodes, edges=(), topo=None, cfg=None, clients=(), layout=None, pending=(), counts=None):
    return mapdata.build_map(
        nodes, edges, topo or {}, cfg or CFG, STATES, counts or {}, set(pending), clients, layout or {}, NOW
    )


def by_id(doc):
    return {n["id"]: n for n in doc["nodes"]}


# ---------------------------------------------------------------- the merge


def test_collector_rows_and_manual_entries_merge_and_manual_wins():
    doc = build(
        [node("gw1", "gateway", "Old name"), node("sw1")],
        [edge("sw1", "gw1")],
        topo={
            "nodes": [
                {"id": "ups1", "kind": "ups", "label": "Rack UPS"},
                {"id": "gw1", "label": "Edge gateway"},
            ],
            "edges": [{"from": "ups1", "to": "gw1", "kind": "power"}],
        },
    )
    nodes = by_id(doc)
    assert nodes["gw1"]["label"] == "Edge gateway" and nodes["gw1"]["source"] == "manual"
    assert nodes["gw1"]["kind"] == "gateway"  # a manual entry without a kind keeps the discovered one
    assert nodes["ups1"]["layer"] == "power"
    assert {(e["src"], e["dst"], e["kind"], e["layer"]) for e in doc["edges"]} == {
        ("sw1", "gw1", "uplink", "network"),
        ("ups1", "gw1", "power", "power"),
    }


def test_an_edge_to_an_unknown_node_is_dropped_not_drawn():
    doc = build([node("sw1")], [edge("sw1", "ghost")], topo={"edges": [{"from": "x", "to": "sw1"}]})
    assert doc["edges"] == []


def test_hiding_a_node_hides_what_sits_inside_it():
    doc = build(
        [node("srv", "server", "nas"), node("srv:ct:a", "container", "a", parent="srv"), node("sw1")],
        [edge("srv", "srv:ct:a", "hosts")],
        topo={"overrides": [{"id": "srv", "hide": True}]},
    )
    assert set(by_id(doc)) == {"sw1"} and doc["edges"] == []


def test_containers_and_vms_can_be_switched_off():
    nodes = [node("srv", "server"), node("c", "container", parent="srv"), node("v", "vm", parent="srv")]
    assert set(by_id(build(nodes, cfg={**CFG, "show_containers": False}))) == {"srv", "v"}
    assert set(by_id(build(nodes, cfg={**CFG, "show_vms": False}))) == {"srv", "c"}


def test_colour_follows_the_system_and_children_inherit_it_from_their_server():
    doc = build(
        [
            node("srv", "server", "nas"),
            node("srv:ct:a", "container", "a", parent="srv"),
            node("ap1", "ap", attrs={"online": False}),
            node("sw1", system="gw"),
            node("lone", "other"),
        ]
    )
    state = {i: n["state"] for i, n in by_id(doc).items()}
    assert state == {"srv": "red", "srv:ct:a": "red", "ap1": "red", "sw1": "green", "lone": "unknown"}


def test_a_failed_run_never_paints_a_node_green():
    doc = mapdata.build_map(
        [node("sw1", system="gw")], [], {}, CFG, STATES, {}, set(), [], {}, NOW, run_ok=False
    )
    assert by_id(doc)["sw1"]["state"] == "unknown" and doc["run_ok"] is False


def test_badges_for_findings_pending_firmware_and_new_nodes():
    first = iso(NOW - timedelta(days=30))
    later = iso(NOW - timedelta(days=2))
    doc = build(
        [
            node("old", first=first),
            node("fw", first=first),
            node("newsw", first=later),
            node("srv", "server", "nas", first=first),
        ],
        pending={"fw"},
        counts={"nas": 2},
    )
    nodes = by_id(doc)
    assert nodes["old"]["badges"] == [] and nodes["fw"]["badges"] == ["update"]
    assert nodes["newsw"]["badges"] == ["new"]  # first seen after the initial scan settled, within the window
    assert nodes["srv"]["badges"] == ["findings:2"]


def test_the_initial_scan_does_not_mark_everything_new():
    t = iso(NOW - timedelta(hours=2))
    assert all(n["badges"] == [] for n in build([node("a", first=t), node("b", first=t)])["nodes"])


def test_clients_are_one_count_node_per_vlan_attached_to_the_gateway():
    clients = [{"mac": f"m{i}", "vlan": "20" if i < 3 else "30", "label": f"c{i}"} for i in range(5)]
    doc = build([node("gw1", "gateway")], clients=clients)
    groups = {n["id"]: n for n in doc["nodes"] if n["kind"] == "client_group"}
    assert set(groups) == {"clients:20", "clients:30"} and groups["clients:20"]["attrs"]["count"] == 3
    assert all(e["dst"] == "gw1" for e in doc["edges"])
    assert not any("c0" in json.dumps(n) for n in doc["nodes"])  # no device name reaches the document


def test_clients_can_be_hidden_or_shown_one_each_with_a_cap():
    clients = [{"mac": f"m{i}", "vlan": "1", "label": f"c{i}"} for i in range(250)]
    assert build([], clients=clients, cfg={**CFG, "group_clients": "none"})["nodes"] == []
    each = build([], clients=clients, cfg={**CFG, "group_clients": "each"})["nodes"]
    assert len(each) == mapdata.MAX_CLIENT_NODES


def test_saved_positions_are_kept_only_for_nodes_that_still_exist():
    doc = build([node("sw1")], layout={"sw1": {"x": 5, "y": 6}, "gone": {"x": 1, "y": 1}})
    assert doc["layout"] == {"sw1": {"x": 5, "y": 6}}


# ---------------------------------------------------------------- the saved layout


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"a": 1},
        {"a": {"x": "1", "y": 2}},
        {"a": {"x": True, "y": 2}},
        {"a": {"x": float("nan"), "y": 2}},
        {"a": {"x": 10**9, "y": 2}},
        {"": {"x": 1, "y": 2}},
        {f"n{i}": {"x": 0, "y": 0} for i in range(mapdata.MAX_LAYOUT_NODES + 1)},
    ],
)
def test_a_malformed_layout_is_refused(bad):
    with pytest.raises(ValueError):
        mapdata.clean_layout(bad)


def test_layout_round_trips_atomically_and_a_broken_file_reads_as_empty(tmp_path):
    path = tmp_path / "map_layout.json"
    assert mapdata.load_layout(path) == {}
    mapdata.save_layout(path, {"a": {"x": 1.234, "y": 2}})
    assert mapdata.load_layout(path) == {"a": {"x": 1.2, "y": 2.0}} and not path.with_suffix(".tmp").exists()
    path.write_text("{not json")
    assert mapdata.load_layout(path) == {}


# ---------------------------------------------------------------- the static drawings


def drawn(doc):
    return mapsvg.place({**doc, "nodes": doc["nodes"]})


def test_a_saved_position_wins_and_unsaved_nodes_do_not_move_it():
    nodes = [node("gw1", "gateway"), node("sw1"), node("sw2")]
    plain = build(nodes)
    moved = build(nodes, layout={"sw1": {"x": 900, "y": 700}})
    assert drawn(moved)["sw1"] == (900.0, 700.0)
    assert drawn(moved)["gw1"] == drawn(plain)["gw1"]  # automatic placement of the others is unchanged
    assert len(set(drawn(plain).values())) == 3  # and no two nodes land on the same spot


def test_containers_sit_inside_their_server_box():
    doc = build(
        [node("srv", "server"), node("a", "container", parent="srv"), node("b", "container", parent="srv")]
    )
    pos, size = drawn(doc), mapsvg.sizes(doc)
    (sx, sy), (w, h) = pos["srv"], size["srv"]
    for child in ("a", "b"):
        cx, cy = pos[child]
        assert abs(cx - sx) <= w / 2 and abs(cy - sy) <= h / 2


def test_svg_escapes_labels_draws_state_colours_and_says_when_data_is_stale():
    doc = build([node("sw1", label='<script>alert("x")</script>', system="gw")])
    svg = mapsvg.render_svg(doc)
    assert "<script>" not in svg and "&lt;script&gt;" in svg and mapsvg.STATE_FILL["green"] in svg
    stale = mapdata.build_map([node("sw1")], [], {}, CFG, STATES, {}, set(), [], {}, NOW, run_ok=False)
    assert "No data today" in mapsvg.render_svg(stale)


def test_a_layer_switched_off_leaves_the_drawing():
    doc = build([node("sw1"), node("ups1", "ups")], cfg={**CFG, "layers": ["network"]})
    svg = mapsvg.render_svg(doc)
    assert "sw1" in svg and "ups1" not in svg


def test_drawio_export_is_plain_xml_with_the_same_nodes():
    doc = build([node("sw1", label='A & "B"'), node("gw1", "gateway")], [edge("sw1", "gw1")])
    xml = mapsvg.render_drawio(doc)
    root = ElementTree.fromstring(xml)  # well-formed, and the label survives the escaping
    assert xml.startswith("<?xml") and 'A & "B"' in [c.get("value") for c in root.iter("mxCell")]
    assert xml.count("vertex=") == 2
    assert xml.count("edge=") == 1


def test_an_empty_map_still_draws():
    assert mapsvg.render_svg(build([])).startswith("<svg")


# ---------------------------------------------------------------- the endpoints and pages


def cfg_dir(env):
    return env.settings.state_dir.parent / "cfg" / "brief"


def enable(env, **extra):
    lines = ["schema: 1", "enabled: true", *[f"{k}: {v}" for k, v in extra.items()]]
    (cfg_dir(env) / "map.yaml").write_text("\n".join(lines) + "\n")


def seed(env):
    with env.store.write() as tx:
        topology.upsert_node(tx.db, "unifi:aa", "gateway", "Gateway", None, {"model": "X"}, iso(NOW))
        topology.upsert_node(tx.db, "unifi:bb", "switch", "Switch", None, {}, iso(NOW))
        topology.upsert_edge(tx.db, "unifi:bb", "unifi:aa", "uplink", iso(NOW))
        tx.db.execute(
            "INSERT INTO client (mac, first_seen, last_seen, hostname, vendor, vlan) VALUES (?, ?, ?, ?, ?, ?)",
            ("00:00:5e:00:53:01", iso(NOW), iso(datetime.now(UTC)), "private-name", "V", "20"),
        )


def test_the_map_is_off_until_map_yaml_enables_it(lab):  # noqa: F811
    http = lab.http()
    for path in ("/api/v1/map", "/map.svg"):
        assert http.get(path).status_code == 404


def test_the_read_only_endpoints_serve_the_map_and_hide_client_names(lab):  # noqa: F811
    enable(lab)
    seed(lab)
    http = lab.http()
    doc = http.get("/api/v1/map").json()
    assert {n["id"] for n in doc["nodes"]} >= {"unifi:aa", "unifi:bb", "clients:20"}
    assert "private-name" not in json.dumps(doc) and "00:00:5e:00:53:01" not in json.dumps(doc)
    svg = http.get("/map.svg")
    assert svg.headers["content-type"].startswith("image/svg+xml") and "private-name" not in svg.text
    page = http.get("/map.html")
    assert page.status_code == 200 and "script-src 'self'" in page.headers["content-security-policy"]
    assert "/static/map.js" in page.text


def test_an_ignored_device_does_not_even_count_toward_its_group(lab):  # noqa: F811
    enable(lab)
    seed(lab)
    (cfg_dir(lab) / "devices.yaml").write_text(
        "schema: 1\ndevices:\n  - {mac: '00:00:5e:00:53:01', label: Hidden, profile: ignore}\n"
    )
    ids = {n["id"] for n in lab.http().get("/api/v1/map").json()["nodes"]}
    assert not any(i.startswith("clients:") for i in ids)


def test_save_as_default_is_audited_and_feeds_the_static_svg(lab):  # noqa: F811
    enable(lab)
    seed(lab)
    page = lab.client.get("/admin/map")
    assert page.status_code == 200 and "Save as default" in page.text
    csrf = re.search(r'data-csrf="([^"]+)"', page.text).group(1)
    body = json.dumps({"unifi:aa": {"x": 321, "y": 123}})
    ok = lab.client.post("/admin/map/layout", content=body, headers={"x-csrf-token": csrf})
    assert ok.status_code == 200
    assert lab.http().get("/api/v1/map").json()["layout"] == {"unifi:aa": {"x": 321.0, "y": 123.0}}
    assert 'x="' in lab.http().get("/map.svg").text
    with lab.store.read() as db:
        rows = [dict(r) for r in db.execute("SELECT user, action FROM audit_log WHERE action = 'map.layout'")]
    assert rows == [{"user": "admin:admin", "action": "map.layout"}]


def test_saving_needs_the_csrf_token_and_a_valid_layout_and_a_login(lab):  # noqa: F811
    enable(lab)
    csrf = re.search(r'data-csrf="([^"]+)"', lab.client.get("/admin/map").text).group(1)
    assert lab.client.post("/admin/map/layout", content="{}").status_code == 403
    bad = lab.client.post(
        "/admin/map/layout", content='{"a": {"x": "no", "y": 1}}', headers={"x-csrf-token": csrf}
    )
    assert bad.status_code == 400 and not (lab.settings.state_dir / "map_layout.json").exists()
    assert (
        lab.client.post("/admin/map/layout", content="not json", headers={"x-csrf-token": csrf}).status_code
        == 400
    )
    assert lab.https().post("/admin/map/layout", content="{}").status_code in (303, 401, 403)


def test_drawio_download_is_off_unless_enabled(lab):  # noqa: F811
    enable(lab)
    seed(lab)
    assert lab.client.get("/admin/map/drawio").status_code == 404
    enable(lab, drawio_export="true")
    r = lab.client.get("/admin/map/drawio")
    assert r.status_code == 200 and "<mxfile>" in r.text


def test_the_index_lists_the_map_only_when_it_is_on(lab):  # noqa: F811
    assert "/api/v1/map" not in lab.http().get("/api/v1").json()["endpoints"]
    enable(lab)
    assert "/api/v1/map" in lab.http().get("/api/v1").json()["endpoints"]
