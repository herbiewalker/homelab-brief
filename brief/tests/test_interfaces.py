"""The standard outputs (Task 13): the sensor registry, MQTT state sensors, `/metrics`, and `/api/v1`.

They are all read from one status document, so one test drives every interface from the same run and
checks that they agree.
"""

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import golden_day as g
import pytest
from fastapi.routing import APIRoute
from test_publish import FakeClient, FakeReason
from test_web import Env

from brief import health, metrics, registry
from brief import publish as p
from brief.store import iso
from brief.systems import System, Systems

BRIEF = Path(__file__).resolve().parents[1]
SYSTEMS_YAML = """schema: 1
systems:
  - id: gw
    label: Gateway
    hosts: [198.51.100.1]
  - id: nas
    label: Storage server
    hosts: [198.51.100.2]
  - id: cam
    label: "Camera \\"north\\""
    hosts: [198.51.100.3]
"""


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """The web apps over a store whose daily run (golden day, re-dated to now) is already on disk."""
    monkeypatch.setattr(health, "receiver_ok", lambda port: True)
    cfg = tmp_path / "cfg" / "brief"
    cfg.mkdir(parents=True)
    (cfg / "systems.yaml").write_text(SYSTEMS_YAML, encoding="utf-8")
    (cfg / "registry.yaml").write_bytes((BRIEF / "registry.yaml").read_bytes())
    monkeypatch.setenv("BRIEF_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("BRIEF_DEFAULTS_DIR", str(tmp_path / "none"))
    daily = tmp_path / "daily"
    store, _, out = g.build_daily(daily)
    store.close()
    latest = tmp_path / "out" / "latest"
    latest.mkdir(parents=True)
    doc = json.loads((out / "latest" / "status.json").read_text())
    doc["generated_at"] = doc["daily_generated_at"] = iso(datetime.now(UTC))
    (latest / "status.json").write_text(json.dumps(doc))
    shutil.rmtree(daily)
    (tmp_path / "state").mkdir()
    return Env(tmp_path / "state", monkeypatch, out_dir=tmp_path / "out")


# ---------------------------------------------------------------- the registry


def test_every_system_gets_a_state_sensor_and_the_file_wins_a_clash():
    two = Systems([System(i, i.upper(), "other", (), "UTC", True) for i in ("gw", "nas")])
    ids = [m["id"] for m in registry.load(systems=two) if m["id"].startswith("system_")]
    assert ids == ["system_gw", "system_nas"]


def test_values_carry_numbers_and_states_and_leave_out_what_is_unknown():
    metric_list = [
        {"id": "events_24h", "kind": "number"},
        {"id": "ingest_lag_s", "kind": "number"},
        {"id": "system_gw", "kind": "state", "system": "gw"},
        {"id": "system_nas", "kind": "state", "system": "nas"},
        {"id": "system_cam", "kind": "state", "system": "cam"},
    ]
    doc = {
        "run_ok": True,
        "pipeline": {"events_24h": 5, "ingest_lag_s": None},
        "systems": [{"id": "gw", "state": "red"}, {"id": "nas", "state": "bogus"}],
    }
    assert registry.values(doc, metric_list) == {"events_24h": 5, "system_gw": "red"}
    assert registry.values({**doc, "run_ok": False}, metric_list) == {}


# ---------------------------------------------------------------- MQTT


def test_a_state_sensor_is_announced_as_an_enum_with_its_options():
    payload = p.discovery_payload({"id": "system_gw", "name": "Gateway status", "kind": "state"})
    assert payload["device_class"] == "enum" and payload["options"] == ["green", "yellow", "red", "unknown"]
    assert "state_class" not in payload and payload["expire_after"] == 7200
    assert p.discovery_payload({"id": "x", "kind": "number"})["state_class"] == "measurement"


def test_a_new_system_is_announced_and_a_removed_one_is_withdrawn():
    client = FakeClient()
    gw = {"id": "system_gw", "name": "Gateway status", "kind": "state", "system": "gw"}
    nas = {"id": "system_nas", "name": "NAS status", "kind": "state", "system": "nas"}
    pub = p.Publisher("broker", metrics=[gw], client=client)
    pub._on_connect(client, None, {}, FakeReason())
    pub.publish({"system_gw": "red"})
    assert client.last("homelab/brief/system_gw/state") == "red"
    client.sent.clear()
    pub.set_metrics([nas])  # systems.yaml changed: gw is gone, nas is new
    pub.publish({"system_nas": "green", "system_gw": "red"})  # a value for a withdrawn sensor is dropped
    sent = {t: pl for t, pl, _ in client.sent}
    assert sent["homeassistant/sensor/homelab_brief/system_gw/config"] == ""
    assert sent["homelab/brief/system_gw/state"] == ""
    assert json.loads(sent["homeassistant/sensor/homelab_brief/system_nas/config"])["name"] == "NAS status"
    assert sent["homelab/brief/system_nas/state"] == "green"
    client.sent.clear()
    pub.set_metrics([nas])  # unchanged: nothing is re-sent
    assert client.sent == []


# ---------------------------------------------------------------- /metrics


SAMPLE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? -?\d+(\.\d+)?(e[+-]?\d+)?$")


def test_metrics_text_is_valid_exposition_format(lab):
    r = lab.http().get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"] == metrics.CONTENT_TYPE
    assert r.headers["cache-control"] == "no-cache"
    lines = r.text.splitlines()
    samples = [x for x in lines if not x.startswith("#")]
    assert samples and all(SAMPLE.match(x) for x in samples), [x for x in samples if not SAMPLE.match(x)]
    for i, line in enumerate(lines):  # every family is introduced by HELP then TYPE
        if not line.startswith("#"):
            name = re.match(r"[^{ ]+", line).group(0)
            assert f"# TYPE {name} gauge" in lines[:i]
    assert 'homelab_brief_system_status{system="cam"} 2' in lines  # the camera is red in the golden day
    assert "homelab_brief_run_ok 1" in lines and r.text.endswith("\n")


def test_metrics_escape_label_values_and_skip_unknown_states():
    doc = {"run_ok": True, "generated_at": "2026-10-03T12:00:00Z", "pipeline": {}, "systems": [
        {"id": 'a"b', "state": "yellow"}, {"id": "u", "state": "unknown"}]}  # fmt: skip
    metric_list = [
        {"id": "system_x", "kind": "state", "system": 'a"b'},
        {"id": "system_u", "kind": "state", "system": "u"},
    ]
    text = metrics.render(doc, metric_list, "0.0.0")
    assert 'homelab_brief_system_status{system="a\\"b"} 1' in text and 'system="u"' not in text


def test_a_failed_run_exports_run_ok_zero_and_no_values(lab, monkeypatch):
    monkeypatch.setattr(health, "receiver_ok", lambda port: False)
    text = lab.http().get("/metrics").text
    assert "homelab_brief_run_ok 0" in text
    assert "homelab_brief_events_24h" not in text and "system_status" not in text


# ---------------------------------------------------------------- /api/v1


def test_the_api_index_and_every_endpoint_answer_without_login(lab):
    client = lab.http()
    index = client.get("/api/v1").json()
    assert index["version"] and len(index["endpoints"]) == 5
    for endpoint in index["endpoints"]:
        r = client.get(endpoint)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", endpoint
        assert r.json()["schema"] == 1 or endpoint.endswith("status"), endpoint


def test_the_api_is_read_only(lab):
    app = lab.app("http")
    api_routes = [r for r in app.state.api_router.routes if isinstance(r, APIRoute)]
    assert api_routes and all(r.methods == {"GET"} for r in api_routes)
    assert lab.http().post("/api/v1/status").status_code == 405
    assert lab.http().delete("/api/v1/alerts").status_code == 405


def test_systems_findings_and_alerts(lab):
    client = lab.http()
    systems_doc = client.get("/api/v1/systems").json()
    states = {s["id"]: s["state"] for s in systems_doc["systems"]}
    assert states == {"gw": "yellow", "nas": "yellow", "cam": "red"}
    assert "hosts" not in systems_doc["systems"][0] and systems_doc["systems"][0]["host_count"] == 1
    findings = client.get("/api/v1/findings").json()
    assert len(findings["stories"]) == 5 and findings["stories"][0]["rank"] == 1
    with lab.store.write() as tx:
        tx.upsert_alert("absence-1", kind="absence", title="A host is silent", level="warning",
                        route="push_now", state="open", opened="2026-10-03T12:00:00Z")  # fmt: skip
        tx.upsert_alert("old", kind="absence", title="gone", state="resolved")
    rows = client.get("/api/v1/alerts").json()["alerts"]
    assert [a["id"] for a in rows] == ["absence-1"] and rows[0]["state"] == "open"
    assert "homelab_brief_alerts_open 1" in client.get("/metrics").text


def test_a_failed_run_has_no_metric_values_in_the_api(lab, monkeypatch):
    monkeypatch.setattr(health, "receiver_ok", lambda port: False)
    doc = lab.http().get("/api/v1/metrics").json()
    assert doc["run_ok"] is False and all(m["value"] is None for m in doc["metrics"])
    assert lab.http().get("/api/v1/findings").json()["run_ok"] is False


def test_status_json_the_api_and_metrics_show_the_same_run(lab):
    client = lab.http()
    a, b = client.get("/status.json").json(), client.get("/api/v1/status").json()
    for doc in (a, b):
        doc.pop("generated_at")  # computed per request
    assert a == b
    values = {m["id"]: m["value"] for m in client.get("/api/v1/metrics").json()["metrics"]}
    text = client.get("/metrics").text
    for name in ("events_24h", "new_signatures_24h", "hosts_silent"):
        assert f"homelab_brief_{name} {values[name]}" in text
    assert values["system_cam"] == "red"
