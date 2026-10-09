"""devices.yaml (private) as a lookup, and the privacy policy that follows from it.

Each device is listed by MAC with a label, an owner, a profile, and optional quiet hours. The profile decides
what the product may keep about that device (docs/guide/06-phase2-device-intelligence.md):

  full           presence timeline, DNS detail (domains and categories), odd-hours checks, traffic, new-device
  security_only  that the device exists and when (new-device), and its traffic volume (traffic-spike); no DNS,
                 no activity timeline. The default for a device that is not listed.
  ignore         nothing at all: no client row, no traffic, no DNS, no presence

`quiet_hours` are UTC like every other time in the config files.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from brief.config import ConfigError, config_file, load_mapping

DEFAULT_PROFILE = "security_only"
_NON_HEX = re.compile(r"[^0-9a-f]")


def norm_mac(value) -> str | None:
    """Lowercase colon-separated MAC (00:00:5e:00:53:01), or None when it is not a MAC."""
    digits = _NON_HEX.sub("", str(value or "").lower())
    if len(digits) != 12:
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


@dataclass(frozen=True)
class Device:
    mac: str
    label: str
    owner: str
    profile: str
    quiet_start: int | None = None  # minutes after midnight UTC
    quiet_end: int | None = None

    def in_quiet_hour(self, hour: int) -> bool:
        """True if the whole UTC hour `hour` falls inside the quiet window (which may wrap midnight)."""
        if self.quiet_start is None or self.quiet_end is None:
            return False
        start, end = self.quiet_start, self.quiet_end
        begin, finish = hour * 60, hour * 60 + 60
        if start <= end:
            return start <= begin and finish <= end
        return begin >= start or finish <= end


def _minutes(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


class DeviceBook:
    def __init__(self, devices: Mapping[str, Device] | None = None):
        self.devices = dict(devices or {})

    @classmethod
    def from_mapping(cls, data: Mapping) -> "DeviceBook":
        out: dict[str, Device] = {}
        for item in data.get("devices") or []:
            mac = norm_mac(item.get("mac"))
            if mac is None:
                continue
            quiet = item.get("quiet_hours") or {}
            out[mac] = Device(
                mac=mac,
                label=str(item.get("label") or "").strip(),
                owner=str(item.get("owner") or "").strip(),
                profile=str(item.get("profile") or DEFAULT_PROFILE),
                quiet_start=_minutes(quiet["start"]) if quiet.get("start") else None,
                quiet_end=_minutes(quiet["end"]) if quiet.get("end") else None,
            )
        return cls(out)

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> "DeviceBook":
        """devices.yaml (or its last good copy). A missing or unreadable file is an empty book, so each device
        gets the default profile: the safe direction."""
        try:
            return cls.from_mapping(load_mapping(config_file("devices.yaml", env)))
        except (ConfigError, OSError, ValueError, TypeError):
            return cls()

    def profile_of(self, mac: str) -> str:
        device = self.devices.get(norm_mac(mac) or "")
        return device.profile if device else DEFAULT_PROFILE

    def get(self, mac: str) -> Device | None:
        return self.devices.get(norm_mac(mac) or "")

    def known(self, mac: str) -> bool:
        return (norm_mac(mac) or "") in self.devices

    def label(self, mac: str, hostname: str | None = None, vendor: str | None = None) -> str:
        """What to call a device in a finding: its label, else its hostname, else its vendor, else the last
        two octets of the MAC (enough to find it, not enough to identify the owner by itself)."""
        device = self.get(mac)
        if device and device.label:
            return device.label
        if hostname:
            return hostname
        tail = (norm_mac(mac) or "????")[-5:]
        return f"{vendor} device …{tail}" if vendor else f"unknown device …{tail}"


@dataclass(frozen=True)
class ClientPolicy:
    """What the ingest may do with client activity found in the logs."""

    book: DeviceBook
    logs: bool = False  # off unless the UniFi collector is on and `client_logs` is true

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> "ClientPolicy":
        try:
            unifi = (load_mapping(config_file("collectors.yaml", env)).get("collectors") or {}).get(
                "unifi"
            ) or {}
        except (ConfigError, OSError, ValueError, TypeError, AttributeError):
            unifi = {}
        logs = bool(unifi.get("enabled")) and unifi.get("client_logs") is True
        return cls(DeviceBook.load(env), logs)
