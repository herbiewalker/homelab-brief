"""15c: the Network page. The rules for network.yaml, the warnings, the syslog test, and the acceptance
scenarios: the right mode in each of the three network modes, a syslog test that turns green, and a bridge
that hides sender IPs raising its warning."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)

from brief import netconfig, netinfo, netpage
from brief.config import ConfigError, load_mapping
from brief.store import Store
from brief.systems import System, Systems

BRIEF = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
OWN, HOST, BRIDGE = "198.51.100.50", "198.51.100.60", "172.18.0.5"
GATEWAY = "172.17.0.1"
SHIPPED = (BRIEF / "network.yaml").read_text(encoding="utf-8")


def parsed(text, tmp_path):
    path = tmp_path / "network.yaml"
    path.write_text(text, encoding="utf-8")
    return load_mapping(path)


# ---------------------------------------------------------------- the rules for the file


def test_the_shipped_file_is_valid(tmp_path):
    netconfig.validate(parsed(SHIPPED, tmp_path))


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "overlay"},
        {"advertised_address": "http://host"},
        {"advertised_address": "host:8080"},
        {"advertised_address": "a b"},
        {"ports": {"https": 70000}},
        {"ports": {"https": 0}},
        {"ports": {"http": "eighty"}},
        {"ports": {"http": True}},
        {"ports": {"syslog": 514, "http": 80, "https": 80}},  # https the same as http
        {"ports": {"telnet": 23}},
        {"ports": [514]},
        {"unknown_senders": "shrug"},
        {"tls_extra_names": ["ok.example", "not valid!"]},
        {"allowed_senders": ["198.51.100.0/99"]},
        {"allowed_senders": "a-string"},
    ],
)
def test_nonsense_is_refused(change):
    base = {"mode": "auto", "advertised_address": "", "ports": {"syslog": 514, "http": 80, "https": 443}}
    with pytest.raises(ConfigError):
        netconfig.validate({**base, **change})


def test_good_values_pass_including_numbers_written_as_strings():
    netconfig.validate(
        {
            "mode": "host",
            "advertised_address": "lab.example",
            "ports": {"syslog": "5514", "http": 8480, "https": 8443},
        }
    )
    netconfig.validate(
        {
            "advertised_address": "198.51.100.7",
            "tls_extra_names": ["a.example"],
            "allowed_senders": ["198.51.100.0/24"],
        }
    )


def test_the_config_guard_uses_the_same_rule_for_network_yaml(tmp_path):
    from brief.configguard import VALIDATORS

    bad = tmp_path / "network.yaml"
    bad.write_text("schema: 1\nports: {https: 99999}\n")
    with pytest.raises(ConfigError):
        VALIDATORS["network.yaml"](bad)


def test_patch_changes_values_and_keeps_every_comment(tmp_path):
    out = netconfig.patch(
        SHIPPED,
        {
            "mode": "host",
            "advertised_address": "lab.example",
            "unknown_senders": "drop",
            "ports": {"syslog": 5514, "https": 8443},
        },
    )
    data = parsed(out, tmp_path)
    assert (
        data["mode"] == "host"
        and data["advertised_address"] == "lab.example"
        and data["unknown_senders"] == "drop"
    )
    assert data["ports"] == {"syslog": 5514, "http": 80, "https": 8443, "ssh": 22}
    for comment in ("# auto detects the mode", "# Ports the processes inside", "# for example"):
        assert comment in out
    assert "mode: host" in out and 'advertised_address: "lab.example"' in out
    netconfig.validate(data)


def test_patch_refuses_a_setting_it_cannot_find_as_a_plain_line():
    with pytest.raises(ConfigError, match="config editor"):
        netconfig.patch("schema: 1\n", {"mode": "host"})


def test_patch_keeps_trailing_comments_on_the_changed_line():
    out = netconfig.patch("ports:\n  syslog: 514   # the usual\n", {"ports": {"syslog": 5514}})
    assert "syslog: 5514   # the usual" in out


# ---------------------------------------------------------------- warnings that need data


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "baseline.db")
    yield s
    s.close()


def traffic(store, rows):
    with store.write() as tx:
        sid = tx.signature_for(0, 1, "app", "t", NOW)
        tx.add_hourly([(f"{NOW:%Y-%m-%d}", NOW.hour, host, sid, "info", n) for host, n in rows])


def test_one_docker_gateway_carrying_everything_means_the_bridge_hides_senders(store):
    traffic(store, [(GATEWAY, 500), ("198.51.100.9", 20)])
    assert GATEWAY in netpage.lost_sender_warning(store, NOW) and "rewriting" in netpage.lost_sender_warning(
        store, NOW
    )


@pytest.mark.parametrize(
    "rows",
    [
        [("198.51.100.9", 500)],  # a real sender
        [(GATEWAY, 100), ("198.51.100.9", 100), ("198.51.100.10", 100)],  # the gateway is only a third
        [(GATEWAY, 10)],  # too little traffic to say
        [("172.18.0.5", 500)],  # Docker range, but not a gateway-looking address
    ],
)
def test_normal_traffic_does_not_raise_the_lost_sender_warning(store, rows):
    traffic(store, rows)
    assert netpage.lost_sender_warning(store, NOW) is None


def test_no_traffic_no_warning(store):
    assert netpage.lost_sender_warning(store, NOW) is None


def test_host_mode_on_the_usual_ports_clashes_with_unraid():
    found = netpage.port_warnings("host", {"syslog": 514, "http": 80, "https": 443})
    assert len(found) == 2 and "8480" in found[0] and "8443" in found[1]  # (514 is netinfo's warning)
    assert netpage.port_warnings("host", {"syslog": 5514, "http": 8480, "https": 8443}) == []
    assert netpage.port_warnings("own_ip", {"syslog": 514, "http": 80, "https": 443}) == []
    assert netpage.port_warnings("bridge", {"syslog": 514, "http": 80, "https": 443}) == []


def test_each_mode_has_its_own_note():
    assert "Host access to custom networks" in netpage.notes("own_ip")[0]
    assert "mapped" in netpage.notes("bridge")[0] and netpage.notes("host") == []


def test_listening_reports_each_port_with_the_probe():
    rows = netpage.listening({"syslog": 514, "http": 80, "https": 443}, probe=lambda port: port != 443)
    assert [(r["port"], r["ok"]) for r in rows] == [(514, True), (80, True), (443, False)]


def test_repointing_tells_receiving_from_silent_from_not_seen(store):
    systems = Systems(
        [
            System("gw", "Gateway", "other", ("198.51.100.1", "198.51.100.2"), "UTC", True),
            System("cam", "Camera", "other", ("198.51.100.3",), "UTC", True),
            System("pc", "Desktop", "other", ("198.51.100.4",), "UTC", False),  # not expected: not listed
        ]
    )
    with store.write() as tx:
        tx.touch_host(
            "198.51.100.1", NOW - timedelta(days=2), NOW - timedelta(minutes=2), system_id="gw", known=True
        )
        tx.touch_host(
            "198.51.100.3", NOW - timedelta(days=9), NOW - timedelta(days=3), system_id="cam", known=True
        )
    rows = {r["entry"]: r["state"] for r in netpage.repointing(systems, store, NOW)}
    assert rows == {"198.51.100.1": "receiving", "198.51.100.2": "not seen", "198.51.100.3": "silent"}


def test_template_settings_cover_the_three_modes_and_mark_the_current_one():
    blocks = netpage.template_settings("host", OWN, {"syslog": 514, "http": 80, "https": 443})
    assert [b["mode"] for b in blocks] == ["own_ip", "host", "bridge"]
    assert [b["current"] for b in blocks] == [False, True, False]
    text = "\n".join(line for b in blocks for line in b["lines"])
    assert (
        OWN in text
        and "Host access to custom networks" in text
        and "5514" in text
        and "Port mappings" in text
    )


def test_pending_restart_names_what_only_a_restart_applies():
    running = {"syslog": 514, "http": 80, "https": 443}
    want = {"ports": {"syslog": 514, "https": 8443}, "advertised": "lab.example"}
    found = netpage.pending_restart(running, "", want)
    assert any("HTTPS port" in f and "8443" in f for f in found) and any("Advertised" in f for f in found)
    assert (
        netpage.pending_restart(
            running, "lab.example", {"ports": {"https": 443}, "advertised": "lab.example"}
        )
        == []
    )
    assert netpage.pending_restart(running, "", {}) == []


# ---------------------------------------------------------------- the syslog test line


def write_events(state, lines, name="2026-10-07.ndjson"):
    folder = state / "events"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def test_the_test_line_is_found_with_the_address_it_came_from(tmp_path):
    write_events(tmp_path, [{"source_ip": "198.51.100.9", "app": "x", "msg": "noise"}] * 5
                 + [{"source_ip": "198.51.100.77", "app": "logger", "msg": "homelab-brief-test-abc123"}])  # fmt: skip
    found = netpage.find_test_line(tmp_path, "homelab-brief-test-abc123")
    assert found == {"source_ip": "198.51.100.77", "app": "logger", "msg": "homelab-brief-test-abc123"}
    assert netpage.find_test_line(tmp_path, "homelab-brief-test-ffffff") is None
    assert netpage.find_test_line(tmp_path / "nothing", "x") is None


def test_the_commands_name_the_target_and_the_token():
    cmds = netpage.test_commands(OWN, 514, "abc123")
    assert all(OWN in c and "homelab-brief-test-abc123" in c for c in cmds) and "-T" in cmds[1]
    assert "<this container's address>" in netpage.test_commands(None, 514, "abc123")[0]


# ---------------------------------------------------------------- the page, in each network mode

MODES = {
    "own_ip": (OWN, ["eth0", "lo"]),
    "host": (HOST, ["eth0", "lo", "docker0", "br0"]),
    "bridge": (BRIDGE, ["eth0", "lo"]),
}


def in_mode(monkeypatch, mode):
    address, names = MODES[mode]
    monkeypatch.setattr(netinfo, "local_address", lambda: address)
    monkeypatch.setattr(netinfo, "interfaces", lambda root=None: names)


def csrf(client, path="/admin/network"):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


@pytest.mark.parametrize("mode", list(MODES))
def test_the_page_reports_each_network_mode_correctly(lab, monkeypatch, mode):  # noqa: F811
    in_mode(monkeypatch, mode)
    html = lab.client.get("/admin/network").text
    assert f'<span class="badge ok">{mode}</span>' in html
    assert MODES[mode][0] in html and "Unraid template settings" in html
    assert re.search(r"<h2>[^<]*<span[^>]*>current</span>", html) or "current" in html
    for title in ("Own IP", "Host", "Bridge"):
        assert title in html


def test_the_page_lists_only_the_warnings_that_belong_to_the_mode(lab, monkeypatch):  # noqa: F811
    in_mode(monkeypatch, "own_ip")
    assert "No network problems found for the own_ip mode." in lab.client.get("/admin/network").text
    in_mode(monkeypatch, "bridge")
    assert "bridge mode: devices must send syslog to the host" in lab.client.get("/admin/network").text


def test_a_bridge_that_hides_sender_ips_raises_the_warning(lab, monkeypatch):  # noqa: F811
    in_mode(monkeypatch, "bridge")
    with lab.store.write() as tx:
        sid = tx.signature_for(0, 9, "app", "t", NOW)
        tx.add_hourly([(f"{datetime.now(UTC):%Y-%m-%d}", datetime.now(UTC).hour, GATEWAY, sid, "info", 400)])
    html = lab.client.get("/admin/network").text
    assert "Almost all syslog" in html and GATEWAY in html and "rewriting the real sender addresses" in html


def test_the_syslog_test_turns_green_when_the_line_arrives(lab, monkeypatch):  # noqa: F811
    in_mode(monkeypatch, "own_ip")
    page = lab.client.get("/admin/network").text
    token = re.search(r"homelab-brief-test-([a-f0-9]+)", page).group(1)
    assert f"{OWN}" in page and 'hx-get="/admin/network/test/' + token in page
    waiting = lab.client.get(f"/admin/network/test/{token}").text
    assert "waiting" in waiting and "hx-trigger" in waiting  # still polling
    write_events(
        lab.settings.state_dir,
        [{"source_ip": "198.51.100.77", "app": "logger", "msg": f"homelab-brief-test-{token}"}],
    )
    green = lab.client.get(f"/admin/network/test/{token}").text
    assert "received" in green and "198.51.100.77" in green and "hx-trigger" not in green  # polling stops
    assert "Docker gateway" not in green


def test_a_test_line_from_a_docker_gateway_says_the_bridge_hides_the_sender(lab, monkeypatch):  # noqa: F811
    in_mode(monkeypatch, "bridge")
    write_events(
        lab.settings.state_dir, [{"source_ip": GATEWAY, "app": "logger", "msg": "homelab-brief-test-abc123"}]
    )
    html = lab.client.get("/admin/network/test/abc123").text
    assert "received" in html and "hiding sender addresses" in html


@pytest.mark.parametrize("token", ["zz", "../../etc", "ABCDEF", "abc 123", "a" * 40])
def test_a_bad_test_token_is_refused(lab, token):  # noqa: F811
    assert lab.client.get(f"/admin/network/test/{token}").status_code in (404, 422)


def card_base(lab, sid):
    """The digest hidden in a settings card (every card of one file shares it)."""
    page = lab.client.get("/admin/network").text
    card = page.split(f'id="card-{sid}"', 1)[1]
    return re.search(r'name="base" value="([^"]+)"', card).group(1)


def save_card(lab, sid, **fields):
    data = {"csrf_token": csrf(lab.client), "base": card_base(lab, sid), **fields}
    return lab.client.post(f"/admin/settings/{sid}/save", data=data, headers={"HX-Request": "true"})


def test_saving_settings_writes_network_yaml_through_the_config_editor(lab, monkeypatch, tmp_path):  # noqa: F811
    ship_network(tmp_path)
    in_mode(monkeypatch, "host")
    r = save_card(lab, "network", **{"f.mode": "host", "f.advertised_address": "lab.example", "f.unknown_senders": "flag",
                                     "f.allowed_senders": "198.51.100.0/24\n203.0.113.5"})  # fmt: skip
    assert (
        r.status_code == 200
        and "saved" in r.headers["hx-trigger"]
        and "Restart the container" in r.headers["hx-trigger"]
    )
    r = save_card(
        lab, "network-ports", **{"f.syslog": "5514", "f.http": "8480", "f.https": "8443", "f.ssh": "22"}
    )
    assert r.status_code == 200, r.text
    saved = (tmp_path / "cfg" / "brief" / "network.yaml").read_text()
    assert (
        "mode: host" in saved
        and "lab.example" in saved
        and "https: 8443" in saved
        and "# auto detects" in saved
    )
    data = yaml.safe_load(saved)
    assert data["allowed_senders"] == ["198.51.100.0/24", "203.0.113.5"] and data["ports"]["syslog"] == 5514
    after = lab.client.get("/admin/network").text
    assert "Restart needed" in after  # the running process still uses 443
    with lab.store.read() as db:
        assert db.execute("SELECT action FROM audit_log WHERE action = 'config.save'").fetchone()


@pytest.mark.parametrize(
    "sid, form, why",
    [
        ("network-ports", {"f.https": "99999"}, "at most"),
        ("network-ports", {"f.https": "abc"}, "whole number"),
        ("network", {"f.mode": "overlay"}, "Choose one of"),
        ("network", {"f.advertised_address": "http://x"}, "address or name"),
        ("network", {"f.allowed_senders": "not an ip!"}, "IP address or a range"),
        ("network-ports", {"f.http": "443"}, "clash"),
    ],
)
def test_bad_network_settings_cannot_be_saved(lab, monkeypatch, tmp_path, sid, form, why):  # noqa: F811
    ship_network(tmp_path)
    in_mode(monkeypatch, "own_ip")
    base = {"f.mode": "auto", "f.unknown_senders": "flag"} if sid == "network" else {}
    r = save_card(lab, sid, **{**base, **form})
    assert r.status_code == 422 and why.lower() in r.text.lower(), r.text[-600:]
    assert not (tmp_path / "cfg" / "brief" / "network.yaml").exists()


def test_the_network_page_needs_a_login_and_a_csrf_token(lab):  # noqa: F811
    assert lab.https().get("/admin/network").status_code == 303
    assert lab.https().get("/admin/network/test/abc123").status_code == 303
    assert lab.client.post("/admin/settings/network/save", data={"f.mode": "host"}).status_code == 403
    assert lab.https().post("/admin/settings/network/save", data={"f.mode": "host"}).status_code == 401


def test_the_network_page_has_no_inline_script_or_style(lab, monkeypatch):  # noqa: F811
    in_mode(monkeypatch, "own_ip")
    html = lab.client.get("/admin/network").text
    assert " style=" not in html and "<script>" not in html


def ship_network(tmp_path):
    """The shipped network.yaml as the image's default (the lab fixture only ships the registry)."""
    (tmp_path / "defaults" / "brief" / "network.yaml").write_text(SHIPPED, encoding="utf-8")


def test_host_mode_on_the_usual_ports_shows_each_clash_once(lab, monkeypatch):  # noqa: F811
    in_mode(monkeypatch, "host")
    html = lab.client.get("/admin/network").text
    assert (
        html.count("can clash with Unraid") == 1
        and html.count("port 80 clashes") == 1
        and html.count("port 443 clashes") == 1
    )
    assert "5514" in html and "8480" in html and "8443" in html
