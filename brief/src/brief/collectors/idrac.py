"""The iDRAC collector: power draw, temperatures, fans, and the hardware event log of a Dell server.

Two ways in, chosen by `protocol:` in collectors.yaml, both read-only and both producing the same sensors:

  ipmi     `ipmitool` over the network (IPMI-over-LAN, "lanplus"). This is the one an older iDRAC (7, in 12th
           generation PowerEdge servers) supports; it has no Redfish. The password goes to ipmitool in the
           environment (`-E`), never on the command line.
  redfish  HTTPS and JSON with HTTP Basic authentication, for iDRAC 8 and 9: the standard Chassis Power and
           Thermal resources and the system event log. Standard library only.

The numbers: `idrac_power_w`, `idrac_temp_max_c`, `idrac_inlet_temp_c`, `idrac_fan_min_rpm`,
`idrac_sensors_not_ok` (sensors reporting a warning or critical state), and `idrac_sel_entries` (size of the
hardware event log). Power is also what Home Assistant could read from a smart plug; use whichever is better.
"""

import base64
import os
import re
from dataclasses import dataclass

from brief.collectors import proc
from brief.collectors.base import Collector, CollectorError, Context
from brief.collectors.framework import register
from brief.collectors.httpjson import ApiError, request_json, tls_context
from brief.config import ConfigError
from brief.jsonlog import log

PROTOCOLS = ("ipmi", "redfish")
RUN = proc.run  # replaced in tests
FETCH = request_json  # replaced in tests
BAD_STATUS = {"nc", "cr", "nr", "lnc", "lcr", "lnr", "unc", "ucr", "unr"}
METRICS = (
    ("power_w", "power draw", "W"),
    ("temp_max_c", "hottest sensor", "°C"),
    ("inlet_temp_c", "inlet (room) temperature", "°C"),
    ("fan_min_rpm", "slowest fan", "RPM"),
    ("sensors_not_ok", "sensors in a warning or critical state", None),
    ("sel_entries", "hardware event log entries", None),
)


@dataclass
class Sensor:
    name: str
    status: str
    value: float | None
    unit: str


# ---------------------------------------------------------------- parsing (pure)

_READING = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(.*)$")


def parse_sdr(text: str) -> list[Sensor]:
    """`ipmitool sdr elist` lines: name | id | status | entity | reading."""
    out = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5 or not parts[0]:
            continue
        reading = _READING.match(parts[4])
        out.append(
            Sensor(
                parts[0],
                parts[2].lower(),
                float(reading.group(1)) if reading else None,
                reading.group(2).strip() if reading else parts[4],
            )
        )
    return out


def parse_sel_entries(text: str) -> int | None:
    found = re.search(r"^\s*Entries\s*:\s*(\d+)", text, re.MULTILINE)
    return int(found.group(1)) if found else None


def parse_dcmi_power(text: str) -> float | None:
    found = re.search(r"Instantaneous power reading:\s*(\d+(?:\.\d+)?)\s*Watts", text)
    return float(found.group(1)) if found else None


def summarize(sensors: list[Sensor], dcmi_power: float | None = None) -> dict[str, float]:
    """The sensor list as the product's numbers."""
    out: dict[str, float] = {}
    temps = [s for s in sensors if s.value is not None and "degrees c" in s.unit.lower()]
    if temps:
        out["temp_max_c"] = max(s.value for s in temps)
        inlet = [s.value for s in temps if "inlet" in s.name.lower() or "ambient" in s.name.lower()]
        if inlet:
            out["inlet_temp_c"] = inlet[0]
    fans = [s.value for s in sensors if s.value is not None and s.unit.upper().startswith("RPM")]
    if fans:
        out["fan_min_rpm"] = min(fans)
    watts = [s for s in sensors if s.value is not None and s.unit.lower().startswith("watt")]
    power = next((s.value for s in watts if "consumption" in s.name.lower()), None)
    if power is None:
        power = next(
            (
                s.value
                for s in watts
                if re.search(r"pwr|power", s.name, re.I) and "limit" not in s.name.lower()
            ),
            None,
        )
    if power is None:
        power = dcmi_power
    if power is not None:
        out["power_w"] = power
    out["sensors_not_ok"] = sum(1 for s in sensors if s.status in BAD_STATUS)
    return out


def summarize_redfish(power: dict, thermal: dict, sel_count: int | None) -> dict[str, float]:
    out: dict[str, float] = {}
    control = (power.get("PowerControl") or [{}])[0] if isinstance(power.get("PowerControl"), list) else {}
    watts = control.get("PowerConsumedWatts")
    if isinstance(watts, (int, float)):
        out["power_w"] = float(watts)
    temps = [
        t for t in thermal.get("Temperatures") or [] if isinstance(t.get("ReadingCelsius"), (int, float))
    ]
    if temps:
        out["temp_max_c"] = float(max(t["ReadingCelsius"] for t in temps))
        inlet = [
            t["ReadingCelsius"] for t in temps if re.search(r"inlet|ambient", str(t.get("Name", "")), re.I)
        ]
        if inlet:
            out["inlet_temp_c"] = float(inlet[0])
    fans = [
        f["Reading"]
        for f in thermal.get("Fans") or []
        if isinstance(f.get("Reading"), (int, float)) and str(f.get("ReadingUnits", "RPM")).upper() == "RPM"
    ]
    if fans:
        out["fan_min_rpm"] = float(min(fans))
    items = [
        *(thermal.get("Temperatures") or []),
        *(thermal.get("Fans") or []),
        *(power.get("PowerSupplies") or []),
    ]
    out["sensors_not_ok"] = sum(
        1 for i in items if str((i.get("Status") or {}).get("Health", "OK")).upper() not in ("OK", "")
    )
    if sel_count is not None:
        out["sel_entries"] = float(sel_count)
    return out


# ---------------------------------------------------------------- the collector


@register
class IdracCollector(Collector):
    name = "idrac"

    def required_vars(self, config: dict) -> list[str]:
        return [str(config[k]) for k in ("host_var", "user_var", "password_var") if config.get(k)]

    def validate(self, config: dict) -> None:
        if config.get("protocol", "redfish") not in PROTOCOLS:
            raise ConfigError(f"protocol must be one of {', '.join(PROTOCOLS)}")
        if config.get("enabled"):
            for key in ("host_var", "user_var", "password_var"):
                if not config.get(key):
                    raise ConfigError(f"{key} is required (the template variable holding it)")
        if "verify_tls" in config and not isinstance(config["verify_tls"], bool):
            raise ConfigError("verify_tls must be true or false")
        cipher = config.get("cipher_suite")
        if cipher is not None and not (isinstance(cipher, int) and 0 <= cipher <= 20):
            raise ConfigError("cipher_suite must be a whole number (3 is the usual iDRAC value)")
        if "privilege" in config and str(config["privilege"]) not in (
            "CALLBACK",
            "USER",
            "OPERATOR",
            "ADMINISTRATOR",
        ):
            raise ConfigError("privilege must be USER, OPERATOR, or ADMINISTRATOR")
        for key in ("chassis_id", "manager_id"):
            if key in config and not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", str(config[key])):
                raise ConfigError(f"{key} may use letters, digits, dots, hyphens, and underscores only")

    def metrics(self, config: dict) -> list[dict]:
        return [
            {
                "id": f"idrac_{suffix}",
                "name": f"iDRAC: {label}",
                "group": "idrac",
                **({"unit": u} if u else {}),
            }
            for suffix, label, u in METRICS
        ]

    # ---- IPMI

    @staticmethod
    def _ipmi_argv(ctx: Context) -> tuple[list[str], dict[str, str]]:
        argv = [
            "ipmitool", "-I", "lanplus", "-H", ctx.var("host_var"), "-U", ctx.var("user_var"), "-E",
            "-L", str(ctx.config.get("privilege", "USER")), "-N", "3", "-R", "1",
        ]  # fmt: skip
        if ctx.config.get("cipher_suite") is not None:
            argv += ["-C", str(ctx.config["cipher_suite"])]
        if ctx.config.get("port"):
            argv += ["-p", str(int(ctx.config["port"]))]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "IPMI_PASSWORD": ctx.var("password_var")}
        return argv, env

    def _read_ipmi(self, ctx: Context) -> dict[str, float]:
        argv, env = self._ipmi_argv(ctx)
        try:
            sensors = parse_sdr(RUN([*argv, "sdr", "elist"], env, 30))
            if not sensors:
                raise CollectorError("the iDRAC returned no sensors")
            power = None
            if not any(s.unit.lower().startswith("watt") for s in sensors):
                try:  # not every firmware lists power as a sensor; DCMI has a direct reading
                    power = parse_dcmi_power(RUN([*argv, "dcmi", "power", "reading"], env, 15))
                except proc.ProcError:
                    power = None
            numbers = summarize(sensors, power)
            try:
                entries = parse_sel_entries(RUN([*argv, "sel", "info"], env, 15))
            except proc.ProcError:
                entries = None
            if entries is not None:
                numbers["sel_entries"] = float(entries)
            return numbers
        except proc.ProcError as exc:
            log("idrac_ipmi_failed", reason=exc.message, detail=exc.detail)
            raise CollectorError(exc.message) from None

    # ---- Redfish

    def _read_redfish(self, ctx: Context) -> dict[str, float]:
        host = ctx.var("host_var")
        base = host if host.startswith(("http://", "https://")) else f"https://{host}"
        token = base64.b64encode(f"{ctx.var('user_var')}:{ctx.var('password_var')}".encode()).decode()
        headers = {"authorization": f"Basic {token}"}
        context = tls_context(base, bool(ctx.config.get("verify_tls", True)))
        chassis = str(ctx.config.get("chassis_id", "System.Embedded.1"))
        manager = str(ctx.config.get("manager_id", "iDRAC.Embedded.1"))

        def get(path: str) -> dict:
            data = FETCH(f"{base.rstrip('/')}{path}", headers, 15.0, context)
            if not isinstance(data, dict):
                raise ApiError("bad_response", "the reply was not an object")
            return data

        try:
            power = get(f"/redfish/v1/Chassis/{chassis}/Power")
            thermal = get(f"/redfish/v1/Chassis/{chassis}/Thermal")
            try:
                sel = get(f"/redfish/v1/Managers/{manager}/LogServices/Sel/Entries").get(
                    "Members@odata.count"
                )
            except ApiError as exc:
                if exc.kind == "auth":
                    raise
                sel = None
        except ApiError as exc:
            log("idrac_redfish_failed", kind=exc.kind, reason=exc.message)
            message = "the iDRAC credentials were rejected" if exc.kind == "auth" else exc.message
            raise CollectorError(message) from None
        return summarize_redfish(power, thermal, sel if isinstance(sel, int) else None)

    # ---- the contract

    def _read(self, ctx: Context) -> dict[str, float]:
        if ctx.config.get("protocol", "redfish") == "ipmi":
            return self._read_ipmi(ctx)
        return self._read_redfish(ctx)

    def test(self, ctx: Context) -> str:
        numbers = self._read(ctx)
        parts = [f"{k}={v:g}" for k, v in sorted(numbers.items())]
        return f"the iDRAC answered over {ctx.config.get('protocol', 'redfish')}: " + ", ".join(parts)

    def run(self, ctx: Context) -> dict[str, float | str]:
        return {f"idrac_{k}": v for k, v in self._read(ctx).items()}
