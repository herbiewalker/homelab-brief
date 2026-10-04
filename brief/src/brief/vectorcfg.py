"""Render vector/vector.yaml for this container start.

Vector 0.58 does not interpolate environment variables by default, and the sender allowlist is a
list, so the container renders the template once at start: the listen port (SYSLOG_PORT, else
ports.syslog in brief/network.yaml, else 514) and the allowed senders (allowed_senders and
unknown_senders in the same file). Everything written into the output is validated first, so a bad
config stops the container with a clear message instead of silently weakening the allowlist.
"""

import ipaddress
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

import yaml

SENDERS_BLOCK = re.compile(
    r"(?P<indent>[ \t]*)# BEGIN rendered:senders\n.*?[ \t]*# END rendered:senders\n", re.DOTALL
)
ADDRESS_LINE = re.compile(r'(address: "0\.0\.0\.0:)\d+(")')
POLICIES = ("flag", "drop")


class ConfigError(ValueError):
    pass


def load_network(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def syslog_port(network: Mapping, env: Mapping[str, str]) -> int:
    raw = (env.get("SYSLOG_PORT") or "").strip() or (network.get("ports") or {}).get("syslog") or 514
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"syslog port must be a number, got {raw!r}") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"syslog port out of range: {port}")
    return port


def allowed_cidrs(network: Mapping) -> list[str]:
    entries = network.get("allowed_senders") or []
    if not isinstance(entries, list):
        raise ConfigError("allowed_senders must be a list of addresses or subnets")
    cidrs = []
    for entry in entries:
        try:
            cidrs.append(str(ipaddress.ip_network(str(entry).strip(), strict=False)))
        except ValueError:
            raise ConfigError(f"allowed_senders: not an address or subnet: {entry!r}") from None
    return cidrs


def unknown_policy(network: Mapping) -> str:
    policy = str(network.get("unknown_senders") or "flag").strip().lower()
    if policy not in POLICIES:
        raise ConfigError(f"unknown_senders must be one of {POLICIES}, got {policy!r}")
    return policy


def render(template: str, port: int, cidrs: list[str], policy: str) -> str:
    if not SENDERS_BLOCK.search(template):
        raise ConfigError("vector.yaml has no rendered:senders block, so the allowlist cannot be applied")
    if len(ADDRESS_LINE.findall(template)) < 2:
        raise ConfigError("vector.yaml should have a UDP and a TCP address line to render the port into")

    def senders(m: re.Match) -> str:
        i = m.group("indent")
        return (
            f"{i}# BEGIN rendered:senders\n"
            f"{i}allowed_senders = {json.dumps(cidrs)}\n"
            f"{i}unknown_policy = {json.dumps(policy)}\n"
            f"{i}# END rendered:senders\n"
        )

    out = SENDERS_BLOCK.sub(senders, template)
    return ADDRESS_LINE.sub(lambda m: f"{m.group(1)}{port}{m.group(2)}", out)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    base = Path(env.get("BRIEF_CONFIG_DIR", "/config"))  # user config: brief/network.yaml
    template_path = Path(env.get("BRIEF_VECTOR_DIR", "/app/vector")) / "vector.yaml"
    network_path = base / "brief" / "network.yaml"
    out_path = Path(env.get("VECTOR_RENDERED", "/run/vector/vector.yaml"))
    try:
        network = load_network(network_path)
        port = syslog_port(network, env)
        cidrs = allowed_cidrs(network)
        policy = unknown_policy(network)
        rendered = render(template_path.read_text(encoding="utf-8"), port, cidrs, policy)
    except (ConfigError, yaml.YAMLError, OSError) as exc:
        print(f"vectorcfg: {exc}", file=sys.stderr)
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    print(f"vectorcfg: port={port} allowed_senders={len(cidrs)} unknown_senders={policy} -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
