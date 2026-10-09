"""Task 19: the Unraid collector and the disk and server detectors, from fixture responses."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import unraid_fixtures as fx
from test_collectors import lab

from brief import detect, playbook
from brief.collectors import CollectorError, framework, smartctl, unraid
from brief.collectors.unraid_api import ApiError
from brief.config import ConfigError
from brief.systems import Systems

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
URL = {
    1: "https://198.51.100.11/graphql",
    2: "https://198.51.100.12/graphql",
    3: "https://198.51.100.13/graphql",
}
YAML = f"""  unraid:
    enabled: true
    smart_source: {{smart}}
    smart_files_path: {{path}}
    servers:
      - {{{{ id: server-1, url: "{URL[1]}", key_var: KEY_ONE }}}}
      - {{{{ id: server-2, url: "{URL[2]}", key_var: KEY_TWO }}}}
      - {{{{ id: server-3, url: "{URL[3]}", key_var: KEY_THREE }}}}
"""
KEYS = {"KEY_ONE": "k1", "KEY_TWO": "k2", "KEY_THREE": "k3"}


@pytest.fixture
def servers(monkeypatch):
    table = {
        URL[1]: fx.healthy_server("A", parity=fx.parity_history(6_600_000, "2026-10-02T21:49:00.000Z")),
        URL[2]: ApiError("unreachable", "could not connect (TimeoutError)"),
        URL[3]: fx.healthy_server(
            "C", parity=fx.parity_history(60_000_000, "2026-10-01T08:30:00.000Z"), notices=[]
        ),
    }
    monkeypatch.setattr(unraid, "API_FACTORY", fx.factory(table))
    return table


def setup(tmp_path, smart="api"):
    path = tmp_path / "smart"
    env, user, store = lab(tmp_path, YAML.format(smart=smart, path=path))
    return env | KEYS, store, path


def rows(store, sql, *args):
    with store.read() as db:
        return [dict(r) for r in db.execute(sql, args)]


# ---------------------------------------------------------------- parsing


def test_disks_get_their_role_from_the_array_and_flash_is_recognised():
    data = fx.healthy_server("A")
    disks = {d.serial: d for d in unraid.parse_disks(data["disks"], data["array"])}
    assert {d.role for d in disks.values()} == {"parity", "data", "cache", "flash"}
    assert disks["A0001"].role == "parity" and disks["A0005"].role == "flash"
    assert disks["A0004"].rotational is False  # an SSD by its model name
    assert (
        disks["A0002"].errors == 2 and disks["A0002"].temperature == 34
    )  # the array's temp when the disk has none
    assert disks["A0003"].temperature is None  # spun down: no reading, not a zero


@pytest.mark.parametrize(
    "status, expected",
    [
        ("OK", True),
        ("PASSED", True),
        ("FAILED", False),
        ("PRE_FAIL", False),
        ("UNKNOWN", None),
        (None, None),
        ("WEIRD", None),
    ],
)
def test_smart_status_is_read_cautiously(status, expected):
    data = fx.healthy_server("A")
    data["disks"]["disks"][0]["smartStatus"] = status
    assert unraid.parse_disks(data["disks"], data["array"])[0].smart_ok is expected


def test_parity_history_dates_and_speeds_are_normalised():
    (first, *_) = unraid.parse_parity(
        {"parityHistory": fx.parity_history(6_644_158, "2026-10-02T21:49:00.000Z")}
    )
    assert first == {
        "date": "2026-10-02T21:49:00Z",
        "duration_s": 150000,
        "speed_bps": 6644158,
        "status": "COMPLETED",
        "errors": 0,
    }
    assert unraid.parse_parity({"parityHistory": [{"date": "garbage"}, "x", None]}) == []


def test_notices_keep_only_warnings_and_alerts():
    kept = unraid.parse_notices(fx.healthy_server("A")["notices"])
    assert [n["id"] for n in kept] == ["n2"] and kept[0]["importance"] == "WARNING"


def test_containers_and_versions():
    data = fx.healthy_server("A")
    assert [c["name"] for c in unraid.parse_containers(data["docker"])] == ["web", "db", "old"]
    items = unraid.parse_versions(data["info"], data["plugins"])
    assert {(i["kind"], i["version"]) for i in items} == {
        ("os", "7.3.2"),
        ("api", "4.37.5"),
        ("plugin", "2026.10.01"),
    }


# ---------------------------------------------------------------- the settings


@pytest.mark.parametrize(
    "extra, message",
    [
        ("smart_source: magic", "smart_source"),
        ("servers: [{id: Server1, url: 'https://x/graphql', key_var: KEY_A}]", "lowercase"),
        (
            "servers: [{id: a, url: 'https://x/graphql', key_var: KEY_A}, {id: a, url: 'https://y/graphql', key_var: KEY_A}]",
            "twice",
        ),
        ("servers: [{id: a, url: 'https://x/graphql'}]", "key_var"),
        ("servers: [{id: a, url: 'ftp://x', key_var: KEY_A}]", "url must look like"),
        ("servers: [{id: a, url: 'https://<server-1-ip>/graphql', key_var: KEY_A}]", "placeholder"),
        ("servers: [{id: a, url: 'https://x/graphql', key_var: KEY_A, verify_tls: maybe}]", "verify_tls"),
    ],
)
def test_bad_server_settings_are_refused(tmp_path, extra, message):
    _, user, _ = lab(tmp_path, f"  unraid:\n    enabled: true\n    {extra}\n")
    with pytest.raises(ConfigError, match=message):
        framework.validate(user / "collectors.yaml")


def test_the_shipped_unraid_block_is_valid_while_disabled():
    from pathlib import Path

    assert (
        framework.validate(Path(__file__).resolve().parents[1] / "collectors.yaml")["unraid"].enabled is False
    )


def test_every_server_gets_its_sensors_and_the_smart_age_only_in_file_mode(tmp_path):
    env, _, _ = setup(tmp_path)
    ids = {m["id"] for m in framework.metric_entries(env)}
    assert {
        "unraid_server_1_reachable",
        "unraid_server_3_disk_temp_max_c",
        "unraid_server_2_parity_speed_mb_s",
    } <= ids
    assert not any(i.endswith("smart_files_age_h") for i in ids)
    env2, _, _ = setup(tmp_path / "b", smart="smartctl_files")
    assert "unraid_server_1_smart_files_age_h" in {m["id"] for m in framework.metric_entries(env2)}


# ---------------------------------------------------------------- a run


def test_a_run_updates_reachable_servers_and_reports_the_unreachable_one(tmp_path, servers):
    env, store, _ = setup(tmp_path)
    outcome = framework.run_due(store, env, NOW)
    assert (
        outcome["unraid"]["ok"] is True
    )  # one server answering is a run; the other is reported as unreachable
    numbers = framework.read_state(store)["unraid"]["metrics"]
    assert numbers["unraid_server_1_reachable"] == 1 and numbers["unraid_server_2_reachable"] == 0
    assert numbers["unraid_server_1_array_started"] == 1
    assert numbers["unraid_server_1_disk_temp_max_c"] == 34 and numbers["unraid_server_1_disk_errors"] == 2
    assert numbers["unraid_server_1_containers_running"] == 2 and numbers["unraid_server_1_notices_open"] == 1
    assert (
        numbers["unraid_server_1_parity_speed_mb_s"] == 6.6
        and numbers["unraid_server_1_parity_age_days"] == 6
    )
    assert "unraid_server_2_array_started" not in numbers  # no data for a server we could not read
    assert numbers["unraid_server_3_notices_open"] == 0


def test_rows_land_in_the_tables(tmp_path, servers):
    env, store, _ = setup(tmp_path)
    framework.run_due(store, env, NOW)
    disks = rows(store, "SELECT disk_id, server, role FROM disk WHERE server = 'server-1' ORDER BY disk_id")
    assert [d["role"] for d in disks] == ["parity", "data", "data", "cache", "flash"]
    assert rows(store, "SELECT COUNT(*) n FROM parity_check WHERE server = 'server-1'")[0]["n"] == 6
    assert rows(store, "SELECT subject FROM server_notice WHERE server = 'server-1'") == [
        {"subject": "Array has 1 disk with read errors"}
    ]
    inv = {
        r["item_id"]: r["current_version"]
        for r in rows(store, "SELECT item_id, current_version FROM inventory")
    }
    assert (
        inv["unraid:server-1:os"] == "7.3.2" and inv["unraid:server-3:plugin:compose.manager"] == "2026.10.01"
    )
    kinds = {r["node_id"]: r["kind"] for r in rows(store, "SELECT node_id, kind FROM topo_node")}
    assert kinds["unraid:server-1"] == "server" and kinds["unraid:server-1:ct:web"] == "container"
    assert rows(store, "SELECT COUNT(*) n FROM topo_edge WHERE src = 'unraid:server-1'")[0]["n"] == 3
    assert not any("vm" in k for k in kinds)  # "VMs are not available" is an empty list, not a failure


def test_notifications_are_replaced_each_run_and_unchanged_smart_values_are_not_rewritten(tmp_path, servers):
    env, store, _ = setup(tmp_path)
    framework.run_due(store, env, NOW)
    before = rows(store, "SELECT COUNT(*) n FROM smart_reading WHERE attr = 'smart_status'")[0]["n"]
    servers[URL[1]]["notices"] = {"notifications": {"list": []}}
    framework.run_due(store, env, NOW + timedelta(hours=1))
    assert rows(store, "SELECT * FROM server_notice WHERE server = 'server-1'") == []
    assert rows(store, "SELECT COUNT(*) n FROM smart_reading WHERE attr = 'smart_status'")[0]["n"] == before
    temps = rows(
        store, "SELECT COUNT(*) n FROM smart_reading WHERE attr = 'temperature' AND disk_id LIKE 'server-1:%'"
    )
    assert temps[0]["n"] == 2 * 3  # sampled every run; the spun-down disk and the flash drive have none


def test_every_server_down_is_a_failed_run_and_a_missing_key_only_skips_that_server(
    tmp_path, monkeypatch, servers
):
    env, store, _ = setup(tmp_path)
    monkeypatch.setattr(
        unraid, "API_FACTORY", fx.factory({u: ApiError("unreachable", "x") for u in URL.values()})
    )
    outcome = framework.run_due(store, env, NOW)
    assert outcome["unraid"] == {"ran": True, "ok": False, "error": "no Unraid server could be read"}
    monkeypatch.setattr(unraid, "API_FACTORY", fx.factory(servers))
    only_one = {k: v for k, v in env.items() if k != "KEY_TWO"}
    assert framework.run_due(store, only_one, NOW + timedelta(hours=1))["unraid"]["ok"] is True
    monkeypatch.setitem(only_one, "KEY_ONE", "rejected")
    assert (
        framework.run_due(store, only_one, NOW + timedelta(hours=2))["unraid"]["ok"] is True
    )  # server 3 still answers


def test_the_connection_test_names_each_server_and_the_fields_it_had_to_drop(tmp_path, monkeypatch, servers):
    env, store, _ = setup(tmp_path)
    original = fx.FakeApi

    class Dropping(original):
        def __init__(self, sections, dropped=None):
            super().__init__(sections, {"disks": ["smartStatus"]})

    monkeypatch.setattr(fx, "FakeApi", Dropping)
    monkeypatch.setattr(unraid, "API_FACTORY", fx.factory(servers))
    ok, message = framework.test_connection("unraid", store, env, NOW)
    assert ok and message.startswith("2 of 3 servers answered")
    assert "server-1: reachable" in message and "no field(s) smartStatus" in message
    assert "vms: not available" in message and "server-2: could not connect" in message


def test_the_connection_test_fails_when_no_server_answers(tmp_path, monkeypatch):
    env, store, _ = setup(tmp_path)
    monkeypatch.setattr(
        unraid,
        "API_FACTORY",
        fx.factory({u: ApiError("auth", "the API key was rejected") for u in URL.values()}),
    )
    ok, message = framework.test_connection("unraid", store, env, NOW)
    assert not ok and "no server could be read" in message and "server-1" in message
    ok, message = framework.test_connection(
        "unraid", store, {k: v for k, v in env.items() if k not in KEYS}, NOW
    )
    assert not ok and "KEY_ONE is not set" in message


# ---------------------------------------------------------------- smartctl files and the detectors


def write_smart(folder, server, serial, **kwargs):
    (folder / server).mkdir(parents=True, exist_ok=True)
    (folder / server / f"{serial}.json").write_text(fx.smartctl_report(serial, **kwargs), encoding="utf-8")


def test_smartctl_files_add_attributes_and_the_detectors_find_them(tmp_path, servers):
    env, store, path = setup(tmp_path, smart="smartctl_files")
    write_smart(path, "server-1", "A0002", pending=0, realloc=0, crc=1)
    write_smart(path, "server-1", "A0003", passed=False, pending=0)
    t0 = NOW - timedelta(days=20)
    framework.run_due(store, env, t0)
    write_smart(path, "server-1", "A0002", pending=8, realloc=3, crc=9)
    write_smart(path, "server-1", "A0003", passed=False, pending=0)
    framework.run_due(store, env, NOW)

    found = detect.run(store, Systems(), NOW, env, kinds=("smart_failing", "smart_trend", "crc_rising"))[
        "findings"
    ]
    by_kind = {}
    for f in found:
        by_kind.setdefault(f.kind, []).append(f)
    assert [f.detail["disk"] for f in by_kind["smart_failing"]] == ["server-1 Test HDD 1TB …0003"]
    trend = {f.detail["attr"]: f for f in by_kind["smart_trend"]}
    assert trend["197:Current_Pending_Sector"].detail["raw"] == 8
    assert trend["5:Reallocated_Sector_Ct"].detail["delta_14d"] == 3
    (crc,) = by_kind["crc_rising"]
    assert crc.detail["delta_14d"] == 8

    advised = playbook.apply(found, playbook.validate(unraid_playbook()))
    buckets = {(f.kind, f.detail.get("attr")): f.detail.get("bucket") for f in advised}
    assert buckets[("smart_failing", None)] == "act_now"
    assert buckets[("smart_trend", "197:Current_Pending_Sector")] == "act_now"
    assert buckets[("smart_trend", "5:Reallocated_Sector_Ct")] == "plan"
    assert buckets[("crc_rising", None)] == "watch"
    pending = next(f for f in advised if f.detail.get("attr") == "197:Current_Pending_Sector")
    assert "reports 8 unreadable sectors" in pending.detail["advice"]


def unraid_playbook():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "playbook.yaml"


def test_a_stale_or_broken_smart_file_does_not_stop_the_run(tmp_path, servers):
    env, store, path = setup(tmp_path, smart="smartctl_files")
    write_smart(path, "server-1", "A0002")
    (path / "server-1" / "junk.json").write_text("not json", encoding="utf-8")
    (path / "server-1" / "noserial.json").write_text("{}", encoding="utf-8")
    framework.run_due(store, env, NOW)
    state = framework.read_state(store)["unraid"]
    assert state["ok"] and "unraid_server_1_smart_files_age_h" in state["metrics"]


def test_a_smart_file_for_a_drive_the_api_did_not_list_is_still_tracked(tmp_path, servers):
    env, store, path = setup(tmp_path, smart="smartctl_files")
    write_smart(path, "server-1", "Z9999", pending=2)
    framework.run_due(store, env, NOW)
    assert rows(store, "SELECT role FROM disk WHERE serial = 'Z9999'") == [{"role": "unassigned"}]


def test_sustained_heat_on_a_spinning_disk_only(tmp_path, servers):
    env, store, _ = setup(tmp_path)
    for hour in range(0, 72, 2):  # 36 samples over three days, always 55 C (the SSD too)
        servers[URL[1]] = fx.healthy_server("A", temps={"sda": 55, "sdb": 55, "sdd": 55, "sdc": 40})
        framework.run_due(store, env, NOW - timedelta(hours=72 - hour))
    found = detect.run(store, Systems(), NOW, env, kinds=("disk_temp",))["findings"]
    assert sorted(f.detail["disk"] for f in found) == [
        "server-1 Test HDD 1TB …0001",
        "server-1 Test HDD 1TB …0002",
    ]
    assert all(f.detail["temp_c"] == 55 for f in found)


def test_a_slow_parity_check_is_a_finding_and_a_normal_one_is_not(tmp_path, servers):
    env, store, _ = setup(tmp_path)
    framework.run_due(store, env, NOW)
    found = detect.run(store, Systems(), NOW, env, kinds=("parity_slow",))["findings"]
    (f,) = found
    assert (
        f.detail["server"] == "server-1" and f.detail["speed"] == 6.6 and f.detail["previous_speed"] == 61.0
    )
    assert f.id == "parity_slow:server-1:2026-10-02T21:49:00Z" and "took 1 d 18 h" in f.title
    later = detect.run(store, Systems(), NOW + timedelta(days=45), env, kinds=("parity_slow",))["findings"]
    assert later == []  # an old slow check is history
    advised = playbook.apply(found, playbook.validate(unraid_playbook()))
    assert (
        advised[0].detail["bucket"] == "watch"
        and "server-1's parity check took 1 d 18 h at 6.6 MB/s" in advised[0].detail["advice"]
    )


def test_a_warning_notification_becomes_a_finding_until_it_is_read(tmp_path, servers):
    env, store, _ = setup(tmp_path)
    framework.run_due(store, env, NOW)
    (f,) = detect.run(store, Systems(), NOW, env, kinds=("unraid_warning",))["findings"]
    assert f.title == "server-1: Array has 1 disk with read errors" and f.detail["importance"] == "warning"


def test_findings_belong_to_the_system_with_the_same_id(tmp_path, servers):
    env, store, _ = setup(tmp_path)
    framework.run_due(store, env, NOW)
    systems = Systems.from_mapping(
        {"systems": [{"id": "server-1", "label": "Server One", "hosts": ["198.51.100.11"]}]}
    )
    (f,) = detect.run(store, systems, NOW, env, kinds=("unraid_warning",))["findings"]
    assert f.system == "server-1"
    (g,) = detect.run(store, Systems(), NOW, env, kinds=("unraid_warning",))["findings"]
    assert g.system is None


def test_version_behind_needs_an_available_version_that_has_waited(tmp_path):
    from brief import detectors_hw as dd

    items = [
        dd.InventoryRow("a", "os", "Unraid OS", "7.3.1", "7.3.2", "2026-08-01T00:00:00Z"),
        dd.InventoryRow("b", "plugin", "p", "1", "2", "2026-10-01T00:00:00Z"),
        dd.InventoryRow("c", "plugin", "q", "1", None, None),
    ]
    (f,) = dd.version_behind(items, NOW, days=30)
    assert f.id == "version_behind:a" and f.detail["days"] == 69 and "7.3.2" in f.title


# ---------------------------------------------------------------- smartctl and the client


def test_smartctl_reports_are_parsed_and_junk_is_refused():
    ata = smartctl.parse(fx.smartctl_report("S1", pending=3, crc=7))
    assert ata.passed and ata.temperature == 34 and ata.rotational is True
    assert ata.attrs["197:Current_Pending_Sector"]["raw"] == 3 and "9:Power_On_Hours" not in ata.attrs
    nvme = smartctl.parse(fx.smartctl_report("S2", nvme=True, pending=4))
    assert nvme.rotational is False and nvme.attrs["nvme:media_errors"]["raw"] == 4 and nvme.temperature == 34
    for bad in ("not json", "[]", json.dumps({"model_name": "x"})):
        with pytest.raises(smartctl.SmartError):
            smartctl.parse(bad)


def test_the_run_error_for_a_collector_without_servers_is_clear(tmp_path):
    env, user, store = lab(tmp_path, "  unraid:\n    enabled: true\n    servers: []\n")
    assert framework.run_due(store, env, NOW)["unraid"]["error"] == "no Unraid server could be read"
    with pytest.raises(CollectorError):
        unraid.UnraidCollector().test(framework.Context("unraid", {"servers": []}, {}, store, NOW))
