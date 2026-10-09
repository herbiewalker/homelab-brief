"""Detectors over client data: new_device, traffic_spike, odd_hours. Pure functions, like detectors.py.

All three are profile-aware, and the profile is applied before anything is read (detect_clients.py skips
`ignore` devices, and `odd_hours` looks only at `full` devices with quiet hours), so a finding never exposes
more about a device than its profile allows.

  new_device     a client MAC that appeared after the first discovery pass and is not listed in devices.yaml
  traffic_spike  a client's last 24 hours of traffic far above its own usual day
  odd_hours      a `full` device online during its own quiet hours

Quiet hours are UTC, like every time in the config files.
"""

import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from brief.detectors import Finding
from brief.store import parse_time

GB = 1e9
DAY = 86400


@dataclass(frozen=True)
class ClientRow:
    mac: str
    label: str  # what to call it (devices.yaml label, else hostname, else vendor and the end of the MAC)
    first_seen: str
    vlan: str | None = None
    vendor: str | None = None


def new_device(
    clients: Iterable[ClientRow], known: set[str], now: datetime, settle_hours: int = 24, show_days: int = 3
) -> list[Finding]:
    """A client first seen more than `settle_hours` after the earliest one (the first pass is the starting
    population, not news), for `show_days`, and not already named in devices.yaml."""
    rows = list(clients)
    if not rows:
        return []
    start = min(parse_time(r.first_seen) for r in rows)
    out = []
    for r in rows:
        seen = parse_time(r.first_seen)
        if r.mac in known or (seen - start).total_seconds() <= settle_hours * 3600:
            continue
        if (now - seen).total_seconds() > show_days * DAY:
            continue
        out.append(
            Finding(
                id=f"new_device:{r.mac}",
                kind="new_device",
                title=f"A new device joined: {r.label}",
                value=1,
                escalates=False,
                detail={
                    "label": r.label,
                    "vlan": r.vlan or "the network",
                    "vendor": r.vendor,
                    "first_seen": r.first_seen,
                },
            )
        )  # fmt: skip
    return out


@dataclass(frozen=True)
class TrafficRow:
    mac: str
    label: str
    today: int  # bytes in the last 24 hours
    history: tuple[int, ...] = ()  # bytes in each of the 24-hour windows before (any order)


def traffic_spike(
    rows: Iterable[TrafficRow], multiple: float = 5, floor_gb: float = 2, min_history_days: int = 3
) -> list[Finding]:
    """Today against the device's own median day; needs `min_history_days` of history and over `floor_gb`."""
    out = []
    for r in rows:
        days = [h for h in r.history if h is not None]
        if len(days) < min_history_days or r.today < floor_gb * GB:
            continue
        median = statistics.median(days)
        baseline = max(median, 1.0)  # a device that is usually silent: any big day is a multiple of "nothing"
        if r.today < multiple * baseline:
            continue
        ratio = r.today / baseline
        out.append(
            Finding(
                id=f"traffic_spike:{r.mac}",
                kind="traffic_spike",
                title=f"{r.label} moved {r.today / GB:.1f} GB in 24 hours, about {ratio:.0f}x its usual day",
                value=round(r.today / GB, 1),
                detail={
                    "device": r.label,
                    "gb": round(r.today / GB, 1),
                    "multiple": round(ratio),
                    "usual_gb": round(median / GB, 2),
                },
            )
        )
    return sorted(out, key=lambda f: -(f.value or 0))


@dataclass(frozen=True)
class PresenceRow:
    mac: str
    label: str
    quiet: tuple[int, ...]  # the UTC hours of the day that are inside the device's quiet window
    online: dict = field(default_factory=dict)  # (day, hour) -> minutes online, for the last 24 hours


def odd_hours(rows: Iterable[PresenceRow], min_online_min: int = 30) -> list[Finding]:
    out = []
    for r in rows:
        minutes = sum(m for (_, hour), m in r.online.items() if hour in r.quiet and m >= min_online_min)
        hours = sorted({h for (_, h), m in r.online.items() if h in r.quiet and m >= min_online_min})
        if not hours:
            continue
        out.append(
            Finding(
                id=f"odd_hours:{r.mac}",
                kind="odd_hours",
                title=f"{r.label} was online during its quiet hours ({minutes} min, around "
                f"{', '.join(f'{h:02d}:00' for h in hours[:3])} UTC)",
                value=minutes,
                detail={"device": r.label, "minutes": minutes, "hours_utc": hours},
            )
        )
    return out
