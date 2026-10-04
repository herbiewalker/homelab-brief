from pathlib import Path

from brief import netinfo as n
from brief.settings import Settings


def describe(address, names=("eth0", "lo"), **kw):
    return n.describe(address_fn=lambda: address, names=list(names), **kw)


def test_a_lan_address_with_only_eth0_is_own_ip():
    d = describe("192.0.2.77")
    assert d["mode"] == "own_ip" and d["address"] == "192.0.2.77"
    assert d["send_syslog_to"] == "192.0.2.77" and d["warnings"] == []


def test_a_docker_internal_address_is_bridge_and_asks_for_the_host_address():
    d = describe("172.18.0.5")
    assert d["mode"] == "bridge" and d["send_syslog_to"] is None
    assert any("advertised_address" in w for w in d["warnings"])
    ok = describe("172.18.0.5", advertised="host.example")
    assert ok["send_syslog_to"] == "host.example" and ok["warnings"] == []


def test_seeing_the_hosts_bridges_means_host_mode():
    assert describe("192.0.2.5", names=("br0", "docker0", "eth0", "lo"))["mode"] == "host"
    d = describe("192.0.2.5", names=("docker0", "eth0"), ports={"syslog": 514})
    assert any("5514" in w for w in d["warnings"])
    assert not describe("192.0.2.5", names=("docker0",), ports={"syslog": 5514})["warnings"]


def test_a_configured_mode_wins_but_a_mismatch_is_flagged():
    d = describe("172.18.0.5", configured_mode="own_ip")
    assert d["mode"] == "own_ip" and d["detected_mode"] == "bridge"
    assert any("looks like bridge" in w for w in d["warnings"])
    assert describe("192.0.2.5", configured_mode="auto")["warnings"] == []


def test_no_route_is_reported():
    d = describe(None)
    assert d["address"] is None and any("no network route" in w for w in d["warnings"])


def test_docker_range_boundaries():
    assert n.in_docker_range("172.16.0.1") and n.in_docker_range("172.31.255.254")
    assert not n.in_docker_range("172.32.0.1") and not n.in_docker_range("192.0.2.1")
    assert not n.in_docker_range(None) and not n.in_docker_range("not-an-ip")


def test_interfaces_reads_a_folder_and_survives_a_missing_one(tmp_path):
    (tmp_path / "eth0").mkdir()
    (tmp_path / "lo").mkdir()
    assert n.interfaces(tmp_path) == ["eth0", "lo"]
    assert n.interfaces(Path(tmp_path / "missing")) == []


def test_the_detected_address_fills_in_only_in_own_ip_mode_and_only_when_unset(tmp_path):
    s = Settings(state_dir=tmp_path)
    own = {"mode": "own_ip", "address": "192.0.2.9"}
    assert n.with_detected_address(s, own).advertised == "192.0.2.9"
    assert (
        n.with_detected_address(Settings(state_dir=tmp_path, advertised="mine.example"), own).advertised
        == "mine.example"
    )
    assert n.with_detected_address(s, {"mode": "bridge", "address": "172.18.0.5"}).advertised == ""
    assert n.with_detected_address(s, {"mode": "own_ip", "address": None}).advertised == ""


def test_the_mode_comes_from_network_yaml(tmp_path):
    folder = tmp_path / "c" / "brief"
    folder.mkdir(parents=True)
    (folder / "network.yaml").write_text("mode: Bridge\n", encoding="utf-8")
    env = {"BRIEF_CONFIG_DIR": str(tmp_path / "c"), "BRIEF_DEFAULTS_DIR": str(tmp_path / "d")}
    assert Settings.from_env(env).network_mode == "bridge"
    assert Settings.from_env({**env, "BRIEF_CONFIG_DIR": str(tmp_path / "none")}).network_mode == "auto"


def test_status_json_carries_the_network_block(tmp_path, monkeypatch):
    from brief import health, status
    from brief.store import Store
    from brief.systems import Systems

    monkeypatch.setattr(health, "receiver_ok", lambda port: True)
    store = Store(tmp_path / "b.db")
    try:
        net = describe("192.0.2.9")
        doc = status.build_status(store, Systems(), 514, 60.0, 5, network=net)
        assert doc["network"]["send_syslog_to"] == "192.0.2.9"
    finally:
        store.close()
