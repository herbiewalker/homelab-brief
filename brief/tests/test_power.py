"""Task 23: power and energy from Home Assistant (statestream), per system, and the power_drift finding."""

import types
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from test_collectors import lab

from brief import detect, playbook, power, render_md, weekly
from brief.collectors import CollectorError, framework, statestream
from brief.config import ConfigError
from brief.store import Store
from brief.systems import Systems

BRIEF = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
ENV = {"MQTT_HOST": "198.51.100.70", "MQTT_USER": "brief", "MQTT_PASSWORD": "mqtt-secret"}
YAML = """  ha_statestream:
    enabled: true
    entities:
      - { entity: sensor.server_2_power, system: server-2, kind: power }
      - { entity: sensor.server_2_energy, system: server-2, kind: energy }
      - { entity: sensor.ups_load_kw, system: rack, kind: power, scale: 1000 }
"""
OFF = "  ha_statestream:\n    enabled: false\n"
TOPIC = "homelab/ha/sensor/{}/state"


class Broker:
    """Replaces statestream.SNAPSHOT: returns the retained states a broker would hold."""

    def __init__(self, states):
        self.states, self.calls = states, []

    def __call__(self, host, port, user, password, topics, window_s):
        self.calls.append((host, port, user, password, list(topics), window_s))
        if isinstance(self.states, Exception):
            raise self.states
        return {
            TOPIC.format(name): value for name, value in self.states.items() if TOPIC.format(name) in topics
        }


@pytest.fixture
def broker(monkeypatch):
    b = Broker({"server_2_power": "110.5", "server_2_energy": "1000.0", "ups_load_kw": "0.25"})
    monkeypatch.setattr(statestream, "SNAPSHOT", b)
    return b


def setup(tmp_path, yaml=YAML):
    env, user, store = lab(tmp_path, yaml)
    return env | ENV, store


def rows(store, sql):
    with store.read() as db:
        return [dict(r) for r in db.execute(sql)]


def put(store, hours_ago, entity, system, watts=None, kwh=None, now=NOW):
    with store.write() as tx:
        tx.db.execute(
            "INSERT OR REPLACE INTO power_reading (ts, entity, system_id, watts, kwh) VALUES (?, ?, ?, ?, ?)",
            ((now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:00:00Z"), entity, system, watts, kwh),
        )


# ---------------------------------------------------------------- reading the states


def test_values_are_numbers_or_nothing():
    assert statestream.parse_value("110.5") == 110.5 and statestream.parse_value(" 3 ") == 3
    assert statestream.parse_value("0.25", 1000) == 250
    for bad in ("unavailable", "unknown", "", "nan", "inf", "12 W", None):
        assert statestream.parse_value(bad) is None


def test_topics_follow_the_statestream_layout():
    (first,) = statestream.entities_of(
        {"entities": [{"entity": "sensor.server_2_power", "system": "server-2"}]}
    )
    assert first.topic("homelab/ha") == "homelab/ha/sensor/server_2_power/state"
    assert first.topic("/homelab/ha/") == "homelab/ha/sensor/server_2_power/state"
    assert first.slug == "sensor_server_2_power"


# ---------------------------------------------------------------- a run


def test_a_run_stores_readings_and_returns_per_entity_and_per_system_numbers(tmp_path, broker):
    env, store = setup(tmp_path)
    put(store, 5, "sensor.server_2_energy", "server-2", kwh=998.5)  # an earlier meter reading
    assert framework.run_due(store, env, NOW)["ha_statestream"]["ok"] is True
    got = framework.read_state(store)["ha_statestream"]["metrics"]
    assert got["ha_statestream_sensor_server_2_power"] == 110.5
    assert got["ha_statestream_sensor_ups_load_kw"] == 250  # scaled from kW
    assert got["ha_statestream_server_2_power_w"] == 110.5 and got["ha_statestream_rack_power_w"] == 250
    assert got["ha_statestream_server_2_energy_24h_kwh"] == 1.5  # from the meter: 1000.0 - 998.5
    assert got["ha_statestream_entities_seen"] == 3
    stored = {
        r["entity"]: r for r in rows(store, "SELECT * FROM power_reading WHERE ts = '2026-10-09T12:00:00Z'")
    }
    assert (
        stored["sensor.server_2_power"]["watts"] == 110.5 and stored["sensor.server_2_power"]["kwh"] is None
    )
    assert (
        stored["sensor.server_2_energy"]["kwh"] == 1000.0
        and stored["sensor.server_2_energy"]["watts"] is None
    )


def test_the_broker_settings_come_from_the_environment_and_only_listed_topics_are_asked_for(tmp_path, broker):
    env, store = setup(tmp_path)
    framework.run_due(store, env, NOW)
    host, port, user, password, topics, window = broker.calls[0]
    assert (host, port, user, password, window) == ("198.51.100.70", 1883, "brief", "mqtt-secret", 5.0)
    assert sorted(topics) == sorted(
        TOPIC.format(n) for n in ("server_2_power", "server_2_energy", "ups_load_kw")
    )


def test_an_unavailable_entity_is_skipped_and_nothing_at_all_is_a_failed_run(tmp_path, broker):
    env, store = setup(tmp_path)
    broker.states["server_2_power"] = "unavailable"
    assert framework.run_due(store, env, NOW)["ha_statestream"]["ok"] is True
    assert (
        "ha_statestream_sensor_server_2_power" not in framework.read_state(store)["ha_statestream"]["metrics"]
    )
    broker.states = {}
    outcome = framework.run_due(store, env, NOW + timedelta(hours=1))
    assert outcome["ha_statestream"]["error"].startswith("no Home Assistant state was received")


def test_a_broker_problem_is_named_without_the_address(tmp_path, broker):
    env, store = setup(tmp_path)
    broker.states = CollectorError("could not connect to the MQTT broker")
    assert (
        framework.run_due(store, env, NOW)["ha_statestream"]["error"]
        == "could not connect to the MQTT broker"
    )
    env2 = {k: v for k, v in env.items() if k != "MQTT_HOST"}
    error = framework.run_due(store, env2, NOW, force=True)["ha_statestream"]["error"]
    assert error == "these variables are not set: MQTT_HOST"


def test_the_connection_test_lists_each_entity(tmp_path, broker):
    env, store = setup(tmp_path)
    broker.states["ups_load_kw"] = "unknown"
    ok, message = framework.test_connection("ha_statestream", store, env, NOW)
    assert ok and message.startswith("2 of 3 entities answered")
    assert "sensor.server_2_power: 110.5 W" in message and "sensor.ups_load_kw: no value" in message
    broker.states = {}
    ok, message = framework.test_connection("ha_statestream", store, env, NOW)
    assert not ok and "is mqtt_statestream on" in message


# ---------------------------------------------------------------- settings


@pytest.mark.parametrize(
    "text, message",
    [
        ("    base_topic: homelab/#\n", "base_topic"),
        ("    window_s: 0\n", "window_s"),
        ("    entities: [{entity: Server Power}]\n", "entity must look like"),
        ("    entities: [{entity: sensor.a}, {entity: sensor.a}]\n", "twice"),
        ("    entities: [{entity: sensor.a, kind: heat}]\n", "kind must be"),
        ("    entities: [{entity: sensor.a, system: Big_System}]\n", "system must be"),
        ("    entities: [{entity: sensor.a, scale: 0}]\n", "scale"),
    ],
)
def test_bad_settings_are_refused(tmp_path, text, message):
    _, user, _ = lab(tmp_path, "  ha_statestream:\n    enabled: true\n" + text)
    with pytest.raises(ConfigError, match=message):
        framework.validate(user / "collectors.yaml")


def test_the_shipped_block_is_valid_while_disabled_and_the_sensors_follow_the_entities(tmp_path):
    assert framework.validate(BRIEF / "collectors.yaml")["ha_statestream"].enabled is False
    env, _ = setup(tmp_path)
    ids = {m["id"] for m in framework.metric_entries(env)}
    wanted = {
        "ha_statestream_sensor_server_2_power",
        "ha_statestream_server_2_power_w",
        "ha_statestream_server_2_energy_24h_kwh",
        "ha_statestream_rack_power_w",
        "ha_statestream_entities_seen",
    }
    assert wanted <= ids
    assert "ha_statestream_rack_energy_24h_kwh" in ids  # a system gets an energy figure even from power alone


# ---------------------------------------------------------------- the real MQTT client, with a fake paho


class FakeReason:
    def __init__(self, failure=False):
        self.is_failure = failure


class FakeMqtt:
    """Stands in for paho's Client: connect, then deliver the retained messages to on_message."""

    def __init__(self, messages, refuse=False, silent=False, unreachable=False):
        self.messages, self.refuse, self.silent, self.unreachable = messages, refuse, silent, unreachable
        self.subscribed, self.credentials, self.stopped = [], None, False

    def __call__(self, *args, **kwargs):  # mqtt.Client(...)
        return self

    def username_pw_set(self, user, password):
        self.credentials = (user, password)

    def connect(self, host, port, keepalive=60):
        if self.unreachable:
            raise OSError("no route")

    def loop_start(self):
        if self.silent:
            return
        self.on_connect(self, None, {}, FakeReason(self.refuse))
        if not self.refuse:
            for topic, payload in self.messages.items():
                self.on_message(self, None, types.SimpleNamespace(topic=topic, payload=payload.encode()))

    def subscribe(self, topics):
        self.subscribed = topics

    def loop_stop(self):
        self.stopped = True

    def disconnect(self):
        pass


@pytest.fixture
def paho(monkeypatch):
    import paho.mqtt.client as real

    def install(**kwargs):
        fake = FakeMqtt(**kwargs)
        monkeypatch.setattr(real, "Client", fake)
        return fake

    return install


def test_collect_messages_gathers_the_retained_states_and_stops_early(paho):
    fake = paho(messages={TOPIC.format("a"): "1.5", TOPIC.format("b"): "2"})
    got = statestream.collect_messages("h", 1883, "u", "p", [TOPIC.format("a"), TOPIC.format("b")], 5)
    assert got == {TOPIC.format("a"): "1.5", TOPIC.format("b"): "2"}
    assert fake.credentials == ("u", "p") and len(fake.subscribed) == 2 and fake.stopped


def test_collect_messages_names_each_failure_safely(paho):
    paho(messages={}, refuse=True)
    with pytest.raises(CollectorError, match="refused the connection"):
        statestream.collect_messages("198.51.100.70", 1883, "u", "p", ["t"], 1)
    paho(messages={}, unreachable=True)
    with pytest.raises(CollectorError) as exc:
        statestream.collect_messages("198.51.100.70", 1883, "u", "p", ["t"], 1)
    assert str(exc.value) == "could not connect to the MQTT broker"
    paho(messages={}, silent=True)
    with pytest.raises(CollectorError, match="did not answer"):
        statestream.collect_messages("h", 1883, "", "", ["t"], 1)


# ---------------------------------------------------------------- energy and draw from the readings


def test_energy_from_a_meter_handles_a_reset_and_needs_an_earlier_reading(tmp_path):
    store = Store(tmp_path / "baseline.db")
    for hours_ago, kwh in (
        (30, 10.0),
        (20, 11.0),
        (10, 12.5),
        (5, 0.4),
        (1, 1.0),
    ):  # restarted between 10 and 5
        put(store, hours_ago, "e", "s", kwh=kwh)
    with store.read() as db:
        assert power.energy_kwh(db, "s", NOW - timedelta(hours=24), NOW) == pytest.approx(
            1.0 + 1.5 + 0.4 + 0.6
        )
        assert power.energy_kwh(db, "s", NOW - timedelta(hours=31), NOW - timedelta(hours=29)) is None
        assert power.energy_kwh(db, "nobody", NOW - timedelta(hours=24), NOW) is None


def test_energy_from_power_alone_counts_each_hourly_sample_as_an_hour(tmp_path):
    store = Store(tmp_path / "baseline.db")
    for hours_ago in range(0, 24):
        put(store, hours_ago, "p", "s", watts=100.0)
    with store.read() as db:
        assert power.energy_kwh(db, "s", NOW - timedelta(hours=24), NOW) == pytest.approx(2.4)


def test_a_meter_wins_over_power_when_a_system_has_both(tmp_path):
    store = Store(tmp_path / "baseline.db")
    for hours_ago in range(0, 6):
        put(store, hours_ago, "p", "s", watts=1000.0)
        put(store, hours_ago, "e", "s", kwh=50.0 + (6 - hours_ago) * 0.1)
    with store.read() as db:
        assert power.energy_kwh(db, "s", NOW - timedelta(hours=24), NOW) == pytest.approx(0.5)


# ---------------------------------------------------------------- power_drift


def seed_draw(store, recent_watts, baseline_watts, system="server-2"):
    for h in range(0, 24):
        put(store, h, "p1", system, watts=recent_watts / 2)
        put(store, h, "p2", system, watts=recent_watts / 2)
    for h in range(24, 24 * 12):
        put(store, h, "p1", system, watts=baseline_watts / 2)
        put(store, h, "p2", system, watts=baseline_watts / 2)


def test_a_system_drawing_well_above_its_usual_is_a_finding(tmp_path):
    env, user, store = lab(tmp_path, OFF)
    seed_draw(store, recent_watts=160, baseline_watts=110)
    systems = Systems.from_mapping(
        {"systems": [{"id": "server-2", "label": "Server Two", "hosts": ["198.51.100.2"]}]}
    )
    (f,) = detect.run(store, systems, NOW, env, kinds=("power_drift",))["findings"]
    assert f.system == "server-2" and f.detail["watts"] == 160 and f.detail["baseline_watts"] == 110
    assert f.title == "Server Two is drawing 160 W on average, 45% above its usual 110 W"
    (advised,) = playbook.apply([f], playbook.validate(BRIEF / "playbook.yaml"))
    assert advised.detail["advice"].startswith(
        "Server Two has drawn 160 W on average over the last day, 45% above"
    )


@pytest.mark.parametrize("recent, baseline", [(125, 110), (60, 110), (28, 20)])
def test_small_changes_and_drops_are_not_findings(tmp_path, recent, baseline):
    # +14%, a drop, and +40% that is only 8 W: each misses a requirement (percent, direction, watts)
    env, user, store = lab(tmp_path, OFF)
    seed_draw(store, recent, baseline)
    assert detect.run(store, Systems(), NOW, env, kinds=("power_drift",))["findings"] == []


def test_it_stays_quiet_until_there_is_a_baseline(tmp_path):
    env, user, store = lab(tmp_path, OFF)
    for h in range(0, 24):
        put(store, h, "p", "s", watts=500.0)
    assert detect.run(store, Systems(), NOW, env, kinds=("power_drift",))["findings"] == []


# ---------------------------------------------------------------- the brief and the weekly report


def test_the_brief_gains_a_power_line_only_when_there_is_power_data():
    digest = types.SimpleNamespace(pipeline={"events_24h": 10, "new_signatures_24h": 0})
    assert not any("power" in line for line in render_md._pipeline(digest))
    digest.pipeline["power"] = [
        {"system": "s", "label": "Server Two", "watts": 110.5, "kwh_24h": 2.65},
        {"system": "t", "label": "Rack", "watts": 250.0, "kwh_24h": None},
        {"system": "u", "label": "Gone", "watts": None, "kwh_24h": 1.0},
    ]
    assert "- power: Server Two 110 W, 2.6 kWh in 24 h; Rack 250 W" in render_md._pipeline(digest)


def test_the_weekly_report_has_a_power_section_only_with_power_data(tmp_path):
    store = Store(tmp_path / "baseline.db")
    monday = date(2026, 10, 5)
    systems = Systems.from_mapping(
        {"systems": [{"id": "server-2", "label": "Server Two", "hosts": ["198.51.100.2"]}]}
    )
    assert "## Power and energy" not in "\n".join(weekly.weekly_markdown(store, systems, monday))
    end = datetime(2026, 10, 12, tzinfo=UTC)
    meter = 1000.0
    for hours_ago in range(24 * 14 - 1, -1, -1):  # oldest first, one continuous meter
        meter += 0.2 if hours_ago >= 168 else 0.1  # the earlier week uses twice as much
        put(store, hours_ago, "e", "server-2", kwh=meter, now=end)
        put(store, hours_ago, "p", "server-2", watts=100.0, now=end)
    text = "\n".join(weekly.weekly_markdown(store, systems, monday))
    assert "## Power and energy" in text
    line = next(ln for ln in text.splitlines() if ln.startswith("| Server Two |"))
    assert "16.8" in line and "-50%" in line and "100 W" in line
