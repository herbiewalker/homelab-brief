"""First-run setup: turn the senders you can see into named systems.

A new install starts with nothing in `systems.yaml`, but as soon as devices send syslog the product has
seen them. This module lists those senders with what each one talks about (its busiest programs), guesses
what kind of device it is (a suggestion, never applied without you), and writes the systems you name
through the config editor, so the result is validated, versioned, audited, and can be rolled back like any
other edit. Both the admin GUI wizard and `brief init` use it.

Discovery through the Unraid and UniFi APIs (servers, devices, topology) arrives with the collectors in
Phase 2; until then the senders themselves are the source.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from brief import admin_data, configedit, sysedit
from brief.config import ConfigError
from brief.store import Store
from brief.systems import ID_RE

TOP_APPS = 3
KINDS = ("gateway", "switch", "ap", "server", "camera", "nas", "iot", "other")
# programs that hint at what a sender is. A hint, never a decision: shown as a suggestion.
HINTS = (
    ({"hostapd", "stahtd", "wevent", "hostapd_cli"}, "ap"),
    ({"mcad", "udapi", "ubios-udapi-server", "dnsmasq", "kernel_fw", "ulogd"}, "gateway"),
    ({"unifi-protect", "ubnt_ai_daemon", "ubnt_avc_logger", "ustorage"}, "camera"),
    ({"smbd", "nmbd", "emhttpd", "nginx", "shfs", "mover", "sshd", "unraid"}, "server"),
    ({"switchd", "lldpd", "stpd"}, "switch"),
)


@dataclass(frozen=True)
class Assignment:
    host: str
    system: str  # what the person typed: an existing id or a new name
    label: str = ""
    kind: str = ""


def slug(text: str) -> str:
    """A system id from a name: 'Main Gateway' -> 'main-gateway'."""
    out = re.sub(r"[^a-z0-9_-]+", "-", text.strip().lower()).strip("-_")
    return out[:40]


def guess_kind(apps: list[str]) -> str:
    """The first kind whose hint programs include one of the sender's busiest programs, else ''."""
    seen = {a.lower() for a in apps}
    for programs, kind in HINTS:
        if seen & programs:
            return kind
    return ""


def senders(store: Store, status: Mapping, now: datetime, systems=None) -> list[dict]:
    """Every sender with its busiest programs and a kind suggestion; unknown senders first. `systems` (the
    current systems.yaml) marks senders you have just assigned; the database learns them next cycle."""
    rows = admin_data.sources(store, dict(status), now)
    assigned = {h.lower(): s.id for s in (systems.systems if systems else []) for h in s.hosts}
    for row in rows:
        if not row["known"] and row["host"].lower() in assigned:
            row["known"], row["system"] = True, assigned[row["host"].lower()]
    low = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    with store.read() as db:
        for row in rows:
            apps = [
                r["app"]
                for r in db.execute(
                    "SELECT s.app AS app, SUM(h.n) AS n FROM hourly h "
                    "JOIN signature s ON s.sig_id = h.sig_id "
                    "WHERE h.host = ? AND h.day >= ? AND s.app IS NOT NULL AND s.app != '' "
                    "GROUP BY s.app ORDER BY n DESC LIMIT ?",
                    (row["host"], low, TOP_APPS),
                )
            ]
            row["apps"] = apps
            row["kind_guess"] = guess_kind(apps)
    return rows


def needs_setup(store: Store) -> int:
    """How many senders are not in any system yet (0 when everything is mapped, or nothing has arrived)."""
    with store.read() as db:
        return int(db.execute("SELECT COUNT(*) FROM host WHERE known = 0").fetchone()[0])


def apply(
    store: Store,
    assignments: list[Assignment],
    user: str,
    state_dir: Path | str,
    env: Mapping[str, str] | None = None,
) -> str:
    """Add each sender to its system (an existing id, or a new system made from the name), saved through the
    config editor. Raises configedit.EditError / ConfigError when the result would not be valid."""
    chosen = [a for a in assignments if a.system.strip()]
    if not chosen:
        raise configedit.EditError("Name at least one system first.")
    text, customised = configedit.current("systems.yaml", env)
    rows, default = sysedit.rows_from_text(text) if customised else ([], "other")
    by_id = {r["id"]: r for r in rows}
    made, added = 0, 0
    for a in chosen:
        sid = a.system.strip() if a.system.strip() in by_id else slug(a.system)
        if not sid or not ID_RE.match(sid):
            raise ConfigError(f"{a.system!r} cannot be turned into a system id (use letters and digits).")
        row = by_id.get(sid)
        if row is None:
            row = {
                "id": sid,
                "label": (a.label or a.system).strip() or sid,
                "kind": a.kind if a.kind in KINDS else "",
                "hosts": "",
                "source_tz": "UTC",
                "expected": True,
                "extra": {},
            }
            rows.append(row)
            by_id[sid] = row
            made += 1
        hosts = sysedit.split_hosts(row["hosts"])
        if a.host.lower() not in {h.lower() for h in hosts}:
            row["hosts"] = ", ".join([*hosts, a.host])
            added += 1
    new_text = sysedit.build_text(rows, default)
    configedit.save(store, "systems.yaml", new_text, user, configedit.digest(text), state_dir, env)
    return f"{added} sender(s) assigned, {made} new system(s) made."
