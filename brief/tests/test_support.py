import io
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher

from brief import cli, health, jsonlog
from brief.auth import Auth
from brief.settings import Settings
from brief.store import Store

GOOD = "correct horse battery"


# ---------- settings ----------


def env_for(tmp_path, **extra):
    return {
        "BRIEF_CONFIG_DIR": str(tmp_path / "config"),
        "BRIEF_DEFAULTS_DIR": str(tmp_path / "defaults"),
        **extra,
    }


def write_network(tmp_path, text):
    folder = tmp_path / "config" / "brief"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "network.yaml").write_text(text, encoding="utf-8")


def test_settings_have_sensible_defaults(tmp_path):
    s = Settings.from_env(env_for(tmp_path))
    assert (s.http_port, s.https_port, s.https_public_port, s.syslog_port) == (80, 443, 443, 514)
    assert s.interval == 60 and s.advertised == "" and s.extra_names == ()


def test_ports_come_from_network_yaml_and_the_environment_overrides_them(tmp_path):
    write_network(tmp_path, "ports: { syslog: 5140, http: 8480, https: 8443 }\nadvertised_address: nas.example\n"
                            "tls_extra_names: [alias.example]\n")  # fmt: skip
    s = Settings.from_env(env_for(tmp_path))
    assert (s.http_port, s.https_port, s.https_public_port, s.syslog_port) == (8480, 8443, 8443, 5140)
    assert s.advertised == "nas.example" and s.extra_names == ("alias.example",)
    o = Settings.from_env(
        env_for(tmp_path, HTTP_PORT="9000", HTTPS_PORT="9443", HTTPS_PUBLIC_PORT="19443", SYSLOG_PORT="5514")
    )
    assert (o.http_port, o.https_port, o.https_public_port, o.syslog_port) == (9000, 9443, 19443, 5514)


@pytest.mark.parametrize("var, value", [("HTTP_PORT", "0"), ("HTTPS_PORT", "99999"), ("HTTP_PORT", "web")])
def test_a_bad_port_is_an_error_not_a_silent_default(tmp_path, var, value):
    with pytest.raises(ValueError, match=var):
        Settings.from_env(env_for(tmp_path, **{var: value}))


# ---------- the command line ----------


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state"


def prompts(*answers):
    it = iter(answers)
    return lambda _label: next(it)


def stored_hash(state):
    store = Store(state / "baseline.db")
    with store.read() as db:
        row = db.execute("SELECT password_hash FROM admin_user").fetchone()
    store.close()
    return row[0] if row else None


def test_set_password_creates_the_first_admin(state, capsys):
    assert cli.set_password("admin", state, prompt=prompts(GOOD, GOOD)) == 0
    assert "created" in capsys.readouterr().out and stored_hash(state).startswith("$argon2id$")


def test_set_password_changes_the_existing_admin_and_ends_its_sessions(state, capsys):
    store = Store(state / "baseline.db")
    auth = Auth(store, hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))
    auth.create_admin("admin", GOOD)
    token, _ = auth.login("admin", GOOD, "198.51.100.5")
    store.close()
    assert cli.set_password("admin", state, "another long passphrase") == 0
    assert "changed" in capsys.readouterr().out
    store = Store(state / "baseline.db")
    assert Auth(store).session(token) is None
    store.close()


def test_a_weak_or_mismatched_password_changes_nothing(state, capsys):
    assert cli.set_password("admin", state, prompt=prompts(GOOD, "different")) == 1
    assert cli.set_password("admin", state, "short") == 1
    err = capsys.readouterr().err
    assert "do not match" in err and "at least" in err
    assert stored_hash(state) is None


def test_you_cannot_create_a_second_account_by_accident(state, capsys):
    cli.set_password("admin", state, GOOD)
    assert cli.set_password("someone-else", state, GOOD) == 1
    assert "--user" in capsys.readouterr().err


def test_the_password_is_never_printed(state, capsys):
    cli.set_password("admin", state, GOOD)
    out = capsys.readouterr()
    assert GOOD not in out.out and GOOD not in out.err


def test_main_reads_the_password_from_stdin_when_asked(state, monkeypatch, capsys):
    monkeypatch.setenv("BRIEF_STATE", str(state))
    monkeypatch.setattr("sys.stdin", io.StringIO(GOOD + "\n"))
    assert cli.main(["admin", "set-password", "--password-stdin"]) == 0
    assert stored_hash(state)


# ---------- health ----------


def test_health_reports_each_part(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "receiver_ok", lambda port: port == 5514)
    store = Store(tmp_path / "baseline.db")
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    ok = health.check_health(store, 5514, 60, uptime_s=10, now=now)
    assert ok["status"] == "ok" and ok["checks"]["worker"]["state"] == "starting"
    down = health.check_health(store, 9999, 60, uptime_s=10, now=now)
    assert down["status"] == "degraded" and down["checks"]["receiver"]["state"] == "down"
    stale = health.check_health(store, 5514, 60, uptime_s=500, now=now)
    assert stale["checks"]["worker"]["state"] == "stale" and stale["status"] == "degraded"
    with store.write() as tx:
        tx.add_metrics([(now - timedelta(seconds=90), "ingest.cycle_seconds", 0.2)])
    fresh = health.check_health(store, 5514, 60, uptime_s=500, now=now)
    assert fresh["checks"]["worker"] == {"ok": True, "state": "running", "last_cycle_s": 90}
    late = health.check_health(store, 5514, 60, uptime_s=500, now=now + timedelta(minutes=10))
    assert late["checks"]["worker"]["state"] == "stale"
    store.close()


def test_a_broken_database_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "receiver_ok", lambda port: True)

    class Broken:
        def read(self):
            raise RuntimeError("database is gone")

    report = health.check_health(Broken(), 514, 60, uptime_s=500)
    assert report["checks"]["database"]["ok"] is False and report["status"] == "degraded"


# ---------- logging ----------


def test_log_lines_are_single_json_objects(capsys):
    jsonlog.log("something_happened", count=3)
    line = json.loads(capsys.readouterr().out)
    assert line["event"] == "something_happened" and line["count"] == 3 and "ts" in line


def test_the_log_formatter_makes_json_for_standard_logging():
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1, "Started server %s", ("process",), None
    )
    out = json.loads(jsonlog.JsonFormatter().format(record))
    assert out["message"] == "Started server process" and out["level"] == "info" and out["event"] == "log"


def test_the_uvicorn_log_config_uses_the_json_formatter():
    cfg = jsonlog.uvicorn_log_config()
    assert cfg["formatters"]["json"]["()"] == "brief.jsonlog.JsonFormatter"
    assert cfg["loggers"]["uvicorn.access"]["level"] == "WARNING"
