"""The detectors against a real (temporary) baseline database."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from brief import cli, detect
from brief.store import Store
from brief.systems import System, Systems

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "baseline.db")
    yield s
    s.close()


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def day_of(k: int) -> str:
    return (NOW - timedelta(days=k)).strftime("%Y-%m-%d")


def systems(*pairs, expected=True):
    return Systems([System(i, i, "other", (h,), "UTC", expected) for i, h in pairs])


def setup_host(tx, host="198.51.100.1", system="sys1", first_days=30, last=None):
    tx.touch_host(
        host, stamp(NOW - timedelta(days=first_days)), last or stamp(NOW - timedelta(minutes=5)), system
    )


def add_sig(tx, cluster=1, first_days=30, last=None, app="app", template="thing happened"):
    first = stamp(NOW - timedelta(days=first_days))
    sid = tx.signature_for(1, cluster, app, template, first)
    tx.touch_signature(sid, first, last or stamp(NOW - timedelta(hours=2)))
    return sid


def steady_history(tx, sid, host="198.51.100.1", per_day=100, days=14, hour=10, severity="warning"):
    # (day, hour, host, sig_id, severity, n); hour 10 of each previous day is inside the right window
    tx.add_hourly([(day_of(k), hour, host, sid, severity, per_day) for k in range(1, days + 1)])


def kinds(result):
    return sorted(f.kind for f in result["findings"])


def test_a_spike_is_a_deviation_after_the_learning_period_and_silent_during_it(store):
    with store.write() as tx:
        setup_host(tx)
        sid = add_sig(tx)
        steady_history(tx, sid)
        tx.add_hourly([(day_of(0), 10, "198.51.100.1", sid, "warning", 900)])
        tx.note_live_data(stamp(NOW - timedelta(days=20)))  # learning is over
    out = detect.run(store, Systems(), NOW, kinds=("deviation",))
    assert out["learning"] is False and [f.kind for f in out["findings"]] == ["deviation"]
    assert out["findings"][0].detail["today"] == 900 and "thing happened" in out["findings"][0].title

    with store.write() as tx:
        tx.db.execute("DELETE FROM meta WHERE key = 'live_since'")
        tx.note_live_data(stamp(NOW - timedelta(days=2)))  # live data only 2 days old: still learning
    quiet = detect.run(store, Systems(), NOW, kinds=("deviation",))
    assert quiet["learning"] is True and quiet["findings"] == []
    forced = detect.run(store, Systems(), NOW, kinds=("deviation",), ignore_learning=True)
    assert len(forced["findings"]) == 1  # --ignore-learning shows what would fire


def test_a_normal_day_is_not_a_deviation(store):
    with store.write() as tx:
        setup_host(tx)
        sid = add_sig(tx)
        steady_history(tx, sid)
        tx.add_hourly([(day_of(0), 10, "198.51.100.1", sid, "warning", 104)])
    assert detect.run(store, Systems(), NOW, kinds=("deviation",), ignore_learning=True)["findings"] == []


def test_a_new_host_is_not_judged_against_days_before_it_existed(store):
    with store.write() as tx:
        setup_host(tx, first_days=1)  # first seen yesterday
        sid = add_sig(tx, first_days=1)
        tx.add_hourly([(day_of(1), 10, "198.51.100.1", sid, "warning", 50)])
        tx.add_hourly([(day_of(0), 10, "198.51.100.1", sid, "warning", 60)])
    # only one day of baseline: not enough to call 60 a spike
    assert detect.run(store, Systems(), NOW, kinds=("deviation",), ignore_learning=True)["findings"] == []


def test_off_hours_needs_history_in_other_hours(store):
    with store.write() as tx:
        setup_host(tx)
        sid = add_sig(tx)
        steady_history(tx, sid, per_day=5, hour=10)
        tx.add_hourly([(day_of(0), 3, "198.51.100.1", sid, "warning", 6)])
    out = detect.run(store, Systems(), NOW, kinds=("off_hours",), ignore_learning=True)
    assert [f.kind for f in out["findings"]] == ["off_hours"] and out["findings"][0].detail["hour_utc"] == 3
    # the same signature at its usual hour is fine
    with store.write() as tx:
        tx.add_hourly([(day_of(0), 10, "198.51.100.1", sid, "warning", 6)])
        tx.db.execute("DELETE FROM hourly WHERE day = ? AND hour = 3", (day_of(0),))
    assert detect.run(store, Systems(), NOW, kinds=("off_hours",), ignore_learning=True)["findings"] == []


def test_novelty_new_signature_story_versus_count_only_and_learning(store):
    with store.write() as tx:
        setup_host(tx)
        loud = add_sig(
            tx, cluster=1, first_days=0, template="disk failure", last=stamp(NOW - timedelta(hours=1))
        )
        calm = add_sig(
            tx, cluster=2, first_days=0, template="user logged in", last=stamp(NOW - timedelta(hours=1))
        )
        tx.db.execute(
            "UPDATE signature SET first_seen = ? WHERE sig_id IN (?, ?)",
            (stamp(NOW - timedelta(hours=3)), loud, calm),
        )
        tx.add_hourly(
            [(day_of(0), 9, "198.51.100.1", loud, "err", 4), (day_of(0), 9, "198.51.100.1", calm, "info", 4)]
        )
        tx.note_live_data(stamp(NOW - timedelta(days=20)))
    out = detect.run(store, systems(("sys1", "198.51.100.1")), NOW, kinds=("novelty",))
    by_title = {f.title: f for f in out["findings"]}
    assert len(by_title) == 2
    assert [f.story for f in by_title.values() if "disk failure" in f.title] == [True]
    assert [f.story for f in by_title.values() if "logged in" in f.title] == [False]
    assert by_title[next(t for t in by_title if "disk failure" in t)].system == "sys1"
    with store.write() as tx:
        tx.db.execute("DELETE FROM meta WHERE key = 'live_since'")
    assert (
        detect.run(store, Systems(), NOW, kinds=("novelty",))["findings"] == []
    )  # no live data yet: learning


def test_a_signature_returning_after_a_quiet_spell_is_found_via_returned_at(store):
    with store.write() as tx:
        setup_host(tx)
        sid = add_sig(tx, first_days=60, last=stamp(NOW - timedelta(days=40)))
        # active long ago, silent for ~38 days, back an hour ago
        tx.add_hourly(
            [
                *[
                    (day_of(k), 5, "198.51.100.1", sid, "warning", 3) for k in range(40, 47)
                ],  # regular before it went quiet
                (day_of(0), 11, "198.51.100.1", sid, "warning", 3),
            ]
        )
        tx.touch_signature(sid, stamp(NOW - timedelta(hours=1)), stamp(NOW - timedelta(hours=1)))
        tx.note_live_data(stamp(NOW - timedelta(days=60)))
        # the host kept logging other things all through the gap, so it is the signature that returned
        other = add_sig(tx, cluster=2, template="chatter")
        tx.add_hourly([(day_of(k), 8, "198.51.100.1", other, "info", 5) for k in range(2, 30)])
    found = detect.run(store, Systems(), NOW, kinds=("novelty",))["findings"]
    assert len(found) == 1 and found[0].detail["state"] == "returned"
    # a day later it is no longer news (it returned more than 24 hours ago)
    later = NOW + timedelta(hours=30)
    assert detect.run(store, Systems(), later, kinds=("novelty",))["findings"] == []


def test_absence_and_clock_skew_run_even_while_learning(store):
    with store.write() as tx:
        setup_host(tx, "198.51.100.1", "sys1", last=stamp(NOW - timedelta(days=3)))
        setup_host(tx, "198.51.100.2", "sys1")
        tx.set_clock_skew("198.51.100.2", 900, stamp(NOW - timedelta(minutes=10)))
    out = detect.run(store, systems(("sys1", "198.51.100.1"), ("sys1b", "x")), NOW)
    assert out["learning"] is True  # no live data yet
    assert kinds(out) == ["absence", "clock_skew"]


def test_absence_findings_use_the_expected_systems_only(store):
    with store.write() as tx:
        setup_host(tx, "198.51.100.1", "sys1", last=stamp(NOW - timedelta(days=3)))
        setup_host(tx, "198.51.100.3", "printer", last=stamp(NOW - timedelta(days=3)))
    both = Systems(
        [
            System("sys1", "s", "o", ("198.51.100.1",), "UTC", True),
            System("printer", "p", "o", ("198.51.100.3",), "UTC", False),
        ]
    )
    out = detect.run(store, both, NOW, kinds=("absence",))
    assert [f.host for f in out["findings"]] == ["198.51.100.1"]


def test_learn_gaps_sets_a_gap_only_for_hosts_with_enough_history(store):
    with store.write() as tx:
        setup_host(tx, "198.51.100.1")
        setup_host(tx, "198.51.100.2")
        sid = add_sig(tx)
        # host 1: active every hour for 3 days; host 2: only 5 hours
        tx.add_hourly(
            [(day_of(k), h, "198.51.100.1", sid, "info", 1) for k in range(1, 4) for h in range(24)]
        )
        tx.add_hourly([(day_of(1), h, "198.51.100.2", sid, "info", 1) for h in range(5)])
    assert detect.learn_gaps(store, NOW) == 1
    with store.read() as db:
        gaps = {
            r["host"]: r["expected_gap_min"] for r in db.execute("SELECT host, expected_gap_min FROM host")
        }
    assert gaps == {"198.51.100.1": 60, "198.51.100.2": None}


def test_suppressed_counts_also_prove_a_host_was_alive(store):
    with store.write() as tx:
        setup_host(tx)
        tx.add_suppressed(
            [(day_of(k), h, "198.51.100.1", "noise", 2) for k in range(1, 3) for h in range(24)]
        )
    assert detect.learn_gaps(store, NOW) == 1


def test_thresholds_come_from_playbook_yaml_and_survive_a_broken_file(tmp_path):
    env = {"BRIEF_CONFIG_DIR": str(tmp_path / "c"), "BRIEF_DEFAULTS_DIR": str(tmp_path / "d")}
    base = detect.thresholds(env)["deviation"]
    assert (base["window_days"], base["min_count"], base["mad_multiple"], base["min_ratio"]) == (14, 20, 4, 3)
    folder = tmp_path / "c" / "brief"
    folder.mkdir(parents=True)
    (folder / "playbook.yaml").write_text("thresholds:\n  deviation: { min_count: 50 }\n", encoding="utf-8")
    t = detect.thresholds(env)
    assert t["deviation"]["min_count"] == 50 and t["deviation"]["window_days"] == 14
    (folder / "playbook.yaml").write_text("thresholds: [broken", encoding="utf-8")
    assert detect.thresholds(env)["deviation"]["min_count"] == 20


def test_the_cli_prints_findings_as_text_and_json(store, tmp_path, monkeypatch, capsys):
    with store.write() as tx:
        setup_host(tx, "198.51.100.1", "sys1", last=stamp(NOW - timedelta(days=3)))
    store.close()
    monkeypatch.setenv("BRIEF_CONFIG_DIR", str(tmp_path / "none"))
    monkeypatch.setenv("BRIEF_DEFAULTS_DIR", str(tmp_path / "none"))
    lines = []
    args = cli.argparse.Namespace(kind=["absence"], json=False, ignore_learning=False, now=stamp(NOW))
    assert cli.run_detect(args, tmp_path, out=lines.append) == 0
    assert lines[0].startswith(
        "0 findings"
    )  # sys1 is not in the (empty) systems.yaml, so nothing is expected
    args.json = True
    cli.run_detect(args, tmp_path, out=lines.append)
    assert json.loads(lines[-1])["findings"] == []


def test_a_signature_is_not_returned_when_its_whole_host_was_down_meanwhile(store):
    # the collector was off for weeks: every signature "comes back" when it resumes, but only the host did
    with store.write() as tx:
        setup_host(tx)
        sid = add_sig(tx, first_days=60, last=stamp(NOW - timedelta(days=40)))
        tx.add_hourly(
            [
                *[
                    (day_of(k), 5, "198.51.100.1", sid, "warning", 3) for k in range(40, 47)
                ],  # regular before it went quiet
                (day_of(0), 11, "198.51.100.1", sid, "warning", 3),
            ]
        )
        tx.touch_signature(sid, stamp(NOW - timedelta(hours=1)), stamp(NOW - timedelta(hours=1)))
        tx.note_live_data(stamp(NOW - timedelta(days=60)))
    assert detect.run(store, Systems(), NOW, kinds=("novelty",))["findings"] == []


def test_days_the_host_was_silent_do_not_drag_the_deviation_baseline_to_zero(store):
    with store.write() as tx:
        setup_host(tx)
        sid = add_sig(tx)
        # logging (100 a day) on six days, completely silent on the other eight (an outage)
        tx.add_hourly([(day_of(k), 10, "198.51.100.1", sid, "warning", 100) for k in range(1, 7)])
        tx.add_hourly([(day_of(0), 10, "198.51.100.1", sid, "warning", 130)])
    # the old zero-filled baseline had a median of 0 and flagged 130; against its real days it is normal
    assert detect.run(store, Systems(), NOW, kinds=("deviation",), ignore_learning=True)["findings"] == []
    with store.write() as tx:
        tx.add_hourly([(day_of(0), 11, "198.51.100.1", sid, "warning", 400)])  # 530 today: a real spike
    assert len(detect.run(store, Systems(), NOW, kinds=("deviation",), ignore_learning=True)["findings"]) == 1


def test_off_hours_ignores_a_signature_with_too_little_history(store):
    with store.write() as tx:
        setup_host(tx)
        sid = add_sig(tx)
        tx.add_hourly(
            [
                (day_of(1), 10, "198.51.100.1", sid, "warning", 2),
                (day_of(0), 3, "198.51.100.1", sid, "warning", 6),
            ]
        )
    assert detect.run(store, Systems(), NOW, kinds=("off_hours",), ignore_learning=True)["findings"] == []
