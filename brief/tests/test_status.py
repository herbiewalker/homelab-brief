import json
from datetime import UTC, datetime, timedelta

import pytest

from brief import health, registry, status
from brief.store import Store
from brief.systems import System, Systems

NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
RULES = {"gap_multiple": 3, "min_minutes": 30, "max_hours": 24}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "baseline.db")
    yield s
    s.close()


def systems(*pairs, expected=True):
    return Systems([System(i, i, "other", (h,), "UTC", expected) for i, h in pairs])


def add_host(store, host, system, last_seen, gap=None, known=True):
    with store.write() as tx:
        tx.touch_host(host, last_seen, last_seen, system_id=system, known=known)
        if gap:
            tx.set_expected_gap(host, gap)


def ago(**kw):
    return NOW - timedelta(**kw)


def test_silent_limit_scales_with_the_learned_gap_and_is_clamped():
    assert status.silent_limit(None, RULES) == timedelta(hours=24)  # nothing learned: the ceiling
    assert status.silent_limit(5, RULES) == timedelta(minutes=30)  # 3 x 5 is under the floor
    assert status.silent_limit(60, RULES) == timedelta(minutes=180)
    assert status.silent_limit(2000, RULES) == timedelta(hours=24)  # clamped to max_hours


def test_a_host_is_silent_only_after_its_limit(store):
    add_host(store, "198.51.100.1", "gw", ago(minutes=20), gap=5)  # within 30 min
    add_host(store, "198.51.100.2", "nas", ago(minutes=45), gap=5)  # over 30 min
    add_host(store, "198.51.100.3", "cam", ago(hours=5))  # no gap learned: 24 h ceiling
    d = status.derive(
        store, systems(("gw", "198.51.100.1"), ("nas", "198.51.100.2"), ("cam", "198.51.100.3")), RULES, NOW
    )
    assert d["hosts_silent"] == 1 and d["silent"][0]["host"] == "198.51.100.2"
    assert d["hosts_checked"] == 3


def test_unknown_hosts_and_systems_that_are_not_expected_never_count_as_silent(store):
    add_host(store, "198.51.100.9", "other", ago(days=3), known=False)
    add_host(store, "198.51.100.8", "printer", ago(days=3))
    d = status.derive(store, systems(("printer", "198.51.100.8"), expected=False), RULES, NOW)
    assert d["hosts_silent"] == 0 and d["hosts_checked"] == 0


def test_events_include_suppressed_ones_and_only_the_last_24_hours(store):
    with store.write() as tx:
        sig = tx.signature_for(1, 1, "app", "t", ago(days=3))
        tx.add_hourly([("2026-10-03", 11, "h", sig, "info", 100), ("2026-10-01", 5, "h", sig, "info", 999)])
        tx.add_suppressed([("2026-10-03", 10, "h", "noise", 25)])
    assert status.derive(store, Systems(), RULES, NOW)["events_24h"] == 125


def test_new_signatures_counts_by_first_seen(store):
    with store.write() as tx:
        tx.signature_for(1, 1, "a", "old", ago(days=30))
        tx.signature_for(1, 2, "a", "fresh", ago(hours=3))
    assert status.derive(store, Systems(), RULES, NOW)["new_signatures_24h"] == 1


def healthy(monkeypatch):
    monkeypatch.setattr(health, "receiver_ok", lambda port: True)


def build(store, uptime=10, now=NOW, env=None):
    return status.build_status(store, Systems(), 514, 60.0, uptime, env, now)


def test_status_is_ok_with_schema_and_a_summary_headline(store, monkeypatch):
    healthy(monkeypatch)
    doc = build(store)
    assert doc["schema"] == 1 and doc["run_ok"] is True
    assert doc["generated_at"] == "2026-10-03T12:00:00Z"
    assert "0 events in 24 hours" in doc["headline"] and "learning" in doc["headline"]
    assert set(doc["pipeline"]) == {
        "events_24h", "new_signatures_24h", "hosts_silent", "hosts_checked",
        "parse_failed_24h", "ingest_lag_s", "db_mb", *status.SELF_KEYS,
    }  # fmt: skip


def test_a_dead_receiver_gives_run_ok_false_and_no_sensor_values(store, monkeypatch):
    monkeypatch.setattr(health, "receiver_ok", lambda port: False)
    doc = build(store)
    assert doc["run_ok"] is False
    assert doc["headline"].startswith("No data:") and "receiver" in doc["headline"]
    assert registry.values(doc, registry.load()) == {}  # the sensors expire, never a calm network


def test_a_stale_worker_is_reported_after_the_grace_period(store, monkeypatch):
    healthy(monkeypatch)
    doc = build(store, uptime=10_000)
    assert doc["run_ok"] is False and "worker stale" in doc["headline"]


def test_an_unreadable_database_is_run_ok_false_not_zeros(store, monkeypatch):
    healthy(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(status, "derive", boom)
    doc = build(store)
    assert doc["run_ok"] is False and "database RuntimeError" in doc["headline"]


def test_metric_values_when_ok(store, monkeypatch, tmp_path):
    healthy(monkeypatch)
    values = registry.values(build(store), registry.load(shipped_registry(tmp_path)))
    assert {k: values[k] for k in ("events_24h", "new_signatures_24h", "hosts_silent")} == {
        "events_24h": 0,
        "new_signatures_24h": 0,
        "hosts_silent": 0,
    }


def test_write_status_is_atomic_and_valid_json(tmp_path):
    path = status.write_status({"schema": 1, "run_ok": True}, tmp_path)
    assert path == tmp_path / "latest" / "status.json"
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == 1
    assert not (tmp_path / "latest" / "status.json.tmp").exists()


def test_thresholds_come_from_playbook_yaml_with_defaults_when_missing(tmp_path):
    env = {"BRIEF_CONFIG_DIR": str(tmp_path / "c"), "BRIEF_DEFAULTS_DIR": str(tmp_path / "d")}
    assert status.absence_thresholds(env) == RULES and status.learning_days(env) == 7
    folder = tmp_path / "c" / "brief"
    folder.mkdir(parents=True)
    (folder / "playbook.yaml").write_text(
        "thresholds:\n  absence: { gap_multiple: 5, max_hours: 12 }\n  learning: { days: 3 }\n",
        encoding="utf-8",
    )
    assert status.absence_thresholds(env) == {"gap_multiple": 5, "min_minutes": 30, "max_hours": 12}
    assert status.learning_days(env) == 3
    (folder / "playbook.yaml").write_text("thresholds: [not, a, map", encoding="utf-8")
    assert status.absence_thresholds(env) == RULES  # a broken edit falls back, it does not crash


def shipped_registry(tmp_path):
    """An environment whose defaults are the registry.yaml that ships with the product."""
    from pathlib import Path

    folder = tmp_path / "defaults" / "brief"
    folder.mkdir(parents=True)
    (folder / "registry.yaml").write_bytes(
        (Path(__file__).resolve().parents[1] / "registry.yaml").read_bytes()
    )
    return {"BRIEF_DEFAULTS_DIR": str(tmp_path / "defaults"), "BRIEF_CONFIG_DIR": str(tmp_path / "none")}
