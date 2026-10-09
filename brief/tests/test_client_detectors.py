"""Task 21: new_device, traffic_spike, and odd_hours, from rows and from the database."""

from datetime import UTC, datetime, timedelta

import pytest
from test_collectors import lab

from brief import clients, detect, playbook
from brief import detectors_clients as dc
from brief.systems import Systems

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
GB = dc.GB
MACS = {n: f"00:00:5e:00:53:{i:02x}" for i, n in enumerate(("a", "b", "c", "d", "e"), start=0x31)}


def row(name, first, vlan=None, vendor=None):
    return dc.ClientRow(MACS[name], f"device {name}", first, vlan, vendor)


# ---------------------------------------------------------------- new_device


def test_the_first_population_is_not_news_but_a_later_arrival_is():
    found = dc.new_device(
        [
            row("a", "2026-10-01T00:00:00Z"),
            row("b", "2026-10-01T05:00:00Z"),
            row("c", "2026-10-08T00:00:00Z", "IoT"),
        ],
        set(),
        NOW,
    )
    assert [f.id for f in found] == [f"new_device:{MACS['c']}"] and found[0].detail["vlan"] == "IoT"
    assert found[0].title == "A new device joined: device c"


def test_a_listed_device_is_never_new_and_news_fades():
    rows = [
        row("a", "2026-10-01T00:00:00Z"),
        row("c", "2026-10-08T00:00:00Z"),
        row("d", "2026-10-02T00:00:00Z"),
    ]
    assert [f.id for f in dc.new_device(rows, {MACS["c"]}, NOW)] == []  # c is known; d was news a week ago
    assert dc.new_device([], set(), NOW) == []


# ---------------------------------------------------------------- traffic_spike


def test_a_spike_is_judged_against_the_devices_own_usual_day():
    quiet = dc.TrafficRow(MACS["a"], "quiet", int(6 * GB), (int(0.5 * GB),) * 5)
    heavy = dc.TrafficRow(MACS["b"], "heavy", int(6 * GB), (int(4 * GB),) * 5)  # 6 GB is normal for this one
    (f,) = dc.traffic_spike([quiet, heavy])
    assert f.id == f"traffic_spike:{MACS['a']}" and f.detail["gb"] == 6.0 and f.detail["multiple"] == 12
    assert "quiet moved 6.0 GB in 24 hours, about 12x its usual day" == f.title


def test_a_spike_needs_history_and_a_floor():
    assert (
        dc.traffic_spike([dc.TrafficRow(MACS["a"], "n", int(9 * GB), (int(GB),))]) == []
    )  # one day of history
    assert (
        dc.traffic_spike([dc.TrafficRow(MACS["a"], "n", int(1.5 * GB), (1000,) * 5)]) == []
    )  # under the floor
    silent = dc.TrafficRow(MACS["a"], "n", int(3 * GB), (0, 0, 0))
    assert len(dc.traffic_spike([silent])) == 1  # a device that is usually silent can still spike


# ---------------------------------------------------------------- odd_hours


def test_only_quiet_hours_with_enough_minutes_count():
    quiet = (23, 0, 1, 2, 3, 4, 5)
    online = {("2026-10-09", 1): 60, ("2026-10-09", 2): 10, ("2026-10-09", 9): 60}
    (f,) = dc.odd_hours([dc.PresenceRow(MACS["a"], "laptop", quiet, online)], min_online_min=30)
    assert f.detail["minutes"] == 60 and f.detail["hours_utc"] == [1] and "01:00 UTC" in f.title
    assert dc.odd_hours([dc.PresenceRow(MACS["a"], "laptop", quiet, {("d", 9): 60})]) == []


# ---------------------------------------------------------------- from the database


DEVICES = f"""schema: 1
devices:
  - {{ mac: "{MACS["a"]}", label: "Full laptop", profile: full, quiet_hours: {{ start: "23:00", end: "06:00" }} }}
  - {{ mac: "{MACS["b"]}", label: "Console", profile: security_only, quiet_hours: {{ start: "23:00", end: "06:00" }} }}
  - {{ mac: "{MACS["c"]}", label: "Plug", profile: ignore }}
"""


def seed(tmp_path):
    env, user, store = lab(tmp_path, "  unifi:\n    enabled: false\n")
    (user / "devices.yaml").write_text(DEVICES, encoding="utf-8")
    with store.write() as tx:
        db = tx.db
        for name, first in (
            ("a", "2026-10-01T00:00:00Z"),
            ("b", "2026-10-01T00:00:00Z"),
            ("d", "2026-10-08T00:00:00Z"),
        ):
            clients.touch_client(db, MACS[name], first, "2026-10-09T11:00:00Z", hostname=f"host-{name}")
        # six days of ordinary traffic for a, b and c, then a huge last 24 hours
        for days_ago in range(1, 7):
            when = NOW - timedelta(days=days_ago)
            for name in ("a", "b", "c"):
                clients.add_hourly(
                    db, when.strftime("%Y-%m-%d"), when.hour, MACS[name], up=int(0.1 * GB), down=int(0.2 * GB)
                )
        for name in ("a", "b", "c"):
            clients.add_hourly(db, "2026-10-09", 10, MACS[name], up=int(1 * GB), down=int(4 * GB))
        for hour in (1, 2):  # a was online in its quiet hours last night, b too
            for name in ("a", "b"):
                clients.add_hourly(db, "2026-10-09", hour, MACS[name], online_min=60)
    return env, store


def test_the_detectors_follow_the_profiles(tmp_path):
    env, store = seed(tmp_path)
    found = detect.run(store, Systems(), NOW, env, kinds=("new_device", "traffic_spike", "odd_hours"))[
        "findings"
    ]
    by_kind = {}
    for f in found:
        by_kind.setdefault(f.kind, []).append(f)
    assert [f.id for f in by_kind["new_device"]] == [f"new_device:{MACS['d']}"]
    assert (
        by_kind["new_device"][0].title == "A new device joined: host-d"
    )  # an unlisted device is named by its hostname
    spikes = {f.detail["device"] for f in by_kind["traffic_spike"]}
    assert spikes == {"Full laptop", "Console"}  # security_only is judged too; the ignored plug never is
    assert [f.detail["device"] for f in by_kind["odd_hours"]] == [
        "Full laptop"
    ]  # security_only keeps no timeline


def test_the_ignored_device_is_invisible_even_if_a_row_slipped_in(tmp_path):
    env, store = seed(tmp_path)
    with store.write() as tx:
        clients.touch_client(
            tx.db, MACS["c"], "2026-10-08T00:00:00Z", "2026-10-09T11:00:00Z", hostname="plug"
        )
    found = detect.run(store, Systems(), NOW, env, kinds=("new_device", "traffic_spike"))["findings"]
    assert all(MACS["c"] not in f.id for f in found)


def test_nothing_is_found_without_client_data(tmp_path):
    env, user, store = lab(tmp_path, "  unifi:\n    enabled: false\n")
    assert (
        detect.run(store, Systems(), NOW, env, kinds=("new_device", "traffic_spike", "odd_hours"))["findings"]
        == []
    )


@pytest.mark.parametrize("kind", ["new_device", "traffic_spike", "odd_hours"])
def test_every_kind_is_a_daily_detector_with_playbook_advice(kind):
    from pathlib import Path

    assert kind in detect.DAILY and kind in detect.ALL
    rules = playbook.validate(Path(__file__).resolve().parents[1] / "playbook.yaml")
    assert any(r.kind == kind for r in rules)


def test_the_advice_reads_naturally(tmp_path):
    from pathlib import Path

    env, store = seed(tmp_path)
    found = detect.run(store, Systems(), NOW, env, kinds=("new_device", "traffic_spike", "odd_hours"))[
        "findings"
    ]
    advised = playbook.apply(found, playbook.validate(Path(__file__).resolve().parents[1] / "playbook.yaml"))
    text = {f.kind: f.detail["advice"] for f in advised}
    assert text["new_device"].startswith("A new device (host-d) joined the network.")
    assert "moved 5 GB today" in text["traffic_spike"] or "moved 5.0 GB today" in text["traffic_spike"]
    assert "was online for 120 minutes during its quiet hours" in text["odd_hours"]
