"""Task 20: the UniFi collector, the log-header firmware fallback, and the two device detectors."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_collectors import lab
from test_unraid_api import Server, serve  # noqa: F401  (a real local HTTP server for the client tests)
from test_worker import event, make_env

from brief import detect, playbook
from brief import detectors_hw as dd
from brief import worker as w
from brief.collectors import framework, unifi
from brief.collectors.httpjson import ApiError
from brief.collectors.unifi_api import UnifiApi
from brief.config import ConfigError
from brief.store import Store
from brief.systems import Systems

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
KEYS = {"UNIFI_URL": "https://198.51.100.20", "UNIFI_API_KEY": "not-a-real-key"}
YAML = "  unifi:\n    enabled: true\n    url_var: UNIFI_URL\n    key_var: UNIFI_API_KEY\n"
MACS = {
    "gw": "00:00:5E:00:53:01",
    "sw": "00:00:5E:00:53:02",
    "ap": "00:00:5E:00:53:03",
    "new": "00:00:5E:00:53:04",
}


def device(key, model, name, firmware="7.5.10", updatable=False, uplink=None, state="ONLINE", features=None):
    return {
        "id": f"id-{key}",
        "macAddress": MACS[key],
        "ipAddress": "198.51.100.30",
        "name": name,
        "model": model,
        "state": state,
        "firmwareVersion": firmware,
        "firmwareUpdatable": updatable,
        "features": features or [],
        "_uplink": uplink,
    }


class FakeUnifi:
    """Stands in for UnifiApi: `world` holds sites and their devices; an ApiError in it is raised."""

    def __init__(self, world):
        self.world = world

    def get(self, path, **params):
        if isinstance(self.world, ApiError):
            raise self.world
        if path == "/v1/info":
            return {"applicationVersion": self.world.get("app", "9.9.9")}
        if "/devices/" in path:
            site, dev = path.split("/")[3], path.split("/")[5]
            row = next(d for d in self.world["sites"][site]["devices"] if d["id"] == dev)
            up = row["_uplink"]
            return {**row, "uplink": {"deviceId": f"id-{up}"} if up else {}}
        raise AssertionError(path)

    def paged(self, path):
        if isinstance(self.world, ApiError):
            raise self.world
        if path == "/v1/sites":
            return [
                {"id": sid, "name": s["name"], "internalReference": s["name"].lower()}
                for sid, s in self.world["sites"].items()
            ]
        site = path.split("/")[3]
        return [{k: v for k, v in d.items() if k != "_uplink"} for d in self.world["sites"][site]["devices"]]


def world(extra=()):
    devices = [
        device("gw", "UCG-Max", "Gateway", "5.0.1"),
        device("sw", "USW-Pro-Max-24-PoE", "Switch", "7.5.15", updatable=True, uplink="gw"),
        device("ap", "U7-Pro-Max", "Access point", "8.7.11", uplink="sw", features=["accessPoint"]),
        *extra,
    ]
    return {"app": "9.4.1", "sites": {"s1": {"name": "Default", "devices": devices}}}


@pytest.fixture
def fake(monkeypatch):
    current = {"world": world()}
    monkeypatch.setattr(unifi, "API_FACTORY", lambda *a, **k: FakeUnifi(current["world"]))
    return current


def setup(tmp_path, extra=""):
    env, _, store = lab(tmp_path, YAML + extra)
    return env | KEYS, store


def rows(store, sql, *args):
    with store.read() as db:
        return [dict(r) for r in db.execute(sql, args)]


# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "model, features, kind",
    [
        ("UCG-Max", None, "gateway"), ("UDM-Pro", None, "gateway"), ("USW-Flex", None, "switch"),
        ("USW-Pro-Max-24-PoE", None, "switch"), ("U7-Pro-Max", None, "ap"), ("U6-Lite", None, "ap"),
        ("USP-PDU-Pro", None, "pdu"), ("Mystery", ["accessPoint"], "ap"), ("Mystery", ["switching"], "switch"),
        ("Mystery", None, "device"), ("", None, "device"),
    ],
)  # fmt: skip
def test_kind_of(model, features, kind):
    assert unifi.kind_of(model, features) == kind


def test_mac_addresses_are_normalised():
    assert unifi.mac12("00:00:5E:00:53:01") == "00005e005301"
    assert unifi.mac12("00005e005301") == "00005e005301"
    assert unifi.mac12("nonsense") is None and unifi.mac12(None) is None


def test_devices_without_an_id_or_a_mac_are_skipped():
    items = [
        {"id": "a", "macAddress": "00:00:5E:00:53:01", "model": "USW-Flex"},
        {"macAddress": "x"},
        {"id": "b"},
    ]
    parsed = unifi.parse_devices(items)
    assert (
        [d.mac for d in parsed] == ["00005e005301"] and parsed[0].name == "USW-Flex" and not parsed[0].online
    )


# ---------------------------------------------------------------- settings


@pytest.mark.parametrize(
    "extra, message",
    [
        ("    api_path: proxy\n", "api_path"),
        ("    api_path: /a/../b\n", "api_path"),
        ("    verify_tls: sometimes\n", "verify_tls"),
        ("    site: 7\n", "site must be text"),
    ],
)
def test_bad_settings_are_refused(tmp_path, extra, message):
    _, user, _ = lab(tmp_path, YAML + extra)
    with pytest.raises(ConfigError, match=message):
        framework.validate(user / "collectors.yaml")


def test_an_enabled_collector_needs_both_variable_names(tmp_path):
    _, user, _ = lab(tmp_path, "  unifi:\n    enabled: true\n    url_var: UNIFI_URL\n")
    with pytest.raises(ConfigError, match="key_var is required"):
        framework.validate(user / "collectors.yaml")


def test_the_shipped_unifi_block_is_valid_while_disabled():
    from pathlib import Path

    assert (
        framework.validate(Path(__file__).resolve().parents[1] / "collectors.yaml")["unifi"].enabled is False
    )


# ---------------------------------------------------------------- a run


def test_a_run_stores_inventory_topology_and_numbers(tmp_path, fake):
    env, store = setup(tmp_path)
    assert framework.run_due(store, env, NOW)["unifi"]["ok"] is True
    numbers = framework.read_state(store)["unifi"]["metrics"]
    assert numbers == {
        "unifi_devices": 3, "unifi_devices_offline": 0, "unifi_updates_available": 1, "unifi_header_devices": 0,
    }  # fmt: skip
    inv = {r["item_id"]: r for r in rows(store, "SELECT * FROM inventory")}
    assert inv["unifi:app"]["current_version"] == "9.4.1" and inv["unifi:app"]["kind"] == "app"
    sw = inv["unifi:00005e005302"]
    assert (sw["kind"], sw["name"], sw["current_version"]) == ("switch", "Switch", "7.5.15")
    assert sw["available_version"] == dd.UPDATE_FLAG and sw["available_since"] == "2026-10-09T12:00:00Z"
    assert inv["unifi:00005e005303"]["available_version"] is None
    nodes = {r["node_id"]: r["kind"] for r in rows(store, "SELECT node_id, kind FROM topo_node")}
    assert nodes == {
        "unifi:00005e005301": "gateway",
        "unifi:00005e005302": "switch",
        "unifi:00005e005303": "ap",
    }
    edges = {(r["src"], r["dst"], r["kind"]) for r in rows(store, "SELECT src, dst, kind FROM topo_edge")}
    assert edges == {
        ("unifi:00005e005302", "unifi:00005e005301", "uplink"),
        ("unifi:00005e005303", "unifi:00005e005302", "uplink"),
    }


def test_the_update_flag_keeps_its_first_date_and_clears_when_the_firmware_is_applied(tmp_path, fake):
    env, store = setup(tmp_path)
    framework.run_due(store, env, NOW)
    framework.run_due(store, env, NOW + timedelta(days=3))
    (row,) = rows(store, "SELECT available_since FROM inventory WHERE item_id = 'unifi:00005e005302'")
    assert row["available_since"] == "2026-10-09T12:00:00Z"  # not moved by later sightings
    fake["world"]["sites"]["s1"]["devices"][1].update(firmwareVersion="7.5.20", firmwareUpdatable=False)
    framework.run_due(store, env, NOW + timedelta(days=4))
    (row,) = rows(
        store,
        "SELECT current_version, available_version, available_since FROM inventory WHERE item_id = 'unifi:00005e005302'",
    )
    assert row == {"current_version": "7.5.20", "available_version": None, "available_since": None}


def test_an_offline_device_is_counted(tmp_path, fake):
    env, store = setup(tmp_path)
    fake["world"]["sites"]["s1"]["devices"][2]["state"] = "OFFLINE"
    framework.run_due(store, env, NOW)
    assert framework.read_state(store)["unifi"]["metrics"]["unifi_devices_offline"] == 1


def test_only_the_named_site_is_read(tmp_path, fake):
    env, store = setup(tmp_path, "    site: Other\n")
    fake["world"]["sites"]["s2"] = {"name": "Other", "devices": [device("new", "USW-Flex", "Spare")]}
    framework.run_due(store, env, NOW)
    assert [r["label"] for r in rows(store, "SELECT label FROM topo_node")] == ["Spare"]
    env2, store2 = setup(tmp_path / "x", "    site: Missing\n")
    assert framework.run_due(store2, env2, NOW)["unifi"]["error"] == "no matching UniFi site was found"


def test_the_connection_test_reports_the_version_and_counts(tmp_path, fake):
    env, store = setup(tmp_path)
    assert framework.test_connection("unifi", store, env, NOW) == (
        True,
        "UniFi Network 9.4.1: 1 site(s), 3 devices, 1 with a firmware update available",
    )
    fake["world"] = ApiError("auth", "the API key was rejected")
    assert framework.test_connection("unifi", store, env, NOW) == (False, "the API key was rejected")
    ok, message = framework.test_connection(
        "unifi", store, {k: v for k, v in env.items() if k != "UNIFI_API_KEY"}, NOW
    )
    assert not ok and "UNIFI_API_KEY is not set" in message


# ---------------------------------------------------------------- the log-header fallback


def put_header(store, host, mac, model, firmware, seen="2026-10-09T11:00:00Z"):
    with store.write() as tx:
        tx.set_device_header(host, mac, model, firmware, seen)


def test_firmware_from_log_headers_fills_in_when_the_api_is_down_and_the_run_still_fails(tmp_path, fake):
    env, store = setup(tmp_path)
    put_header(store, "198.51.100.31", "00005e005309", "USW-Flex", "7.5.10+17129")
    fake["world"] = ApiError("unreachable", "could not connect (TimeoutError)")
    outcome = framework.run_due(store, env, NOW)
    assert outcome["unifi"] == {"ran": True, "ok": False, "error": "could not connect (TimeoutError)"}
    (row,) = rows(store, "SELECT kind, name, current_version FROM inventory")
    assert (row["kind"], row["name"], row["current_version"]) == ("switch", "USW-Flex", "7.5.10+17129")


def test_the_missing_key_is_named_and_headers_still_land(tmp_path, fake):
    env, store = setup(tmp_path)
    env = {k: v for k, v in env.items() if k != "UNIFI_API_KEY"}
    put_header(store, "198.51.100.31", "00005e005309", "U7-Pro-Max", "8.7.11+99")
    assert framework.run_due(store, env, NOW)["unifi"]["error"] == "the variable UNIFI_API_KEY is not set"
    assert rows(store, "SELECT current_version FROM inventory") == [{"current_version": "8.7.11+99"}]


def test_the_api_wins_over_headers_for_the_same_device_and_headers_cover_the_rest(tmp_path, fake):
    env, store = setup(tmp_path)
    put_header(
        store, "198.51.100.32", "00005e005302", "USW-Pro-Max-24-PoE", "7.5.15+1"
    )  # the switch the API knows
    put_header(store, "198.51.100.33", "00005e005399", "USP-PDU-Pro", "7.2.123+16565")  # one it does not
    framework.run_due(store, env, NOW)
    inv = {r["item_id"]: r for r in rows(store, "SELECT * FROM inventory")}
    assert (
        inv["unifi:00005e005302"]["current_version"] == "7.5.15"
        and inv["unifi:00005e005302"]["name"] == "Switch"
    )
    assert (
        inv["unifi:00005e005399"]["kind"] == "pdu"
        and inv["unifi:00005e005399"]["current_version"] == "7.2.123+16565"
    )
    assert framework.read_state(store)["unifi"]["metrics"]["unifi_header_devices"] == 1


def test_a_header_does_not_overwrite_a_device_name_the_api_gave(tmp_path, fake):
    env, store = setup(tmp_path)
    framework.run_due(store, env, NOW)
    fake["world"] = ApiError("unreachable", "x")
    put_header(store, "198.51.100.32", "00005e005302", "USW-Pro-Max-24-PoE", "7.5.16+2")
    framework.run_due(store, env, NOW + timedelta(hours=2))
    (row,) = rows(store, "SELECT name, current_version FROM inventory WHERE item_id = 'unifi:00005e005302'")
    assert row == {"name": "Switch", "current_version": "7.5.16+2"}


def test_the_ingest_remembers_the_newest_header_per_sender(tmp_path):
    env, _ = make_env(tmp_path)
    state = tmp_path / "state"
    folder = state / "events"
    folder.mkdir(parents=True)

    def line(stamp, firmware):
        return json.dumps(
            {"source_ip": "198.51.100.32", "app": "app", "msg": "hello", "severity": 6, "ts": "Oct  9 10:00:00",
             "timestamp": stamp, "device_mac": "00005e005302", "model": "USW-Flex", "firmware": firmware}
        )  # fmt: skip

    (folder / "2026-10-09.ndjson").write_text(
        "\n".join(
            [
                line("2026-10-09T10:00:00Z", "7.5.10+1"),
                line("2026-10-09T11:00:00Z", "7.5.15+2"),
                event("plain"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    worker = w.Worker(state, env)
    worker.run_once()
    got = rows(worker.store, "SELECT host, mac, model, firmware FROM device_header")
    assert got == [
        {"host": "198.51.100.32", "mac": "00005e005302", "model": "USW-Flex", "firmware": "7.5.15+2"}
    ]


def test_an_older_sighting_never_replaces_a_newer_one(tmp_path):
    store = Store(tmp_path / "baseline.db")
    put_header(store, "h", "00005e005302", "USW-Flex", "7.5.15", seen="2026-10-09T11:00:00Z")
    put_header(store, "h", "00005e005302", "USW-Flex", "7.5.10", seen="2026-09-01T11:00:00Z")
    assert rows(store, "SELECT firmware FROM device_header") == [{"firmware": "7.5.15"}]


# ---------------------------------------------------------------- the detectors


def inv(
    item_id="a",
    kind="switch",
    name="Switch",
    current="7.5.10",
    available=dd.UPDATE_FLAG,
    since=None,
    model="USW-Flex",
):
    return dd.InventoryRow(item_id, kind, name, current, available, since, model)


def test_firmware_pending_is_young_and_version_behind_is_old():
    young, old = inv("y", since="2026-10-01T00:00:00Z"), inv("o", since="2026-08-01T00:00:00Z")
    pending = dd.firmware_pending([young, old, inv("n", available=None), inv("os", kind="os")], NOW, 30)
    assert [f.id for f in pending] == ["firmware_pending:y"] and "update available" in pending[0].title
    behind = dd.version_behind([young, old], NOW, 30)
    assert [f.id for f in behind] == ["version_behind:o"]
    assert behind[0].title == "Switch has had a firmware update available for 69 days (running 7.5.10)"


def test_software_inventory_is_not_a_firmware_finding():
    assert (
        dd.firmware_pending([inv("p", kind="plugin"), inv("a", kind="app"), inv("k", kind="api")], NOW) == []
    )


def nodes(*specs):
    return [dd.NodeRow(i, k, i, ts) for i, k, ts in specs]


def test_the_first_pass_is_the_inventory_and_a_later_device_is_news():
    found = dd.new_infrastructure(
        nodes(
            ("unifi:a", "gateway", "2026-10-01T00:00:00Z"),
            ("unifi:b", "switch", "2026-10-01T00:30:00Z"),  # same first pass
            ("unifi:c", "ap", "2026-10-08T00:00:00Z"),  # a day and more after: news
            ("unifi:ct", "container", "2026-10-08T00:00:00Z"),  # not infrastructure
        ),
        NOW,
    )
    assert [f.id for f in found] == ["new_infrastructure:unifi:c"] and found[0].detail["device_kind"] == "ap"


def test_news_fades_and_each_source_has_its_own_first_pass():
    old = nodes(("unifi:a", "gateway", "2026-09-01T00:00:00Z"), ("unifi:c", "ap", "2026-09-20T00:00:00Z"))
    assert dd.new_infrastructure(old, NOW) == []  # c was news in September, not now
    two = nodes(
        ("unifi:a", "gateway", "2026-09-01T00:00:00Z"), ("unraid:s1", "server", "2026-10-08T00:00:00Z")
    )
    assert (
        dd.new_infrastructure(two, NOW) == []
    )  # the first Unraid server is that source's starting inventory


def test_run_two_days_apart_finds_the_new_device_and_playbook_words_it(tmp_path, fake):
    env, store = setup(tmp_path)
    framework.run_due(store, env, NOW - timedelta(days=2))
    fake["world"] = world([device("new", "USW-Flex", "Spare switch", "7.5.10", updatable=True)])
    framework.run_due(store, env, NOW)
    found = detect.run(store, Systems(), NOW, env, kinds=("new_infrastructure", "firmware_pending"))[
        "findings"
    ]
    kinds = sorted(f.kind for f in found)
    assert kinds == ["firmware_pending", "firmware_pending", "new_infrastructure"]
    from pathlib import Path

    advised = playbook.apply(found, playbook.validate(Path(__file__).resolve().parents[1] / "playbook.yaml"))
    texts = {f.kind: f.detail["advice"] for f in advised if f.detail.get("advice")}
    assert texts["new_infrastructure"].startswith("A new switch (Spare switch) appeared on the network map")
    assert "firmware update available (running" in texts["firmware_pending"]


# ---------------------------------------------------------------- the client


def test_the_client_pages_through_a_list_and_sends_the_key_as_a_header(serve):  # noqa: F811
    s = serve(
        (200, {"offset": 0, "limit": 2, "count": 2, "totalCount": 3, "data": [{"id": "1"}, {"id": "2"}]}),
        (200, {"offset": 2, "limit": 2, "count": 1, "totalCount": 3, "data": [{"id": "3"}]}),
    )
    base = s.url.removesuffix("/graphql")
    api = UnifiApi(base, "secret-key", api_path="/integration")
    assert [x["id"] for x in api.paged("/v1/sites")] == ["1", "2", "3"]
    assert [key for key, _ in s.seen] == ["secret-key", "secret-key"]
    assert api.base == base + "/integration"


def test_the_client_maps_failures_to_safe_errors(serve):  # noqa: F811
    s = serve((401, {"message": "no"}))
    with pytest.raises(ApiError) as exc:
        UnifiApi(s.url.removesuffix("/graphql"), "secret-key").get("/v1/info")
    assert exc.value.kind == "auth" and "secret-key" not in exc.value.message
    with pytest.raises(ApiError) as exc:
        UnifiApi("http://127.0.0.1:9", "k", timeout=2).get("/v1/info")
    assert exc.value.kind == "unreachable"
    s2 = serve((200, {"unexpected": True}))
    with pytest.raises(ApiError) as exc:
        UnifiApi(s2.url.removesuffix("/graphql"), "k").paged("/v1/sites")
    assert exc.value.kind == "bad_response"
