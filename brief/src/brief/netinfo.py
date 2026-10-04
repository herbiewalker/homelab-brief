"""What network is this container on, and what address should devices send syslog to?

Docker (the Unraid template) decides the container's mode and IP; the container only reports what it
sees and warns when something looks wrong. Modes:

  own_ip   a LAN address of its own (br0 or another macvlan/ipvlan network): devices use that address
  host     shares the host's network: devices use the host's address, ports must not clash with Unraid
  bridge   a Docker-internal address (172.16.0.0/12) behind port mapping: devices use the HOST's
           address, and the container cannot know it, so `advertised_address` should be set

network.yaml `mode` overrides detection when it is wrong. The address is read from the routing table
with a UDP connect, which sends no packet.
"""

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

DOCKER_RANGE = ipaddress.ip_network("172.16.0.0/12")
MODES = ("own_ip", "host", "bridge")


def local_address() -> str | None:
    """The address this container uses to reach the LAN, or None when it has no route."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # documentation range; connect on UDP only picks a route
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def interfaces(root: Path = Path("/sys/class/net")) -> list[str]:
    try:
        return sorted(p.name for p in root.iterdir())
    except OSError:
        return []


def in_docker_range(address: str | None) -> bool:
    try:
        return bool(address) and ipaddress.ip_address(address) in DOCKER_RANGE
    except ValueError:
        return False


def detect_mode(address: str | None, names: list[str]) -> str:
    # a container in host mode sees the host's own bridges and physical NICs, not just eth0 and lo
    if any(n == "docker0" or n.startswith("br-") or n == "br0" for n in names):
        return "host"
    return "bridge" if in_docker_range(address) else "own_ip"


def network_info(settings) -> dict:
    """describe() for a Settings object (mode and advertised address from network.yaml)."""
    return describe(settings.network_mode, settings.advertised, {"syslog": settings.syslog_port})


def with_detected_address(settings, net: dict | None = None):
    """Settings with advertised_address filled in from the detected LAN address, but only in own-IP
    mode and only when none is configured (in host or bridge mode the container cannot know it)."""
    net = net or network_info(settings)
    if not settings.advertised and net["mode"] == "own_ip" and net["address"]:
        return replace(settings, advertised=net["address"])
    return settings


def describe(
    configured_mode: str = "auto",
    advertised: str = "",
    ports: dict | None = None,
    address_fn: Callable[[], str | None] = local_address,
    names: list[str] | None = None,
) -> dict:
    """{mode, detected_mode, address, advertised, send_syslog_to, warnings} for logs and status.json."""
    address = address_fn()
    detected = detect_mode(address, interfaces() if names is None else names)
    mode = configured_mode if configured_mode in MODES else detected
    warnings = []
    if configured_mode in MODES and configured_mode != detected:
        warnings.append(f"network.yaml says {configured_mode} but this looks like {detected}")
    if address is None:
        warnings.append("no network route found; the container cannot be reached from the LAN")
    if mode == "bridge" and not advertised:
        warnings.append(
            "bridge mode: devices must send syslog to the host's address and the mapped port; "
            "set advertised_address in network.yaml so links and certificates use it"
        )
    if mode == "host" and (ports or {}).get("syslog") == 514:
        warnings.append(
            "host mode on port 514 can clash with Unraid's own syslog server; use 5514 if it does"
        )
    send_to = advertised or (address if mode == "own_ip" else None)
    return {
        "mode": mode,
        "detected_mode": detected,
        "address": address,
        "advertised": advertised or None,
        "send_syslog_to": send_to,
        "warnings": warnings,
    }
