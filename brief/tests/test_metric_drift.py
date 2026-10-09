"""Task 18: metric_drift, the detector over any collector metric's own history."""

from datetime import UTC, datetime, timedelta

from test_collectors import fake, lab  # noqa: F401  (fake: the stand-in collector fixture)

from brief import detect
from brief import detectors as d
from brief.store import Store
from brief.systems import Systems

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def rows(recent, baseline, metric_id="unraid_power_w", name="Power draw"):
    return [d.MetricRow(metric_id, name, tuple(recent), tuple(baseline))]


def test_a_recent_mean_far_from_the_earlier_mean_is_a_finding():
    found = d.metric_drift(rows([130.0] * 30, [100.0] * 30))
    assert len(found) == 1
    f = found[0]
    assert f.kind == "metric_drift" and f.id == "metric_drift:unraid_power_w"
    assert f.detail["change_pct"] == 30.0 and f.detail["direction"] == "up" and f.value == 30.0
    assert "Power draw averaged 130" in f.title and "+30%" in f.title and "(100)" in f.title


def test_a_drop_is_a_finding_too():
    f = d.metric_drift(rows([60.0] * 30, [100.0] * 30))[0]
    assert f.detail["direction"] == "down" and f.detail["change_pct"] == -40.0


def test_a_small_change_is_not():
    assert d.metric_drift(rows([110.0] * 30, [100.0] * 30)) == []


def test_it_needs_history_in_both_windows():
    assert d.metric_drift(rows([200.0] * 5, [100.0] * 30)) == []
    assert d.metric_drift(rows([200.0] * 30, [100.0] * 5)) == []


def test_a_baseline_of_zero_cannot_be_judged():
    assert d.metric_drift(rows([5.0] * 30, [0.0] * 30)) == []


def test_the_thresholds_are_arguments():
    assert d.metric_drift(rows([110.0] * 30, [100.0] * 30), min_change_pct=5)
    assert d.metric_drift(rows([130.0] * 3, [100.0] * 3), min_samples=2)


def test_biggest_change_first():
    both = rows([130.0] * 30, [100.0] * 30, "a_one", "A") + rows([300.0] * 30, [100.0] * 30, "b_two", "B")
    assert [f.detail["metric"] for f in d.metric_drift(both)] == ["b_two", "a_one"]


def put(store, metric_id, start, hours, value):
    with store.write() as tx:
        tx.add_metrics(
            [(start + timedelta(hours=h), f"collector.{metric_id}", value(h)) for h in range(hours)]
        )


def test_rows_split_at_the_recent_window_and_replay_sees_only_the_past(tmp_path):
    store = Store(tmp_path / "baseline.db")
    start = NOW - timedelta(days=28)
    put(store, "unraid_power_w", start, 28 * 24, lambda h: 100.0 if h < 21 * 24 else 140.0)
    with store.write() as tx:  # the product's own samples are not judged
        tx.add_metrics([(NOW - timedelta(hours=1), "storage.archive_bytes", 1.0)])
    (row,) = detect.metric_rows(store, NOW, 7, 21)
    assert (
        row.metric_id == "unraid_power_w" and len(row.recent) == 7 * 24 - 1 and len(row.baseline) == 21 * 24
    )
    earlier = detect.metric_rows(store, NOW - timedelta(days=8), 7, 21)  # before the change
    assert earlier == [] or all(max(r.recent) == 100.0 for r in earlier)


def test_detect_run_finds_the_drift_and_names_it_from_the_registry(tmp_path):
    env, user, store = lab(tmp_path, "  unifi:\n    enabled: true\n    key_var: UNIFI_API_KEY\n")
    start = NOW - timedelta(days=28)
    put(store, "unifi_clients", start, 28 * 24, lambda h: 20.0 if h < 21 * 24 else 40.0)
    result = detect.run(store, Systems(), NOW, env, kinds=("metric_drift",), ignore_learning=True)
    (f,) = result["findings"]
    assert f.detail["direction"] == "up" and f.title.startswith("Clients online averaged 40")
    quiet = detect.run(store, Systems(), NOW - timedelta(days=14), env, kinds=("metric_drift",))
    assert quiet["findings"] == []  # as of two weeks ago nothing had changed yet


def test_metric_drift_is_a_daily_detector():
    assert "metric_drift" in detect.DAILY and "metric_drift" in detect.ALL
