"""Gather the rows the disk and server detectors need, as of `now` (so a replay sees only the past).

The detectors themselves are in detectors_hw.py. This module is the part that reads the database
(read-only connection) and runs them, so detect.run only has to ask for the kinds it wants.
"""

from collections import defaultdict
from datetime import datetime, timedelta

from brief import detectors_hw as dd
from brief.detectors import Finding
from brief.store import Store, iso

KINDS = (
    "smart_failing",
    "smart_trend",
    "crc_rising",
    "disk_temp",
    "parity_slow",
    "unraid_warning",
    "version_behind",
    "firmware_pending",
    "new_infrastructure",
)


def _label(server: str, model: str, serial: str) -> str:
    return f"{server} {model} …{serial[-4:]}" if serial else f"{server} {model}"


def disk_states(store: Store, now: datetime, window_days: int, temp_days: int) -> list[dd.DiskState]:
    start, temp_start = iso(now - timedelta(days=window_days)), iso(now - timedelta(days=temp_days))
    with store.read() as db:
        disks = db.execute("SELECT disk_id, server, model, serial, role FROM disk").fetchall()
        if not disks:
            return []
        series: dict[tuple[str, str], list] = defaultdict(list)
        for r in db.execute(
            "SELECT disk_id, attr, ts, raw FROM smart_reading WHERE ts <= ? AND attr != 'temperature' "
            "ORDER BY disk_id, attr, ts",
            (iso(now),),
        ):
            series[(r["disk_id"], r["attr"])].append((r["ts"], r["raw"]))
        temps: dict[str, list[float]] = defaultdict(list)
        for r in db.execute(
            "SELECT disk_id, raw FROM smart_reading WHERE attr = 'temperature' AND ts > ? AND ts <= ?",
            (temp_start, iso(now)),
        ):
            if r["raw"] is not None:
                temps[r["disk_id"]].append(float(r["raw"]))
    out = []
    for d in disks:
        latest, before, smart_ok = {}, {}, None
        for (disk_id, attr), points in series.items():
            if disk_id != d["disk_id"]:
                continue
            if attr == "smart_status":
                smart_ok = bool(points[-1][1])
                continue
            latest[attr] = points[-1][1]
            earlier = [raw for ts, raw in points if ts <= start]
            before[attr] = (
                earlier[-1] if earlier else points[0][1]
            )  # no reading that old: since the first one
        model = d["model"] or "unknown"
        out.append(
            dd.DiskState(
                disk_id=d["disk_id"],
                server=d["server"],
                label=_label(d["server"], model, d["serial"] or ""),
                role=d["role"] or "unassigned",
                solid_state=any(w in model.lower() for w in ("ssd", "nvme", "solid")),
                smart_ok=smart_ok,
                latest=latest,
                before=before,
                temps=tuple(temps.get(d["disk_id"], ())),
            )
        )
    return out


def run(store: Store, now: datetime, t: dict, kinds: tuple[str, ...]) -> list[Finding]:
    """Findings for the requested disk and server kinds. `t` is detect.thresholds()."""
    wanted = set(kinds) & set(KINDS)
    if not wanted:
        return []
    found: list[Finding] = []
    if wanted & {"smart_failing", "smart_trend", "crc_rising", "disk_temp"}:
        window = int(t["smart_trend"]["window_days"])
        disks = disk_states(store, now, window, int(t["disk_temp_hdd_c"]["days"]))
        if "smart_failing" in wanted:
            found += dd.smart_failing(disks)
        if "smart_trend" in wanted:
            found += dd.smart_trend(disks, window)
        if "crc_rising" in wanted:
            found += dd.crc_rising(disks, window)
        if "disk_temp" in wanted:
            found += dd.disk_temp(disks, t["disk_temp_hdd_c"]["warn"], t["disk_temp_hdd_c"]["days"])
    with store.read() as db:
        if "parity_slow" in wanted:
            p = t["parity_slow"]
            rows = [
                dd.ParityRow(
                    r["server"],
                    r["date"],
                    r["duration_s"] or 0,
                    r["speed_bps"] or 0,
                    r["status"] or "",
                    r["errors"] or 0,
                )
                for r in db.execute(
                    "SELECT server, date, duration_s, speed_bps, status, errors "
                    "FROM parity_check WHERE date <= ?",
                    (iso(now),),
                )
            ]
            found += dd.parity_slow(
                rows,
                now,
                p["floor_mb_s"],
                p["drop_vs_previous_pct"],
                int(p["max_age_days"]),
                int(p["history"]),
            )
        if "unraid_warning" in wanted:
            found += dd.unraid_warning(
                dd.NoticeRow(
                    r["server"],
                    r["notice_id"],
                    r["importance"] or "",
                    r["subject"] or "",
                    r["description"] or "",
                )
                for r in db.execute(
                    "SELECT server, notice_id, importance, subject, description "
                    "FROM server_notice WHERE ts <= ?",
                    (iso(now),),
                )
            )
        if wanted & {"version_behind", "firmware_pending"}:
            inventory = [
                dd.InventoryRow(
                    r["item_id"],
                    r["kind"] or "",
                    r["name"] or "",
                    r["current_version"] or "",
                    r["available_version"],
                    r["available_since"],
                    r["model"] or "",
                )
                for r in db.execute(
                    "SELECT item_id, kind, name, model, current_version, available_version, available_since "
                    "FROM inventory WHERE available_version IS NOT NULL"
                )
            ]
            days = int(t["version_behind"]["days"])
            if "version_behind" in wanted:
                found += dd.version_behind(inventory, now, days)
            if "firmware_pending" in wanted:
                found += dd.firmware_pending(inventory, now, days)
        if "new_infrastructure" in wanted:
            n = t["new_infrastructure"]
            found += dd.new_infrastructure(
                (
                    dd.NodeRow(r["node_id"], r["kind"] or "", r["label"] or r["node_id"], r["first_seen"])
                    for r in db.execute(
                        "SELECT node_id, kind, label, first_seen FROM topo_node WHERE first_seen <= ?",
                        (iso(now),),
                    )
                ),
                now,
                int(n["settle_hours"]),
                int(n["show_days"]),
            )
    return found
