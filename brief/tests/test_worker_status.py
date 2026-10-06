"""The worker's hourly job: status.json plus the MQTT sensors (Task 7)."""

import json
import threading

from test_worker import events_file, make_env

from brief import health
from brief import worker as w


class FakePublisher:
    def __init__(self):
        self.published, self.forgotten, self.started, self.metrics = [], 0, False, []

    def start(self):
        self.started = True

    def publish(self, values):
        self.published.append(values)

    def set_metrics(self, metrics):
        self.metrics = metrics

    def forget(self):
        self.forgotten += 1


def healthy(monkeypatch):
    monkeypatch.setattr(health, "receiver_ok", lambda port: True)


def idle_cycle(stop, stop_after=1):
    calls = {"n": 0}

    def cycle():
        calls["n"] += 1
        if calls["n"] >= stop_after:
            stop.set()
        return type("R", (), {"backlog_bytes": 0})()

    return cycle


def test_the_hourly_job_writes_status_json_and_publishes_the_sensors(tmp_path, monkeypatch):
    healthy(monkeypatch)
    env, _ = make_env(tmp_path)
    state = tmp_path / "state"
    events_file(state, "one", "two")
    worker = w.Worker(state, env)
    worker.publisher = FakePublisher()
    worker.run_once()
    doc = worker.publish_status()
    written = json.loads((tmp_path / "out" / "latest" / "status.json").read_text(encoding="utf-8"))
    assert written == doc and doc["run_ok"] is True
    pipeline = doc["pipeline"]  # the sample events are from 2026-09-26, so the 24-hour counts are 0
    published = worker.publisher.published
    assert len(published) == 1 and set(published[0]) >= {
        "events_24h", "new_signatures_24h", "hosts_silent", "parse_failed_24h", "ingest_lag_s", "db_mb",
    }  # fmt: skip
    assert all(published[0][k] == pipeline[k] for k in published[0])


def test_a_failed_run_publishes_no_values(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "receiver_ok", lambda port: False)
    env, _ = make_env(tmp_path)
    worker = w.Worker(tmp_path / "state", env)
    worker.publisher = FakePublisher()
    doc = worker.publish_status()
    assert doc["run_ok"] is False and doc["headline"].startswith("No data")
    assert worker.publisher.published == [] and worker.publisher.forgotten == 1


def test_the_loop_publishes_once_per_interval_after_a_cycle(tmp_path, monkeypatch):
    healthy(monkeypatch)
    env, _ = make_env(tmp_path)
    worker = w.Worker(tmp_path / "state", env)
    worker.publisher = FakePublisher()
    stop = threading.Event()
    worker.run_once = idle_cycle(stop, stop_after=3)
    worker.run_forever(stop, interval=0.001, publish_every=3600)
    assert worker.publisher.started and len(worker.publisher.published) == 1  # three cycles, one hourly job


def test_a_broken_status_job_never_stops_ingest(tmp_path, capsys):
    env, _ = make_env(tmp_path)
    worker = w.Worker(tmp_path / "state", env)

    def boom():
        raise RuntimeError("out folder is full")

    worker.publish_status = boom
    stop = threading.Event()
    worker.run_once = idle_cycle(stop)
    worker.run_forever(stop, interval=0.001)
    assert "status_failed" in capsys.readouterr().out


def test_mqtt_is_off_without_a_host_and_a_bad_port_is_logged_not_fatal(tmp_path, capsys):
    env, _ = make_env(tmp_path)
    assert w.Worker(tmp_path / "s1", env).publisher is None
    bad = w.Worker(tmp_path / "s2", {**env, "MQTT_HOST": "broker.example", "MQTT_PORT": "nope"})
    assert bad.publisher is None and "mqtt_config_error" in capsys.readouterr().out
