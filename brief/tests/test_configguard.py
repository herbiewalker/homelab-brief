"""Task 14: config reload with validation and a last-good fallback, and what a broken file reports."""

import json
from datetime import UTC, datetime
from pathlib import Path

import golden_day as g
import pytest

from brief import config, configguard, detect, outputs
from brief.configguard import ConfigGuard
from brief.routing import Priorities
from brief.schedule import Schedule
from brief.systems import Systems

BRIEF = Path(__file__).resolve().parents[1]
GOOD_SYSTEMS = "schema: 1\nsystems:\n  - id: gw\n    label: Gateway\n    hosts: [198.51.100.1]\n"
OTHER_SYSTEMS = "schema: 1\nsystems:\n  - id: nas\n    label: Storage\n    hosts: [198.51.100.2]\n"


@pytest.fixture
def lab(tmp_path):
    user = tmp_path / "config" / "brief"
    user.mkdir(parents=True)
    defaults = tmp_path / "defaults" / "brief"
    defaults.mkdir(parents=True)
    (defaults / "systems.yaml").write_text("schema: 1\nsystems: []\n")
    env = {"BRIEF_CONFIG_DIR": str(tmp_path / "config"), "BRIEF_DEFAULTS_DIR": str(tmp_path / "defaults")}
    state = tmp_path / "state"
    return env, user, state, ConfigGuard(state, env)


def write(user, name, text):
    (user / name).write_text(text, encoding="utf-8")


def system_ids(env):
    return [s.id for s in Systems.load(config.config_file("systems.yaml", env)).systems]


# ---------------------------------------------------------------- the guard


def test_a_good_file_passes_and_is_kept_as_the_last_good_copy(lab):
    env, user, state, guard = lab
    write(user, "systems.yaml", GOOD_SYSTEMS)
    assert guard.check() == {}
    assert (state / "config-last-good" / "systems.yaml").read_text() == GOOD_SYSTEMS
    assert configguard.read_errors(state) == {} and configguard.findings(state) == []


def test_a_broken_edit_is_replaced_by_the_last_good_copy_for_every_reader(lab, capsys):
    env, user, state, guard = lab
    write(user, "systems.yaml", GOOD_SYSTEMS)
    guard.check()
    write(user, "systems.yaml", "schema: 1\nsystems: [this is: not : valid\n")
    errors = guard.check()
    assert errors["systems.yaml"]["using"] == "the last good copy"
    assert system_ids(env) == ["gw"]  # every config_file() reader now gets the good copy
    out = capsys.readouterr().out
    assert out.count('"event": "config_error"') == 1 and '"file": "systems.yaml"' in out
    guard.check()
    assert "config_error" not in capsys.readouterr().out  # said once, not every cycle


def test_a_file_that_was_never_good_falls_back_to_the_shipped_default(lab):
    env, user, state, guard = lab
    write(user, "systems.yaml", "schema: 1\nsystems:\n  - id: Bad Id\n")
    errors = guard.check()
    assert errors["systems.yaml"]["using"] == "the built-in default"
    assert system_ids(env) == []


def test_fixing_the_file_clears_the_error_and_uses_it_again(lab, capsys):
    env, user, state, guard = lab
    write(user, "systems.yaml", GOOD_SYSTEMS)
    guard.check()
    write(user, "systems.yaml", "not: [valid")
    guard.check()
    write(user, "systems.yaml", OTHER_SYSTEMS)
    assert guard.check() == {}
    assert system_ids(env) == ["nas"] and configguard.read_errors(state) == {}
    assert "config_recovered" in capsys.readouterr().out
    assert (state / "config-last-good" / "systems.yaml").read_text() == OTHER_SYSTEMS


def test_a_deleted_file_is_not_an_error(lab):
    env, user, state, guard = lab
    write(user, "systems.yaml", GOOD_SYSTEMS)
    guard.check()
    (user / "systems.yaml").unlink()
    assert guard.check() == {} and system_ids(env) == []  # back to the shipped default


@pytest.mark.parametrize(
    "name, text",
    [
        ("priorities.yaml", "schema: 1\nkinds:\n  absence: { route: shout_loudly }\n"),
        ("playbook.yaml", "schema: 1\nthresholds:\n  absence: { gap_multiple: lots }\n"),
        ("playbook.yaml", "schema: 1\nthresholds: [1, 2]\n"),
        ("schedule.yaml", 'schema: 1\ndaily_run_utc: "quarter to six"\n'),
        ("schedule.yaml", "schema: 1\nweekly_day: someday\n"),
        ("registry.yaml", "schema: 1\nmetrics:\n  - name: no id here\n"),
        ("ignore.yaml", 'schema: 1\nignore:\n  - name: bad\n    match: "(unclosed"\n'),
        ("notify.yaml", "schema: 9\n"),
        ("views.yaml", "- just\n- a list\n"),
    ],
)
def test_each_kind_of_file_is_checked_for_its_own_rules(lab, name, text):
    env, user, state, guard = lab
    write(user, name, text)
    assert name in guard.check()


def test_files_that_are_fine_are_not_reported(lab):
    env, user, state, guard = lab
    for name in ("priorities.yaml", "playbook.yaml", "registry.yaml", "notify.yaml", "views.yaml", "ai.yaml"):
        write(user, name, (BRIEF / name).read_text(encoding="utf-8"))
    write(user, "schedule.yaml", (BRIEF / "schedule.yaml").read_text(encoding="utf-8"))
    assert guard.check() == {}


def test_a_broken_playbook_keeps_the_last_good_thresholds(lab):
    env, user, state, guard = lab
    write(user, "playbook.yaml", "schema: 1\nthresholds:\n  absence: { max_hours: 12 }\n")
    guard.check()
    write(user, "playbook.yaml", "schema: 1\nthresholds:\n  absence: { max_hours: soon }\n")
    guard.check()
    assert detect.thresholds(env)["absence"]["max_hours"] == 12


def test_a_broken_priorities_file_keeps_the_last_good_routes(lab):
    env, user, state, guard = lab
    write(user, "priorities.yaml", "schema: 1\nkinds:\n  novelty: { rank: 77, route: push_now }\n")
    guard.check()
    write(user, "priorities.yaml", "schema: 1\nkinds:\n  novelty: { route: nonsense }\n")
    guard.check()
    assert Priorities.load(env).kinds["novelty"].rank == 77


def test_the_guard_survives_an_unwritable_state_folder(lab, tmp_path):
    env, user, state, _ = lab
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the state folder should be")
    guard = ConfigGuard(blocker, env)
    write(user, "systems.yaml", GOOD_SYSTEMS)
    assert guard.check() == {}  # nowhere to keep a copy or the errors: still no crash


# ---------------------------------------------------------------- the finding


def test_a_broken_file_becomes_a_config_error_finding_with_a_stable_id(lab):
    env, user, state, guard = lab
    write(user, "schedule.yaml", 'schema: 1\ndaily_run_utc: "nope"\n')
    guard.check()
    (found,) = configguard.findings(state)
    assert found.id == "config_error-schedule.yaml" and found.kind == "config_error"
    assert "schedule.yaml is not valid" in found.title and "built-in default" in found.title
    assert found.system == "brief" and found.escalates is False and found.story


def test_the_daily_brief_reports_a_config_error_and_the_tile_for_the_product(tmp_path):
    store = g.build_store(tmp_path / "work")
    state = tmp_path / "state"
    state.mkdir()
    (state / configguard.ERRORS_FILE).write_text(
        json.dumps(
            {"errors": {"ignore.yaml": {"error": "rules must be a list", "using": "the last good copy"}}}
        )
    )
    try:
        d = outputs.run_daily(
            store, g.SYSTEMS, g.RUN_AT, tmp_path / "out", env=g.golden_env(tmp_path), state_dir=state
        )
    finally:
        store.close()
    assert d.run_ok
    titles = [s.title for s in d.stories]
    assert any("ignore.yaml is not valid" in t for t in titles)
    tiles = {s.name: s.state for s in d.systems}
    assert tiles["Homelab Brief"] == "yellow"  # visible, but a daily-headline item, not a push


def test_a_config_error_alert_is_raised_hourly_and_resolved_when_fixed(tmp_path):
    store = g.build_store(tmp_path / "work")
    state = tmp_path / "state"
    state.mkdir()
    errors = state / configguard.ERRORS_FILE
    errors.write_text(json.dumps({"errors": {"x.yaml": {"error": "bad", "using": "the last good copy"}}}))
    env = g.golden_env(tmp_path)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    try:
        outputs.run_hourly_alerts(store, g.SYSTEMS, now, env, {"run_ok": True, "pipeline": {}}, None, state)
        with store.read() as db:
            row = db.execute("SELECT state FROM alert WHERE finding_id = 'config_error-x.yaml'").fetchone()
        assert row["state"] == "open"
        errors.write_text(json.dumps({"errors": {}}))
        for _ in range(4):  # resolved after a few quiet runs
            outputs.run_hourly_alerts(
                store, g.SYSTEMS, now, env, {"run_ok": True, "pipeline": {}}, None, state
            )
        with store.read() as db:
            row = db.execute("SELECT state FROM alert WHERE finding_id = 'config_error-x.yaml'").fetchone()
        assert row["state"] == "resolved"
    finally:
        store.close()


def test_a_filling_disk_pushes_a_storage_low_alert(tmp_path):
    store = g.build_store(tmp_path / "work")
    env = g.golden_env(tmp_path)
    sent = []

    class Spy:
        def send_alert(self, payload):
            sent.append(payload)
            return True

        def send_headline(self, text):
            return True

    pipeline = {"archive_free_pct": 4.0, "archive_free_gb": 40.0, "storage_days_left": 3.0}
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    try:
        outputs.run_hourly_alerts(
            store, g.SYSTEMS, now, env, {"run_ok": True, "pipeline": pipeline}, Spy(), tmp_path
        )
    finally:
        store.close()
    low = [p for p in sent if p["kind"] == "storage_low"]  # (the golden day also has a silent host)
    assert len(low) == 1 and "running low" in low[0]["title"]


# ---------------------------------------------------------------- the schedule


def test_the_schedule_has_a_daily_and_a_weekly_time():
    s = Schedule.from_mapping({"daily_run_utc": "05:45", "weekly_day": "Friday", "weekly_run_utc": "07:10"})
    assert s.daily == (5, 45) and s.weekly_day == 4 and s.weekly == (7, 10)
    wednesday = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    assert s.daily_at(wednesday) == datetime(2026, 10, 7, 5, 45, tzinfo=UTC)
    assert s.weekly_at(wednesday) == datetime(2026, 10, 9, 7, 10, tzinfo=UTC)
    assert s.weekly_key(wednesday) == "2026-10-05"  # the Monday that week starts on


@pytest.mark.parametrize("bad", [{"daily_run_utc": "25:00"}, {"daily_run_utc": "6"}, {"weekly_day": "x"}])
def test_a_bad_schedule_is_a_config_error(bad):
    with pytest.raises(config.ConfigError):
        Schedule.from_mapping(bad)
