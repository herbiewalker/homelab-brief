"""devices.yaml and topology.yaml: the files that describe one household's devices and layout.

They hold real MACs, names, and owners, so they exist only in your config folder (never in this
repository) and are redacted from the support bundle. The collectors that read them arrive in Phase 2;
the rules below keep them well formed from the day you write them, so the config editor, the config
guard, and a rollback treat them like every other file.
"""

import re
from collections.abc import Mapping

from brief.config import ConfigError

MAC = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
PROFILES = ("full", "security_only", "ignore")
NODE_KINDS = (
    "internet", "gateway", "switch", "ap", "server", "vm", "container", "service", "camera",
    "iot_group", "client_group", "ups", "display", "other",
)  # fmt: skip
EDGE_KINDS = ("uplink", "hosts", "depends", "power")
MAX_ENTRIES = 5000


def _list(data: Mapping, key: str, file: str) -> list:
    value = data.get(key) or []
    if not isinstance(value, list):
        raise ConfigError(f"{file}: {key} must be a list")
    if len(value) > MAX_ENTRIES:
        raise ConfigError(f"{file}: {key} has more than {MAX_ENTRIES} entries")
    return value


def validate_devices(data: Mapping) -> None:
    seen: set[str] = set()
    for i, item in enumerate(_list(data, "devices", "devices.yaml"), 1):
        where = f"devices.yaml: device #{i}"
        if not isinstance(item, Mapping):
            raise ConfigError(f"{where} must be a mapping")
        mac = str(item.get("mac") or "")
        if not MAC.match(mac):
            raise ConfigError(f"{where}: mac must look like 00:00:5E:00:53:01")
        key = mac.lower().replace("-", ":")
        if key in seen:
            raise ConfigError(f"devices.yaml: a device is listed twice (device #{i})")
        seen.add(key)
        profile = str(item.get("profile") or "security_only")
        if profile not in PROFILES:
            raise ConfigError(f"{where}: profile must be one of {', '.join(PROFILES)}")
        for field in ("label", "owner"):
            if field in item and not isinstance(item[field], str):
                raise ConfigError(f"{where}: {field} must be text")
        quiet = item.get("quiet_hours")
        if quiet is not None:
            if not isinstance(quiet, Mapping) or not all(
                CLOCK.match(str(quiet.get(k) or "")) for k in ("start", "end")
            ):
                raise ConfigError(f'{where}: quiet_hours needs start and end like "23:00"')


def validate_topology(data: Mapping) -> None:
    ids: set[str] = set()
    for i, item in enumerate(_list(data, "nodes", "topology.yaml"), 1):
        where = f"topology.yaml: node #{i}"
        if not isinstance(item, Mapping):
            raise ConfigError(f"{where} must be a mapping")
        node = str(item.get("id") or "").strip()
        if not node or len(node) > 64:
            raise ConfigError(f"{where}: every node needs an id (up to 64 characters)")
        if node in ids:
            raise ConfigError(f"topology.yaml: node id {node!r} is used twice")
        ids.add(node)
        kind = str(item.get("kind") or "other")
        if kind not in NODE_KINDS:
            raise ConfigError(f"{where}: kind must be one of {', '.join(NODE_KINDS)}")
    for i, item in enumerate(_list(data, "edges", "topology.yaml"), 1):
        where = f"topology.yaml: edge #{i}"
        if not isinstance(item, Mapping):
            raise ConfigError(f"{where} must be a mapping")
        if not str(item.get("from") or "").strip() or not str(item.get("to") or "").strip():
            raise ConfigError(f"{where}: needs from and to")
        if str(item.get("kind") or "uplink") not in EDGE_KINDS:
            raise ConfigError(f"{where}: kind must be one of {', '.join(EDGE_KINDS)}")
    for i, item in enumerate(_list(data, "overrides", "topology.yaml"), 1):
        where = f"topology.yaml: override #{i}"
        if not isinstance(item, Mapping) or not str(item.get("id") or "").strip():
            raise ConfigError(f"{where}: needs an id")
        if "hide" in item and not isinstance(item["hide"], bool):
            raise ConfigError(f"{where}: hide must be true or false")
