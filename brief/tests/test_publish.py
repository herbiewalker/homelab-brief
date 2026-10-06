import json

import pytest

from brief import publish as p
from brief.config import ConfigError


class FakeInfo:
    def wait_for_publish(self, timeout=None):
        return True


class FakeReason:
    def __init__(self, failure=False):
        self.is_failure = failure


class FakeClient:
    def __init__(self):
        self.sent = []  # (topic, payload, retain)
        self.subscribed = []
        self.will = None
        self.auth = None
        self.started = self.stopped = self.disconnected = False

    def username_pw_set(self, user, password):
        self.auth = (user, password)

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, retain)

    def reconnect_delay_set(self, **kw):
        pass

    def connect_async(self, host, port, keepalive=60):
        self.target = (host, port)

    def loop_start(self):
        self.started = True

    def loop_stop(self):
        self.stopped = True

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.sent.append((topic, payload, retain))
        return FakeInfo()

    def topics(self):
        return [t for t, _, _ in self.sent]

    def last(self, topic):
        return [pl for t, pl, _ in self.sent if t == topic][-1]


METRICS = [
    {"id": "events_24h", "name": "Log events, last 24h", "unit": "events", "interval": "hourly"},
    {"id": "hosts_silent", "name": "Silent hosts", "interval": "hourly"},
    {"id": "db_mb", "name": "DB size", "unit": "MB", "interval": "daily"},
]


def make(metrics=METRICS, **kw):
    client = FakeClient()
    return p.Publisher("broker.example", 1883, "brief", "pw", metrics, client=client, **kw), client


def test_discovery_payload_follows_the_guide():
    d = p.discovery_payload(METRICS[0])
    assert d["unique_id"] == "homelab_brief_events_24h"
    assert d["state_topic"] == "homelab/brief/events_24h/state"
    assert d["availability_topic"] == "homelab/brief/availability"
    assert d["expire_after"] == 7200 and d["state_class"] == "measurement"
    assert d["unit_of_measurement"] == "events"
    assert d["device"] == {"identifiers": ["homelab_brief"], "name": "Homelab Brief"}
    assert "unit_of_measurement" not in p.discovery_payload(METRICS[1])
    assert p.discovery_payload(METRICS[2])["expire_after"] == 172800  # daily metric: two days


def test_the_last_will_is_offline_and_retained():
    pub, client = make()
    assert client.will == ("homelab/brief/availability", "offline", True)
    assert client.auth == ("brief", "pw")
    pub.start()
    assert client.started and client.target == ("broker.example", 1883)


def test_on_connect_sends_discovery_then_online_and_everything_is_retained():
    pub, client = make()
    pub._on_connect(client, None, None, FakeReason())
    topics = client.topics()
    assert "homeassistant/sensor/homelab_brief/events_24h/config" in topics
    assert topics[-1] == "homelab/brief/availability" and client.last(topics[-1]) == "online"
    assert all(retain for _, _, retain in client.sent)
    cfg = json.loads(client.last("homeassistant/sensor/homelab_brief/hosts_silent/config"))
    assert cfg["name"] == "Silent hosts"


def test_a_refused_connection_sends_nothing():
    pub, client = make()
    pub._on_connect(client, None, None, FakeReason(failure=True))
    assert client.sent == [] and pub.connected is False


def test_values_wait_for_the_connection_and_are_sent_on_connect():
    pub, client = make()
    pub.publish({"events_24h": 5, "hosts_silent": 0})
    assert client.sent == []  # not connected: nothing queued in the client
    pub._on_connect(client, None, None, FakeReason())
    assert client.last("homelab/brief/events_24h/state") == "5"
    assert client.last("homelab/brief/hosts_silent/state") == "0"


def test_values_are_published_every_time_when_connected_even_if_unchanged():
    pub, client = make()
    pub._on_connect(client, None, None, FakeReason())
    client.sent.clear()
    pub.publish({"events_24h": 5})
    pub.publish({"events_24h": 5})  # expire_after needs the repeat
    assert client.topics().count("homelab/brief/events_24h/state") == 2


def test_unknown_metric_ids_are_ignored():
    pub, client = make()
    pub._on_connect(client, None, None, FakeReason())
    client.sent.clear()
    pub.publish({"not_in_registry": 1})
    assert client.sent == []


def test_forget_stops_a_reconnect_from_refreshing_stale_values():
    pub, client = make()
    pub.publish({"events_24h": 5})
    pub.forget()
    pub._on_connect(client, None, None, FakeReason())
    assert "homelab/brief/events_24h/state" not in client.topics()


def test_a_clean_stop_publishes_offline_first():
    pub, client = make()
    pub._on_connect(client, None, None, FakeReason())
    pub.stop()
    assert client.last("homelab/brief/availability") == "offline"
    assert client.disconnected and client.stopped


def test_no_broker_configured_means_no_publisher():
    assert p.Publisher.from_env({}, METRICS) is None
    assert p.Publisher.from_env({"MQTT_HOST": "  "}, METRICS) is None


def test_from_env_reads_host_port_and_credentials(monkeypatch):
    seen = {}

    class Capture(p.Publisher):
        def __init__(self, host, port, user, password, metrics, **kw):
            seen.update(host=host, port=port, user=user, password=password)

    monkeypatch.setattr(p, "Publisher", Capture)
    Capture.from_env({"MQTT_HOST": "b", "MQTT_PORT": "1884", "MQTT_USER": "u", "MQTT_PASSWORD": "x"}, [])
    assert seen == {"host": "b", "port": 1884, "user": "u", "password": "x"}
    with pytest.raises(ValueError, match="MQTT_PORT"):
        Capture.from_env({"MQTT_HOST": "b", "MQTT_PORT": "abc"}, [])


def test_registry_loading_filters_and_validates(tmp_path):
    folder = tmp_path / "c" / "brief"
    folder.mkdir(parents=True)
    env = {"BRIEF_CONFIG_DIR": str(tmp_path / "c"), "BRIEF_DEFAULTS_DIR": str(tmp_path / "d")}
    (folder / "registry.yaml").write_text(
        "schema: 1\nmetrics:\n"
        "  - { id: a, kind: number }\n  - { id: b, kind: state }\n  - { id: c, kind: number }\n",
        encoding="utf-8",
    )
    assert [m["id"] for m in p.load_registry(env)] == ["a", "c"]  # state sensors come later
    assert [m["id"] for m in p.load_registry(env, only=("c",))] == ["c"]
    (folder / "registry.yaml").write_text("schema: 1\nmetrics:\n  - { name: no id }\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="needs an id"):
        p.load_registry(env)


def test_the_shipped_registry_offers_the_derived_sensors(tmp_path):
    from pathlib import Path

    derived = ("events_24h", "new_signatures_24h", "hosts_silent")

    shipped = (
        Path(__file__).resolve().parents[1] / "registry.yaml"
    )  # brief/ in the repo, /app in the test image
    folder = tmp_path / "brief"
    folder.mkdir()
    (folder / "registry.yaml").write_bytes(shipped.read_bytes())
    ids = [m["id"] for m in p.load_registry({"BRIEF_DEFAULTS_DIR": str(tmp_path)}, only=derived)]
    assert set(ids) == set(derived)


# ---------------------------------------------------------------- alerts, headline, feedback


def connected():
    pub, client = make()
    pub._on_connect(client, None, None, FakeReason())
    client.sent.clear()
    return pub, client


class Msg:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload if isinstance(payload, bytes) else payload.encode()


def test_it_subscribes_to_the_feedback_topics_on_connect():
    pub, client = make()
    pub._on_connect(client, None, None, FakeReason())
    assert client.subscribed == ["homelab/brief/feedback/#"]


def test_an_alert_is_published_once_and_not_retained():
    pub, client = connected()
    assert pub.send_alert({"finding_id": "absence-1", "state": "new"}) is True
    topic, payload, retain = client.sent[0]
    assert topic == "homelab/brief/alert" and json.loads(payload)["state"] == "new" and retain is False


def test_the_headline_is_retained_for_the_phone_push():
    pub, client = connected()
    assert pub.send_headline("5 things need a look") is True
    assert client.sent[0] == ("homelab/brief/headline", "5 things need a look", True)


def test_nothing_is_sent_or_queued_while_the_broker_is_away():
    pub, client = make()
    assert pub.send_alert({"x": 1}) is False and pub.send_headline("x") is False and client.sent == []


def test_a_button_press_becomes_a_queued_feedback_message():
    pub, _ = connected()
    pub._on_message(None, None, Msg("homelab/brief/feedback/absence-198.51.100.3", b"ack"))
    pub._on_message(None, None, Msg("homelab/brief/feedback/group-deviation-198.51.100.1", b" Snooze_24h \n"))
    assert pub.next_feedback() == ("absence-198.51.100.3", "ack")
    assert pub.next_feedback() == ("group-deviation-198.51.100.1", "snooze_24h")
    assert pub.next_feedback() is None


def test_feedback_that_looks_wrong_is_dropped(capsys):
    pub, _ = connected()
    for topic, payload in (
        ("homelab/brief/feedback/ok-id", b"rm -rf /"),  # not an action
        ("homelab/brief/feedback/bad id with spaces", b"ack"),
        ("homelab/brief/feedback/", b"ack"),
        ("homelab/brief/feedback/ok-id", b"\xff\xfe"),  # not text
        ("homelab/other/feedback/ok-id", b"ack"),  # not ours
    ):
        pub._on_message(None, None, Msg(topic, payload))
    assert pub.next_feedback() is None
