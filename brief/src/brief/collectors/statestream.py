"""The Home Assistant collector: power and energy that Home Assistant already knows, read from MQTT.

Home Assistant owns power data (the Energy dashboard, smart plugs, the UPS through NUT). It publishes the
entities you choose with its `mqtt_statestream` integration, one topic per entity:

    <base_topic>/<domain>/<object_id>/state        for example   homelab/ha/sensor/server_2_power/state

The container needs no Home Assistant token and no inbound connection: this collector connects to the
same MQTT broker as the publisher (MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD), subscribes to just the
topics of the entities listed in collectors.yaml, listens for a few seconds (statestream messages are
retained, so the current values arrive at once), and disconnects. Each hourly run stores one reading per
entity in `power_reading`.

Two kinds of entity:

  power    instantaneous watts (a smart plug, a UPS load); `scale: 1000` turns kilowatts into watts
  energy   a cumulative meter in kWh (total_increasing); `scale: 0.001` turns watt-hours into kWh

Each entity is attached to a `system` (an id from systems.yaml, or any name), so the brief and the weekly
report can say "Server 2 used 2.6 kWh yesterday" and the `power_drift` finding can say a system draws more
than usual.
"""

import re
import threading
from dataclasses import dataclass

from brief import power
from brief.collectors.base import Collector, CollectorError, Context
from brief.collectors.framework import register
from brief.config import ConfigError
from brief.jsonlog import log
from brief.store import iso

ENTITY = re.compile(r"^([a-z_]+)\.([a-z0-9_]+)$")
SYSTEM = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
BASE_TOPIC = re.compile(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$")
KINDS = ("power", "energy")
UNITS = {"power": "W", "energy": "kWh"}
MAX_ENTITIES = 200
DEFAULT_WINDOW_S = 5.0


@dataclass(frozen=True)
class Entity:
    entity: str
    domain: str
    name: str
    system: str | None
    kind: str
    scale: float
    label: str

    @property
    def slug(self) -> str:
        return self.entity.replace(".", "_")

    def topic(self, base: str) -> str:
        return f"{base.strip('/')}/{self.domain}/{self.name}/state"


def entities_of(config: dict) -> list[Entity]:
    out = []
    for item in config.get("entities") or []:
        if not isinstance(item, dict):
            continue
        match = ENTITY.match(str(item.get("entity", "")))
        if not match:
            continue
        out.append(
            Entity(
                str(item["entity"]),
                match.group(1),
                match.group(2),
                str(item["system"]) if item.get("system") else None,
                str(item.get("kind", "power")),
                float(item.get("scale", 1)),
                str(item.get("label") or item["entity"]),
            )
        )
    return out


def system_slug(system: str) -> str:
    return system.replace("-", "_")


def parse_value(payload: str | None, scale: float = 1.0) -> float | None:
    """A statestream payload as a number, or None for unavailable, unknown, empty, or not a number."""
    if payload is None:
        return None
    try:
        value = float(payload.strip())
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value * scale


def collect_messages(host, port, user, password, topics: list[str], window_s: float) -> dict[str, str]:
    """Connect, subscribe to `topics`, wait until all have arrived or `window_s` has passed, disconnect.
    Returns {topic: payload}. Raises CollectorError with a message that names no address."""
    import paho.mqtt.client as mqtt

    got: dict[str, str] = {}
    done, connected = threading.Event(), threading.Event()
    failure: list[str] = []

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            failure.append("the MQTT broker refused the connection (check the user and password)")
            done.set()
            return
        connected.set()
        client.subscribe([(t, 0) for t in topics])

    def on_message(client, userdata, message):
        got[message.topic] = message.payload.decode("utf-8", "replace")[:64]
        if len(got) >= len(topics):
            done.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="homelab-brief-statestream")
    if user:
        client.username_pw_set(user, password)
    client.on_connect, client.on_message = on_connect, on_message
    try:
        client.connect(host, port, keepalive=30)
    except OSError:
        raise CollectorError("could not connect to the MQTT broker") from None
    client.loop_start()
    try:
        done.wait(window_s)
    finally:
        client.loop_stop()
        client.disconnect()
    if failure:
        raise CollectorError(failure[0])
    if not connected.is_set():
        raise CollectorError("the MQTT broker did not answer")
    return got


SNAPSHOT = collect_messages  # replaced in tests


@register
class StatestreamCollector(Collector):
    name = "ha_statestream"

    def required_vars(self, config: dict) -> list[str]:
        return ["MQTT_HOST"]  # the same broker the publisher uses

    def validate(self, config: dict) -> None:
        base = str(config.get("base_topic", "homelab/ha"))
        if not BASE_TOPIC.match(base.strip("/")):
            raise ConfigError("base_topic must be plain topic levels like homelab/ha (no # or +)")
        try:
            window = float(config.get("window_s", DEFAULT_WINDOW_S))
        except (TypeError, ValueError):
            raise ConfigError("window_s must be a number of seconds") from None
        if not 1 <= window <= 30:
            raise ConfigError("window_s must be between 1 and 30")
        items = config.get("entities") or []
        if not isinstance(items, list) or len(items) > MAX_ENTITIES:
            raise ConfigError(f"entities must be a list of at most {MAX_ENTITIES}")
        seen = set()
        for index, item in enumerate(items, 1):
            where = f"entity #{index}"
            if not isinstance(item, dict) or not ENTITY.match(str(item.get("entity", ""))):
                raise ConfigError(f"{where}: entity must look like sensor.server_2_power")
            if item["entity"] in seen:
                raise ConfigError(f"{where}: {item['entity']} is listed twice")
            seen.add(item["entity"])
            if str(item.get("kind", "power")) not in KINDS:
                raise ConfigError(f"{where}: kind must be one of {', '.join(KINDS)}")
            if item.get("system") and not SYSTEM.match(str(item["system"])):
                raise ConfigError(f"{where}: system must be lowercase letters, digits and hyphens")
            try:
                if float(item.get("scale", 1)) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise ConfigError(f"{where}: scale must be a number above zero") from None

    def metrics(self, config: dict) -> list[dict]:
        out = [
            {
                "id": "ha_statestream_entities_seen",
                "name": "Home Assistant: entities answering",
                "group": "power",
            }
        ]
        systems: dict[str, set[str]] = {}
        for e in entities_of(config):
            out.append(
                {
                    "id": f"ha_statestream_{e.slug}",
                    "name": f"Power: {e.label}",
                    "group": "power",
                    "unit": UNITS[e.kind],
                }
            )
            if e.system:
                systems.setdefault(e.system, set()).add(e.kind)
        for system in sorted(systems):
            base = f"ha_statestream_{system_slug(system)}"
            if "power" in systems[system]:
                out.append(
                    {"id": f"{base}_power_w", "name": f"Power: {system} draw", "group": "power", "unit": "W"}
                )
            out.append(
                {
                    "id": f"{base}_energy_24h_kwh",
                    "name": f"Power: {system} energy, 24 h",
                    "group": "power",
                    "unit": "kWh",
                }
            )
        return out

    # ---- reading

    def _snapshot(self, ctx: Context, entities: list[Entity]) -> dict[str, str]:
        base = str(ctx.config.get("base_topic", "homelab/ha"))
        try:
            port = int((ctx.env.get("MQTT_PORT") or "1883").strip())
        except ValueError:
            raise CollectorError("MQTT_PORT is not a number") from None
        return SNAPSHOT(
            (ctx.env.get("MQTT_HOST") or "").strip(),
            port,
            ctx.env.get("MQTT_USER", ""),
            ctx.env.get("MQTT_PASSWORD", ""),
            [e.topic(base) for e in entities],
            float(ctx.config.get("window_s", DEFAULT_WINDOW_S)),
        )

    def test(self, ctx: Context) -> str:
        entities = entities_of(ctx.config)
        if not entities:
            raise CollectorError("no entities are listed in collectors.yaml")
        messages = self._snapshot(ctx, entities)
        base = str(ctx.config.get("base_topic", "homelab/ha"))
        lines = []
        for e in entities:
            value = parse_value(messages.get(e.topic(base)), e.scale)
            lines.append(
                f"{e.entity}: " + (f"{value:g} {UNITS[e.kind]}" if value is not None else "no value")
            )
        answering = sum(1 for line in lines if not line.endswith("no value"))
        if not answering:
            raise CollectorError(
                "no Home Assistant state was received; is mqtt_statestream on, with this base_topic?\n"
                + "\n".join(lines)
            )
        return f"{answering} of {len(entities)} entities answered\n" + "\n".join(lines)

    def run(self, ctx: Context) -> dict[str, float | str]:
        entities = entities_of(ctx.config)
        if not entities:
            raise CollectorError("no entities are listed in collectors.yaml")
        base = str(ctx.config.get("base_topic", "homelab/ha"))
        messages = self._snapshot(ctx, entities)
        stamp = iso(power.floor_hour(ctx.now))
        numbers: dict[str, float | str] = {}
        draw: dict[str, float] = {}
        seen = 0
        with ctx.store.write() as tx:
            for e in entities:
                value = parse_value(messages.get(e.topic(base)), e.scale)
                if value is None:
                    log("statestream_no_value", entity=e.entity)
                    continue
                seen += 1
                numbers[f"ha_statestream_{e.slug}"] = round(value, 3)
                watts, kwh = (value, None) if e.kind == "power" else (None, value)
                tx.db.execute(
                    "INSERT OR REPLACE INTO power_reading (ts, entity, system_id, watts, kwh) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (stamp, e.entity, e.system, watts, kwh),
                )
                if e.system and e.kind == "power":
                    draw[e.system] = draw.get(e.system, 0.0) + value
        if not seen:
            raise CollectorError(
                "no Home Assistant state was received; is mqtt_statestream on, with this base_topic?"
            )
        numbers["ha_statestream_entities_seen"] = seen
        with ctx.store.read() as db:
            for system in sorted({e.system for e in entities if e.system}):
                base_id = f"ha_statestream_{system_slug(system)}"
                if system in draw:
                    numbers[f"{base_id}_power_w"] = round(draw[system], 1)
                used = power.energy_kwh(db, system, ctx.now - power.HOUR * 24, ctx.now)
                if used is not None:
                    numbers[f"{base_id}_energy_24h_kwh"] = used
        return numbers
