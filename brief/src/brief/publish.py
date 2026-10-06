"""MQTT publisher: Home Assistant discovery, retained states, and availability with a last will.

The sensors come from brief/registry.yaml (restricted to the metrics the program can compute, see
the registry). Topics follow docs/guide/04-publish.md:

  homelab/brief/availability                online | offline (retained; offline is the last will)
  homelab/brief/<metric_id>/state           the latest value (retained)
  homeassistant/sensor/homelab_brief/<metric_id>/config   discovery (retained)
  homelab/brief/alert                       one JSON message per notification (not retained)
  homelab/brief/headline                    today's headline for the phone push (retained)
  homelab/brief/feedback/<finding_id>       ack, snooze_24h, or snooze_7d, from Home Assistant (we subscribe)

States are published on every hourly run, not only on change: discovery sets `expire_after` to about
twice the interval, so a value that never repeated would make a healthy sensor expire. When the
broker is unreachable nothing is queued; the latest values are sent when the connection returns.
Nothing here ever logs the broker password.
"""

import json
import queue
import re
import threading
from collections.abc import Mapping

from brief import registry
from brief.jsonlog import log

BASE_TOPIC = "homelab/brief"
DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "homelab_brief"
INTERVAL_SECONDS = {"hourly": 3600, "daily": 86400}
FEEDBACK_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
FEEDBACK_ACTION = re.compile(r"^[a-z0-9_]{1,32}$")


def load_registry(env: Mapping[str, str] | None = None, only: tuple[str, ...] | None = None) -> list[dict]:
    """The number metrics of registry.yaml (see brief.registry.load for the full set with system states)."""
    return [
        m
        for m in registry.read_file(env)
        if m.get("kind", "number") == "number" and (only is None or m["id"] in only)
    ]


def discovery_payload(metric: dict, base: str = BASE_TOPIC) -> dict:
    interval = INTERVAL_SECONDS.get(str(metric.get("interval", "hourly")), 3600)
    payload = {
        "name": metric.get("name") or metric["id"],
        "unique_id": f"{NODE_ID}_{metric['id']}",
        "state_topic": f"{base}/{metric['id']}/state",
        "availability_topic": f"{base}/availability",
        "expire_after": 2 * interval,
        "device": {"identifiers": [NODE_ID], "name": "Homelab Brief"},
    }
    if metric.get("kind") == "state":  # green | yellow | red | unknown
        payload["device_class"] = "enum"
        payload["options"] = list(registry.STATES)
    else:
        payload["state_class"] = "measurement"
    if metric.get("unit"):
        payload["unit_of_measurement"] = str(metric["unit"])
    return payload


class Publisher:
    def __init__(
        self,
        host: str,
        port: int = 1883,
        user: str = "",
        password: str = "",
        metrics: list[dict] | None = None,
        base: str = BASE_TOPIC,
        client=None,
    ):
        self.host, self.port, self.base = host, port, base
        self.metrics = list(metrics or [])
        self._lock = threading.Lock()
        self._feedback: queue.SimpleQueue = queue.SimpleQueue()
        self._latest: dict[str, int | float | str] = {}
        self.connected = False
        if client is None:
            import paho.mqtt.client as mqtt

            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="homelab-brief")
        self.client = client
        if user:
            client.username_pw_set(user, password)
        client.will_set(f"{base}/availability", "offline", qos=1, retain=True)
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

    @classmethod
    def from_env(cls, env: Mapping[str, str], metrics: list[dict]) -> "Publisher | None":
        """None when MQTT_HOST is empty: MQTT is optional, the rest of the product runs without it."""
        host = (env.get("MQTT_HOST") or "").strip()
        if not host:
            return None
        try:
            port = int((env.get("MQTT_PORT") or "1883").strip())
        except ValueError:
            raise ValueError(f"MQTT_PORT must be a number, got {env.get('MQTT_PORT')!r}") from None
        return cls(host, port, env.get("MQTT_USER", ""), env.get("MQTT_PASSWORD", ""), metrics)

    # -- connection (paho runs these on its own thread)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            log("mqtt_connect_failed", host=self.host, port=self.port, reason=str(reason_code))
            return
        with self._lock:
            self.connected = True
            self._send_discovery()
            self._send_states(self._latest)
            client.publish(f"{self.base}/availability", "online", qos=1, retain=True)
            client.subscribe(f"{self.base}/feedback/#", qos=1)
        log("mqtt_connected", host=self.host, port=self.port, sensors=len(self.metrics))

    def _on_disconnect(self, client, userdata, *args) -> None:
        self.connected = False
        log("mqtt_disconnected", host=self.host, port=self.port)

    def _send_discovery(self) -> None:
        for metric in self.metrics:
            topic = f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/{metric['id']}/config"
            self.client.publish(topic, json.dumps(discovery_payload(metric, self.base)), qos=1, retain=True)

    def _send_states(self, values: Mapping[str, int | float | str]) -> None:
        for metric_id, value in values.items():
            self.client.publish(f"{self.base}/{metric_id}/state", str(value), qos=1, retain=True)

    # -- API

    def start(self) -> None:
        self.client.connect_async(self.host, self.port, keepalive=60)
        self.client.loop_start()

    def set_metrics(self, metrics: list[dict]) -> None:
        """Use a new sensor list (systems.yaml changed): announce the new sensors and withdraw the ones
        that are gone (an empty retained discovery message removes a sensor from Home Assistant)."""
        with self._lock:
            old = {m["id"] for m in self.metrics}
            new = {m["id"] for m in metrics}
            if old == new:
                self.metrics = list(metrics)
                return
            gone = old - new
            self.metrics = list(metrics)
            for metric_id in gone:
                self._latest.pop(metric_id, None)
                if self.connected:
                    self.client.publish(
                        f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/{metric_id}/config", "", qos=1, retain=True
                    )
                    self.client.publish(f"{self.base}/{metric_id}/state", "", qos=1, retain=True)
            if self.connected:
                self._send_discovery()

    def publish(self, values: Mapping[str, int | float | str]) -> None:
        """Remember the values and send them now when connected (otherwise on the next connect)."""
        known = {m["id"] for m in self.metrics}
        values = {k: v for k, v in values.items() if k in known}
        with self._lock:
            self._latest.update(values)
            if self.connected:
                self._send_states(values)

    # -- alerts, headline, and feedback

    def send_alert(self, payload: dict) -> bool:
        """Publish one alert (not retained). False when the broker is not connected: nothing is queued."""
        if not self.connected:
            return False
        info = self.client.publish(f"{self.base}/alert", json.dumps(payload), qos=1, retain=False)
        return getattr(info, "rc", 0) == 0

    def send_headline(self, text: str) -> bool:
        if not self.connected:
            return False
        info = self.client.publish(f"{self.base}/headline", text, qos=1, retain=True)
        return getattr(info, "rc", 0) == 0

    def _on_message(self, client, userdata, message) -> None:
        """A Home Assistant button press: homelab/brief/feedback/<finding_id> with ack or snooze_*."""
        prefix = f"{self.base}/feedback/"
        topic = getattr(message, "topic", "")
        if not topic.startswith(prefix):
            return
        finding_id = topic[len(prefix) :]
        try:
            action = bytes(message.payload).decode("utf-8").strip().lower()
        except (UnicodeDecodeError, TypeError):
            return
        if FEEDBACK_ID.match(finding_id) and FEEDBACK_ACTION.match(action):
            self._feedback.put((finding_id, action))
        else:
            log("mqtt_feedback_ignored", topic=topic[:80])

    def next_feedback(self):
        """The next queued (finding_id, action), or None."""
        try:
            return self._feedback.get_nowait()
        except queue.Empty:
            return None

    def forget(self) -> None:
        """Drop the remembered values (the run failed), so a reconnect cannot refresh stale ones."""
        with self._lock:
            self._latest.clear()

    def stop(self) -> None:
        """Publish offline on a clean shutdown (a crash is covered by the last will)."""
        try:
            if self.connected:
                info = self.client.publish(f"{self.base}/availability", "offline", qos=1, retain=True)
                info.wait_for_publish(timeout=3)
            self.client.disconnect()
        finally:
            self.client.loop_stop()
