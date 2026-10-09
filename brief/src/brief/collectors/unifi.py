"""The UniFi collector: network devices, their firmware, and how they are connected.

Reads the UniFi Network Integration API with a read-only key (collectors.yaml `unifi`, the console address
and the key come from the template variables named in `url_var` and `key_var`) and writes:

  inventory     one row per device: model, running firmware, and whether an update is available
                (the API reports a yes/no flag, not the version; `available_since` records when the flag
                first appeared, which `version_behind` uses)
  topo_node/edge  every device and its uplink, for the network map; a device seen for the first time later
                feeds the `new_infrastructure` finding

Firmware is also read from the `<mac>,<model>-<firmware>` token that switches, access points, and PDUs put
in front of every syslog line (stored by the ingest as `device_header`). That fallback runs on every pass,
so inventory has firmware even when the API cannot be reached or no key is set.

Clients, traffic, and DNS are Task 21; this collector never reads them.
"""

import json
import re
from dataclasses import dataclass
from datetime import timedelta

from brief import clients
from brief.collectors import topology
from brief.collectors.base import Collector, CollectorError, Context
from brief.collectors.framework import register
from brief.collectors.httpjson import ApiError
from brief.collectors.unifi_api import UnifiApi
from brief.config import ConfigError, config_file, load_mapping
from brief.detectors_hw import UPDATE_FLAG
from brief.devices import DeviceBook, norm_mac
from brief.jsonlog import log
from brief.store import iso

API_FACTORY = UnifiApi  # replaced in tests
GATEWAY = re.compile(r"^(udm|ucg|uxg|udr|udw|usg|ucgf|uck)", re.IGNORECASE)
SWITCH = re.compile(r"^(usw|usl|us-)", re.IGNORECASE)
ACCESS_POINT = re.compile(r"^(u6|u7|uap|uwb|e7|ubb|uk-ultra|unifi ?ap)", re.IGNORECASE)
PDU = re.compile(r"^(usp|pdu)", re.IGNORECASE)
HEADER_MAC = re.compile(r"^[0-9a-f]{12}$")
METRICS = (
    ("devices", "devices", None),
    ("devices_offline", "devices offline", None),
    ("updates_available", "firmware updates available", None),
    ("header_devices", "devices with firmware from log headers", None),
)


def mac12(value) -> str | None:
    """A MAC address as twelve lowercase hex digits, or None."""
    text = re.sub(r"[^0-9a-f]", "", str(value or "").lower())
    return text if len(text) == 12 else None


def kind_of(model: str, features: list | None = None) -> str:
    """gateway, switch, ap, pdu, or device. The model name decides; `features` only breaks ties."""
    for kind, pattern in (("gateway", GATEWAY), ("switch", SWITCH), ("ap", ACCESS_POINT), ("pdu", PDU)):
        if pattern.match(model or ""):
            return kind
    names = {str(f).lower() for f in features or []}
    if "accesspoint" in names:
        return "ap"
    if "switching" in names:
        return "switch"
    return "device"


@dataclass
class Device:
    api_id: str
    mac: str
    name: str
    model: str
    firmware: str
    online: bool
    updatable: bool
    kind: str
    uplink_mac: str | None = None


def parse_devices(items: list[dict]) -> list[Device]:
    out = []
    for row in items:
        mac = mac12(row.get("macAddress"))
        if not mac or not row.get("id"):
            continue
        model = str(row.get("model") or "").strip()
        out.append(
            Device(
                api_id=str(row["id"]),
                mac=mac,
                name=str(row.get("name") or model or mac).strip(),
                model=model,
                firmware=str(row.get("firmwareVersion") or "").strip(),
                online=str(row.get("state") or "").upper() == "ONLINE",
                updatable=row.get("firmwareUpdatable") is True,
                kind=kind_of(model, row.get("features") if isinstance(row.get("features"), list) else None),
            )
        )
    return out


@dataclass
class Client:
    mac: str  # colon form
    hostname: str | None = None
    vendor: str | None = None
    vlan: str | None = None
    tx: int | None = None  # byte counters since the device joined, from the access point's side
    rx: int | None = None

    def merged_over(self, base: "Client | None") -> "Client":
        """This record (richer) with any field it lacks taken from `base`."""
        if base is None:
            return self
        return Client(
            self.mac,
            self.hostname or base.hostname,
            self.vendor or base.vendor,
            self.vlan or base.vlan,
            self.tx if self.tx is not None else base.tx,
            self.rx if self.rx is not None else base.rx,
        )


def parse_clients(items: list[dict]) -> list[Client]:
    """Connected clients from the Integration API list. A client without a MAC (a VPN session) is skipped."""
    out = []
    for row in items:
        mac = norm_mac(row.get("macAddress"))
        if mac:
            out.append(Client(mac, str(row.get("name") or "").strip() or None))
    return out


def _int(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_stations(payload: dict) -> list[Client]:
    """Clients from the older `stat/sta` endpoint, which also has vendor, network name, and byte counters."""
    out = []
    for row in payload.get("data") or []:
        mac = norm_mac(row.get("mac")) if isinstance(row, dict) else None
        if mac:
            name = str(row.get("hostname") or row.get("name") or "").strip() or None
            out.append(
                Client(
                    mac, name, str(row.get("oui") or "").strip() or None,
                    str(row.get("network") or "").strip() or None,
                    _int(row.get("tx_bytes")),
                    _int(row.get("rx_bytes")),
                )
            )  # fmt: skip
    return out


def _rollup_days(env) -> int:
    try:
        return max(7, int(load_mapping(config_file("retention.yaml", env)).get("rollup_after_days", 90)))
    except (ConfigError, OSError, ValueError, TypeError):
        return 90


def _config(config: dict, key: str, default):
    value = config.get(key)
    return default if value in (None, "") else value


@register
class UnifiCollector(Collector):
    name = "unifi"

    def required_vars(self, config: dict) -> list[str]:
        return []  # the log-header firmware needs no key; a missing key is reported after that step

    def validate(self, config: dict) -> None:
        for key in ("url_var", "key_var"):
            if config.get("enabled") and not config.get(key):
                raise ConfigError(f"{key} is required (the template variable holding the {key[:-4]})")
        path = str(_config(config, "api_path", "/proxy/network/integration"))
        if not path.startswith("/") or ".." in path or "?" in path:
            raise ConfigError("api_path must start with / (for example /proxy/network/integration)")
        if "verify_tls" in config and not isinstance(config["verify_tls"], bool):
            raise ConfigError("verify_tls must be true or false")
        if "site" in config and not isinstance(config["site"], str):
            raise ConfigError("site must be text (a site name; leave out for every site)")
        for key in ("clients", "traffic", "client_logs"):
            if key in config and not isinstance(config[key], bool):
                raise ConfigError(f"{key} must be true or false")
        if config.get("traffic") and not config.get("clients"):
            raise ConfigError("traffic needs clients: true")
        path = str(_config(config, "traffic_path", "/proxy/network/api/s/{site}/stat/sta"))
        if not path.startswith("/") or ".." in path or "?" in path or path.count("{site}") != 1:
            raise ConfigError("traffic_path must start with / and contain {site} once")

    def metrics(self, config: dict) -> list[dict]:
        declared = [
            {
                "id": f"unifi_{suffix}",
                "name": f"UniFi: {label}",
                "group": "unifi",
                **({"unit": u} if u else {}),
            }
            for suffix, label, u in METRICS
        ]
        if config.get("clients"):
            declared.append({"id": "unifi_clients", "name": "UniFi: clients connected", "group": "unifi"})
        if config.get("traffic"):
            declared.append(
                {
                    "id": "unifi_traffic_ok",
                    "name": "UniFi: client traffic readable (1 = yes)",
                    "group": "unifi",
                }
            )
        return declared

    # ---- connecting

    def _api(self, ctx: Context) -> UnifiApi:
        url, key = ctx.var("url_var"), ctx.var("key_var")  # CollectorError names the variable if it is unset
        return API_FACTORY(
            url,
            key,
            api_path=str(_config(ctx.config, "api_path", "/proxy/network/integration")),
            timeout=float(_config(ctx.config, "request_timeout_s", 10)),
            verify_tls=bool(_config(ctx.config, "verify_tls", True)),
        )

    @staticmethod
    def _sites(api: UnifiApi, wanted: str | None) -> list[dict]:
        sites = [s for s in api.paged("/v1/sites") if s.get("id")]
        if wanted:
            sites = [s for s in sites if wanted in (s.get("name"), s.get("internalReference"))]
        if not sites:
            raise CollectorError(
                "no matching UniFi site was found" if wanted else "the console reported no sites"
            )
        return sites

    def test(self, ctx: Context) -> str:
        try:
            api = self._api(ctx)
            version = ""
            try:
                version = str(api.get("/v1/info").get("applicationVersion") or "")
            except ApiError as exc:
                if exc.kind in ("auth", "unreachable"):
                    raise
            sites = self._sites(api, ctx.config.get("site"))
            devices = [d for s in sites for d in parse_devices(api.paged(f"/v1/sites/{s['id']}/devices"))]
        except ApiError as exc:
            raise CollectorError(exc.message) from None
        pending = sum(d.updatable for d in devices)
        return (
            f"UniFi Network {version or '(version not reported)'}: {len(sites)} site(s), "
            f"{len(devices)} devices, {pending} with a firmware update available"
        )

    # ---- the run

    def run(self, ctx: Context) -> dict[str, float | str]:
        api_failure: CollectorError | None = None
        seen_macs: set[str] = set()
        numbers: dict[str, float | str] = {}
        try:
            api = self._api(ctx)
            devices, application, sites = self._fetch(api, ctx.config.get("site"))
            seen_macs = {d.mac for d in devices}
            self._store(ctx, devices, application)
            numbers = {
                "unifi_devices": len(devices),
                "unifi_devices_offline": sum(not d.online for d in devices),
                "unifi_updates_available": sum(d.updatable for d in devices),
            }
            if ctx.config.get("clients"):
                numbers.update(self._clients(ctx, api, sites))
        except ApiError as exc:
            api_failure = CollectorError(exc.message)
            log("unifi_api_failed", kind=exc.kind, reason=exc.message)
        except CollectorError as exc:
            api_failure = exc
        header_count = self._from_headers(ctx, seen_macs)
        if api_failure is not None:
            raise api_failure  # the log-header firmware is already stored; the run still reports the failure
        numbers["unifi_header_devices"] = header_count
        return numbers

    def _fetch(self, api: UnifiApi, site: str | None) -> tuple[list[Device], str, list[dict]]:
        try:
            application = str(api.get("/v1/info").get("applicationVersion") or "")
        except ApiError as exc:
            if exc.kind in ("auth", "unreachable"):
                raise
            application = ""
        devices: list[Device] = []
        sites = self._sites(api, site)
        for s in sites:
            site_devices = parse_devices(api.paged(f"/v1/sites/{s['id']}/devices"))
            by_api_id = {d.api_id: d for d in site_devices}
            for d in site_devices:
                try:  # the list has no uplink; the device's own record does
                    uplink = (api.get(f"/v1/sites/{s['id']}/devices/{d.api_id}").get("uplink") or {}).get(
                        "deviceId"
                    )
                except ApiError as exc:
                    if exc.kind in ("auth", "unreachable"):
                        raise
                    uplink = None
                d.uplink_mac = by_api_id[uplink].mac if uplink in by_api_id else None
            devices += site_devices
        return devices, application, sites

    # ---- clients (Task 21), every write gated by the device's profile

    def _clients(self, ctx: Context, api: UnifiApi, sites: list[dict]) -> dict[str, float | str]:
        found: dict[str, Client] = {}
        for s in sites:
            for client in parse_clients(api.paged(f"/v1/sites/{s['id']}/clients")):
                found[client.mac] = client
        numbers: dict[str, float | str] = {"unifi_clients": len(found)}
        if ctx.config.get("traffic"):
            numbers["unifi_traffic_ok"] = 0
            template = str(_config(ctx.config, "traffic_path", "/proxy/network/api/s/{site}/stat/sta"))
            try:
                for s in sites:
                    ref = str(s.get("internalReference") or str(s.get("name") or "default").lower())
                    for client in parse_stations(api.get_root(template.format(site=ref))):
                        base = found.get(client.mac)
                        found[client.mac] = client.merged_over(base)
                numbers["unifi_traffic_ok"] = 1
            except ApiError as exc:
                log("unifi_traffic_failed", kind=exc.kind, reason=exc.message)
        self._store_clients(
            ctx, found, bool(ctx.config.get("traffic")) and numbers.get("unifi_traffic_ok") == 1
        )
        return numbers

    @staticmethod
    def _store_clients(ctx: Context, found: dict[str, "Client"], with_traffic: bool) -> None:
        """Write what each device's profile allows. An `ignore` device is not stored at all, not even its
        traffic counters; a `security_only` device gets a client row and traffic but no presence minutes."""
        book = DeviceBook.load(ctx.env)
        now = iso(ctx.now)
        day, hour = now[:10], ctx.now.hour
        with ctx.store.write() as tx:
            db = tx.db
            row = db.execute("SELECT value FROM meta WHERE key = 'unifi:traffic'").fetchone()
            try:
                counters = json.loads(row["value"]) if row else {}
            except ValueError:
                counters = {}
            kept: dict[str, list[int]] = {}
            for mac, c in found.items():
                profile = book.profile_of(mac)
                if profile == "ignore":
                    continue
                clients.touch_client(db, mac, now, now, c.hostname, c.vendor, c.vlan)
                up = down = 0
                if with_traffic and c.tx is not None and c.rx is not None:
                    before = counters.get(mac)
                    kept[mac] = [c.tx, c.rx]
                    if before:  # the first sighting has no earlier counter, so it adds nothing
                        down = c.tx - before[0] if c.tx >= before[0] else c.tx  # a smaller counter: it reset
                        up = c.rx - before[1] if c.rx >= before[1] else c.rx
                online = 60 if profile == "full" else 0
                if up or down or online:
                    clients.add_hourly(db, day, hour, mac, up, down, online)
            if with_traffic:
                tx.set_meta("unifi:traffic", json.dumps(kept))
            today = now[:10]
            if tx.get_meta("clients:last_rollup") != today:
                cutoff = (ctx.now - timedelta(days=_rollup_days(ctx.env))).strftime("%Y-%m-%d")
                clients.rollup(db, cutoff)
                tx.set_meta("clients:last_rollup", today)

    # ---- storing

    @staticmethod
    def _inventory(db, item_id, kind, name, model, version, available, now) -> None:
        db.execute(
            "INSERT INTO inventory (item_id, kind, name, model, current_version, available_version, "
            "checked_at, "
            "available_since) VALUES (?, ?, ?, ?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE ? END) "
            "ON CONFLICT (item_id) DO UPDATE SET kind = excluded.kind, name = excluded.name, "
            "model = excluded.model, current_version = excluded.current_version, "
            "available_version = excluded.available_version, checked_at = excluded.checked_at, "
            "available_since = CASE WHEN excluded.available_version IS NULL THEN NULL "
            "WHEN inventory.available_version IS NULL THEN excluded.checked_at "
            "ELSE inventory.available_since END",
            (item_id, kind, name, model, version, available, now, available, now),
        )

    def _store(self, ctx: Context, devices: list[Device], application: str) -> None:
        now = iso(ctx.now)
        with ctx.store.write() as tx:
            db = tx.db
            if application:
                self._inventory(
                    db, "unifi:app", "app", "UniFi Network application", "", application, None, now
                )
            for d in devices:
                node = f"unifi:{d.mac}"
                self._inventory(
                    db, node, d.kind, d.name, d.model, d.firmware, UPDATE_FLAG if d.updatable else None, now
                )
                topology.upsert_node(
                    db,
                    node,
                    d.kind,
                    d.name,
                    None,
                    {"model": d.model, "firmware": d.firmware, "online": d.online},
                    now,
                )
                if d.uplink_mac:
                    topology.upsert_edge(db, node, f"unifi:{d.uplink_mac}", "uplink", now)

    @staticmethod
    def _from_headers(ctx: Context, skip: set[str]) -> int:
        """Firmware from the syslog header token, for every device the API did not cover this pass."""
        now = iso(ctx.now)
        done = 0
        with ctx.store.write() as tx:
            rows = tx.db.execute("SELECT mac, model, firmware FROM device_header").fetchall()
            for row in rows:
                mac = mac12(row["mac"])
                if not mac or mac in skip:
                    continue
                kind = kind_of(row["model"])
                tx.db.execute(
                    "INSERT INTO inventory (item_id, kind, name, model, current_version, available_version, "
                    "checked_at, available_since) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL) "
                    "ON CONFLICT (item_id) DO UPDATE SET current_version = excluded.current_version, "
                    "checked_at = excluded.checked_at",
                    (f"unifi:{mac}", kind, row["model"], row["model"], row["firmware"], now),
                )
                done += 1
        return done
