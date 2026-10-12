from datetime import UTC, datetime, timedelta

import pytest

from brief import systemhistory as sh
from brief.store import Store

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "baseline.db")
    yield s
    s.close()


def status(*pairs, run_ok=True):
    return {"run_ok": run_ok, "systems": [{"id": i, "state": st} for i, st in pairs]}


def test_the_worst_state_of_the_day_wins_and_never_improves(store):
    sh.record(store, status(("nas", "green")), NOW)
    sh.record(store, status(("nas", "red")), NOW + timedelta(hours=1))
    sh.record(store, status(("nas", "green")), NOW + timedelta(hours=2))
    cell = sh.bars(store, NOW)["nas"][-1]
    assert cell["state"] == "red" and cell["kind"] == "bad" and cell["day"] == "2026-10-11"


def test_a_day_nobody_recorded_is_no_data_not_green(store):
    sh.record(store, status(("nas", "green")), NOW - timedelta(days=3))
    cells = sh.bars(store, NOW)["nas"]
    assert len(cells) == sh.DAYS
    assert cells[-4]["state"] == "green"
    assert [c["state"] for c in cells[-3:]] == ["none", "none", "none"]
    assert sh.uptime_pct(cells) is None  # one recorded day is not enough to quote an uptime


def test_a_failed_run_records_nothing(store):
    assert sh.record(store, status(("nas", "green"), run_ok=False), NOW) == 0
    assert sh.bars(store, NOW) == {}


def test_uptime_is_none_with_nothing_recorded():
    assert sh.uptime_pct(sh.empty_bar(NOW)) is None


def test_uptime_counts_only_recorded_days(store):
    for back, state in ((2, "green"), (1, "yellow"), (0, "green")):
        sh.record(store, status(("nas", state)), NOW - timedelta(days=back))
    assert sh.uptime_pct(sh.bars(store, NOW)["nas"]) == pytest.approx(66.7)


def test_events_by_hour_is_a_full_zero_filled_day(store):
    with store.write() as tx:
        sig = tx.signature_for(1, 1, "app", "x", NOW)
        tx.add_hourly([("2026-10-11", 11, "198.51.100.1", sig, "info", 7)])
    series = sh.events_by_hour(store, NOW)
    assert len(series) == 24 and series[-2] == 7.0 and sum(series) == 7.0


def test_metric_series_is_thinned_to_the_requested_points(store):
    with store.write() as tx:
        tx.add_metrics([(NOW - timedelta(minutes=10 * i), "process.memory_mb", float(i)) for i in range(60)])
    series = sh.metric_series(store, "process.memory_mb", NOW, hours=24, points=12)
    assert len(series) == 12


def test_timeline_lists_recent_notifications_newest_first(store):
    with store.write() as tx:
        tx.add_push("f1", "new", "alert", "warn", "Disk errors", False, NOW - timedelta(hours=5))
        tx.add_push("f1", "resolved", "alert", "warn", "Disk errors", False, NOW - timedelta(hours=1))
        tx.add_push("h", "new", "headline", None, "Headline", False, NOW)
    rows = sh.timeline(store, NOW)
    assert [r["word"] for r in rows] == ["Resolved", "Opened"] and rows[0]["kind"] == "ok"
