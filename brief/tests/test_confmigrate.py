"""Task 17: config files are brought up to this version's schema on upgrade, safely."""

import tarfile
from datetime import UTC, datetime

import pytest

from brief import __version__, confmigrate

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
V1 = "schema: 1\nsystems:\n  - id: gw\n    label: Gateway\n    hosts: [198.51.100.1]\n"


@pytest.fixture
def lab(tmp_path):
    user = tmp_path / "config" / "brief"
    user.mkdir(parents=True)
    env = {"BRIEF_CONFIG_DIR": str(tmp_path / "config")}
    return env, user, tmp_path / "state"


def test_nothing_to_do_changes_nothing_and_stamps_the_version(lab):
    env, user, state = lab
    state.mkdir()
    (user / "systems.yaml").write_text(V1)
    report = confmigrate.run(state, env, NOW)
    assert report.migrated == [] and report.backup is None
    assert (user / "systems.yaml").read_text() == V1
    assert (state / confmigrate.VERSION_FILE).read_text().strip() == __version__


def test_an_old_file_is_migrated_after_a_backup(lab, monkeypatch):
    env, user, state = lab
    state.mkdir()
    (user / "systems.yaml").write_text(V1)
    (user / "ignore.yaml").write_text("schema: 1\nrules: []\n")
    monkeypatch.setitem(confmigrate.CURRENT, "systems.yaml", 2)
    monkeypatch.setitem(confmigrate.STEPS, ("systems.yaml", 1), lambda text: text)
    # the real validator accepts only the current schema; a real bump changes it in the same commit
    monkeypatch.setitem(confmigrate.VALIDATORS, "systems.yaml", lambda path: None)
    report = confmigrate.run(state, env, NOW)
    assert report.migrated == ["systems.yaml"] and report.failed == {}
    assert (user / "systems.yaml").read_text().startswith("schema: 2\n")
    assert report.backup is not None and report.backup.name == f"pre-{__version__}-20260102-030405.tar.gz"
    with tarfile.open(report.backup) as tar:
        assert tar.extractfile("config/systems.yaml").read().decode() == V1
        assert "config/ignore.yaml" in tar.getnames()
    assert (user / "ignore.yaml").read_text() == "schema: 1\nrules: []\n"  # up to date: left alone


def test_a_step_that_makes_an_invalid_file_leaves_the_original(lab, monkeypatch):
    env, user, state = lab
    state.mkdir()
    (user / "systems.yaml").write_text(V1)
    monkeypatch.setitem(confmigrate.CURRENT, "systems.yaml", 2)
    monkeypatch.setitem(confmigrate.STEPS, ("systems.yaml", 1), lambda text: "- not a mapping\n")
    report = confmigrate.run(state, env, NOW)
    assert report.migrated == [] and "systems.yaml" in report.failed
    assert (user / "systems.yaml").read_text() == V1


def test_a_missing_step_is_a_failure_not_a_crash(lab, monkeypatch):
    env, user, state = lab
    state.mkdir()
    (user / "systems.yaml").write_text(V1)
    monkeypatch.setitem(confmigrate.CURRENT, "systems.yaml", 3)
    report = confmigrate.run(state, env, NOW)
    assert "no migration from schema 1" in report.failed["systems.yaml"]
    assert (user / "systems.yaml").read_text() == V1


def test_a_newer_file_is_never_touched(lab):
    env, user, state = lab
    state.mkdir()
    text = V1.replace("schema: 1", "schema: 9")
    (user / "systems.yaml").write_text(text)
    report = confmigrate.run(state, env, NOW)
    assert report.too_new == ["systems.yaml"] and report.migrated == []
    assert (user / "systems.yaml").read_text() == text


def test_broken_yaml_is_left_for_the_config_guard(lab):
    env, user, state = lab
    state.mkdir()
    (user / "systems.yaml").write_text("schema: 1\nsystems: [unclosed\n")
    report = confmigrate.run(state, env, NOW)
    assert report.migrated == [] and report.failed == {}


def test_only_the_newest_backups_are_kept(lab, monkeypatch):
    env, user, state = lab
    state.mkdir()
    (user / "systems.yaml").write_text(V1)
    folder = state / confmigrate.BACKUP_DIR
    folder.mkdir()
    for n in range(14):
        (folder / f"pre-0.0.{n:02d}-20250101-000000.tar.gz").write_bytes(b"x")
    monkeypatch.setitem(confmigrate.CURRENT, "systems.yaml", 2)
    monkeypatch.setitem(confmigrate.STEPS, ("systems.yaml", 1), lambda text: text)
    confmigrate.run(state, env, NOW)
    assert len(list(folder.glob("pre-*.tar.gz"))) == confmigrate.KEEP_BACKUPS


def test_a_downgrade_is_noticed_and_does_not_restamp(lab, capsys):
    env, user, state = lab
    state.mkdir()
    (state / confmigrate.VERSION_FILE).write_text("99.0.0\n")
    confmigrate.run(state, env, NOW)
    assert "downgrade_detected" in capsys.readouterr().out
    assert (state / confmigrate.VERSION_FILE).read_text().strip() == "99.0.0"


def test_a_read_only_state_folder_does_not_stop_the_start(lab, tmp_path):
    env, user, _ = lab
    blocker = tmp_path / "state-is-a-file"
    blocker.write_text("x")
    (user / "systems.yaml").write_text(V1)
    report = confmigrate.run(blocker, env, NOW)  # cannot create files under a file
    assert report.migrated == []
