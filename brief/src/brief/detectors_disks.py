"""Detectors over what the Unraid collector stores: disks, parity checks, notifications, versions.

Pure functions, like detectors.py: rows in, findings out, no database and no clock (`now` is passed).
Starting values are in brief/playbook.yaml `thresholds`; the wording of the advice is in its `rules`
(playbook.py), which read the `detail` fields set here: disk, server, attr, raw, delta_14d.

  smart_failing   a drive reports an overall SMART failure
  smart_trend     a watched attribute went up over the window (reallocated, reported uncorrectable,
                  spin retries, NVMe media errors), or is non-zero (pending, offline-uncorrectable)
  crc_rising      UDMA CRC errors went up (a cable or backplane problem, not the disk)
  disk_temp       a spinning disk stayed at or above the limit for the whole window
  parity_slow     the latest parity check was far slower than that server's earlier ones, or below a floor
  unraid_warning  a server shows an unread warning or alert notification
  version_behind  an update has been available for longer than the limit
"""

import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from brief.detectors import Finding
from brief.store import parse_time

PENDING = ("197:Current_Pending_Sector", "198:Offline_Uncorrectable")
TREND = (
    "5:Reallocated_Sector_Ct",
    "187:Reported_Uncorrect",
    "196:Reallocated_Event_Count",
    "10:Spin_Retry_Count",
    "nvme:media_errors",
)
CRC = "199:UDMA_CRC_Error_Count"
DAY = 86400


@dataclass(frozen=True)
class DiskState:
    disk_id: str
    server: str
    label: str  # "<server> <model> ...<last 4 of the serial>", what the advice calls the drive
    role: str
    solid_state: bool
    smart_ok: bool | None = None  # the drive's own verdict, latest reading
    latest: dict = field(default_factory=dict)  # attr -> raw now
    before: dict = field(
        default_factory=dict
    )  # attr -> raw at the start of the window (or the first reading)
    temps: tuple[float, ...] = ()  # temperature samples inside the temperature window


def smart_failing(disks: Iterable[DiskState]) -> list[Finding]:
    return [
        Finding(
            id=f"smart_failing:{d.disk_id}",
            kind="smart_failing",
            title=f"{d.label} reports a failed SMART status",
            value=1,
            escalates=False,
            detail={"disk": d.label, "server": d.server, "role": d.role},
        )
        for d in disks
        if d.smart_ok is False
    ]


def _attr_title(label: str, attr: str, raw: int, delta: int, window_days: int) -> str:
    name = attr.split(":", 1)[1].replace("_", " ")
    if attr in PENDING:
        return f"{label}: {raw} {name.lower()}"
    return f"{label}: {name.lower()} rose by {delta} in {window_days} days (now {raw})"


def smart_trend(disks: Iterable[DiskState], window_days: int = 14) -> list[Finding]:
    out = []
    for d in disks:
        for attr in (*PENDING, *TREND):
            raw = d.latest.get(attr)
            if raw is None:
                continue
            delta = raw - d.before.get(attr, raw)
            if not ((attr in PENDING and raw > 0) or (attr in TREND and delta > 0)):
                continue
            out.append(
                Finding(
                    id=f"smart_trend:{d.disk_id}:{attr}",
                    kind="smart_trend",
                    title=_attr_title(d.label, attr, raw, delta, window_days),
                    value=raw if attr in PENDING else delta,
                    detail={
                        "disk": d.label,
                        "server": d.server,
                        "attr": attr,
                        "raw": raw,
                        "delta_14d": delta,
                        "window_days": window_days,
                    },
                )
            )
    return out


def crc_rising(disks: Iterable[DiskState], window_days: int = 14) -> list[Finding]:
    out = []
    for d in disks:
        raw = d.latest.get(CRC)
        if raw is None or raw - d.before.get(CRC, raw) <= 0:
            continue
        delta = raw - d.before[CRC]
        out.append(
            Finding(
                id=f"crc_rising:{d.disk_id}",
                kind="crc_rising",
                title=f"{d.label}: CRC errors rose by {delta} in {window_days} days (now {raw})",
                value=delta,
                detail={"disk": d.label, "server": d.server, "raw": raw, "delta_14d": delta},
            )
        )
    return out


def disk_temp(
    disks: Iterable[DiskState], warn_c: float = 50, days: int = 3, min_samples: int = 12
) -> list[Finding]:
    """A spinning disk whose coolest sample in the window is still at or above `warn_c`."""
    out = []
    for d in disks:
        if d.solid_state or d.role == "flash" or len(d.temps) < min_samples:
            continue
        if min(d.temps) < warn_c:
            continue
        mean = statistics.fmean(d.temps)
        out.append(
            Finding(
                id=f"disk_temp:{d.disk_id}",
                kind="disk_temp",
                title=f"{d.label} has run at {mean:.0f} °C or more for {days} days",
                value=round(mean, 1),
                detail={"disk": d.label, "server": d.server, "temp_c": round(mean, 1), "days": days},
            )
        )
    return out


@dataclass(frozen=True)
class ParityRow:
    server: str
    date: str
    duration_s: int
    speed_bps: int
    status: str
    errors: int = 0


def _duration(seconds: int) -> str:
    hours = round(seconds / 3600)
    return f"{hours // 24} d {hours % 24} h" if hours >= 24 else f"{max(hours, 1)} h"


def parity_slow(
    checks: Iterable[ParityRow],
    now: datetime,
    floor_mb_s: float = 40,
    drop_vs_previous_pct: float = 50,
    max_age_days: int = 40,
    history: int = 5,
) -> list[Finding]:
    """Per server: the latest completed check against the median of the few before it."""
    by_server: dict[str, list[ParityRow]] = {}
    for c in checks:
        if c.status.upper() == "COMPLETED" and c.speed_bps > 0:
            by_server.setdefault(c.server, []).append(c)
    out = []
    for server, rows in by_server.items():
        rows.sort(key=lambda r: r.date)
        latest, earlier = rows[-1], rows[-1 - history : -1]
        if (now - parse_time(latest.date)).total_seconds() > max_age_days * DAY:
            continue  # an old slow check is history, not a current problem
        speed = latest.speed_bps / 1e6
        previous = statistics.median(r.speed_bps for r in earlier) / 1e6 if earlier else None
        slow = speed < floor_mb_s or (
            previous is not None and speed < previous * (1 - drop_vs_previous_pct / 100)
        )
        if not slow:
            continue
        out.append(
            Finding(
                id=f"parity_slow:{server}:{latest.date}",
                kind="parity_slow",
                title=f"{server}'s parity check took {_duration(latest.duration_s)} at {speed:.1f} MB/s"
                + (f" (previously about {previous:.0f} MB/s)" if previous else ""),
                value=round((previous or floor_mb_s) / max(speed, 0.1), 1),
                escalates=False,  # a past check does not get worse; the next one supersedes it
                detail={
                    "server": server,
                    "date": latest.date,
                    "duration": _duration(latest.duration_s),
                    "speed": round(speed, 1),
                    "previous_speed": round(previous, 1) if previous else None,
                    "errors": latest.errors,
                },
            )
        )
    return out


@dataclass(frozen=True)
class NoticeRow:
    server: str
    notice_id: str
    importance: str
    subject: str
    description: str = ""


def unraid_warning(notices: Iterable[NoticeRow]) -> list[Finding]:
    out = []
    for n in notices:
        subject = n.subject or "a notification"
        out.append(
            Finding(
                id=f"unraid_warning:{n.server}:{n.notice_id}",
                kind="unraid_warning",
                title=f"{n.server}: {subject[:120]}",
                value=2 if n.importance == "ALERT" else 1,
                escalates=False,
                detail={
                    "server": n.server,
                    "importance": n.importance.lower(),
                    "subject": subject,
                    "description": n.description,
                },
            )
        )
    return out


@dataclass(frozen=True)
class InventoryRow:
    item_id: str
    kind: str
    name: str
    current_version: str
    available_version: str | None
    available_since: str | None


def version_behind(items: Iterable[InventoryRow], now: datetime, days: int = 30) -> list[Finding]:
    out = []
    for i in items:
        if not i.available_version or not i.available_since:
            continue
        waiting = (now - parse_time(i.available_since)).days
        if waiting < days:
            continue
        out.append(
            Finding(
                id=f"version_behind:{i.item_id}",
                kind="version_behind",
                title=(
                    f"{i.name} {i.available_version} has been available for {waiting} days "
                    f"(running {i.current_version})"
                ),
                value=waiting,
                detail={
                    "name": i.name,
                    "item_kind": i.kind,
                    "current_version": i.current_version,
                    "available_version": i.available_version,
                    "days": waiting,
                },
            )
        )
    return out
