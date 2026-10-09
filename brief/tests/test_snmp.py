"""Task 22: the SNMP collector, from fixture device answers (the net-snmp tools are faked)."""

import os
import stat
from datetime import UTC, datetime, timedelta

import pytest
from test_collectors import lab

from brief.collectors import CollectorError, framework, proc, snmp
from brief.config import ConfigError

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
ENV = {"SNMP_V3_USER": "monitor", "SNMP_V3_AUTH_KEY": "auth-secret-1", "SNMP_V3_PRIV_KEY": "priv-secret-2"}
HEAD = "  snmp:\n    enabled: true\n    user_var: SNMP_V3_USER\n    auth_key_var: SNMP_V3_AUTH_KEY\n    priv_key_var: SNMP_V3_PRIV_KEY\n"
TARGETS = """    targets:
      - { id: switch-1, host: 198.51.100.51, profile: switch }
      - { id: ups-1, host: 198.51.100.52, profile: ups }
      - { id: pdu-1, host: 198.51.100.53, profile: custom, oids: [{ name: outlet_w, oid: "1.3.6.1.4.1.99.1.1.0", scale: 0.1, unit: W }] }
"""


class Devices:
    """A fake network: host -> {oid: value}; a host that is a ProcError is down. Records every call."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, argv, env=None, timeout=20.0):
        self.calls.append((argv, dict(env or {})))
        host = next(a for a in argv if a.startswith("198.51.100."))
        device = self.table[host]
        if isinstance(device, proc.ProcError):
            raise device
        oids = [a for a in argv[argv.index(host) + 1 :]]
        if argv[0] == "snmpget":
            return "\n".join(
                f".{o} {device[o]}" if o in device else f".{o} No Such Object available on this agent"
                for o in oids
            )
        prefix = oids[0] + "."
        return "\n".join(f".{o} {v}" for o, v in device.items() if o.startswith(prefix))


SWITCH = {
    snmp.SYS_UPTIME: "8640000",  # 24 hours in hundredths of a second
    **{f"{snmp.IF_OPER_STATUS}.{i}": s for i, s in enumerate(("up(1)", "up(1)", "down(2)", "up(1)"), 1)},
    **{f"{snmp.IF_IN_ERRORS}.{i}": e for i, e in enumerate((10, 0, 5, 0), 1)},
    **{f"{snmp.IF_OUT_ERRORS}.{i}": e for i, e in enumerate((0, 0, 1, 0), 1)},
}
UPS = {
    snmp.UPS["battery_status"]: "normal(2)", snmp.UPS["minutes_remaining"]: "45", snmp.UPS["charge_pct"]: "100",
    snmp.UPS["output_source"]: "normal(3)", snmp.UPS["input_v"]: "231", snmp.UPS["load_pct"]: "18", snmp.UPS["power_w"]: "120",
}  # fmt: skip
PDU = {"1.3.6.1.4.1.99.1.1.0": "1234"}


@pytest.fixture
def net(monkeypatch):
    devices = Devices({"198.51.100.51": SWITCH, "198.51.100.52": UPS, "198.51.100.53": PDU})
    monkeypatch.setattr(snmp, "RUN", devices)
    return devices


def setup(tmp_path, head=HEAD, targets=TARGETS, env=ENV):
    e, user, store = lab(tmp_path, head + targets)
    return e | env, store


# ---------------------------------------------------------------- parsing


def test_tool_output_is_parsed_and_missing_values_are_left_out():
    text = '.1.3.6.1.2.1.1.3.0 123456\n.1.3.6.1.2.1.1.5.0 "sw-name"\n.1.3.6.1.2.1.1.9.0 No Such Object available on this agent\n'
    assert snmp.parse_output(text) == {"1.3.6.1.2.1.1.3.0": "123456", "1.3.6.1.2.1.1.5.0": "sw-name"}
    assert snmp.parse_output(".1.3.6.1.2.1.2.2.1.8.9 No more variables left in this MIB View") == {}
    assert snmp.parse_output("garbage\n\n") == {}


def test_numbers_and_enumerations():
    assert snmp.number("231") == 231 and snmp.number("12.5 V") == 12.5 and snmp.number("abc") is None
    assert snmp.enum_value("up(1)") == 1 and snmp.enum_value("2") == 2 and snmp.enum_value(None) is None


def test_ups_values():
    got = snmp.ups_numbers(UPS)
    assert got == {
        "charge_pct": 100, "minutes_remaining": 45, "load_pct": 18, "input_v": 231, "power_w": 120,
        "on_battery": 0, "battery_ok": 1,
    }  # fmt: skip
    on_battery = snmp.ups_numbers(
        {snmp.UPS["output_source"]: "battery(5)", snmp.UPS["battery_status"]: "low(3)"}
    )
    assert on_battery == {"on_battery": 1, "battery_ok": 0}


def test_switch_values():
    status = {k: v for k, v in SWITCH.items() if k.startswith(snmp.IF_OPER_STATUS)}
    assert snmp.switch_numbers("8640000", status, 3) == {
        "uptime_h": 24.0,
        "ports_total": 4,
        "ports_up": 3,
        "if_errors": 3,
    }


def test_custom_values_are_scaled():
    assert snmp.custom_numbers([{"name": "w", "oid": ".1.3.6.1.4.1.99.1.1.0", "scale": 0.1}], PDU) == {
        "w": 123.4
    }


# ---------------------------------------------------------------- credentials stay private


def test_the_private_config_for_each_security_level():
    base = {"version": "v3", "user_var": "U", "auth_key_var": "A", "priv_key_var": "P"}
    both = snmp.snmp_conf(base, {"U": "monitor", "A": "auth-key-1", "P": "priv-key-2"})
    assert (
        "defSecurityLevel authPriv" in both
        and "defAuthPassphrase auth-key-1" in both
        and "defPrivType AES" in both
    )
    auth_only = snmp.snmp_conf(base, {"U": "monitor", "A": "auth-key-1"})
    assert "authNoPriv" in auth_only and "Priv" not in auth_only.replace("authNoPriv", "")
    assert "noAuthNoPriv" in snmp.snmp_conf(base, {"U": "monitor"})
    v2 = snmp.snmp_conf({"version": "v2c", "community_var": "C"}, {"C": "lan-community"})
    assert v2 == "defVersion 2c\ndefCommunity lan-community\n"


def test_a_credential_with_a_line_break_is_refused():
    with pytest.raises(CollectorError, match="line break"):
        snmp.snmp_conf({"version": "v2c", "community_var": "C"}, {"C": "x\ndefSecurityName root"})


def test_the_session_folder_is_private_and_is_deleted(tmp_path):
    with snmp.private_session(
        {"version": "v3", "user_var": "U", "auth_key_var": "A"}, {"U": "u", "A": "a-secret-key"}
    ) as session:
        folder = session["SNMPCONFPATH"]
        assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(os.path.join(folder, "snmp.conf")).st_mode) == 0o600
        assert session["MIBS"] == "" and "a-secret-key" not in "".join(session.values())
    assert not os.path.exists(folder)


def test_no_secret_ever_reaches_a_command_line_or_the_environment(tmp_path, net):
    env, store = setup(tmp_path)
    framework.run_due(store, env, NOW)
    assert net.calls
    for argv, call_env in net.calls:
        text = " ".join(argv) + " ".join(call_env.values())
        for secret in ENV.values():
            assert secret not in text
        assert argv[0] in ("snmpget", "snmpbulkwalk") and "-On" in argv


# ---------------------------------------------------------------- a run


def test_every_profile_produces_its_numbers(tmp_path, net):
    env, store = setup(tmp_path)
    assert framework.run_due(store, env, NOW)["snmp"]["ok"] is True
    got = framework.read_state(store)["snmp"]["metrics"]
    assert (
        got["snmp_switch_1_reachable"] == 1
        and got["snmp_switch_1_ports_up"] == 3
        and got["snmp_switch_1_uptime_h"] == 24.0
    )
    assert "snmp_switch_1_if_errors" not in got  # the first poll has nothing to compare with
    assert (
        got["snmp_ups_1_charge_pct"] == 100
        and got["snmp_ups_1_battery_ok"] == 1
        and got["snmp_ups_1_power_w"] == 120
    )
    assert got["snmp_pdu_1_outlet_w"] == 123.4


def test_interface_errors_are_the_change_since_the_last_poll_and_a_reset_is_handled(tmp_path, net):
    env, store = setup(tmp_path)
    framework.run_due(store, env, NOW)
    net.table["198.51.100.51"] = {**SWITCH, f"{snmp.IF_IN_ERRORS}.1": "25"}  # +15
    framework.run_due(store, env, NOW + timedelta(hours=1))
    assert framework.read_state(store)["snmp"]["metrics"]["snmp_switch_1_if_errors"] == 15
    net.table["198.51.100.51"] = {**SWITCH, f"{snmp.IF_IN_ERRORS}.1": "2"}  # the switch restarted
    framework.run_due(store, env, NOW + timedelta(hours=2))
    assert framework.read_state(store)["snmp"]["metrics"]["snmp_switch_1_if_errors"] == 2 + 5 + 1 + 0


def test_a_silent_device_is_reported_and_the_rest_still_update(tmp_path, net):
    env, store = setup(tmp_path)
    net.table["198.51.100.52"] = proc.ProcError("no answer (timed out)")
    assert framework.run_due(store, env, NOW)["snmp"]["ok"] is True
    got = framework.read_state(store)["snmp"]["metrics"]
    assert got["snmp_ups_1_reachable"] == 0 and "snmp_ups_1_charge_pct" not in got
    assert got["snmp_switch_1_reachable"] == 1


def test_all_devices_silent_is_a_failed_run(tmp_path, net):
    env, store = setup(tmp_path)
    for host in list(net.table):
        net.table[host] = proc.ProcError("no answer (timed out)")
    assert framework.run_due(store, env, NOW)["snmp"] == {
        "ran": True,
        "ok": False,
        "error": "no SNMP target could be read",
    }


def test_a_device_that_answers_but_has_nothing_expected_is_not_counted(tmp_path, net):
    env, store = setup(tmp_path)
    net.table["198.51.100.52"] = {}
    got_ok = framework.run_due(store, env, NOW)["snmp"]["ok"]
    assert got_ok and framework.read_state(store)["snmp"]["metrics"]["snmp_ups_1_reachable"] == 0


def test_missing_credentials_name_the_variables(tmp_path, net):
    env, store = setup(tmp_path, env={})
    assert (
        framework.run_due(store, env, NOW)["snmp"]["error"]
        == "these variables are not set: SNMP_V3_USER, SNMP_V3_AUTH_KEY"
    )


def test_v2c_needs_only_the_community(tmp_path, net):
    head = "  snmp:\n    enabled: true\n    version: v2c\n    community_var: SNMP_COMMUNITY\n"
    env, store = setup(tmp_path, head, env={"SNMP_COMMUNITY": "lan-comm"})
    assert framework.run_due(store, env, NOW)["snmp"]["ok"] is True
    assert ["-v", "2c"] == net.calls[0][0][net.calls[0][0].index("-v") : net.calls[0][0].index("-v") + 2]


def test_the_connection_test_says_which_devices_answered(tmp_path, net):
    env, store = setup(tmp_path)
    net.table["198.51.100.53"] = proc.ProcError("the credentials were rejected")
    ok, message = framework.test_connection("snmp", store, env, NOW)
    assert ok and message.startswith("2 of 3 targets answered")
    assert "switch-1: answered" in message and "pdu-1: the credentials were rejected" in message


# ---------------------------------------------------------------- settings


@pytest.mark.parametrize(
    "text, message",
    [
        ("    version: v1\n", "version must be"),
        ("    auth_protocol: ROT13\n", "auth_protocol"),
        ("    priv_protocol: XOR\n", "priv_protocol"),
        ("    targets: [{id: Bad_Id, host: 198.51.100.1, profile: switch}]\n", "lowercase"),
        (
            "    targets: [{id: a, host: 198.51.100.1, profile: switch}, {id: a, host: 198.51.100.2, profile: ups}]\n",
            "twice",
        ),
        ("    targets: [{id: a, host: '<switch-ip>', profile: switch}]\n", "set host"),
        ("    targets: [{id: a, host: 198.51.100.1, profile: toaster}]\n", "profile must be"),
        ("    targets: [{id: a, host: 198.51.100.1, profile: custom}]\n", "needs oids"),
        (
            "    targets: [{id: a, host: 198.51.100.1, profile: custom, oids: [{name: X, oid: 1.3.6}]}]\n",
            "lowercase name",
        ),
        (
            "    targets: [{id: a, host: 198.51.100.1, profile: custom, oids: [{name: x, oid: nope}]}]\n",
            "numeric oid",
        ),
        (
            "    targets: [{id: a, host: 198.51.100.1, profile: custom, oids: [{name: x, oid: 1.3.6, scale: big}]}]\n",
            "scale",
        ),
    ],
)
def test_bad_settings_are_refused(tmp_path, text, message):
    _, user, _ = lab(tmp_path, HEAD + text)
    with pytest.raises(ConfigError, match=message):
        framework.validate(user / "collectors.yaml")


def test_v3_needs_its_variable_names(tmp_path):
    _, user, _ = lab(tmp_path, "  snmp:\n    enabled: true\n    user_var: SNMP_V3_USER\n")
    with pytest.raises(ConfigError, match="auth_key_var is required"):
        framework.validate(user / "collectors.yaml")


def test_the_shipped_block_is_valid_while_disabled_and_the_sensors_follow_the_targets(tmp_path):
    from pathlib import Path

    assert (
        framework.validate(Path(__file__).resolve().parents[1] / "collectors.yaml")["snmp"].enabled is False
    )
    env, _ = setup(tmp_path)
    ids = {m["id"] for m in framework.metric_entries(env)}
    assert {
        "snmp_switch_1_ports_up",
        "snmp_ups_1_charge_pct",
        "snmp_pdu_1_outlet_w",
        "snmp_pdu_1_reachable",
    } <= ids
    assert not any("ups_1_ports" in i for i in ids)
