"""The worker's daily job, the parse-failure metric, and /status.json carrying the daily stories."""

import json
from datetime import UTC, datetime, timedelta

from test_worker import events_file, make_env

from brief import health
from brief import worker as w
from brief.settings import Settings


def at(h, m=0, day=4):
    return datetime(2026, 10, day, h, m, tzinfo=UTC)


def seed(worker):
    """One counted event, so the install has received data (an empty install is not a failed night)."""
    with worker.store.write() as tx:
        sid = tx.signature_for(1, 1, "app", "thing", "2026-10-01T00:00:00Z")
        tx.add_hourly([("2026-10-01", 0, "198.51.100.1", sid, "info", 1)])


def schedule(user_dir, text):
    (user_dir / "schedule.yaml").write_text(text, encoding="utf-8")


def test_the_daily_run_is_due_after_its_time_once_a_day(tmp_path):
    env, user = make_env(tmp_path)
    worker = w.Worker(tmp_path / "state", env)
    assert worker.daily_due(at(7)) is False  # nothing has ever arrived: not a failed night, just not started
    seed(worker)
    assert worker.daily_due(at(5, 59)) is False  # before the default 06:00
    assert worker.daily_due(at(6, 0)) is True
    with worker.store.write() as tx:
        tx.set_meta("last_daily_date", "2026-10-04")
    assert worker.daily_due(at(23)) is False  # already ran today
    assert worker.daily_due(at(6, 0, day=5)) is True  # tomorrow it is due again


def test_the_daily_time_comes_from_schedule_yaml_and_a_bad_value_falls_back(tmp_path, capsys):
    env, user = make_env(tmp_path)
    worker = w.Worker(tmp_path / "state", env)
    seed(worker)
    schedule(user, 'schema: 1\ndaily_run_utc: "05:45"\n')
    assert worker.daily_due(at(5, 44)) is False and worker.daily_due(at(5, 45)) is True
    schedule(user, 'schema: 1\ndaily_run_utc: "quarter to six"\n')
    assert worker.daily_due(at(5, 59)) is False and worker.daily_due(at(6, 0)) is True
    assert "config_error" in capsys.readouterr().out


def test_the_worker_runs_the_daily_job_and_remembers_the_date(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "receiver_ok", lambda port: True)
    env, _ = make_env(tmp_path)
    state = tmp_path / "state"
    events_file(state, "one", "two")
    worker = w.Worker(state, env)
    worker.run_once()
    digest = worker.run_daily(at(7))
    assert (tmp_path / "out" / "latest" / "digest.md").exists() and (
        tmp_path / "out" / "latest" / "status.json"
    ).exists()
    assert not digest.run_ok  # the sample events are from September 26th: nothing in the last 24 hours
    with worker.store.read() as db:
        assert (
            db.execute("SELECT value FROM meta WHERE key = 'last_daily_date'").fetchone()[0] == "2026-10-04"
        )


def test_the_daily_job_waits_for_a_backlog_to_clear(tmp_path):
    env, _ = make_env(tmp_path)
    worker = w.Worker(tmp_path / "state", env)
    ran = []
    worker.daily_due = lambda *a, **k: True
    worker.run_daily = lambda *a, **k: ran.append(1)
    worker._daily_if_due(backlog=True)
    assert ran == []
    worker._daily_if_due(backlog=False)
    assert ran == [1]


def test_a_crashing_daily_job_is_logged_and_never_stops_ingest(tmp_path, capsys):
    env, _ = make_env(tmp_path)
    worker = w.Worker(tmp_path / "state", env)
    worker.daily_due = lambda *a, **k: True

    def boom(*a, **k):
        raise RuntimeError("disk full")

    worker.run_daily = boom
    worker._daily_if_due(backlog=False)
    assert "daily_failed" in capsys.readouterr().out


def test_the_hourly_status_keeps_the_daily_stories_the_worker_wrote(tmp_path, monkeypatch):
    ok = {"status": "ok", "checks": {"database": {"ok": True}}}
    monkeypatch.setattr(health, "check_health", lambda *a, **k: ok)
    env, _ = make_env(tmp_path)
    worker = w.Worker(tmp_path / "state", env)
    folder = tmp_path / "out" / "latest"
    folder.mkdir(parents=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    (folder / "status.json").write_text(
        json.dumps(
            {
                "daily_generated_at": stamp,
                "run_ok": True,
                "headline": "From the daily run",
                "stories": [{"id": "x"}],
            }
        ),
        encoding="utf-8",
    )
    doc = worker.publish_status()
    assert doc["stories"] == [{"id": "x"}] and doc["headline"] == "From the daily run"
    assert json.loads((folder / "status.json").read_text())["stories"] == [
        {"id": "x"}
    ]  # and it survives the rewrite


def test_the_ingest_cycle_records_parse_failures(tmp_path):
    env, _ = make_env(tmp_path)
    state = tmp_path / "state"
    (state / "events").mkdir(parents=True)
    bad = {"source_ip": "198.51.100.1", "msg": "x", "parse_failed": True, "timestamp": "2026-09-26T17:23:00Z"}
    (state / "events" / "a.ndjson").write_text(
        "\n".join(json.dumps(bad) for _ in range(3)) + "\n", encoding="utf-8"
    )
    worker = w.Worker(state, env)
    worker.run_once()
    with worker.store.read() as db:
        assert (
            db.execute("SELECT value FROM metric WHERE metric_id = 'ingest.parse_failed'").fetchone()[0]
            == 3.0
        )


def test_settings_know_the_output_folder(tmp_path):
    env = {
        "BRIEF_OUT": str(tmp_path / "o"),
        "BRIEF_CONFIG_DIR": str(tmp_path),
        "BRIEF_DEFAULTS_DIR": str(tmp_path),
    }
    assert Settings.from_env(env).out_dir == tmp_path / "o"
    assert str(
        Settings.from_env({"BRIEF_CONFIG_DIR": str(tmp_path), "BRIEF_DEFAULTS_DIR": str(tmp_path)}).out_dir
    ) in (
        "/out",
        "\\out",
    )


def test_the_web_status_json_carries_the_daily_stories(tmp_path, monkeypatch):
    from test_web import Env

    ok = {"status": "ok", "checks": {"database": {"ok": True}}}
    monkeypatch.setattr(health, "check_health", lambda *a, **k: ok)
    env = Env(tmp_path, monkeypatch, out_dir=tmp_path / "out")
    try:
        folder = tmp_path / "out" / "latest"
        folder.mkdir(parents=True)
        stamp = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (folder / "status.json").write_text(
            json.dumps(
                {
                    "daily_generated_at": stamp,
                    "run_ok": True,
                    "headline": "3 things need a look",
                    "stories": [1],
                }
            ),
            encoding="utf-8",
        )
        doc = env.http().get("/status.json").json()
        assert doc["stories"] == [1] and doc["headline"] == "3 things need a look"
    finally:
        env.store.close()


# ---------------------------------------------------------------- alerts through the worker


class Channel:
    """Stands in for the MQTT publisher: records alerts and headlines, and queues Home Assistant feedback."""

    def __init__(self):
        self.alerts, self.headlines, self.queue, self.published = [], [], [], []

    def send_alert(self, payload):
        self.alerts.append(payload)
        return True

    def send_headline(self, text):
        self.headlines.append(text)
        return True

    def next_feedback(self):
        return self.queue.pop(0) if self.queue else None

    def publish(self, values):
        self.published.append(values)

    def forget(self):
        pass


def alerting_worker(tmp_path, monkeypatch):
    """A worker with one expected host that has been silent for years, and no quiet hours."""
    ok = {"status": "ok", "checks": {"database": {"ok": True}}}
    monkeypatch.setattr(health, "check_health", lambda *a, **k: ok)
    env, user = make_env(tmp_path)
    (user / "systems.yaml").write_text(
        "schema: 1\nsystems:\n  - id: sys1\n    label: Gateway\n    hosts: [198.51.100.1]\n", encoding="utf-8"
    )
    (user / "priorities.yaml").write_text(
        "schema: 1\nquiet_hours: { start: '00:00', end: '00:00' }\n"
        "kinds:\n  absence: { rank: 80, route: push_now }\n  pipeline_error: { rank: 90, route: push_now }\n",
        encoding="utf-8",
    )
    worker = w.Worker(tmp_path / "state", env)
    with worker.store.write() as tx:
        tx.touch_host("198.51.100.1", "2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z", system_id="sys1")
    worker.publisher = Channel()
    return worker


def test_the_hourly_job_sends_an_alert_for_a_silent_host_once(tmp_path, monkeypatch):
    worker = alerting_worker(tmp_path, monkeypatch)
    worker.publish_status()
    worker.publish_status()  # an hour later: nothing new to say
    sent = worker.publisher.alerts
    assert [(a["state"], a["finding_id"], a["system"]) for a in sent] == [
        ("new", "absence-198.51.100.1", "sys1")
    ]
    assert sent[0]["actions"] == ["ack", "snooze_24h", "snooze_7d"]


def test_a_button_press_from_home_assistant_stops_the_repeats(tmp_path, monkeypatch):
    worker = alerting_worker(tmp_path, monkeypatch)
    worker.publish_status()
    worker.publisher.queue.append(("absence-198.51.100.1", "ack"))
    worker._apply_feedback()
    with worker.store.read() as db:
        assert db.execute("SELECT state FROM alert").fetchone()[0] == "acked"
        assert db.execute("SELECT action FROM feedback").fetchone()[0] == "ack"
    assert worker.publisher.queue == []


def test_a_bad_feedback_message_never_stops_the_worker(tmp_path, monkeypatch, capsys):
    worker = alerting_worker(tmp_path, monkeypatch)
    worker.publisher.next_feedback = lambda: (_ for _ in ()).throw(RuntimeError("queue broke"))
    worker._apply_feedback()
    assert "feedback_failed" in capsys.readouterr().out
    worker.publisher = None
    worker._apply_feedback()  # without MQTT there is nothing to apply, and nothing breaks


def test_a_failing_alert_run_is_logged_and_the_status_still_goes_out(tmp_path, monkeypatch, capsys):
    worker = alerting_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(w, "run_hourly_alerts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    doc = worker.publish_status()
    assert "alerts_failed" in capsys.readouterr().out and doc["run_ok"] is True
    assert (tmp_path / "out" / "latest" / "status.json").exists()


def test_the_daily_job_sends_the_headline_through_the_channel(tmp_path, monkeypatch):
    worker = alerting_worker(tmp_path, monkeypatch)
    worker.run_daily(at(7))  # no events in the last 24 hours: the failure path, which still says so
    assert worker.publisher.headlines and worker.publisher.headlines[0].startswith("No data today")
    assert [a["kind"] for a in worker.publisher.alerts] == ["pipeline_error"]
