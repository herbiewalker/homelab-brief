"""Task 21: client presence and traffic from the UniFi collector, limited by each device's profile."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_collectors import lab
from test_unifi import FakeUnifi, world

from brief.collectors import framework, unifi
from brief.collectors.httpjson import ApiError
from brief.config import ConfigError

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
FULL, SECURITY, IGNORED, NEWCOMER = (
    "00:00:5e:00:53:21",
    "00:00:5e:00:53:22",
    "00:00:5e:00:53:23",
    "00:00:5e:00:53:24",
)
KEYS = {"UNIFI_URL": "https://198.51.100.20", "UNIFI_API_KEY": "not-a-real-key"}
YAML = "  unifi:\n    enabled: true\n    url_var: UNIFI_URL\n    key_var: UNIFI_API_KEY\n    clients: true\n"
DEVICES = f"""schema: 1
devices:
  - {{ mac: "{FULL}", label: "Full laptop", profile: full }}
  - {{ mac: "{SECURITY}", label: "Security console", profile: security_only }}
  - {{ mac: "{IGNORED}", label: "Ignored plug", profile: ignore }}
"""


class FakeClients(FakeUnifi):
    def __init__(self, world, clients, stations):
        super().__init__(world)
        self.clients, self.stations = clients, stations

    def paged(self, path):
        if path.endswith("/clients"):
            if isinstance(self.clients, ApiError):
                raise self.clients
            return [
                {"id": f"c-{m}", "macAddress": m.upper(), "name": n, "type": "WIRELESS"}
                for m, n in self.clients
            ]
        return super().paged(path)

    def get_root(self, path):
        assert path == "/proxy/network/api/s/default/stat/sta"
        if isinstance(self.stations, ApiError):
            raise self.stations
        return {"meta": {"rc": "ok"}, "data": self.stations}


def station(mac, tx, rx, name=None, oui="Acme", network="LAN"):
    return {"mac": mac, "hostname": name, "oui": oui, "network": network, "tx_bytes": tx, "rx_bytes": rx}


@pytest.fixture
def api(monkeypatch):
    state = {
        "clients": [(FULL, "laptop-host"), (SECURITY, "console-host"), (IGNORED, "plug-host")],
        "stations": [],
    }
    monkeypatch.setattr(
        unifi, "API_FACTORY", lambda *a, **k: FakeClients(world(), state["clients"], state["stations"])
    )
    return state


def setup(tmp_path, extra="", devices=DEVICES):
    env, user, store = lab(tmp_path, YAML + extra)
    if devices:
        (user / "devices.yaml").write_text(devices, encoding="utf-8")
    return env | KEYS, store


def rows(store, sql):
    with store.read() as db:
        return [dict(r) for r in db.execute(sql)]


# ---------------------------------------------------------------- settings


@pytest.mark.parametrize(
    "extra, message",
    [
        ("    traffic: true\n    clients: false\n", "traffic needs clients"),
        ("    clients: maybe\n", "clients must be true or false"),
        ("    client_logs: 1\n", "client_logs must be true or false"),
        ("    traffic_path: /api/stat\n", "traffic_path"),
        ("    traffic_path: /a/{site}/{site}\n", "traffic_path"),
    ],
)
def test_bad_client_settings_are_refused(tmp_path, extra, message):
    _, user, _ = lab(
        tmp_path, "  unifi:\n    enabled: true\n    url_var: UNIFI_URL\n    key_var: UNIFI_API_KEY\n" + extra
    )
    with pytest.raises(ConfigError, match=message):
        framework.validate(user / "collectors.yaml")


def test_the_client_sensors_exist_only_when_asked_for(tmp_path):
    env, _ = setup(tmp_path)
    assert "unifi_clients" in {m["id"] for m in framework.metric_entries(env)}
    assert "unifi_traffic_ok" not in {m["id"] for m in framework.metric_entries(env)}
    env2, _ = setup(tmp_path / "b", "    traffic: true\n")
    assert "unifi_traffic_ok" in {m["id"] for m in framework.metric_entries(env2)}
    plain = lab(
        tmp_path / "c", "  unifi:\n    enabled: true\n    url_var: UNIFI_URL\n    key_var: UNIFI_API_KEY\n"
    )[0]
    assert "unifi_clients" not in {m["id"] for m in framework.metric_entries(plain)}


# ---------------------------------------------------------------- presence and profiles


def test_presence_respects_every_profile(tmp_path, api):
    env, store = setup(tmp_path)
    assert framework.run_due(store, env, NOW)["unifi"]["ok"] is True
    assert (
        framework.read_state(store)["unifi"]["metrics"]["unifi_clients"] == 3
    )  # a count of everyone connected
    stored = {r["mac"]: r for r in rows(store, "SELECT * FROM client")}
    assert set(stored) == {FULL, SECURITY}  # the ignored device is not stored
    assert stored[FULL]["hostname"] == "laptop-host" and stored[FULL]["first_seen"] == "2026-10-09T12:00:00Z"
    minutes = {r["mac"]: r["online_min"] for r in rows(store, "SELECT mac, online_min FROM client_hourly")}
    assert minutes == {FULL: 60}  # presence minutes for the full profile only


def test_a_second_poll_in_the_same_hour_does_not_double_presence(tmp_path, api):
    env, store = setup(tmp_path)
    framework.run_due(store, env, NOW)
    framework.run_due(store, env, NOW + timedelta(minutes=20), force=True)
    assert rows(store, "SELECT online_min FROM client_hourly") == [{"online_min": 60}]


def test_with_no_devices_file_everyone_is_security_only(tmp_path, api):
    env, store = setup(tmp_path, devices=None)
    framework.run_due(store, env, NOW)
    assert {r["mac"] for r in rows(store, "SELECT mac FROM client")} == {FULL, SECURITY, IGNORED}
    assert rows(store, "SELECT * FROM client_hourly") == []  # no presence timeline for anyone


def test_clients_are_off_by_default(tmp_path, api):
    env, user, store = lab(
        tmp_path, "  unifi:\n    enabled: true\n    url_var: UNIFI_URL\n    key_var: UNIFI_API_KEY\n"
    )
    framework.run_due(store, env | KEYS, NOW)
    assert rows(store, "SELECT * FROM client") == []


# ---------------------------------------------------------------- traffic


def test_traffic_is_the_change_in_the_counters_and_a_reset_is_handled(tmp_path, api):
    env, store = setup(tmp_path, "    traffic: true\n")
    api["stations"] = [
        station(FULL, 1000, 500, "laptop-host"),
        station(SECURITY, 4000, 100),
        station(IGNORED, 9, 9),
    ]
    framework.run_due(store, env, NOW)
    assert (
        rows(store, "SELECT * FROM client_hourly WHERE bytes_up > 0 OR bytes_down > 0") == []
    )  # first sighting adds nothing
    api["stations"] = [
        station(FULL, 3000, 900, "laptop-host"),
        station(SECURITY, 4500, 300),
        station(IGNORED, 99, 99),
    ]
    framework.run_due(store, env, NOW + timedelta(hours=1))
    got = {
        r["mac"]: (r["bytes_down"], r["bytes_up"])
        for r in rows(store, "SELECT mac, bytes_up, bytes_down FROM client_hourly")
        if r["bytes_down"]
    }
    assert got == {FULL: (2000, 400), SECURITY: (500, 200)}
    api["stations"] = [
        station(FULL, 120, 40, "laptop-host"),
        station(SECURITY, 4600, 310),
    ]  # the laptop reconnected
    framework.run_due(store, env, NOW + timedelta(hours=2))
    laptop = rows(
        store, f"SELECT bytes_down, bytes_up FROM client_hourly WHERE mac = '{FULL}' ORDER BY day, hour DESC"
    )
    assert (120, 40) in {(r["bytes_down"], r["bytes_up"]) for r in laptop}
    assert IGNORED not in json.dumps(rows(store, "SELECT value FROM meta WHERE key = 'unifi:traffic'"))
    assert framework.read_state(store)["unifi"]["metrics"]["unifi_traffic_ok"] == 1


def test_the_station_record_adds_vendor_and_network_to_the_presence_record(tmp_path, api):
    env, store = setup(tmp_path, "    traffic: true\n")
    api["stations"] = [station(FULL, 1, 1, "laptop-host", oui="Acme Corp", network="IoT")]
    framework.run_due(store, env, NOW)
    row = rows(store, f"SELECT hostname, vendor, vlan FROM client WHERE mac = '{FULL}'")[0]
    assert row == {"hostname": "laptop-host", "vendor": "Acme Corp", "vlan": "IoT"}


def test_unreadable_traffic_is_reported_but_presence_still_works(tmp_path, api):
    env, store = setup(tmp_path, "    traffic: true\n")
    api["stations"] = ApiError("auth", "the API key was rejected")
    outcome = framework.run_due(store, env, NOW)
    assert outcome["unifi"]["ok"] is True
    numbers = framework.read_state(store)["unifi"]["metrics"]
    assert numbers["unifi_traffic_ok"] == 0 and numbers["unifi_clients"] == 3
    assert {r["mac"] for r in rows(store, "SELECT mac FROM client")} == {FULL, SECURITY}


def test_old_hours_are_rolled_up_once_a_day(tmp_path, api):
    env, store = setup(tmp_path)
    with store.write() as tx:
        for hour in (1, 2):
            tx.db.execute("INSERT INTO client_hourly VALUES ('2026-05-01', ?, ?, 10, 10, 60)", (hour, FULL))
    framework.run_due(store, env, NOW)
    old = rows(store, "SELECT hour, bytes_up, online_min FROM client_hourly WHERE day = '2026-05-01'")
    assert old == [{"hour": 24, "bytes_up": 20, "online_min": 120}]
    assert rows(store, "SELECT value FROM meta WHERE key = 'clients:last_rollup'") == [
        {"value": "2026-10-09"}
    ]
