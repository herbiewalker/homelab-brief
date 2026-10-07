"""What the Network page shows, as plain data: the detected mode, the warnings that go with it, whether
each port answers, where each expected sender stands, the settings to copy into the Unraid template,
and the result of the live syslog test.

The container never changes its own Docker network; this page reports and explains, and the only thing it
can change is brief/network.yaml (through the config editor's rules, in admin_network.py).
"""

import json
import os
import socket
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

from brief import detect, detectors, netinfo
from brief.config import ConfigError, config_file, load_mapping
from brief.store import Store, parse_time
from brief.systems import Systems

SEARCH_BYTES = 1_000_000  # how much of the end of an events file the syslog test reads
SEARCH_FILES = 3
SINGLE_SENDER_SHARE = 0.9  # one sender carrying this share of the traffic looks like a rewriting gateway
SINGLE_SENDER_MIN_EVENTS = 20
SUGGESTED = {"syslog": 5514, "http": 8480, "https": 8443}  # for host mode, away from Unraid's own ports
UNRAID = {"syslog": 514, "http": 80, "https": 443}


# ---------------------------------------------------------------- warnings that need the database


def lost_sender_warning(store: Store, now: datetime) -> str | None:
    """The bridge problem: Docker rewrites every sender to its own gateway address, so host identity is
    lost. Seen as one Docker-internal address carrying almost all the traffic."""
    low = now - timedelta(hours=24)
    key = f"{low:%Y-%m-%d}{low.hour:02d}"
    with store.read() as db:
        rows = db.execute(
            "SELECT host, SUM(n) AS n FROM ("
            "SELECT host, n FROM hourly WHERE day || printf('%02d', hour) >= ? "
            "UNION ALL SELECT host, n FROM suppressed WHERE day || printf('%02d', hour) >= ?) "
            "GROUP BY host ORDER BY n DESC",
            (key, key),
        ).fetchall()
    total = sum(r["n"] for r in rows)
    if not rows or total < SINGLE_SENDER_MIN_EVENTS:
        return None
    top = rows[0]
    if (
        netinfo.in_docker_range(top["host"])
        and top["host"].endswith(".1")
        and top["n"] >= SINGLE_SENDER_SHARE * total
    ):
        return (
            f"Almost all syslog ({round(100 * top['n'] / total)}%) appears to come from {top['host']}, "
            "a Docker "
            "gateway address: the bridge is rewriting the real sender addresses, so devices cannot be told "
            "apart. Switch to own-IP or host mode (see the template settings below)."
        )
    return None


def port_warnings(mode: str, ports: Mapping[str, int]) -> list[str]:
    """Host mode shares the Unraid host's ports, so the defaults can collide with Unraid's own services."""
    if mode != "host":
        return []
    out = []  # (the syslog port 514 clash is already warned about by netinfo.describe, for status.json too)
    if ports.get("http") == UNRAID["http"]:
        out.append(f"Host mode on port 80 clashes with the Unraid web UI; use {SUGGESTED['http']}.")
    if ports.get("https") == UNRAID["https"]:
        out.append(f"Host mode on port 443 clashes with the Unraid web UI; use {SUGGESTED['https']}.")
    return out


def notes(mode: str) -> list[str]:
    if mode == "own_ip":
        return [
            "The Unraid host itself can send syslog to this container only if Docker settings has "
            '"Host access to custom networks" turned on. If the host server never shows up under '
            "Sources, check that."
        ]
    if mode == "bridge":
        return ["Devices must send to the Unraid host's address and the port mapped in the template."]
    return []


# ---------------------------------------------------------------- what is listening


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        socket.create_connection((host, port), timeout=0.5).close()
        return True
    except OSError:
        return False


def listening(ports: Mapping[str, int], probe=port_open) -> list[dict]:
    names = {"syslog": "Syslog receiver (TCP)", "http": "Read-only views (HTTP)", "https": "Admin (HTTPS)"}
    return [{"name": names[k], "port": ports[k], "ok": bool(probe(ports[k]))} for k in names if k in ports]


# ---------------------------------------------------------------- repointing status


def repointing(systems: Systems, store: Store, now: datetime) -> list[dict]:
    """Every expected sender listed in systems.yaml, and whether it is reaching this container."""
    with store.read() as db:
        seen = {r["host"]: r for r in db.execute("SELECT host, last_seen, expected_gap_min FROM host")}
    rows = []
    for system in systems.systems:
        if not system.expected:
            continue
        for entry in system.hosts:
            row = seen.get(entry)
            if row is None:
                state, last = "not seen", None
            else:
                last = row["last_seen"]
                limit = detectors.silent_limit(row["expected_gap_min"], detect.thresholds()["absence"])
                state = "receiving" if now - parse_time(last) <= limit else "silent"
            rows.append({"system": system.label, "entry": entry, "state": state, "last_seen": last})
    return rows


# ---------------------------------------------------------------- template settings per mode


def template_settings(current: str, address: str | None, ports: Mapping[str, int]) -> list[dict]:
    """The Unraid template fields for each mode, as lines to copy. `current` is highlighted."""
    ip = address or "<a free address on your LAN>"
    host = "<the Unraid host's address>"
    return [
        {
            "mode": "own_ip",
            "title": "Own IP (recommended)",
            "current": current == "own_ip",
            "lines": [
                "Network Type: Custom: br0 (or your macvlan or ipvlan network)",
                f"Fixed IP address: {ip}",
                "Port mappings: none (the standard ports 514, 80 and 443 are on the container's own address)",
                f"Devices send syslog to: {ip}, port 514",
                'Docker settings: turn on "Host access to custom networks" '
                "so the Unraid host itself can send syslog",
            ],
        },
        {
            "mode": "host",
            "title": "Host",
            "current": current == "host",
            "lines": [
                "Network Type: Host",
                f"In network.yaml set the ports away from Unraid's own: syslog {SUGGESTED['syslog']}, "
                f"http {SUGGESTED['http']}, https {SUGGESTED['https']}",
                f"Devices send syslog to: {host}, port {SUGGESTED['syslog']}",
                "Docker settings: nothing extra",
            ],
        },
        {
            "mode": "bridge",
            "title": "Bridge",
            "current": current == "bridge",
            "lines": [
                "Network Type: Bridge",
                "Port mappings (host port to container port): "
                f"{ports.get('syslog', 514)} to {ports.get('syslog', 514)} "
                f"(UDP and TCP), {ports.get('http', 80)} to {ports.get('http', 80)}, "
                f"{ports.get('https', 443)} to {ports.get('https', 443)}",
                f"Devices send syslog to: {host}, the mapped syslog port",
                "Check this page's warnings: a bridge can hide the real sender addresses",
            ],
        },
    ]


# ---------------------------------------------------------------- the live syslog test


def find_test_line(state_dir: Path | str, token: str, now: datetime | None = None) -> dict | None:
    """The newest event whose message holds `token`, searched in the end of the newest events files.
    {source_ip, app, msg}, or None when it has not arrived (yet)."""
    folder = Path(state_dir) / "events"
    if not folder.exists():
        return None
    needle = token.encode("utf-8")
    files = sorted(folder.glob("*.ndjson"), key=lambda p: p.stat().st_mtime, reverse=True)[:SEARCH_FILES]
    for path in files:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(handle.tell() - SEARCH_BYTES, 0))
                tail = handle.read()
        except OSError:
            continue
        for line in reversed(tail.splitlines()):
            if needle in line:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                return {
                    "source_ip": str(event.get("source_ip", "")),
                    "app": str(event.get("app", "")),
                    "msg": str(event.get("msg", ""))[:200],
                }
    return None


def test_commands(target: str | None, port: int, token: str) -> list[str]:
    where = target or "<this container's address>"
    message = f"homelab-brief-test-{token}"
    return [
        f'logger -d -n {where} -P {port} "{message}"   # UDP',
        f'logger -T -n {where} -P {port} "{message}"   # TCP',
    ]


# ---------------------------------------------------------------- the settings the file asks for


def configured(env: Mapping[str, str] | None = None) -> dict:
    """The ports and advertised address as network.yaml says now (what a restart would use)."""
    try:
        data = load_mapping(config_file("network.yaml", env))
    except (ConfigError, OSError):
        return {}
    ports = data.get("ports") or {}
    return {
        "mode": str(data.get("mode") or "auto"),
        "advertised": str(data.get("advertised_address") or "").strip(),
        "unknown_senders": str(data.get("unknown_senders") or "flag"),
        "ports": {
            k: int(v)
            for k, v in ports.items()
            if k in ("syslog", "http", "https", "ssh") and str(v).isdigit()
        },
    }


def pending_restart(running: Mapping[str, int], advertised: str, want: Mapping) -> list[str]:
    """What a saved network.yaml changes that only a container restart applies."""
    out = []
    labels = {"syslog": "Syslog port", "http": "HTTP port", "https": "HTTPS port"}
    for key, label in labels.items():
        wanted = (want.get("ports") or {}).get(key)
        if wanted is not None and key in running and wanted != running[key]:
            out.append(f"{label}: running on {running[key]}, the file says {wanted}")
    if want and (want.get("advertised") or "") != (advertised or "") and want.get("advertised") is not None:
        running_as = advertised or "the detected one"
        asked_for = want["advertised"] or "the detected one"
        out.append(f"Advertised address: running with {running_as}, the file says {asked_for}")
    return out
