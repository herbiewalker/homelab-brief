"""The Unraid collector: array, parity history, disks, containers, VMs, notifications, and versions.

For each server in collectors.yaml (`servers:`, each with a URL and the name of a template variable that
holds a read-only API key) one run asks the Unraid GraphQL API for its sections and writes:

  disk / smart_reading   the disks, their overall SMART verdict and temperature, and (from smartctl files,
                         `smart_source: smartctl_files`) the attributes the API does not expose
  parity_check           every parity check the server has run (duration and speed)
  server_notice          unread warning and alert notifications
  inventory              OS, API, and plugin versions
  topo_node / topo_edge  the server, its containers and VMs, for the network map

and returns a few numbers per server (`unraid_<id>_...`) for Home Assistant and `/metrics`. Servers are
independent: one that cannot be reached is reported as `unraid_<id>_reachable` 0 and the others still
update. The detectors that read these tables are in `detectors_hw.py`.

Nothing here changes a server: only GraphQL queries are sent.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from brief.collectors import smartctl, topology
from brief.collectors.base import Collector, CollectorError, Context
from brief.collectors.framework import register
from brief.collectors.unraid_api import ApiError, UnraidApi
from brief.config import ConfigError
from brief.jsonlog import log
from brief.store import iso, parse_time

SERVER_ID = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
SMART_SOURCES = ("api", "smartctl_files")
API_FACTORY = UnraidApi  # replaced in tests
SMART_FILE_MAX_AGE_H = 72
SAMPLE_EVERY = 24 * 3600  # an unchanged SMART value is rewritten at most this often
FLASH = re.compile(r"flash drive|usb", re.IGNORECASE)
SOLID_STATE = re.compile(r"ssd|nvme|solid", re.IGNORECASE)

# What each section asks for. A field the server does not know is dropped (see unraid_api.py).
SECTIONS: dict[str, dict] = {
    "info": {"info": {"versions": {"core": {"unraid": None, "api": None, "kernel": None}}}},
    "array": {
        "array": {
            "state": None,
            "parities": {"name": None, "device": None, "status": None, "temp": None, "numErrors": None},
            "disks": {"name": None, "device": None, "status": None, "temp": None, "numErrors": None},
            "caches": {"name": None, "device": None, "status": None, "temp": None, "numErrors": None},
        }
    },
    "disks": {
        "disks": {
            "id": None,
            "device": None,
            "name": None,
            "serialNum": None,
            "size": None,
            "temperature": None,
            "smartStatus": None,
        }
    },
    "parity": {
        "parityHistory": {"date": None, "duration": None, "speed": None, "status": None, "errors": None}
    },
    "docker": {"docker": {"containers": {"id": None, "names": None, "image": None, "state": None}}},
    "vms": {"vms": {"domains": {"id": None, "name": None, "state": None}}},
    "notices": {
        "notifications": {
            "list(filter: {type: UNREAD, offset: 0, limit: 50})": {
                "id": None,
                "subject": None,
                "description": None,
                "importance": None,
                "timestamp": None,
            }
        }
    },
    "plugins": {"plugins": {"name": None, "version": None}},
}
OPTIONAL_SECTIONS = {"docker", "vms", "notices", "plugins", "parity"}  # a server may not have them

METRICS = (
    ("reachable", "reachable (1 = yes)", None),
    ("array_started", "array started (1 = yes)", None),
    ("disk_temp_max_c", "hottest disk", "°C"),
    ("disk_errors", "array disk errors", "errors"),
    ("parity_speed_mb_s", "last parity check speed", "MB/s"),
    ("parity_age_days", "days since last parity check", "days"),
    ("containers_running", "containers running", None),
    ("notices_open", "unread warnings", None),
)


def slug(server_id: str) -> str:
    return server_id.replace("-", "_")


# ---------------------------------------------------------------- parsing (pure)


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc(value) -> str | None:
    """An API timestamp (2026-10-02T21:49:00.000Z) as the store's UTC text, or None when it is not one."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return iso(moment if moment.tzinfo else moment.replace(tzinfo=UTC))


def _device(value) -> str:
    return str(value or "").removeprefix("/dev/").strip()


@dataclass
class Disk:
    serial: str
    model: str
    device: str
    size: int | None
    temperature: int | None
    smart_ok: bool | None
    role: str = "unassigned"
    errors: int = 0
    array_status: str | None = None
    rotational: bool | None = None
    attrs: dict = field(default_factory=dict)


def parse_disks(data: dict, array: dict) -> list[Disk]:
    """The API's disks, with each one's role and error count taken from the array section."""
    roles: dict[str, tuple[str, dict]] = {}
    for key, role in (("parities", "parity"), ("disks", "data"), ("caches", "cache")):
        for entry in (array.get("array") or {}).get(key) or []:
            if isinstance(entry, dict) and entry.get("device"):
                roles[_device(entry["device"])] = (role, entry)
    out = []
    for row in data.get("disks") or []:
        if not isinstance(row, dict) or not row.get("serialNum"):
            continue
        model = str(row.get("name") or "unknown").strip()
        device = _device(row.get("device"))
        role, entry = roles.get(device, ("unassigned", {}))
        if role == "unassigned" and FLASH.search(model):
            role = "flash"
        status = str(row.get("smartStatus") or "").upper()
        temp = _number(row.get("temperature"))
        if temp is None:
            temp = _number(entry.get("temp"))
        out.append(
            Disk(
                serial=str(row["serialNum"]).strip(),
                model=model,
                device=device,
                size=int(row["size"]) if _number(row.get("size")) is not None else None,
                temperature=int(temp) if temp is not None else None,
                smart_ok=False
                if "FAIL" in status
                else (True if status in ("OK", "PASSED", "HEALTHY") else None),
                role=role,
                errors=int(_number(entry.get("numErrors")) or 0),
                array_status=str(entry["status"]) if entry.get("status") else None,
                rotational=False if SOLID_STATE.search(model) else None,
            )
        )
    return out


def parse_parity(data: dict) -> list[dict]:
    out = []
    for row in data.get("parityHistory") or []:
        if not isinstance(row, dict) or not _utc(row.get("date")):
            continue
        out.append(
            {
                "date": _utc(row["date"]),
                "duration_s": int(_number(row.get("duration")) or 0),
                "speed_bps": int(_number(row.get("speed")) or 0),
                "status": str(row.get("status") or ""),
                "errors": int(_number(row.get("errors")) or 0),
            }
        )
    return out


def parse_containers(data: dict) -> list[dict]:
    out = []
    for row in (data.get("docker") or {}).get("containers") or []:
        if not isinstance(row, dict):
            continue
        names = row.get("names") or []
        name = str(names[0] if isinstance(names, list) and names else row.get("name") or "").lstrip("/")
        if name:
            out.append(
                {"name": name, "image": str(row.get("image") or ""), "state": str(row.get("state") or "")}
            )
    return out


def parse_vms(data: dict) -> list[dict]:
    out = []
    for row in (data.get("vms") or {}).get("domains") or []:
        if isinstance(row, dict) and row.get("name"):
            out.append({"name": str(row["name"]), "state": str(row.get("state") or "")})
    return out


def parse_notices(data: dict) -> list[dict]:
    block = data.get("notifications") or {}
    rows = next((v for k, v in block.items() if k.startswith("list")), None) or []
    out = []
    for row in rows:
        if (
            isinstance(row, dict)
            and row.get("id")
            and str(row.get("importance", "")).upper() in ("WARNING", "ALERT")
        ):
            out.append(
                {
                    "id": str(row["id"]),
                    "ts": _utc(row.get("timestamp")) or "",
                    "importance": str(row["importance"]).upper(),
                    "subject": str(row.get("subject") or "")[:200],
                    "description": str(row.get("description") or "")[:400],
                }
            )
    return out


def parse_versions(info: dict, plugins: dict) -> list[dict]:
    core = ((info.get("info") or {}).get("versions") or {}).get("core") or {}
    items = []
    for key, item_key, kind, name in (
        ("unraid", "os", "os", "Unraid OS"),
        ("api", "api", "api", "Unraid API"),
    ):
        if core.get(key):
            items.append(
                {
                    "key": item_key,
                    "kind": kind,
                    "name": name,
                    "version": str(core[key]),
                }
            )
    for plugin in plugins.get("plugins") or []:
        if isinstance(plugin, dict) and plugin.get("name"):
            items.append(
                {
                    "key": f"plugin:{plugin['name']}",
                    "kind": "plugin",
                    "name": str(plugin["name"]),
                    "version": str(plugin.get("version") or ""),
                }
            )
    return items


# ---------------------------------------------------------------- the collector


def _servers(config: dict) -> list[dict]:
    return [s for s in config.get("servers") or [] if isinstance(s, dict)]


@register
class UnraidCollector(Collector):
    name = "unraid"

    def required_vars(self, config: dict) -> list[str]:
        return []  # a missing key is reported for that server only

    def validate(self, config: dict) -> None:
        source = config.get("smart_source", "api")
        if source not in SMART_SOURCES:
            raise ConfigError(f"smart_source must be one of {', '.join(SMART_SOURCES)}")
        seen = set()
        for server in _servers(config):
            sid = str(server.get("id", ""))
            if not SERVER_ID.match(sid):
                raise ConfigError(f"server id {sid!r} must be lowercase letters, digits and hyphens")
            if slug(sid) in seen:
                raise ConfigError(f"server id {sid!r} appears twice")
            seen.add(slug(sid))
            if not server.get("key_var"):
                raise ConfigError(f"server {sid} needs key_var (the template variable holding its API key)")
            url = str(server.get("url", ""))
            if not url.startswith(("https://", "http://")) or "/graphql" not in url:
                raise ConfigError(f"server {sid}: url must look like https://<address>/graphql")
            if config.get("enabled") and "<" in url:
                raise ConfigError(f"server {sid}: replace the placeholder in url with the server's address")
            if "verify_tls" in server and not isinstance(server["verify_tls"], bool):
                raise ConfigError(f"server {sid}: verify_tls must be true or false")

    def metrics(self, config: dict) -> list[dict]:
        out = []
        for server in _servers(config):
            sid = str(server.get("id", ""))
            for suffix, label, unit in METRICS:
                entry = {"id": f"unraid_{slug(sid)}_{suffix}", "name": f"{sid}: {label}", "group": "unraid"}
                if unit:
                    entry["unit"] = unit
                out.append(entry)
            if config.get("smart_source") == "smartctl_files":
                out.append(
                    {
                        "id": f"unraid_{slug(sid)}_smart_files_age_h",
                        "name": f"{sid}: SMART files age",
                        "group": "unraid",
                        "unit": "h",
                    }
                )
        return out

    # ---- connection test: every section on every server, nothing stored

    def test(self, ctx: Context) -> str:
        lines, reached = [], 0
        for server in _servers(ctx.config):
            sid = str(server["id"])
            api = self._api(ctx, server)
            if isinstance(api, str):
                lines.append(f"{sid}: {api}")
                continue
            try:
                notes = self._probe(api)
            except ApiError as exc:
                lines.append(f"{sid}: {exc.message}")
                continue
            reached += 1
            lines.append(
                f"{sid}: reachable" + (f"; {'; '.join(notes)}" if notes else "; every section answered")
            )
        if not reached:
            raise CollectorError("no server could be read:\n" + "\n".join(lines))
        return f"{reached} of {len(lines)} servers answered\n" + "\n".join(lines)

    def _probe(self, api: UnraidApi) -> list[str]:
        notes = []
        for name, selection in SECTIONS.items():
            try:
                _, dropped = api.section(selection)
            except ApiError as exc:
                if name in OPTIONAL_SECTIONS and exc.kind == "graphql":
                    notes.append(f"{name}: not available ({exc.message[:60]})")
                    continue
                raise
            if dropped:
                notes.append(f"{name}: this server has no field(s) {', '.join(dropped)}")
        return notes

    # ---- the run

    def _api(self, ctx: Context, server: dict):
        """An API client for one server, or a short reason string when its key is missing."""
        key = (ctx.env.get(str(server["key_var"])) or "").strip()
        if not key:
            return f"the variable {server['key_var']} is not set"
        return API_FACTORY(
            str(server["url"]),
            key,
            timeout=float(ctx.config.get("request_timeout_s", 10)),
            verify_tls=bool(server.get("verify_tls", True)),
        )

    def run(self, ctx: Context) -> dict[str, float | str]:
        numbers: dict[str, float | str] = {}
        reached = 0
        for server in _servers(ctx.config):
            sid, prefix = str(server["id"]), f"unraid_{slug(str(server['id']))}_"
            numbers[prefix + "reachable"] = 0
            api = self._api(ctx, server)
            if isinstance(api, str):
                log("unraid_server_skipped", server=sid, reason=api)
                continue
            try:
                snapshot = self._fetch(api)
            except ApiError as exc:
                log("unraid_server_failed", server=sid, kind=exc.kind, reason=exc.message)
                continue
            extras = {}
            if ctx.config.get("smart_source") == "smartctl_files":
                extras = self._smart_files(ctx, sid, snapshot["disks"])
            self._store(ctx, sid, snapshot)
            reached += 1
            numbers.update({prefix + k: v for k, v in self._numbers(snapshot, ctx.now).items()})
            numbers.update({prefix + k: v for k, v in extras.items()})
            numbers[prefix + "reachable"] = 1
        if not reached:
            raise CollectorError("no Unraid server could be read")
        return numbers

    def _fetch(self, api: UnraidApi) -> dict:
        raw: dict[str, dict] = {}
        for name, selection in SECTIONS.items():
            try:
                raw[name], dropped = api.section(selection)
            except ApiError as exc:
                if name in OPTIONAL_SECTIONS and exc.kind == "graphql":
                    raw[name] = {}  # for example "VMs are not available" on a server without them
                    continue
                raise
            if dropped:
                log("unraid_fields_dropped", section=name, fields=",".join(dropped))
        disks = parse_disks(raw["disks"], raw["array"])
        return {
            "array_state": str(((raw["array"].get("array") or {}).get("state")) or "").upper(),
            "disks": disks,
            "parity": parse_parity(raw["parity"]),
            "containers": parse_containers(raw["docker"]),
            "vms": parse_vms(raw["vms"]),
            "notices": parse_notices(raw["notices"]),
            "versions": parse_versions(raw["info"], raw["plugins"]),
        }

    @staticmethod
    def _numbers(snapshot: dict, now: datetime) -> dict[str, float]:
        disks: list[Disk] = snapshot["disks"]
        temps = [d.temperature for d in disks if d.temperature is not None and d.role != "flash"]
        out: dict[str, float] = {
            "array_started": 1 if snapshot["array_state"] == "STARTED" else 0,
            "disk_errors": sum(d.errors for d in disks),
            "containers_running": sum(1 for c in snapshot["containers"] if c["state"].upper() == "RUNNING"),
            "notices_open": len(snapshot["notices"]),
        }
        if temps:
            out["disk_temp_max_c"] = max(temps)
        done = [p for p in snapshot["parity"] if p["status"].upper() == "COMPLETED"]
        if done:
            latest = max(done, key=lambda p: p["date"])
            out["parity_speed_mb_s"] = round(latest["speed_bps"] / 1e6, 1)
            out["parity_age_days"] = max(0, (now - parse_time(latest["date"])).days)
        return out

    # ---- SMART attribute files (the fallback for what the API does not expose)

    def _smart_files(self, ctx: Context, sid: str, disks: list[Disk]) -> dict[str, float]:
        folder = Path(str(ctx.config.get("smart_files_path", "/import-smart"))) / sid
        by_serial = {d.serial: d for d in disks}
        newest = 0.0
        if folder.is_dir():
            for path in sorted(folder.glob("*.json")):
                try:
                    reading = smartctl.parse(path.read_text(encoding="utf-8"))
                    newest = max(newest, path.stat().st_mtime)
                except (OSError, smartctl.SmartError, UnicodeDecodeError) as exc:
                    log("smart_file_skipped", server=sid, file=path.name, reason=str(exc)[:80])
                    continue
                disk = by_serial.get(reading.serial)
                if disk is None:  # a drive the API did not list (or the API is limited): still worth tracking
                    disk = Disk(reading.serial, reading.model, "", None, None, None)
                    disks.append(disk)
                    by_serial[reading.serial] = disk
                disk.attrs = reading.attrs
                if reading.passed is False:
                    disk.smart_ok = False
                elif reading.passed and disk.smart_ok is None:
                    disk.smart_ok = True
                if disk.temperature is None:
                    disk.temperature = reading.temperature
                if reading.rotational is not None:
                    disk.rotational = reading.rotational
        if not newest:
            return {}
        return {"smart_files_age_h": round((ctx.now.timestamp() - newest) / 3600, 1)}

    # ---- storing

    def _store(self, ctx: Context, sid: str, snapshot: dict) -> None:
        now = iso(ctx.now)
        node = f"unraid:{sid}"
        with ctx.store.write() as tx:
            db = tx.db
            for disk in snapshot["disks"]:
                disk_id = f"{sid}:{disk.serial}"
                db.execute(
                    "INSERT INTO disk (disk_id, server, model, serial, role, first_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (disk_id) DO UPDATE SET server = excluded.server, model = excluded.model, "
                    "role = excluded.role",
                    (disk_id, sid, disk.model, disk.serial, disk.role, now),
                )
                if disk.role != "flash":
                    self._readings(db, disk_id, disk, ctx.now)
            for p in snapshot["parity"]:
                db.execute(
                    "INSERT OR REPLACE INTO parity_check "
                    "(server, date, duration_s, speed_bps, status, errors) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (sid, p["date"], p["duration_s"], p["speed_bps"], p["status"], p["errors"]),
                )
            db.execute("DELETE FROM server_notice WHERE server = ?", (sid,))
            for n in snapshot["notices"]:
                db.execute(
                    "INSERT OR REPLACE INTO server_notice "
                    "(server, notice_id, ts, importance, subject, description) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (sid, n["id"], n["ts"], n["importance"], n["subject"], n["description"]),
                )
            for item in snapshot["versions"]:
                db.execute(
                    "INSERT INTO inventory (item_id, kind, name, model, current_version, available_version, "
                    "checked_at, available_since) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL) "
                    "ON CONFLICT (item_id) DO UPDATE SET name = excluded.name, "
                    "current_version = excluded.current_version, checked_at = excluded.checked_at",
                    (f"{node}:{item['key']}", item["kind"], item["name"], sid, item["version"], now),
                )
            self._topology(db, node, sid, snapshot, now)

    @staticmethod
    def _topology(db, node: str, sid: str, snapshot: dict, now: str) -> None:
        topology.upsert_node(db, node, "server", sid, None, {"os": "unraid"}, now)
        for c in snapshot["containers"]:
            child = f"{node}:ct:{c['name']}"
            attrs = {"state": c["state"], "image": c["image"]}
            topology.upsert_node(db, child, "container", c["name"], node, attrs, now)
            topology.upsert_edge(db, node, child, "hosts", now)
        for vm in snapshot["vms"]:
            child = f"{node}:vm:{vm['name']}"
            topology.upsert_node(db, child, "vm", vm["name"], node, {"state": vm["state"]}, now)
            topology.upsert_edge(db, node, child, "hosts", now)

    @staticmethod
    def _readings(db, disk_id: str, disk: Disk, now: datetime) -> None:
        """Temperature every run; the SMART verdict and attributes when they changed or a day has passed."""
        last = {
            row["attr"]: (row["raw"], parse_time(row["ts"]))
            for row in db.execute(
                "SELECT r.attr, r.raw, r.ts FROM smart_reading r JOIN "
                "(SELECT attr, MAX(ts) m FROM smart_reading WHERE disk_id = ? GROUP BY attr) x "
                "ON x.attr = r.attr AND x.m = r.ts WHERE r.disk_id = ?",
                (disk_id, disk_id),
            )
        }
        ts = iso(now)

        def put(attr, value, worst, raw, always=False):
            before = last.get(attr)
            if (
                always
                or before is None
                or before[0] != raw
                or (now - before[1]).total_seconds() >= SAMPLE_EVERY
            ):
                db.execute(
                    "INSERT OR REPLACE INTO smart_reading (ts, disk_id, attr, value, worst, raw) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ts, disk_id, attr, value, worst, raw),
                )

        if disk.temperature is not None:
            put("temperature", disk.temperature, None, disk.temperature, always=True)
        if disk.smart_ok is not None:
            put("smart_status", int(disk.smart_ok), None, int(disk.smart_ok))
        for attr, row in disk.attrs.items():
            put(attr, row.get("value"), row.get("worst"), row["raw"])
