import json
import threading
from pathlib import Path

from brief import worker as w
from brief.config import config_file

BRIEF = Path(__file__).resolve().parents[1]


def make_env(tmp_path, **extra):
    config = tmp_path / "config"
    (config / "brief").mkdir(parents=True)
    (config / "brief" / "registry.yaml").write_bytes(
        (BRIEF / "registry.yaml").read_bytes()
    )  # the shipped sensors
    return {
        "BRIEF_CONFIG_DIR": str(config),
        "BRIEF_DEFAULTS_DIR": str(BRIEF.parent / "nonexistent"),
        "BRIEF_OUT": str(tmp_path / "out"),
        **extra,
    }, config / "brief"


def event(msg, host="198.51.100.1"):
    return json.dumps({"source_ip": host, "app": "app", "msg": msg, "severity": 6,
                       "ts": "Sep 26 13:22:33", "timestamp": "2026-09-26T17:23:00Z"})  # fmt: skip


def events_file(state, *msgs):
    folder = state / "events"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "2026-09-26.ndjson").write_text("\n".join(event(m) for m in msgs) + "\n", encoding="utf-8")


def test_the_user_config_wins_over_the_default_that_ships_in_the_image(tmp_path):
    env, user = make_env(tmp_path, BRIEF_DEFAULTS_DIR=str(BRIEF.parent))
    defaults = tmp_path / "defaults" / "brief"
    defaults.mkdir(parents=True)
    env["BRIEF_DEFAULTS_DIR"] = str(tmp_path / "defaults")
    assert config_file("ignore.yaml", env) == defaults / "ignore.yaml"
    (user / "ignore.yaml").write_text("schema: 1\n", encoding="utf-8")
    assert config_file("ignore.yaml", env) == user / "ignore.yaml"


def test_a_cycle_ingests_events_with_no_config_at_all(tmp_path):
    env, _ = make_env(tmp_path)
    state = tmp_path / "state"
    events_file(state, "one thing", "another thing")
    worker = w.Worker(state, env)
    result = worker.run_once()
    assert result.events == 2 and result.counted == 2
    assert worker.pipeline.generation == 1


def test_a_broken_config_edit_keeps_the_last_good_version_and_is_logged(tmp_path, capsys):
    env, user = make_env(tmp_path)
    (user / "ignore.yaml").write_text(
        'schema: 1\nignore:\n  - { name: noise, match: "boring" }\n', encoding="utf-8"
    )
    state = tmp_path / "state"
    events_file(state, "boring stuff", "interesting stuff")
    worker = w.Worker(state, env)
    assert worker.run_once().suppressed == 1
    (user / "ignore.yaml").write_text("ignore: [this is: not valid", encoding="utf-8")
    capsys.readouterr()
    with (state / "events" / "2026-09-26.ndjson").open("a", encoding="utf-8") as f:
        f.write(event("boring again") + "\n" + event("new interesting") + "\n")
    result = worker.run_once()
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert any(o["event"] == "config_error" and o["file"] == "ignore.yaml" for o in out)
    assert (result.suppressed, result.counted) == (1, 1)  # the old rule is still in force


def test_systems_yaml_is_picked_up_on_the_next_cycle(tmp_path):
    env, user = make_env(tmp_path)
    state = tmp_path / "state"
    events_file(state, "hello")
    worker = w.Worker(state, env)
    worker.run_once()
    with worker.store.read() as db:
        assert db.execute("SELECT known FROM host").fetchone()[0] == 0
    (user / "systems.yaml").write_text(
        "schema: 1\nsystems:\n  - id: gw\n    hosts: [198.51.100.1]\n", encoding="utf-8"
    )
    with (state / "events" / "2026-09-26.ndjson").open("a", encoding="utf-8") as f:
        f.write(event("hello again") + "\n")
    worker.run_once()
    with worker.store.read() as db:
        assert tuple(db.execute("SELECT known, system_id FROM host").fetchone()) == (1, "gw")


def test_the_loop_survives_a_failing_cycle_and_stops_on_request(tmp_path, capsys):
    env, _ = make_env(tmp_path)
    worker = w.Worker(tmp_path / "state", env)
    calls = {"n": 0}
    stop = threading.Event()

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk hiccup")
        stop.set()
        return type("R", (), {"backlog_bytes": 0})()

    worker.run_once = flaky
    worker.run_forever(stop, interval=0.01)
    assert calls["n"] == 2
    assert "cycle_failed" in capsys.readouterr().out


def test_a_backlog_starts_the_next_cycle_immediately(tmp_path):
    env, _ = make_env(tmp_path)
    worker = w.Worker(tmp_path / "state", env)
    stop = threading.Event()
    results = iter([type("R", (), {"backlog_bytes": 10})(), type("R", (), {"backlog_bytes": 0})()])

    def fake():
        r = next(results)
        if r.backlog_bytes == 0:
            stop.set()
        return r

    worker.run_once = fake
    worker.run_forever(stop, interval=3600)  # would hang for an hour if it waited after a backlog
