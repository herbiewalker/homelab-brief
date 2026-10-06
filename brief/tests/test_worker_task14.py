"""Task 14 through the worker: weekly runs, the config guard each cycle, self-monitoring samples, and a stall."""

import json
import time
from datetime import UTC, datetime, timedelta

from test_worker import events_file, make_env
from test_worker_status import FakePublisher as _Fake
from test_worker_status import healthy

from brief import health
from brief import worker as w


class FakePublisher(_Fake):
    """The sensor publisher plus the alert channel the worker also hands to the lifecycle."""

    def send_alert(self, payload):
        return False

    def send_headline(self, text):
        return False

    def next_feedback(self):
        return None


GOOD = "schema: 1\nsystems:\n  - id: gw\n    label: Gateway\n    hosts: [198.51.100.1]\n"


def worker_with_data(tmp_path, monkeypatch, **extra):
    healthy(monkeypatch)
    env, user = make_env(tmp_path, **extra)
    state = tmp_path / "state"
    events_file(state, "one", "two")
    wk = w.Worker(state, env)
    wk.run_once()
    return wk, user, env


# ---------------------------------------------------------------- weekly


def test_the_weekly_report_runs_once_a_week_at_its_time(tmp_path, monkeypatch):
    wk, _, _ = worker_with_data(tmp_path, monkeypatch)
    monday = datetime(2026, 10, 5, tzinfo=UTC)  # the default time is Monday 06:30
    assert wk.weekly_due(monday.replace(hour=6, minute=0)) is False
    assert wk.weekly_due(monday.replace(hour=7)) is True
    path = wk.run_weekly(monday.replace(hour=7))
    assert path.exists() and path.parent == tmp_path / "out" / "weekly"
    assert wk.weekly_due(monday.replace(hour=9)) is False  # done for this week
    assert wk.weekly_due(monday.replace(hour=9) + timedelta(days=3)) is False
    assert wk.weekly_due((monday + timedelta(days=7)).replace(hour=7)) is True  # the next Monday


def test_the_weekly_report_waits_for_data_and_follows_schedule_yaml(tmp_path, monkeypatch):
    healthy(monkeypatch)
    env, user = make_env(tmp_path)
    wk = w.Worker(tmp_path / "state", env)
    monday_late = datetime(2026, 10, 5, 12, tzinfo=UTC)
    assert wk.weekly_due(monday_late) is False  # a brand-new install has nothing to report
    wk2, user2, _ = worker_with_data(tmp_path / "b", monkeypatch)
    (user2 / "schedule.yaml").write_text('schema: 1\nweekly_day: friday\nweekly_run_utc: "08:00"\n')
    assert wk2.weekly_due(monday_late) is False  # Friday this week
    assert wk2.weekly_due(datetime(2026, 10, 9, 8, 30, tzinfo=UTC)) is True


def test_a_failing_weekly_job_never_stops_ingest(tmp_path, monkeypatch, capsys):
    wk, _, _ = worker_with_data(tmp_path, monkeypatch)
    wk.weekly_due = lambda now=None: True

    def boom(now=None):
        raise RuntimeError("out folder is full")

    wk.run_weekly = boom
    wk._weekly_if_due(False)
    assert "weekly_failed" in capsys.readouterr().out
    wk._weekly_if_due(True)  # a backlog: not tried at all


# ---------------------------------------------------------------- the guard each cycle


def test_a_broken_config_keeps_the_pipeline_running_and_reports_the_error(tmp_path, monkeypatch, capsys):
    wk, user, env = worker_with_data(tmp_path, monkeypatch)
    (user / "systems.yaml").write_text(GOOD)
    wk.run_once()
    assert [s.id for s in wk.systems.systems] == ["gw"]
    (user / "systems.yaml").write_text("schema: 1\nsystems: [broken: : yaml\n")
    wk.run_once()
    assert [s.id for s in wk.systems.systems] == ["gw"]  # the last good copy is still in force
    errors = json.loads((tmp_path / "state" / "config_errors.json").read_text())["errors"]
    assert errors["systems.yaml"]["using"] == "the last good copy"
    assert capsys.readouterr().out.count('"event": "config_error"') == 1


# ---------------------------------------------------------------- self-monitoring


def test_the_hourly_job_samples_the_archive_and_reports_it(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "syslog-1.ndjson").write_bytes(b"x" * 2_000_000)
    wk, _, _ = worker_with_data(tmp_path, monkeypatch, BRIEF_ARCHIVE=str(archive))
    doc = wk.publish_status()
    assert doc["pipeline"]["archive_mb"] == 2.0 and doc["pipeline"]["archive_free_gb"] > 0
    assert (
        doc["pipeline"]["memory_mb"] and doc["pipeline"]["storage_days_left"] is None
    )  # one sample: no trend
    with wk.store.read() as db:
        kinds = {r["metric_id"] for r in db.execute("SELECT metric_id FROM metric")}
    assert {"storage.archive_bytes", "storage.free_bytes", "process.memory_mb"} <= kinds


def test_a_broken_sampler_does_not_stop_the_status(tmp_path, monkeypatch, capsys):
    wk, _, _ = worker_with_data(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(w.selfmon, "record", boom)
    assert wk.publish_status()["run_ok"] is True
    assert "selfmon_failed" in capsys.readouterr().out


# ---------------------------------------------------------------- a simulated stall


def test_a_stalled_worker_expires_the_sensors_and_the_brief_says_no_data_today(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "receiver_ok", lambda port: True)
    env, _ = make_env(tmp_path)
    state = tmp_path / "state"
    events_file(state, "one", "two")
    wk = w.Worker(state, env)
    wk.publisher = FakePublisher()
    wk.started = time.monotonic() - 10_000  # up for hours, yet no cycle has ever completed: it is stuck
    status = wk.publish_status()
    assert status["run_ok"] is False and status["headline"].startswith("No data")
    assert wk.publisher.published == [] and wk.publisher.forgotten == 1  # the sensors expire
    digest = wk.run_daily()
    assert digest.run_ok is False and digest.headline.startswith("No data today")
    brief = (tmp_path / "out" / "latest" / "brief.html").read_text(encoding="utf-8")
    assert "No data today" in brief and "worker stale" in brief
