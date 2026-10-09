"""Client activity: what the logs and the UniFi collector add to client, client_hourly and dns_hourly.

Everything here is gated by the device's profile (devices.py) at the point where it would be stored:

  ignore         nothing is stored and the log line is not counted or sampled either
  security_only  a `client` row (that it exists, and when) and traffic volume; DNS and the activity
                 timeline are dropped before they reach a table, and the log line stays out of the
                 generic signature counts
  full           the above plus DNS detail and hourly presence

Two log sources feed it (UniFi must be enabled with `client_logs: true`, see devices.ClientPolicy):

  gateway DNS    `coredns[..]: {"type":"dnsAdBlock","category":..,"domain":..,"mac":..}` JSON lines.
                 The gateway logs the blocked or categorised queries, not every query, so `queries`
                 counts logged lines and `blocked` those of a blocking type; it is a view of
                 filtering, not total DNS volume.
  access points  `EVENT_STA_JOIN` / `EVENT_STA_LEAVE <vap>: <mac>` from the wevent program: when a device was
                 last seen, and that a new one appeared, before the next hourly poll.

Writes are batched: the ingest accumulates per cycle and `flush` runs inside the cycle's one transaction.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field

from brief.devices import norm_mac

STA_EVENT = re.compile(r"EVENT_STA_(JOIN|LEAVE) \S+: ([0-9A-Fa-f:]{17})")
TOP_DOMAINS = 20
DAILY = 24  # the `hour` value of a rolled-up daily row
MAX_DOMAIN = 120


@dataclass(frozen=True)
class ClientEvent:
    kind: str  # dns, join, or leave
    mac: str  # colon form
    domain: str | None = None
    category: str | None = None
    blocked: bool = False


def parse(app: str, msg: str) -> ClientEvent | None:
    """A client event from one parsed log line, or None for any other line."""
    if app == "coredns" and msg.startswith("{"):
        try:
            data = json.loads(msg)
        except ValueError:
            return None
        mac = norm_mac(data.get("mac")) if isinstance(data, dict) else None
        domain = data.get("domain") if isinstance(data, dict) else None
        if mac is None or not isinstance(domain, str):
            return None
        kind = str(data.get("type") or "")
        category = data.get("category")
        return ClientEvent(
            "dns",
            mac,
            domain.lower()[:MAX_DOMAIN],
            str(category)[:40] if category else None,
            "block" in kind.lower(),
        )
    if app == "wevent":
        found = STA_EVENT.search(msg)
        if found and norm_mac(found.group(2)):
            return ClientEvent("join" if found.group(1) == "JOIN" else "leave", norm_mac(found.group(2)))
    return None


@dataclass
class Activity:
    """What one ingest cycle saw, already filtered by profile."""

    presence: dict[str, list] = field(default_factory=dict)  # mac -> [first stamp, last stamp]
    dns: dict[tuple, list] = field(
        default_factory=dict
    )  # (day, hour, mac) -> [queries, blocked, domains, categories]

    def see(self, mac: str, stamp: str) -> None:
        span = self.presence.get(mac)
        if span is None:
            self.presence[mac] = [stamp, stamp]
        else:
            span[0], span[1] = min(span[0], stamp), max(span[1], stamp)

    def record(self, event: ClientEvent, profile: str, stamp: str, day: str, hour: int) -> None:
        """Keep what `profile` allows of `event`."""
        if profile == "ignore":
            return
        self.see(event.mac, stamp)
        if profile != "full" or event.kind != "dns":
            return
        row = self.dns.setdefault((day, hour, event.mac), [0, 0, Counter(), Counter()])
        row[0] += 1
        row[1] += int(event.blocked)
        row[2][event.domain] += 1
        if event.category:
            row[3][event.category] += 1

    def __bool__(self) -> bool:
        return bool(self.presence or self.dns)


# ---------------------------------------------------------------- writing


def touch_client(db, mac: str, first: str, last: str, hostname=None, vendor=None, vlan=None) -> None:
    """A sighting. first_seen only ever moves earlier, last_seen later; a name that is known is not erased."""
    db.execute(
        "INSERT INTO client (mac, first_seen, last_seen, hostname, vendor, vlan) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (mac) DO UPDATE SET first_seen = MIN(client.first_seen, excluded.first_seen), "
        "last_seen = MAX(client.last_seen, excluded.last_seen), "
        "hostname = COALESCE(excluded.hostname, client.hostname), "
        "vendor = COALESCE(excluded.vendor, client.vendor), "
        "vlan = COALESCE(excluded.vlan, client.vlan)",
        (mac, first, last, hostname, vendor, vlan),
    )


def add_hourly(
    db, day: str, hour: int, mac: str, up: int = 0, down: int = 0, online_min: int | None = None
) -> None:
    """Traffic adds up within the hour; presence is the most minutes seen (a second poll adds nothing)."""
    db.execute(
        "INSERT INTO client_hourly (day, hour, mac, bytes_up, bytes_down, online_min) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (day, hour, mac) DO UPDATE SET "
        "bytes_up = COALESCE(client_hourly.bytes_up, 0) + excluded.bytes_up, "
        "bytes_down = COALESCE(client_hourly.bytes_down, 0) + excluded.bytes_down, "
        "online_min = MAX(COALESCE(client_hourly.online_min, 0), COALESCE(excluded.online_min, 0))",
        (day, hour, mac, up, down, online_min or 0),
    )


def _merge(existing: str | None, domains: Counter, categories: Counter) -> str:
    base = {"domains": {}, "categories": {}}
    if existing:
        try:
            loaded = json.loads(existing)
            base = {
                "domains": dict(loaded.get("domains") or {}),
                "categories": dict(loaded.get("categories") or {}),
            }
        except (ValueError, AttributeError):
            pass
    d, c = Counter(base["domains"]), Counter(base["categories"])
    d.update(domains)
    c.update(categories)
    return json.dumps(
        {"domains": dict(d.most_common(TOP_DOMAINS)), "categories": dict(c.most_common(TOP_DOMAINS))}
    )


def flush(db, activity: Activity) -> None:
    """Write one cycle's client activity (inside the cycle's transaction)."""
    for mac, (first, last) in activity.presence.items():
        touch_client(db, mac, first, last)
    for (day, hour, mac), (queries, blocked, domains, categories) in activity.dns.items():
        row = db.execute(
            "SELECT top_domains FROM dns_hourly WHERE day = ? AND hour = ? AND client = ?", (day, hour, mac)
        ).fetchone()
        db.execute(
            "INSERT INTO dns_hourly (day, hour, client, queries, blocked, top_domains) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (day, hour, client) DO UPDATE SET queries = dns_hourly.queries + excluded.queries, "
            "blocked = dns_hourly.blocked + excluded.blocked, top_domains = excluded.top_domains",
            (
                day,
                hour,
                mac,
                queries,
                blocked,
                _merge(row["top_domains"] if row else None, domains, categories),
            ),
        )


def rollup(db, cutoff_day: str) -> int:
    """Turn hourly rows older than `cutoff_day` (YYYY-MM-DD) into one daily row each (hour 24); repeatable.
    Returns the number of hourly rows removed."""
    removed = 0
    db.execute(
        "INSERT INTO client_hourly (day, hour, mac, bytes_up, bytes_down, online_min) "
        "SELECT day, ?, mac, SUM(COALESCE(bytes_up, 0)), SUM(COALESCE(bytes_down, 0)), "
        "SUM(COALESCE(online_min, 0)) "
        "FROM client_hourly WHERE day < ? AND hour < ? GROUP BY day, mac "
        "ON CONFLICT (day, hour, mac) DO UPDATE SET bytes_up = client_hourly.bytes_up + excluded.bytes_up, "
        "bytes_down = client_hourly.bytes_down + excluded.bytes_down, "
        "online_min = client_hourly.online_min + excluded.online_min",
        (DAILY, cutoff_day, DAILY),
    )
    removed += db.execute(
        "DELETE FROM client_hourly WHERE day < ? AND hour < ?", (cutoff_day, DAILY)
    ).rowcount
    groups: dict[tuple, list] = {}
    for r in db.execute(
        "SELECT day, hour, client, queries, blocked, top_domains FROM dns_hourly WHERE day < ? AND hour < ?",
        (cutoff_day, DAILY),
    ):
        g = groups.setdefault((r["day"], r["client"]), [0, 0, Counter(), Counter()])
        g[0] += r["queries"] or 0
        g[1] += r["blocked"] or 0
        try:
            loaded = json.loads(r["top_domains"] or "{}")
            g[2].update(loaded.get("domains") or {})
            g[3].update(loaded.get("categories") or {})
        except (ValueError, AttributeError):
            pass
    for (day, client), (queries, blocked, domains, categories) in groups.items():
        existing = db.execute(
            "SELECT queries, blocked, top_domains FROM dns_hourly WHERE day = ? AND hour = ? AND client = ?",
            (day, DAILY, client),
        ).fetchone()
        merged = _merge(existing["top_domains"] if existing else None, domains, categories)
        db.execute(
            "INSERT OR REPLACE INTO dns_hourly (day, hour, client, queries, blocked, top_domains) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (day, DAILY, client, queries + (existing["queries"] if existing else 0),
             blocked + (existing["blocked"] if existing else 0), merged),
        )  # fmt: skip
    removed += db.execute("DELETE FROM dns_hourly WHERE day < ? AND hour < ?", (cutoff_day, DAILY)).rowcount
    return removed
