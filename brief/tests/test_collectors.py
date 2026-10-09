"""Task 18: the collector framework. Enabling or disabling one adds or removes its metrics with no other
change, and a failing collector never breaks the run."""

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from test_worker import make_env
from test_worker_status import FakePublisher, healthy

from brief import registry
from brief import worker as w
from brief.collectors import Collector, CollectorError, Context, framework
from brief.config import ConfigError
from brief.status import build_status
from brief.store import Store
from brief.systems import Systems

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


class Fake(Collector):
    """Stands in for a real collector; the tests change `behaviour` to make it succeed or fail."""

    name = "unifi"
    behaviour = staticmethod(lambda ctx: {"unifi_clients": 7, "unifi_ignored": 1, "unifi_text": "x"})
    calls = 0

    def metrics(self, config):
        return [
            {"id": "unifi_clients", "name": "Clients online", "unit": "clients"},
            {"id": "unifi_aps", "name": "Access points"},
        ]

    def validate(self, config):
        if config.get("bad"):
            raise ConfigError("bad is not allowed")

    def test(self, ctx):
        ctx.var("key_var")
        return "reached the controller"

    def run(self, ctx):
        type(self).calls += 1
        return type(self).behaviour(ctx)


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    Fake.calls = 0
    Fake.behaviour = staticmethod(lambda ctx: {"unifi_clients": 7, "unifi_ignored": 1, "unifi_text": "x"})
    monkeypatch.setitem(framework.REGISTRY, "unifi", Fake)
    framework._hung.clear()


def lab(tmp_path, collectors_yaml: str, **extra):
    env, user = make_env(tmp_path, **extra)
    (user / "collectors.yaml").write_text("schema: 1\ncollectors:\n" + collectors_yaml, encoding="utf-8")
    return env, user, Store(tmp_path / "state" / "baseline.db")


ON = "  unifi:\n    enabled: true\n    key_var: UNIFI_API_KEY\n"
OFF = "  unifi:\n    enabled: false\n    key_var: UNIFI_API_KEY\n"
KEY = {"UNIFI_API_KEY": "not-a-real-key"}


# ---------------------------------------------------------------- the file


@pytest.mark.parametrize(
    "text, message",
    [
        ("  nope:\n    enabled: true\n", "unknown collector"),
        ("  unifi:\n    enabled: yes please\n", "true or false"),
        ("  unifi:\n    interval: sometimes\n", "interval"),
        ("  unifi:\n    interval: 500h\n", "interval"),
        ("  unifi:\n    timeout_s: 0\n", "timeout_s"),
        ("  unifi:\n    api_key: abc123\n", "credential"),
        ("  unifi:\n    token: abc123\n", "credential"),
        ("  snmp:\n    targets:\n      - { host: x, community: public }\n", "credential"),
        ("  unifi:\n    key_var: not a name\n", "variable name"),
        ("  unifi:\n    bad: true\n", "bad is not allowed"),
        ("  unifi: just text\n", "must be a mapping"),
    ],
)
def test_a_bad_file_is_refused_with_a_message_that_names_the_problem(tmp_path, text, message):
    _, user, _ = lab(tmp_path, text)
    with pytest.raises(ConfigError, match=message):
        framework.validate(user / "collectors.yaml")


def test_the_shipped_file_is_valid_and_everything_starts_disabled():
    from pathlib import Path

    shipped = Path(__file__).resolve().parents[1] / "collectors.yaml"
    settings = framework.validate(shipped)
    assert settings and not any(s.enabled for s in settings.values())


def test_intervals():
    assert framework.parse_interval("hourly") == timedelta(hours=1)
    assert framework.parse_interval("daily") == timedelta(days=1)
    assert framework.parse_interval("6h") == timedelta(hours=6)


def test_required_variables_are_found_at_any_depth():
    config = {"key_var": "A_KEY", "servers": [{"id": "x", "key_var": "B_KEY"}, {"id": "y", "key_var": ""}]}
    assert framework.required_vars(config) == ["A_KEY", "B_KEY"]


# ---------------------------------------------------------------- enabling and disabling


def test_enabling_a_collector_adds_its_metrics_and_disabling_removes_them(tmp_path):
    env, user, _ = lab(tmp_path, ON)
    ids = {m["id"] for m in registry.load(env, Systems())}
    assert {"collector_unifi_ok", "unifi_clients", "unifi_aps"} <= ids
    (user / "collectors.yaml").write_text("schema: 1\ncollectors:\n" + OFF, encoding="utf-8")
    ids = {m["id"] for m in registry.load(env, Systems())}
    assert not {i for i in ids if "unifi" in i}


def test_a_metric_that_does_not_start_with_the_collector_name_is_skipped(tmp_path, monkeypatch):
    env, _, _ = lab(tmp_path, ON)
    monkeypatch.setattr(Fake, "metrics", lambda self, c: [{"id": "events_24h", "name": "steal"}])
    ids = [m["id"] for m in framework.metric_entries(env)]
    assert ids == ["collector_unifi_ok"]


def test_a_broken_collectors_file_means_no_collector_metrics_not_a_crash(tmp_path):
    env, user, _ = lab(tmp_path, ON)
    (user / "collectors.yaml").write_text("schema: 1\ncollectors: [oops\n", encoding="utf-8")
    assert framework.metric_entries(env) == []


# ---------------------------------------------------------------- running


def test_a_run_stores_state_numbers_and_history(tmp_path):
    env, _, store = lab(tmp_path, ON)
    env |= KEY
    outcome = framework.run_due(store, env, NOW)
    assert outcome == {"unifi": {"ran": True, "ok": True, "error": None}}
    state = framework.read_state(store)["unifi"]
    assert state["ok"] and state["metrics"] == {"unifi_clients": 7}  # only declared numbers are kept
    with store.read() as db:
        rows = db.execute("SELECT metric_id, value FROM metric WHERE metric_id LIKE 'collector.%'").fetchall()
    assert [(r["metric_id"], r["value"]) for r in rows] == [("collector.unifi_clients", 7.0)]


def test_a_collector_runs_again_only_when_its_interval_has_passed(tmp_path):
    env, _, store = lab(tmp_path, ON)
    env |= KEY
    framework.run_due(store, env, NOW)
    assert framework.run_due(store, env, NOW + timedelta(minutes=30))["unifi"] == {"ran": False}
    assert framework.run_due(store, env, NOW + timedelta(minutes=56))["unifi"]["ran"] is True
    assert Fake.calls == 2
    assert framework.run_due(store, env, NOW + timedelta(minutes=57), force=True)["unifi"]["ran"] is True


def test_a_failing_collector_keeps_its_last_data_and_hides_the_details(tmp_path):
    env, _, store = lab(tmp_path, ON)
    env |= KEY
    framework.run_due(store, env, NOW)

    def boom(ctx):
        raise ValueError("could not reach 198.51.100.7 with key hunter2")

    Fake.behaviour = staticmethod(boom)
    outcome = framework.run_due(store, env, NOW + timedelta(hours=1))
    assert outcome["unifi"] == {"ran": True, "ok": False, "error": "unexpected error (ValueError)"}
    state = framework.read_state(store)["unifi"]
    assert not state["ok"] and state["metrics"] == {"unifi_clients": 7} and state["last_ok"]
    assert "198.51.100.7" not in json.dumps(state) and "hunter2" not in json.dumps(state)


def test_a_collector_error_message_is_shown(tmp_path):
    env, _, store = lab(tmp_path, ON)
    env |= KEY

    def refused(ctx):
        raise CollectorError("the API key was rejected")

    Fake.behaviour = staticmethod(refused)
    assert framework.run_due(store, env, NOW)["unifi"]["error"] == "the API key was rejected"


def test_one_failing_collector_does_not_stop_another(tmp_path, monkeypatch):
    class Other(Collector):
        name = "dns_filter"

        def metrics(self, config):
            return [{"id": "dns_filter_queries", "name": "Queries"}]

        def run(self, ctx):
            return {"dns_filter_queries": 3}

    monkeypatch.setitem(framework.REGISTRY, "dns_filter", Other)
    env, _, store = lab(tmp_path, ON + "  dns_filter:\n    enabled: true\n")
    env |= KEY

    def boom(ctx):
        raise RuntimeError("x")

    Fake.behaviour = staticmethod(boom)
    outcome = framework.run_due(store, env, NOW)
    assert outcome["unifi"]["ok"] is False and outcome["dns_filter"]["ok"] is True


def test_a_missing_variable_is_named_and_the_collector_is_not_started(tmp_path):
    env, _, store = lab(tmp_path, ON)
    outcome = framework.run_due(store, env, NOW)
    assert outcome["unifi"]["error"] == "these variables are not set: UNIFI_API_KEY"
    assert Fake.calls == 0


def test_a_slow_collector_is_cut_off_and_not_started_twice(tmp_path):
    env, _, store = lab(tmp_path, ON.replace("enabled: true", "enabled: true\n    timeout_s: 1"))
    env |= KEY
    release = threading.Event()
    Fake.behaviour = staticmethod(lambda ctx: release.wait(10) and {})
    first = framework.run_due(store, env, NOW)
    assert first["unifi"]["error"] == "timed out after 1 seconds"
    second = framework.run_due(store, env, NOW + timedelta(hours=1))
    assert second["unifi"]["error"] == "the previous run is still hanging"
    release.set()
    framework._hung["unifi"].join(2)
    assert framework.run_due(store, env, NOW + timedelta(hours=2))["unifi"]["ran"] is True


def test_an_enabled_collector_this_version_does_not_have_fails_politely(tmp_path):
    env, _, store = lab(tmp_path, "  dns_filter:\n    enabled: true\n")
    outcome = framework.run_due(store, env, NOW)
    assert outcome["dns_filter"]["error"] == "this collector is not available in this version"
    assert [m["id"] for m in framework.metric_entries(env)] == ["collector_dns_filter_ok"]


def test_a_non_mapping_result_is_a_failure(tmp_path):
    env, _, store = lab(tmp_path, ON)
    env |= KEY
    Fake.behaviour = staticmethod(lambda ctx: None)
    assert framework.run_due(store, env, NOW)["unifi"]["ok"] is False


# ---------------------------------------------------------------- the connection test


def test_connection_test_works_before_enabling_and_stores_nothing(tmp_path):
    env, _, store = lab(tmp_path, OFF)
    assert framework.test_connection("unifi", store, env | KEY, NOW) == (True, "reached the controller")
    ok, message = framework.test_connection("unifi", store, env, NOW)
    assert not ok and "UNIFI_API_KEY" in message
    assert framework.read_state(store) == {}
    assert framework.test_connection("nope", store, env, NOW)[0] is False
    assert framework.test_connection("snmp", store, env, NOW) == (False, "snmp is not in collectors.yaml")


# ---------------------------------------------------------------- what reaches the outputs


def test_registry_values_carry_the_numbers_and_the_ok_flag(tmp_path):
    env, _, store = lab(tmp_path, ON)
    env |= KEY
    framework.run_due(store, env, NOW)
    status = build_status(store, Systems(), 514, 60, 5, env, now=NOW)
    status["run_ok"] = True
    metrics = registry.load(env, Systems())
    assert registry.values(status, metrics)["unifi_clients"] == 7
    assert registry.values(status, metrics)["collector_unifi_ok"] == 1

    def boom(ctx):
        raise RuntimeError("x")

    Fake.behaviour = staticmethod(boom)
    framework.run_due(store, env, NOW + timedelta(hours=1))
    status = build_status(store, Systems(), 514, 60, 5, env, now=NOW)
    status["run_ok"] = True
    values = registry.values(status, metrics)
    assert values["collector_unifi_ok"] == 0 and values["unifi_clients"] == 7  # last good data stays


def test_a_disabled_collector_is_absent_from_the_status(tmp_path):
    env, user, store = lab(tmp_path, ON)
    env |= KEY
    framework.run_due(store, env, NOW)
    assert "unifi" in build_status(store, Systems(), 514, 60, 5, env, now=NOW)["collectors"]
    (user / "collectors.yaml").write_text("schema: 1\ncollectors:\n" + OFF, encoding="utf-8")
    assert build_status(store, Systems(), 514, 60, 5, env, now=NOW)["collectors"] == {}


def test_the_hourly_job_runs_collectors_and_publishes_their_sensors(tmp_path, monkeypatch):
    healthy(monkeypatch)
    env, _, _ = lab(tmp_path, ON, **KEY)
    worker = w.Worker(tmp_path / "state", env)
    worker.publisher = FakePublisher()
    worker.run_once()
    doc = worker.publish_status()
    assert doc["collectors"]["unifi"]["ok"] is True
    assert {"collector_unifi_ok", "unifi_clients"} <= {m["id"] for m in worker.publisher.metrics}


def test_the_hourly_job_survives_a_collector_that_explodes(tmp_path, monkeypatch):
    healthy(monkeypatch)
    env, _, _ = lab(tmp_path, ON, **KEY)

    def boom(ctx):
        raise RuntimeError("x")

    Fake.behaviour = staticmethod(boom)
    worker = w.Worker(tmp_path / "state", env)
    worker.publisher = FakePublisher()
    worker.run_once()
    doc = worker.publish_status()
    assert doc["run_ok"] is True and doc["collectors"]["unifi"]["ok"] is False


def test_context_var_reads_the_named_variable(tmp_path):
    ctx = Context("unifi", {"key_var": "UNIFI_API_KEY"}, KEY, None, NOW)
    assert ctx.var("key_var") == "not-a-real-key"
    with pytest.raises(CollectorError, match="not set in collectors.yaml"):
        ctx.var("url_var")
    with pytest.raises(CollectorError, match="the variable OTHER is not set"):
        Context("unifi", {"x_var": "OTHER"}, {}, None, NOW).var("x_var")
