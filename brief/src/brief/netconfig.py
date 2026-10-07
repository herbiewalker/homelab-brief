"""brief/network.yaml: what is allowed in it, and how the Network page changes it.

`validate` is the rule the config guard and the config editor both apply, so a bad port or address never
reaches the pipeline. `patch` changes a few scalar settings inside the file's text and leaves everything
else, including comments, as the user wrote it (the file is documentation as much as configuration).
"""

import ipaddress
import re
from collections.abc import Mapping

from brief.config import ConfigError
from brief.netinfo import MODES

PORT_KEYS = ("syslog", "http", "https", "ssh")
UNKNOWN_POLICIES = ("flag", "drop")
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def valid_address(text: str) -> bool:
    """An IP address or a host name (no scheme, port, or path)."""
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return bool(HOSTNAME.match(text))


def validate(data: Mapping) -> None:
    """Raise ConfigError for anything the product could not run with."""
    mode = str(data.get("mode") or "auto")
    if mode != "auto" and mode not in MODES:
        raise ConfigError(f"network.yaml: mode must be auto, {', '.join(MODES)} (got {mode!r})")
    advertised = str(data.get("advertised_address") or "").strip()
    if advertised and not valid_address(advertised):
        raise ConfigError("network.yaml: advertised_address must be an IP address or host name, with no port")
    ports = data.get("ports") or {}
    if not isinstance(ports, Mapping):
        raise ConfigError("network.yaml: ports must be a mapping")
    values: dict[str, int] = {}
    for key, raw in ports.items():
        if key not in PORT_KEYS:
            raise ConfigError(f"network.yaml: unknown port setting {key!r} (use {', '.join(PORT_KEYS)})")
        raw = int(raw) if isinstance(raw, str) and raw.strip().isdigit() else raw
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 65535:
            raise ConfigError(f"network.yaml: ports.{key} must be a number from 1 to 65535 (got {raw!r})")
        values[key] = raw
    if len(set(values.values())) != len(values):
        raise ConfigError("network.yaml: syslog, http, https, and ssh each need their own port")
    policy = str(data.get("unknown_senders") or "flag")
    if policy not in UNKNOWN_POLICIES:
        raise ConfigError(f"network.yaml: unknown_senders must be one of {', '.join(UNKNOWN_POLICIES)}")
    names = data.get("tls_extra_names") or []
    if not isinstance(names, list) or not all(isinstance(n, str) and valid_address(n) for n in names):
        raise ConfigError("network.yaml: tls_extra_names must be a list of IP addresses or host names")
    senders = data.get("allowed_senders") or []
    if not isinstance(senders, list):
        raise ConfigError("network.yaml: allowed_senders must be a list")
    for entry in senders:
        try:
            ipaddress.ip_network(str(entry), strict=False)
        except ValueError:
            raise ConfigError(
                f"network.yaml: allowed_senders entry {entry!r} is not an address or network"
            ) from None


# ---------------------------------------------------------------- changing a few values in the text


def _line(text: str, pattern: str, replacement: str) -> str:
    """Replace the value on the line matching `pattern` and keep its trailing comment."""
    rx = re.compile(pattern + r"(?P<tail>\s*(#.*)?)$", re.MULTILINE)
    if not rx.search(text):
        raise ConfigError("That setting is not in your network.yaml as a plain line; use the config editor.")
    return rx.sub(lambda m: m.group("head") + replacement + m.group("tail"), text, count=1)


def patch(text: str, changes: Mapping) -> str:
    """The file text with the given settings changed: mode, advertised_address, unknown_senders, and
    ports as {syslog, http, https, ssh}. Anything else in the file is left alone."""
    if "mode" in changes:
        text = _line(text, r"(?P<head>^mode:[ \t]*)[^#\n]*?", str(changes["mode"]))
    if "advertised_address" in changes:
        text = _line(
            text, r"(?P<head>^advertised_address:[ \t]*)[^#\n]*?", f'"{changes["advertised_address"]}"'
        )
    if "unknown_senders" in changes:
        text = _line(text, r"(?P<head>^unknown_senders:[ \t]*)[^#\n]*?", str(changes["unknown_senders"]))
    for key, value in (changes.get("ports") or {}).items():
        text = _line(text, rf"(?P<head>^[ \t]+{re.escape(key)}:[ \t]*)[0-9]+", str(int(value)))
    return text
