"""Task 22: the iDRAC collector over IPMI (ipmitool is faked) and Redfish (the fetch is faked)."""

from datetime import UTC, datetime

import pytest
from test_collectors import lab

from brief.collectors import framework, idrac, proc
from brief.collectors.httpjson import ApiError
from brief.config import ConfigError

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
ENV = {"IDRAC_HOST": "198.51.100.60", "IDRAC_USER": "monitor", "IDRAC_PASSWORD": "idrac-secret-pw"}
YAML = "  idrac:\n    enabled: true\n    protocol: {protocol}\n    host_var: IDRAC_HOST\n    user_var: IDRAC_USER\n    password_var: IDRAC_PASSWORD\n"
SDR = """Inlet Temp       | 04h | ok  |  7.1 | 22 degrees C
Exhaust Temp     | 01h | ok  |  7.1 | 31 degrees C
Temp             | 0Eh | ok  |  3.1 | 38 degrees C
Temp             | 0Fh | ok  |  3.2 | 41 degrees C
Fan1 RPM         | 30h | ok  |  7.1 | 3720 RPM
Fan2 RPM         | 31h | ok  |  7.1 | 3600 RPM
Fan3 RPM         | 32h | nc  |  7.1 | 1200 RPM
Pwr Consumption  | 77h | ok  |  7.1 | 98 Watts
Current 1        | 6Bh | ok  | 10.1 | 0.4 Amps
PS1 Status       | 6Ch | ok  | 10.1 | Presence detected
Riser Presence   | A0h | ns  |  7.1 | Not Readable
"""
SEL_INFO = "SEL Information\nVersion          : 1.5 (v1.5, v2 compliant)\nEntries          : 12\nFree Space       : 9000 bytes\n"


class Ipmitool:
    def __init__(
        self, sdr=SDR, error=None, dcmi="Instantaneous power reading:                   105 Watts\n"
    ):
        self.sdr, self.error, self.dcmi, self.calls = sdr, error, dcmi, []

    def __call__(self, argv, env=None, timeout=20.0):
        self.calls.append((argv, dict(env or {})))
        if self.error:
            raise self.error
        if argv[-2:] == ["sdr", "elist"]:
            return self.sdr
        if argv[-2:] == ["sel", "info"]:
            return SEL_INFO
        if argv[-3:] == ["dcmi", "power", "reading"]:
            return self.dcmi
        raise AssertionError(argv)


def setup(tmp_path, protocol, extra=""):
    env, user, store = lab(tmp_path, YAML.format(protocol=protocol) + extra)
    return env | ENV, store


# ---------------------------------------------------------------- parsing


def test_the_sensor_list_is_summarised():
    got = idrac.summarize(idrac.parse_sdr(SDR))
    assert got == {
        "temp_max_c": 41,
        "inlet_temp_c": 22,
        "fan_min_rpm": 1200,
        "power_w": 98,
        "sensors_not_ok": 1,
    }


def test_power_falls_back_to_dcmi_when_no_sensor_reports_watts():
    sensors = idrac.parse_sdr("Inlet Temp | 04h | ok | 7.1 | 22 degrees C\n")
    assert "power_w" not in idrac.summarize(sensors)
    assert (
        idrac.summarize(sensors, idrac.parse_dcmi_power("Instantaneous power reading:  105 Watts"))["power_w"]
        == 105
    )


def test_unreadable_sensors_and_odd_lines_are_ignored():
    sensors = idrac.parse_sdr("garbage\n\nX | 01h | ns | 7.1 | no reading\n")
    assert [(s.name, s.value) for s in sensors] == [("X", None)]
    assert idrac.parse_sel_entries(SEL_INFO) == 12 and idrac.parse_sel_entries("nothing") is None


POWER = {
    "PowerControl": [{"PowerConsumedWatts": 112}],
    "PowerSupplies": [{"Status": {"Health": "OK"}}, {"Status": {"Health": "Critical"}}],
}
THERMAL = {
    "Temperatures": [
        {"Name": "System Board Inlet Temp", "ReadingCelsius": 21, "Status": {"Health": "OK"}},
        {"Name": "CPU1 Temp", "ReadingCelsius": 44, "Status": {"Health": "OK"}},
    ],
    "Fans": [
        {"Name": "Fan 1", "Reading": 4000, "ReadingUnits": "RPM", "Status": {"Health": "OK"}},
        {"Name": "Fan 2", "Reading": 3800, "ReadingUnits": "RPM", "Status": {"Health": "Warning"}},
    ],
}


def test_the_redfish_resources_are_summarised():
    assert idrac.summarize_redfish(POWER, THERMAL, 7) == {
        "power_w": 112, "temp_max_c": 44, "inlet_temp_c": 21, "fan_min_rpm": 3800, "sensors_not_ok": 2, "sel_entries": 7,
    }  # fmt: skip
    assert idrac.summarize_redfish({}, {}, None) == {"sensors_not_ok": 0}


# ---------------------------------------------------------------- IPMI


def test_ipmi_produces_the_numbers_and_keeps_the_password_off_the_command_line(tmp_path, monkeypatch):
    tool = Ipmitool()
    monkeypatch.setattr(idrac, "RUN", tool)
    env, store = setup(tmp_path, "ipmi")
    assert framework.run_due(store, env, NOW)["idrac"]["ok"] is True
    got = framework.read_state(store)["idrac"]["metrics"]
    assert got == {
        "idrac_temp_max_c": 41, "idrac_inlet_temp_c": 22, "idrac_fan_min_rpm": 1200, "idrac_power_w": 98,
        "idrac_sensors_not_ok": 1, "idrac_sel_entries": 12,
    }  # fmt: skip
    for argv, call_env in tool.calls:
        assert (
            "idrac-secret-pw" not in " ".join(argv)
            and "-E" in argv
            and "-I" in argv
            and argv[0] == "ipmitool"
        )
        assert call_env["IPMI_PASSWORD"] == "idrac-secret-pw"
    assert [argv[-2:] for argv, _ in tool.calls] == [["sdr", "elist"], ["sel", "info"]]


def test_ipmi_options_and_dcmi_fallback(tmp_path, monkeypatch):
    tool = Ipmitool(sdr="Inlet Temp | 04h | ok | 7.1 | 22 degrees C\n")
    monkeypatch.setattr(idrac, "RUN", tool)
    env, store = setup(tmp_path, "ipmi", "    cipher_suite: 3\n    privilege: OPERATOR\n    port: 624\n")
    framework.run_due(store, env, NOW)
    argv = tool.calls[0][0]
    assert (
        argv[argv.index("-C") + 1] == "3"
        and argv[argv.index("-L") + 1] == "OPERATOR"
        and argv[argv.index("-p") + 1] == "624"
    )
    assert framework.read_state(store)["idrac"]["metrics"]["idrac_power_w"] == 105


@pytest.mark.parametrize(
    "error, shown",
    [
        (
            proc.ProcError("the credentials were rejected", "RAKP 2 HMAC is invalid"),
            "the credentials were rejected",
        ),
        (proc.ProcError("no answer (timed out)"), "no answer (timed out)"),
        (
            proc.ProcError("ipmitool is not installed in this image"),
            "ipmitool is not installed in this image",
        ),
    ],
)
def test_ipmi_failures_are_named_safely(tmp_path, monkeypatch, error, shown):
    monkeypatch.setattr(idrac, "RUN", Ipmitool(error=error))
    env, store = setup(tmp_path, "ipmi")
    outcome = framework.run_due(store, env, NOW)
    assert outcome["idrac"] == {"ran": True, "ok": False, "error": shown}


def test_an_empty_sensor_list_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(idrac, "RUN", Ipmitool(sdr=""))
    env, store = setup(tmp_path, "ipmi")
    assert framework.run_due(store, env, NOW)["idrac"]["error"] == "the iDRAC returned no sensors"


def test_missing_variables_are_named(tmp_path, monkeypatch):
    monkeypatch.setattr(idrac, "RUN", Ipmitool())
    env, store = setup(tmp_path, "ipmi")
    env.pop("IDRAC_PASSWORD")
    assert (
        framework.run_due(store, env, NOW)["idrac"]["error"] == "these variables are not set: IDRAC_PASSWORD"
    )


# ---------------------------------------------------------------- Redfish


class Fetch:
    def __init__(self, pages, error=None):
        self.pages, self.error, self.calls = pages, error, []

    def __call__(self, url, headers, timeout, context, body=None, error_json=False):
        self.calls.append((url, headers))
        if self.error:
            raise self.error
        for suffix, page in self.pages.items():
            if url.endswith(suffix):
                if isinstance(page, ApiError):
                    raise page
                return page
        raise AssertionError(url)


PAGES = {"/Power": POWER, "/Thermal": THERMAL, "/Sel/Entries": {"Members@odata.count": 9}}


def test_redfish_produces_the_numbers_with_basic_authentication(tmp_path, monkeypatch):
    fetch = Fetch(PAGES)
    monkeypatch.setattr(idrac, "FETCH", fetch)
    env, store = setup(tmp_path, "redfish")
    assert framework.run_due(store, env, NOW)["idrac"]["ok"] is True
    assert framework.read_state(store)["idrac"]["metrics"]["idrac_sel_entries"] == 9
    urls = [u for u, _ in fetch.calls]
    assert urls[0] == "https://198.51.100.60/redfish/v1/Chassis/System.Embedded.1/Power"
    assert urls[2].endswith("/redfish/v1/Managers/iDRAC.Embedded.1/LogServices/Sel/Entries")
    assert all(
        h["authorization"].startswith("Basic ") and "idrac-secret-pw" not in h["authorization"]
        for _, h in fetch.calls
    )


def test_redfish_rejected_credentials_and_an_unreachable_idrac(tmp_path, monkeypatch):
    env, store = setup(tmp_path, "redfish")
    monkeypatch.setattr(idrac, "FETCH", Fetch({}, ApiError("auth", "the API key was rejected")))
    assert framework.run_due(store, env, NOW)["idrac"]["error"] == "the iDRAC credentials were rejected"
    monkeypatch.setattr(
        idrac, "FETCH", Fetch({}, ApiError("unreachable", "could not connect (TimeoutError)"))
    )
    assert (
        framework.run_due(store, env, NOW, force=True)["idrac"]["error"] == "could not connect (TimeoutError)"
    )


def test_a_missing_event_log_does_not_cost_the_other_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        idrac,
        "FETCH",
        Fetch({**PAGES, "/Sel/Entries": ApiError("unreachable", "the server answered HTTP 404")}),
    )
    env, store = setup(tmp_path, "redfish")
    framework.run_due(store, env, NOW)
    got = framework.read_state(store)["idrac"]["metrics"]
    assert "idrac_sel_entries" not in got and got["idrac_power_w"] == 112


def test_a_full_url_in_the_host_variable_is_used_as_given(tmp_path, monkeypatch):
    fetch = Fetch(PAGES)
    monkeypatch.setattr(idrac, "FETCH", fetch)
    env, store = setup(tmp_path, "redfish")
    env["IDRAC_HOST"] = "https://198.51.100.60:8443"
    framework.run_due(store, env, NOW)
    assert fetch.calls[0][0].startswith("https://198.51.100.60:8443/redfish/v1/")


# ---------------------------------------------------------------- the contract and settings


def test_the_connection_test_reports_the_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(idrac, "RUN", Ipmitool())
    env, store = setup(tmp_path, "ipmi")
    ok, message = framework.test_connection("idrac", store, env, NOW)
    assert ok and message.startswith("the iDRAC answered over ipmi:") and "power_w=98" in message


@pytest.mark.parametrize(
    "extra, message",
    [
        ("    protocol: carrier-pigeon\n", "protocol must be"),
        ("    cipher_suite: three\n", "cipher_suite"),
        ("    privilege: ROOT\n", "privilege"),
        ("    chassis_id: ../etc\n", "chassis_id"),
        ("    verify_tls: sometimes\n", "verify_tls"),
    ],
)
def test_bad_settings_are_refused(tmp_path, extra, message):
    _, user, _ = lab(
        tmp_path,
        "  idrac:\n    enabled: true\n    host_var: IDRAC_HOST\n    user_var: IDRAC_USER\n    password_var: IDRAC_PASSWORD\n"
        + extra,
    )
    with pytest.raises(ConfigError, match=message):
        framework.validate(user / "collectors.yaml")


def test_the_shipped_block_is_valid_while_disabled():
    from pathlib import Path

    assert (
        framework.validate(Path(__file__).resolve().parents[1] / "collectors.yaml")["idrac"].enabled is False
    )


def test_the_sensors_exist_once_it_is_enabled(tmp_path):
    env, _ = setup(tmp_path, "ipmi")
    assert {"idrac_power_w", "idrac_fan_min_rpm", "idrac_sel_entries", "collector_idrac_ok"} <= {
        m["id"] for m in framework.metric_entries(env)
    }
