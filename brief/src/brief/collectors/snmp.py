"""The SNMP collector: numbers from switches, UPSes, and anything else that answers SNMP.

Uses the net-snmp command line tools in the image (`snmpget`, `snmpbulkwalk`) with numeric OIDs, so no MIB
files are needed. SNMPv3 is preferred (authentication and encryption); v2c is allowed on a trusted LAN. The
credentials never appear on a command line: they are written to a throwaway `snmp.conf` (mode 600, in a
private temporary folder, deleted afterwards) that the tools read through SNMPCONFPATH.

Each target (`targets:` in collectors.yaml) has a `profile` that decides what is read:

  ups      RFC 1628 UPS-MIB: charge, minutes remaining, load, input voltage, power, on battery, battery ok
  switch   uptime, ports up and total, and interface errors since the last poll
  custom   the OIDs you list (`oids:`), each a number with an optional scale, for a PDU or anything else

Targets are independent: one that does not answer is reported as `snmp_<id>_reachable` 0 and the rest update.
Nothing is ever written to a device (only `snmpget` and `snmpbulkwalk` are used).
"""

import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from brief.collectors import proc
from brief.collectors.base import Collector, CollectorError, Context
from brief.collectors.framework import register
from brief.config import ConfigError
from brief.jsonlog import log

TARGET_ID = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
OID = re.compile(r"^\.?[0-9]+(\.[0-9]+)+$")
PROFILES = ("switch", "ups", "custom")
AUTH_PROTOCOLS = ("MD5", "SHA", "SHA-224", "SHA-256", "SHA-384", "SHA-512")
PRIV_PROTOCOLS = ("DES", "AES", "AES-192", "AES-256")
VERSIONS = ("v3", "v2c")
RUN = proc.run  # replaced in tests
TOOL_OPTIONS = ["-On", "-Oq", "-Ot", "-Oe", "-t", "3", "-r", "1"]  # numeric OIDs, plain values, 3 s x 2 tries
IF_OPER_STATUS, IF_IN_ERRORS, IF_OUT_ERRORS = (
    "1.3.6.1.2.1.2.2.1.8",
    "1.3.6.1.2.1.2.2.1.14",
    "1.3.6.1.2.1.2.2.1.20",
)
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
UPS = {  # RFC 1628 (UPS-MIB), single-line UPS
    "battery_status": "1.3.6.1.2.1.33.1.2.1.0",  # 2 = normal, 3 = low, 4 = depleted
    "minutes_remaining": "1.3.6.1.2.1.33.1.2.3.0",
    "charge_pct": "1.3.6.1.2.1.33.1.2.4.0",
    "output_source": "1.3.6.1.2.1.33.1.4.1.0",  # 5 = on battery
    "input_v": "1.3.6.1.2.1.33.1.3.3.1.3.1",
    "load_pct": "1.3.6.1.2.1.33.1.4.4.1.5.1",
    "power_w": "1.3.6.1.2.1.33.1.4.4.1.4.1",
}
PROFILE_METRICS = {
    "ups": (
        ("charge_pct", "battery charge", "%"),
        ("minutes_remaining", "minutes of battery left", "min"),
        ("load_pct", "load", "%"),
        ("input_v", "input voltage", "V"),
        ("power_w", "output power", "W"),
        ("on_battery", "on battery (1 = yes)", None),
        ("battery_ok", "battery normal (1 = yes)", None),
    ),
    "switch": (
        ("uptime_h", "uptime", "h"),
        ("ports_up", "ports up", None),
        ("ports_total", "ports", None),
        ("if_errors", "interface errors since the last poll", "errors"),
    ),
}


def slug(target_id: str) -> str:
    return target_id.replace("-", "_")


@dataclass
class Target:
    id: str
    host: str
    profile: str
    port: int | None = None
    oids: list[dict] = field(default_factory=list)


def targets_of(config: dict) -> list[Target]:
    out = []
    for item in config.get("targets") or []:
        if isinstance(item, dict):
            out.append(
                Target(
                    str(item.get("id", "")),
                    str(item.get("host", "")),
                    str(item.get("profile", "")),
                    int(item["port"]) if isinstance(item.get("port"), int) else None,
                    [o for o in item.get("oids") or [] if isinstance(o, dict)],
                )
            )
    return out


# ---------------------------------------------------------------- parsing (pure)

_MISSING = ("no such", "no more variables", "end of mib")


def parse_output(text: str) -> dict[str, str]:
    """`.1.3.6... value` lines (snmpget / snmpbulkwalk with -On -Oq -Ot -Oe) -> {oid without the leading dot:
    value}. An OID the device does not have is left out."""
    out = {}
    for line in text.splitlines():
        match = re.match(r"^\.?(\d+(?:\.\d+)+)\s+(.*)$", line.strip())
        if not match:
            continue
        value = match.group(2).strip().strip('"')
        if value.lower().startswith(_MISSING):
            continue
        out[match.group(1)] = value
    return out


def number(value: str | None) -> float | None:
    """The leading number of a value ("3600", "12.5", "230 V"), or None."""
    if value is None:
        return None
    found = re.match(r"^\s*(-?\d+(?:\.\d+)?)", value)
    return float(found.group(1)) if found else None


def enum_value(value: str | None) -> float | None:
    """A number from "up(1)" or "1" (net-snmp prints enumerations as name(number) unless -Oe is honoured)."""
    if value is None:
        return None
    inside = re.search(r"\((-?\d+)\)", value)
    return float(inside.group(1)) if inside else number(value)


def column(values: dict[str, str], base: str) -> list[str]:
    """The values of one table column, from a walk of `base`."""
    prefix = base + "."
    return [v for oid, v in values.items() if oid.startswith(prefix)]


def ups_numbers(values: dict[str, str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in ("charge_pct", "minutes_remaining", "load_pct", "input_v", "power_w"):
        n = number(values.get(UPS[key]))
        if n is not None:
            out[key] = n
    source = enum_value(values.get(UPS["output_source"]))
    if source is not None:
        out["on_battery"] = 1 if source == 5 else 0
    status = enum_value(values.get(UPS["battery_status"]))
    if status is not None:
        out["battery_ok"] = 1 if status == 2 else 0
    return out


def switch_numbers(uptime: str | None, status: dict[str, str], errors: float | None) -> dict[str, float]:
    out: dict[str, float] = {}
    ticks = number(uptime)
    if ticks is not None:
        out["uptime_h"] = round(ticks / 100 / 3600, 1)  # TimeTicks are hundredths of a second
    states = [enum_value(v) for v in column(status, IF_OPER_STATUS)]
    states = [s for s in states if s is not None]
    if states:
        out["ports_total"] = len(states)
        out["ports_up"] = sum(1 for s in states if s == 1)
    if errors is not None:
        out["if_errors"] = errors
    return out


def custom_numbers(oids: list[dict], values: dict[str, str]) -> dict[str, float]:
    out = {}
    for item in oids:
        n = number(values.get(str(item["oid"]).lstrip(".")))
        if n is not None:
            out[str(item["name"])] = round(n * float(item.get("scale", 1)), 3)
    return out


# ---------------------------------------------------------------- the collector


def _safe_secret(value: str) -> str:
    if any(c in value for c in "\r\n\0"):
        raise CollectorError("an SNMP credential contains a line break")
    return value


def snmp_conf(config: dict, env) -> str:
    """The text of the private snmp.conf for these settings (the caller keeps it out of argv and logs)."""
    if config.get("version", "v3") == "v2c":
        community = _safe_secret((env.get(str(config.get("community_var"))) or "").strip())
        return f"defVersion 2c\ndefCommunity {community}\n"
    user = _safe_secret((env.get(str(config.get("user_var"))) or "").strip())
    auth = _safe_secret((env.get(str(config.get("auth_key_var"))) or "").strip())
    priv = _safe_secret((env.get(str(config.get("priv_key_var"))) or "").strip())
    lines = ["defVersion 3", f"defSecurityName {user}"]
    if auth and priv:
        lines += [
            "defSecurityLevel authPriv",
            f"defAuthType {config.get('auth_protocol', 'SHA')}",
            f"defAuthPassphrase {auth}",
            f"defPrivType {config.get('priv_protocol', 'AES')}",
            f"defPrivPassphrase {priv}",
        ]
    elif auth:
        lines += [
            "defSecurityLevel authNoPriv",
            f"defAuthType {config.get('auth_protocol', 'SHA')}",
            f"defAuthPassphrase {auth}",
        ]
    else:
        lines += ["defSecurityLevel noAuthNoPriv"]
    return "\n".join(lines) + "\n"


@contextmanager
def private_session(config: dict, env) -> Iterator[dict[str, str]]:
    """A temporary folder holding snmp.conf, and the environment that makes the tools read only that."""
    with tempfile.TemporaryDirectory(prefix="hb-snmp-") as folder:
        os.chmod(folder, 0o700)
        conf = Path(folder) / "snmp.conf"
        conf.touch(mode=0o600)
        conf.write_text(snmp_conf(config, env), encoding="utf-8")
        yield {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": folder,
            "SNMPCONFPATH": folder,
            "SNMP_PERSISTENT_DIR": folder,
            "MIBS": "",
        }


def _address(target: Target) -> str:
    return f"udp:{target.host}:{target.port}" if target.port else target.host


@register
class SnmpCollector(Collector):
    name = "snmp"

    def required_vars(self, config: dict) -> list[str]:
        if config.get("version", "v3") == "v2c":
            return [str(config["community_var"])] if config.get("community_var") else []
        return [str(config[k]) for k in ("user_var", "auth_key_var") if config.get(k)]

    def validate(self, config: dict) -> None:
        version = config.get("version", "v3")
        if version not in VERSIONS:
            raise ConfigError(f"version must be one of {', '.join(VERSIONS)}")
        if config.get("enabled"):
            needed = ("community_var",) if version == "v2c" else ("user_var", "auth_key_var")
            for key in needed:
                if not config.get(key):
                    raise ConfigError(
                        f"{key} is required for SNMP {version} (the template variable holding it)"
                    )
        if config.get("auth_protocol", "SHA") not in AUTH_PROTOCOLS:
            raise ConfigError(f"auth_protocol must be one of {', '.join(AUTH_PROTOCOLS)}")
        if config.get("priv_protocol", "AES") not in PRIV_PROTOCOLS:
            raise ConfigError(f"priv_protocol must be one of {', '.join(PRIV_PROTOCOLS)}")
        seen = set()
        for target in targets_of(config):
            if not TARGET_ID.match(target.id):
                raise ConfigError(f"target id {target.id!r} must be lowercase letters, digits and hyphens")
            if slug(target.id) in seen:
                raise ConfigError(f"target id {target.id!r} appears twice")
            seen.add(slug(target.id))
            if not target.host or (config.get("enabled") and "<" in target.host):
                raise ConfigError(f"target {target.id}: set host to the device's address")
            if target.profile not in PROFILES:
                raise ConfigError(f"target {target.id}: profile must be one of {', '.join(PROFILES)}")
            if target.profile == "custom":
                self._check_oids(target)

    @staticmethod
    def _check_oids(target: Target) -> None:
        if not target.oids:
            raise ConfigError(f"target {target.id}: a custom profile needs oids")
        names = set()
        for item in target.oids:
            if not NAME.match(str(item.get("name", ""))) or str(item["name"]) in names:
                raise ConfigError(f"target {target.id}: each oid needs a unique lowercase name")
            names.add(str(item["name"]))
            if not OID.match(str(item.get("oid", ""))):
                raise ConfigError(
                    f"target {target.id}: {item['name']} needs a numeric oid like 1.3.6.1.2.1.1.3.0"
                )
            try:
                float(item.get("scale", 1))
            except (TypeError, ValueError):
                raise ConfigError(f"target {target.id}: {item['name']} scale must be a number") from None

    def metrics(self, config: dict) -> list[dict]:
        out = []
        for t in targets_of(config):
            base = f"snmp_{slug(t.id)}_"
            out.append({"id": base + "reachable", "name": f"{t.id}: answers SNMP (1 = yes)", "group": "snmp"})
            if t.profile == "custom":
                for o in t.oids:
                    entry = {
                        "id": base + str(o.get("name")),
                        "name": f"{t.id}: {o.get('label') or o.get('name')}",
                        "group": "snmp",
                    }
                    if o.get("unit"):
                        entry["unit"] = str(o["unit"])
                    out.append(entry)
            for suffix, label, unit in PROFILE_METRICS.get(t.profile, ()):
                entry = {"id": base + suffix, "name": f"{t.id}: {label}", "group": "snmp"}
                if unit:
                    entry["unit"] = unit
                out.append(entry)
        return out

    # ---- talking to a device

    def _get(self, session: dict, config: dict, target: Target, oids: list[str]) -> dict[str, str]:
        return parse_output(
            RUN(["snmpget", *TOOL_OPTIONS, *self._version_flags(config), _address(target), *oids], session)
        )

    def _walk(self, session: dict, config: dict, target: Target, oid: str) -> dict[str, str]:
        argv = ["snmpbulkwalk", *TOOL_OPTIONS, *self._version_flags(config), _address(target), oid]
        return parse_output(RUN(argv, session))

    @staticmethod
    def _version_flags(config: dict) -> list[str]:
        return ["-v", "2c"] if config.get("version", "v3") == "v2c" else ["-v", "3"]

    def _read(self, session: dict, config: dict, target: Target, previous_errors: float | None):
        """The numbers for one target, and the interface error total to remember for next time."""
        if target.profile == "ups":
            return ups_numbers(self._get(session, config, target, list(UPS.values()))), None
        if target.profile == "custom":
            wanted = [str(o["oid"]).lstrip(".") for o in target.oids]
            return custom_numbers(target.oids, self._get(session, config, target, wanted)), None
        base = self._get(session, config, target, [SYS_UPTIME])
        status = self._walk(session, config, target, IF_OPER_STATUS)
        total = 0.0
        for oid in (IF_IN_ERRORS, IF_OUT_ERRORS):
            counts = (number(v) for v in column(self._walk(session, config, target, oid), oid))
            total += sum(n for n in counts if n is not None)
        delta = (
            None
            if previous_errors is None
            else (total - previous_errors if total >= previous_errors else total)
        )
        return switch_numbers(base.get(SYS_UPTIME), status, delta), total

    def test(self, ctx: Context) -> str:
        targets = targets_of(ctx.config)
        if not targets:
            raise CollectorError("no targets are listed in collectors.yaml")
        lines, answered = [], 0
        with private_session(ctx.config, ctx.env) as session:
            for t in targets:
                try:
                    numbers, _ = self._read(session, ctx.config, t, None)
                except proc.ProcError as exc:
                    lines.append(f"{t.id}: {exc.message}")
                    continue
                if not numbers:
                    lines.append(
                        f"{t.id}: answered, but none of the expected values exist (check the profile)"
                    )
                    continue
                answered += 1
                lines.append(f"{t.id}: answered ({len(numbers)} values)")
        if not answered:
            raise CollectorError("no target could be read:\n" + "\n".join(lines))
        return f"{answered} of {len(targets)} targets answered\n" + "\n".join(lines)

    def run(self, ctx: Context) -> dict[str, float | str]:
        numbers: dict[str, float | str] = {}
        remembered: dict[str, float] = {}
        answered = 0
        with ctx.store.read() as db:
            before = {
                r["key"]: float(r["value"])
                for r in db.execute("SELECT key, value FROM meta WHERE key LIKE 'snmp:%'")
            }
        with private_session(ctx.config, ctx.env) as session:
            for t in targets_of(ctx.config):
                prefix = f"snmp_{slug(t.id)}_"
                numbers[prefix + "reachable"] = 0
                try:
                    got, total = self._read(session, ctx.config, t, before.get(f"snmp:{t.id}:if_errors"))
                except proc.ProcError as exc:
                    log("snmp_target_failed", target=t.id, reason=exc.message, detail=exc.detail)
                    continue
                if not got:
                    log("snmp_target_empty", target=t.id, profile=t.profile)
                    continue
                answered += 1
                numbers[prefix + "reachable"] = 1
                numbers.update({prefix + k: v for k, v in got.items()})
                if total is not None:
                    remembered[f"snmp:{t.id}:if_errors"] = total
        if remembered:
            with ctx.store.write() as tx:
                for key, value in remembered.items():
                    tx.set_meta(key, str(value))
        if not answered:
            raise CollectorError("no SNMP target could be read")
        return numbers
