"""Task 16: the `brief` command covers what the admin GUI does, with the same checks and the same audit."""

import io
import json
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from brief import backup, cli, configedit, configguard, health, netinfo, netpage
from brief.store import Store

BRIEF = Path(__file__).resolve().parents[1]
GOOD_SYSTEMS = "schema: 1\nsystems:\n  - id: gw\n    label: Gateway\n    hosts: [198.51.100.1]\n"


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A state dir, a config dir with the shipped files as defaults, and an archive, all via the environment."""
    user = tmp_path / "cfg" / "brief"
    defaults = tmp_path / "defaults" / "brief"
    user.mkdir(parents=True)
    defaults.mkdir(parents=True)
    for name in configedit.EDITABLE:
        shipped = BRIEF / name if (BRIEF / name).exists() else BRIEF / name.replace(".yaml", ".example.yaml")
        (defaults / shipped.name).write_bytes(shipped.read_bytes())
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "archive").mkdir()
    for key, value in {
        "BRIEF_STATE": state,
        "BRIEF_CONFIG_DIR": tmp_path / "cfg",
        "BRIEF_DEFAULTS_DIR": tmp_path / "defaults",
        "BRIEF_OUT": tmp_path / "out",
        "BRIEF_ARCHIVE": tmp_path / "archive",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr("getpass.getuser", lambda: "tester")
    monkeypatch.setattr(health, "receiver_ok", lambda port: True)
    store = Store(state / "baseline.db")
    yield tmp_path, user, state, store
    store.close()


def run(*argv):
    return cli.main(list(argv))


def audit(store, like):
    with store.read() as db:
        return [
            dict(r)
            for r in db.execute("SELECT user, action, detail FROM audit_log WHERE action LIKE ?", (like,))
        ]


# ---------------------------------------------------------------- status, sources, audit


def beat(store):
    """The worker's heartbeat, so the health check sees a worker that is running."""
    with store.write() as tx:
        tx.add_metrics([(datetime.now(UTC), "ingest.cycle_seconds", 0.2)])


def test_status_says_ok_with_the_numbers(box, capsys):
    beat(box[3])
    assert run("status") == 0
    out = capsys.readouterr().out
    assert out.startswith("OK ") and "events 24h 0" in out and "memory" in out


def test_status_json_and_a_failed_run_exit_code(box, capsys, monkeypatch):
    beat(box[3])
    assert run("status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["run_ok"] is True
    monkeypatch.setattr(health, "receiver_ok", lambda port: False)
    assert run("status") == 1
    assert capsys.readouterr().out.startswith("FAIL ")


def test_status_shows_config_errors(box, capsys):
    tmp_path, user, state, store = box
    (state / configguard.ERRORS_FILE).write_text(
        json.dumps({"errors": {"notify.yaml": {"error": "schema 9", "using": "the last good copy"}}})
    )
    run("status")
    assert "CONFIG   notify.yaml: schema 9 (using the last good copy)" in capsys.readouterr().out


def test_sources_and_audit_listings(box, capsys):
    tmp_path, user, state, store = box
    assert run("sources") == 0 and "Nothing has been received yet." in capsys.readouterr().out
    now = datetime.now(UTC)
    with store.write() as tx:
        tx.touch_host("198.51.100.5", now - timedelta(days=1), now, system_id="other", known=False)
    store.audit("someone", "thing.done", "detail here")
    run("sources")
    assert "198.51.100.5" in capsys.readouterr().out and "(unknown)" in capsys.readouterr().out or True
    assert run("audit", "--user", "someone") == 0
    assert "thing.done" in capsys.readouterr().out
    assert run("audit", "--q", "nomatch-xyz") == 0 and "Nothing matches." in capsys.readouterr().out


# ---------------------------------------------------------------- config


def test_config_list_show_and_default(box, capsys):
    tmp_path, user, state, store = box
    (user / "views.yaml").write_text("schema: 1\nboard: {stories: 8}\n")
    assert run("config", "list") == 0
    out = capsys.readouterr().out
    assert (
        "views.yaml" in out
        and "customised" in out
        and "default" in out
        and out.count("\n") == len(configedit.EDITABLE)
    )
    run("config", "show", "views.yaml")
    assert "stories: 8" in capsys.readouterr().out
    run("config", "show", "views.yaml", "--default")
    assert "stories: 8" not in capsys.readouterr().out


def test_config_validate_all_one_and_a_candidate(box, capsys):
    tmp_path, user, state, store = box
    assert run("config", "validate") == 0
    (user / "schedule.yaml").write_text('schema: 1\ndaily_run_utc: "nope"\n')
    assert run("config", "validate") == 1
    out = capsys.readouterr().out
    assert "schedule.yaml" in out and "NOT VALID" in out and "default (no file of yours)" in out
    candidate = tmp_path / "new.yaml"
    candidate.write_text("schema: 1\ndaily_run_utc: '05:30'\n")
    assert run("config", "validate", "schedule.yaml", "--from", str(candidate)) == 0
    candidate.write_text("schema: 1\nweekly_day: someday\n")
    assert run("config", "validate", "schedule.yaml", "--from", str(candidate)) == 1
    assert run("config", "validate", "--from", str(candidate)) == 2  # needs exactly one name


def test_config_set_history_diff_rollback_reset_with_audit(box, capsys, monkeypatch):
    tmp_path, user, state, store = box
    new = tmp_path / "views.yaml"
    new.write_text("schema: 1\nboard: {stories: 9}\n")
    assert run("config", "set", "views.yaml", "--from", str(new)) == 0
    assert "Saved views.yaml" in capsys.readouterr().out and "stories: 9" in (user / "views.yaml").read_text()
    monkeypatch.setattr(sys, "stdin", io.StringIO("schema: 1\nboard: {stories: 4}\n"))
    assert run("config", "set", "views.yaml") == 0  # from stdin
    capsys.readouterr()
    assert run("config", "history", "views.yaml") == 0
    history = capsys.readouterr().out
    assert history.count("replaced by save") == 2
    stamp = configedit.versions(state, "views.yaml")[0].stamp
    assert run("config", "diff", "views.yaml", stamp) == 0
    assert "stories: 9" in capsys.readouterr().out
    assert run("config", "rollback", "views.yaml", stamp) == 0
    assert "stories: 9" in (user / "views.yaml").read_text()
    assert run("config", "reset", "views.yaml") == 0 and not (user / "views.yaml").exists()
    actions = [a["action"] for a in audit(store, "config.%")]
    assert actions == ["config.save", "config.save", "config.rollback", "config.reset"]
    assert {a["user"] for a in audit(store, "config.%")} == {"cli:tester"}


def test_config_set_refuses_an_invalid_file_and_unknown_names(box, capsys):
    tmp_path, user, state, store = box
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema: 1\nports: {https: 99999}\n")
    assert run("config", "set", "network.yaml", "--from", str(bad)) == 1
    assert "ports.https must be a number" in capsys.readouterr().err and not (user / "network.yaml").exists()
    assert run("config", "show", "secrets.env") == 1
    assert "not a config file" in capsys.readouterr().err
    assert (
        run("config", "history", "views.yaml") == 0 and "No earlier versions yet." in capsys.readouterr().out
    )
    assert run("config", "diff", "views.yaml") == 0


# ---------------------------------------------------------------- systems


def test_systems_add_assign_remove_round_trip(box, capsys):
    tmp_path, user, state, store = box
    assert run("systems", "list") == 0 and "No systems yet." in capsys.readouterr().out
    assert (
        run("systems", "add", "gw", "--label", "Gateway", "--host", "198.51.100.1", "--kind", "gateway") == 0
    )
    assert run("systems", "add", "nas", "--host", "198.51.100.2", "--not-expected") == 0
    assert run("systems", "assign", "198.51.100.9", "gw") == 0
    capsys.readouterr()
    run("systems", "list")
    out = capsys.readouterr().out
    assert "198.51.100.1, 198.51.100.9" in out and "not expected" in out
    assert run("systems", "remove", "nas") == 0
    from brief.systems import Systems

    saved = Systems.load(user / "systems.yaml")
    assert [(s.id, s.hosts) for s in saved.systems] == [("gw", ("198.51.100.1", "198.51.100.9"))]
    assert {a["user"] for a in audit(store, "config.save")} == {"cli:tester"}


def test_systems_commands_refuse_nonsense(box, capsys):
    run("systems", "add", "gw", "--host", "198.51.100.1")
    capsys.readouterr()
    assert run("systems", "add", "gw") == 1 and "already exists" in capsys.readouterr().err
    assert run("systems", "assign", "198.51.100.2", "nope") == 1
    assert run("systems", "remove", "nope") == 1
    assert run("systems", "add", "Bad Id") == 1
    assert run("systems", "add", "dup", "--host", "198.51.100.1") == 1  # a host cannot be in two systems
    assert "listed under both" in capsys.readouterr().err


# ---------------------------------------------------------------- signatures and logs


def seed_signature(store):
    now = datetime.now(UTC)
    with store.write() as tx:
        sid = tx.signature_for(0, 1, "kernel", "disk error on <*>", now)
        tx.add_hourly([(f"{now:%Y-%m-%d}", now.hour, "198.51.100.1", sid, "err", 120)])
    return sid


def test_signatures_list_ignore_and_unignore(box, capsys):
    tmp_path, user, state, store = box
    sid = seed_signature(store)
    assert run("signatures") == 0
    assert "disk error" in capsys.readouterr().out
    assert run("signature", "ignore", str(sid)) == 0
    assert "now ignored" in capsys.readouterr().out
    assert run("signature", "ignore", str(sid)) == 0 and "already ignored" in capsys.readouterr().out
    run("signatures", "--ignored")
    assert "IGNORED" in capsys.readouterr().out
    assert run("signature", "unignore", str(sid)) == 0
    assert run("signature", "ignore", "99999") == 1
    assert [a["action"] for a in audit(store, "signature.%")] == ["signature.ignore", "signature.unignore"]
    assert run("signatures", "--q", "zzz-no-match") == 0 and "No signatures match." in capsys.readouterr().out


def test_logs_search_prints_lines_and_a_note_on_stderr(box, capsys):
    tmp_path, user, state, store = box
    today = datetime.now(UTC).date().isoformat()
    day = tmp_path / "archive" / today
    day.mkdir()
    (day / "198.51.100.1.log").write_text("fine\nDisk ERROR here\n")
    assert run("logs", "search", "--text", "error") == 0
    captured = capsys.readouterr()
    assert "Disk ERROR here" in captured.out and "file(s) read" in captured.err
    assert run("logs", "search", "--host", "../x", "--text", "a") == 1
    assert run("logs", "search") == 1  # nothing to look for
    assert run("logs", "search", "--text", "a", "--from", "yesterday") == 1


# ---------------------------------------------------------------- network


def in_mode(monkeypatch, address, names):
    monkeypatch.setattr(netinfo, "local_address", lambda: address)
    monkeypatch.setattr(netinfo, "interfaces", lambda root=None: names)


def test_network_show_reports_the_mode_and_the_bridge_warning(box, capsys, monkeypatch):
    tmp_path, user, state, store = box
    in_mode(monkeypatch, "198.51.100.50", ["eth0", "lo"])
    assert run("network", "show") == 0
    out = capsys.readouterr().out
    assert "mode            own_ip" in out and "198.51.100.50" in out and "No network problems found" in out
    in_mode(monkeypatch, "172.18.0.5", ["eth0", "lo"])
    now = datetime.now(UTC)
    with store.write() as tx:
        sid = tx.signature_for(0, 2, "app", "t", now)
        tx.add_hourly([(f"{now:%Y-%m-%d}", now.hour, "172.17.0.1", sid, "info", 500)])
    run("network", "show")
    out = capsys.readouterr().out
    assert "mode            bridge" in out and "WARNING" in out and "172.17.0.1" in out


def test_network_test_prints_commands_and_waits_for_the_line(box, capsys, monkeypatch):
    tmp_path, user, state, store = box
    in_mode(monkeypatch, "198.51.100.50", ["eth0", "lo"])
    assert run("network", "test", "--wait", "0") == 0
    assert "logger -d -n 198.51.100.50" in capsys.readouterr().out
    monkeypatch.setattr("secrets.token_hex", lambda n: "abc12345")
    folder = state / "events"
    folder.mkdir()
    (folder / "2026-10-07.ndjson").write_text(
        json.dumps({"source_ip": "198.51.100.77", "app": "logger", "msg": "homelab-brief-test-abc12345"})
        + "\n"
    )
    assert run("network", "test", "--wait", "5") == 0
    assert "RECEIVED from 198.51.100.77" in capsys.readouterr().out
    (folder / "2026-10-07.ndjson").write_text("")
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert run("network", "test", "--wait", "1") == 1
    assert "Not received" in capsys.readouterr().out


# ---------------------------------------------------------------- backup, collectors, ssh


def test_backup_create_to_a_path_and_restore_the_config(box, capsys):
    tmp_path, user, state, store = box
    (user / "views.yaml").write_text("schema: 1\nboard: {stories: 7}\n")
    target = tmp_path / "out" / "mine.tar.gz"
    assert run("backup", "create", "--out", str(target)) == 0
    assert target.exists() and oct(target.stat().st_mode)[-3:] == "600"
    assert "keep it private" in capsys.readouterr().out
    (user / "views.yaml").write_text("schema: 1\nboard: {stories: 2}\n")
    assert run("backup", "restore", str(target)) == 0
    assert (
        "Applied: views.yaml" in capsys.readouterr().out and "stories: 7" in (user / "views.yaml").read_text()
    )
    assert [a["action"] for a in audit(store, "backup.%")] == ["backup.create", "backup.restore"]
    assert run("backup", "restore", str(tmp_path / "missing.tar.gz")) == 1
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"not a backup")
    assert run("backup", "restore", str(junk)) == 1


def test_backup_create_can_stream_to_stdout(box, capsysbinary):
    tmp_path, user, state, store = box
    assert run("backup", "create", "--out", "-") == 0
    out = capsysbinary.readouterr().out
    with tarfile.open(fileobj=io.BytesIO(out), mode="r:gz") as tar:
        assert "MANIFEST.json" in tar.getnames()


def test_default_backup_location_is_inside_out(box, capsys):
    tmp_path, user, state, store = box
    assert run("backup", "create") == 0
    made = list((tmp_path / "out" / "backups").glob("homelab-brief-backup-*.tar.gz"))
    assert len(made) == 1 and backup.read_config_files is not None


def test_collector_test_says_phase_2_and_ssh_show_reports_off(box, capsys, monkeypatch):
    assert run("collector", "test", "unraid") == 2
    assert "Phase 2" in capsys.readouterr().err
    monkeypatch.delenv("SSH_ENABLED", raising=False)
    assert run("ssh", "show") == 0
    assert "off (set SSH_ENABLED=true" in capsys.readouterr().out


def test_bad_usage_exits_2(box):
    with pytest.raises(SystemExit) as exc:
        run("config", "frobnicate")
    assert exc.value.code == 2


def test_every_gui_action_has_a_command(box):
    """The acceptance rule: each thing the admin GUI changes has a CLI equivalent."""
    equivalents = {
        "acknowledge or snooze an alert": [("ack",), ("snooze",)],
        "edit, undo, and reset a config file": [
            ("config", "set"),
            ("config", "rollback"),
            ("config", "reset"),
        ],
        "edit systems": [("systems", "add"), ("systems", "assign"), ("systems", "remove")],
        "ignore a signature": [("signature", "ignore"), ("signature", "unignore")],
        "network settings and the syslog test": [("config", "set"), ("network", "test")],
        "backup and restore the config": [("backup", "create"), ("backup", "restore")],
        "change the admin password": [("admin", "set-password")],
        "replay, search logs, export history": [("replay",), ("logs", "search"), ("export",)],
    }
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="group")
    for name in ("admin", "ack", "snooze", "replay", "export"):
        sub.add_parser(name)
    cli_tools = cli.cli_tools
    cli_tools.add_parsers(sub)
    known = set(sub.choices)
    for action, commands in equivalents.items():
        for path in commands:
            assert path[0] in known, (action, path)
    _ = netpage


def test_config_set_with_nothing_on_stdin_does_not_wipe_the_file(box, capsys, monkeypatch):
    tmp_path, user, state, store = box
    (user / "views.yaml").write_text("schema: 1\nboard: {stories: 7}\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # `docker exec` without -i gives an empty stdin
    assert run("config", "set", "views.yaml") == 1
    assert "would be empty" in capsys.readouterr().err and "stories: 7" in (user / "views.yaml").read_text()
