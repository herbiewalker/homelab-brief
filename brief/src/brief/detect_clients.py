"""Gather what the client detectors need, as of `now`, applying each device's profile first.

The detectors themselves are in detectors_clients.py. A device on the `ignore` profile is skipped here even if
a row for it exists (it should not, but this is the second lock), and `odd_hours` reads only `full` devices
that have quiet hours in devices.yaml.
"""

from collections import defaultdict
from datetime import datetime

from brief import detectors_clients as dc
from brief.detectors import Finding
from brief.devices import DeviceBook
from brief.store import Store, iso

KINDS = ("new_device", "traffic_spike", "odd_hours")
HIDX = "(CAST(strftime('%s', day) AS INTEGER) / 3600 + hour)"  # the hour bucket as hours since 1970


def _now_index(now: datetime) -> int:
    return int(now.timestamp() // 3600)


def run(store: Store, now: datetime, t: dict, kinds: tuple[str, ...], book: DeviceBook) -> list[Finding]:
    wanted = set(kinds) & set(KINDS)
    if not wanted:
        return []
    found: list[Finding] = []
    with store.read() as db:
        names = {r["mac"]: (r["hostname"], r["vendor"], r["vlan"]) for r in db.execute(
            "SELECT mac, hostname, vendor, vlan FROM client"
        )}  # fmt: skip

        def label(mac: str) -> str:
            hostname, vendor, _ = names.get(mac, (None, None, None))
            return book.label(mac, hostname, vendor)

        if "new_device" in wanted:
            n = t["new_device"]
            rows = [
                dc.ClientRow(r["mac"], label(r["mac"]), r["first_seen"], r["vlan"], r["vendor"])
                for r in db.execute(
                    "SELECT mac, first_seen, vlan, vendor FROM client WHERE first_seen <= ?", (iso(now),)
                )
                if book.profile_of(r["mac"]) != "ignore"
            ]  # fmt: skip
            found += dc.new_device(rows, set(book.devices), now, int(n["settle_hours"]), int(n["show_days"]))
        if "traffic_spike" in wanted:
            s = t["traffic_spike"]
            idx = _now_index(now)
            buckets: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
            for r in db.execute(
                f"SELECT mac, {HIDX} AS hidx, COALESCE(bytes_up, 0) + COALESCE(bytes_down, 0) AS total "
                f"FROM client_hourly WHERE hour < 24 AND {HIDX} > ? AND {HIDX} <= ?",
                (idx - 24 * (int(s["window_days"]) + 1), idx),
            ):
                if book.profile_of(r["mac"]) != "ignore":
                    buckets[r["mac"]][(idx - r["hidx"]) // 24] += r["total"]
            rows = [
                dc.TrafficRow(mac, label(mac), per.get(0, 0), tuple(v for k, v in per.items() if k >= 1))
                for mac, per in buckets.items()
            ]
            found += dc.traffic_spike(rows, s["multiple"], s["floor_gb"], int(s["min_history_days"]))
        if "odd_hours" in wanted:
            idx = _now_index(now)
            watched = {
                d.mac: d
                for d in book.devices.values()
                if d.profile == "full" and d.quiet_start is not None and d.quiet_end is not None
            }
            online: dict[str, dict] = defaultdict(dict)
            for r in db.execute(
                f"SELECT mac, day, hour, online_min FROM client_hourly "
                f"WHERE hour < 24 AND {HIDX} > ? AND {HIDX} <= ?",
                (idx - 24, idx),
            ):
                if r["mac"] in watched and (r["online_min"] or 0) > 0:
                    online[r["mac"]][(r["day"], r["hour"])] = r["online_min"]
            rows = [
                dc.PresenceRow(
                    mac, label(mac), tuple(h for h in range(24) if d.in_quiet_hour(h)), online[mac]
                )
                for mac, d in watched.items()
                if mac in online
            ]
            found += dc.odd_hours(rows, int(t["odd_hours"]["min_online_min"]))
    return found
