"""systems.yaml: which hosts belong to which system, and what timezone their headers use.

A system is one dashboard tile and one status sensor. A sender is matched by its IP or by the
hostname in the log header (case-insensitive). Senders that match nothing roll up to default_system
and are marked unknown, so the admin GUI can show them for you to name.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brief.config import ConfigError, is_placeholder, load_mapping

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class System:
    id: str
    label: str
    kind: str
    hosts: tuple[str, ...]
    source_tz: str
    expected: bool


@dataclass(frozen=True)
class Resolved:
    system_id: str
    known: bool
    source_tz: str


def _tz(name: str, where: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ConfigError(f"systems.yaml: {where}: unknown timezone {name!r}") from None
    return name


class Systems:
    def __init__(
        self, systems: list[System] | None = None, default_system: str = "other", default_tz: str = "UTC"
    ):
        self.systems = list(systems or [])
        self.default_system = default_system
        self.default_tz = default_tz
        self._by_host: dict[str, System] = {}
        seen_ids: set[str] = set()
        for system in self.systems:
            if system.id in seen_ids:
                raise ConfigError(f"systems.yaml: duplicate system id {system.id!r}")
            seen_ids.add(system.id)
            for host in system.hosts:
                key = host.lower()
                if key in self._by_host and self._by_host[key].id != system.id:
                    other = self._by_host[key].id
                    raise ConfigError(
                        f"systems.yaml: host {host!r} is listed under both {other!r} and {system.id!r}"
                    )
                self._by_host[key] = system

    @classmethod
    def from_mapping(cls, data: dict) -> "Systems":
        default_system = str(data.get("default_system") or "other")
        if not ID_RE.match(default_system):
            raise ConfigError(f"systems.yaml: default_system {default_system!r} is not a valid id")
        raw = data.get("systems") or []
        if not isinstance(raw, list):
            raise ConfigError("systems.yaml: systems must be a list")
        systems = []
        for i, item in enumerate(raw, 1):
            where = f"system #{i}"
            if not isinstance(item, dict):
                raise ConfigError(f"systems.yaml: {where} must be a mapping")
            sid = str(item.get("id") or "")
            if not ID_RE.match(sid):
                raise ConfigError(
                    f"systems.yaml: {where}: id {sid!r} must be lowercase letters, digits, - or _"
                )
            hosts = item.get("hosts", [])
            if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
                raise ConfigError(f"systems.yaml: {sid}: hosts must be a list of names or addresses")
            systems.append(
                System(
                    id=sid,
                    label=str(item.get("label") or sid),
                    kind=str(item.get("kind") or "other"),
                    hosts=tuple(h.strip() for h in hosts if h.strip() and not is_placeholder(h)),
                    source_tz=_tz(str(item.get("source_tz") or "UTC"), sid),
                    expected=bool(item.get("expected", True)),
                )
            )
        return cls(systems, default_system)

    @classmethod
    def load(cls, path: Path) -> "Systems":
        return cls.from_mapping(load_mapping(path))

    def resolve(self, source_ip: str, header_host: str | None = None) -> Resolved:
        for name in (source_ip, header_host):
            system = self._by_host.get((name or "").lower())
            if system is not None:
                return Resolved(system.id, True, system.source_tz)
        return Resolved(self.default_system, False, self.default_tz)
