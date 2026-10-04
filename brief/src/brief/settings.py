"""Runtime settings shared by the web servers, the health check, and the CLI.

Ports come from brief/network.yaml, and an environment variable overrides each one (so the first
start can be tuned before the file exists), which lets the same image run in own-IP, host, and
bridge modes.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from brief.config import config_file, load_mapping
from brief.vectorcfg import syslog_port


def _port(env: Mapping[str, str], var: str, configured, default: int) -> int:
    raw = (env.get(var) or "").strip() or configured or default
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{var} must be a number, got {raw!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"{var} out of range: {port}")
    return port


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    http_port: int = 80
    https_port: int = 443
    https_public_port: int = 443  # what a browser should use when the template maps a different port
    syslog_port: int = 514
    interval: float = 60.0
    advertised: str = ""
    network_mode: str = "auto"
    extra_names: tuple[str, ...] = ()
    announce_setup: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        network = load_mapping(config_file("network.yaml", env))
        ports = network.get("ports") or {}
        https = _port(env, "HTTPS_PORT", ports.get("https"), 443)
        return cls(
            state_dir=Path(env.get("BRIEF_STATE", "/state")),
            http_port=_port(env, "HTTP_PORT", ports.get("http"), 80),
            https_port=https,
            https_public_port=_port(env, "HTTPS_PUBLIC_PORT", None, https),
            syslog_port=syslog_port(network, env),
            interval=float(env.get("BRIEF_INGEST_INTERVAL", "60")),
            advertised=str(network.get("advertised_address") or "").strip(),
            network_mode=str(network.get("mode") or "auto").strip().lower(),
            extra_names=tuple(str(n) for n in (network.get("tls_extra_names") or [])),
        )
