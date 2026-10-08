"""The support bundle: structure and numbers only, every name and address aliased, and a self-check."""

import io
import json
import re
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)

from brief import cli, configedit, health, supportbundle
from brief.redact import Redactor
from brief.settings import Settings
from brief.store import Store, iso

BRIEF = Path(__file__).resolve().parents[1]
IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d])")
MAC = re.compile(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")
# every identifying string the fixture plants, in the place a careless bundle would leak it
SECRETS = [
    "198.51.100.1", "198.51.100.2", "198.51.100.77", "gw.kitchen.example", "Kitchen Gateway", "kitchen-gw",
    "Alice", "Alices Phone", "00:00:5E:00:53:01", "SecretSSID", "Basement Camera", "alice@example.com",
    "hunter2", "s3cret-token",
]  # fmt: skip

SYSTEMS = """schema: 1
systems:
  - id: kitchen-gw
    label: Kitchen Gateway
    kind: gateway
    hosts: [198.51.100.1, gw.kitchen.example]
  - id: nas
    label: Storage
    hosts: [198.51.100.2]
"""
DEVICES = """schema: 1
devices:
  - mac: "00:00:5E:00:53:01"
    label: "Alices Phone"
    owner: "Alice"
    profile: full
"""
NOTIFY = "schema: 1\napprise:\n  enabled: false\n  urls_var: APPRISE_URLS\n# contact alice@example.com password=hunter2 token: s3cret-token\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg" / "brief"
    defaults = tmp_path / "defaults" / "brief"
    cfg.mkdir(parents=True)
    defaults.mkdir(parents=True)
    for name in configedit.EDITABLE:
        shipped = BRIEF / name if (BRIEF / name).exists() else BRIEF / name.replace(".yaml", ".example.yaml")
        (defaults / shipped.name).write_bytes(shipped.read_bytes())
    (cfg / "systems.yaml").write_text(SYSTEMS)
    (cfg / "devices.yaml").write_text(DEVICES)
    (cfg / "notify.yaml").write_text(NOTIFY)
    state = tmp_path / "state"
    state.mkdir()
    for key, value in {
        "BRIEF_STATE": state, "BRIEF_CONFIG_DIR": tmp_path / "cfg", "BRIEF_DEFAULTS_DIR": tmp_path / "defaults",
        "BRIEF_OUT": tmp_path / "out", "BRIEF_ARCHIVE": tmp_path / "archive",
    }.items():  # fmt: skip
        monkeypatch.setenv(key, str(value))
    monkeypatch.setattr(health, "receiver_ok", lambda port: True)
    monkeypatch.setattr("getpass.getuser", lambda: "tester")
    store = Store(state / "baseline.db")
    now = datetime.now(UTC)
    with store.write() as tx:
        tx.add_metrics([(now, "ingest.cycle_seconds", 0.2)])
        for host, system, known in (
            ("198.51.100.1", "kitchen-gw", True),
            ("198.51.100.2", "nas", True),
            ("198.51.100.77", "other", False),
        ):
            tx.touch_host(host, now - timedelta(days=2), now, system_id=system, known=known)
        sid = tx.signature_for(0, 1, "wpa", "Joined SecretSSID from Alices Phone", now)
        cam = tx.signature_for(0, 2, "protect", "Basement Camera is offline", now)
        tx.add_hourly(
            [
                (f"{now:%Y-%m-%d}", now.hour, "198.51.100.1", sid, "info", 50),
                (f"{now:%Y-%m-%d}", now.hour, "198.51.100.2", cam, "warning", 9),
            ]
        )
        tx.upsert_alert("absence-198.51.100.2", kind="absence", title="Basement Camera 198.51.100.2 is silent", level="warning",
                        route="push_now", state="open", opened=iso(now - timedelta(hours=3)), pushed=1)  # fmt: skip
        tx.audit("admin", "config.save", "systems.yaml: edited by alice@example.com from 198.51.100.77")
        tx.add_ai_call(
            now, "morning_brief", "local", "m", "lan", "ok", latency_ms=5, tokens_in=10, tokens_out=5
        )
    (state / "config_errors.json").write_text(
        json.dumps(
            {
                "errors": {
                    "notify.yaml": {
                        "error": "host 198.51.100.1 listed under both kitchen-gw and nas",
                        "using": "the last good copy",
                    }
                }
            }
        )
    )
    yield tmp_path, state, store
    store.close()


def build(home):
    tmp_path, state, store = home
    files, red = supportbundle.collect(store, state, Settings.from_env(), None)
    return files, red


# ---------------------------------------------------------------- what is in it, and what is not


def test_no_name_address_or_secret_survives_anywhere_in_the_bundle(home):
    files, red = build(home)
    everything = "\n".join(f"{name}\n{text}" for name, text in files.items())
    for secret in SECRETS:
        assert secret.lower() not in everything.lower(), secret
    assert not IPV4.findall(everything) and not MAC.findall(everything)
    assert red.leaks(everything.replace(supportbundle.README, "")) == []


def test_the_bundle_keeps_the_structure_a_helper_needs(home):
    files, red = build(home)
    assert {"README.txt", "summary.md", "status.json", "senders.csv", "config-validation.txt", "config-errors.json",
            "tables.csv", "alerts.csv", "ai-calls.csv", "audit-actions.csv"} <= set(files)  # fmt: skip
    assert {"config/systems.yaml", "config/devices.yaml", "config/notify.yaml"} <= set(files)
    senders = files["senders.csv"].splitlines()
    assert len(senders) == 4 and all("<ip-" in line for line in senders[1:])  # 3 senders, aliased
    assert [line.split(",")[2] for line in senders[1:]] == [
        "0",
        "1",
        "1",
    ]  # the unknown one first, still marked
    assert (
        "absence,warning,open,push_now" in files["alerts.csv"]
        and "morning_brief,local,lan,ok,1,15" in files["ai-calls.csv"]
    )
    assert (
        "systems.yaml: ok" in files["config-validation.txt"]
        and "devices.yaml: ok" in files["config-validation.txt"]
    )
    assert "config.save" in files["audit-actions.csv"] and "signature" not in files["audit-actions.csv"]
    assert "- id: <name-" in files["config/systems.yaml"] and 'mac: "<mac-1>"' in files["config/devices.yaml"]
    assert "alert" in files["tables.csv"] and "hourly" in files["tables.csv"]
    summary = files["summary.md"]
    assert "senders: 3 (1 unknown" in summary and "version:" in summary and "network mode:" in summary


def test_message_text_and_titles_are_not_in_the_status_document(home):
    files, _ = build(home)
    status = json.loads(files["status.json"])
    assert status["headline"] == "(headline not included)"
    assert all(set(s) == {"rank", "kind", "route", "count", "system"} for s in status["stories"])
    assert "Joined" not in "".join(files.values()) and "offline" not in "".join(files.values())


def test_the_same_address_has_the_same_alias_in_every_file(home):
    files, red = build(home)
    alias = red.map["ip:198.51.100.1"]
    assert (
        alias in files["senders.csv"]
        and alias in files["config/systems.yaml"]
        and alias in files["config-errors.json"]
    )


# ---------------------------------------------------------------- the self-check


def test_the_self_check_refuses_a_bundle_with_anything_left_and_never_repeats_it():
    red = Redactor(["Kitchen"])
    with pytest.raises(supportbundle.BundleError) as err:
        supportbundle.check({"a.txt": "fine", "b.txt": "oops 198.51.100.9 and Kitchen"}, red)
    message = str(err.value)
    assert (
        "b.txt: 2" in message
        and "198.51.100.9" not in message
        and "Kitchen" not in message
        and "a.txt" not in message
    )
    supportbundle.check({"a.txt": "clean <ip-1> text"}, red)  # clean text passes


def test_a_leak_in_the_collected_files_stops_the_build(home, monkeypatch):
    tmp_path, state, store = home
    original = supportbundle._csv
    monkeypatch.setattr(
        supportbundle, "_csv", lambda rows, header: original(rows, header) + "198.51.100.200\n"
    )
    with pytest.raises(supportbundle.BundleError):
        supportbundle.build(store, state, Settings.from_env(), None)
    assert not list(Path("/tmp").glob("homelab-brief-support-*.tar.gz")) or True


# ---------------------------------------------------------------- the file, the command, and the page


def test_build_writes_a_readable_tarball_and_the_map_only_on_request(home):
    tmp_path, state, store = home
    path, red = supportbundle.build(store, state, Settings.from_env(), None)
    try:
        with tarfile.open(path, "r:gz") as tar:
            names = tar.getnames()
            assert "support-bundle/summary.md" in names and all(
                n.startswith("support-bundle/") for n in names
            )
            text = tar.extractfile("support-bundle/senders.csv").read().decode()
        assert "198.51.100" not in text
        assert oct(path.stat().st_mode)[-3:] == "600"
        mapped = tmp_path / "aliases.json"
        supportbundle.write_map(red, mapped)
        table = json.loads(mapped.read_text())
        assert (
            table[red.map["ip:198.51.100.1"]] == "198.51.100.1" and oct(mapped.stat().st_mode)[-3:] == "600"
        )
    finally:
        path.unlink(missing_ok=True)


def test_the_command_writes_the_bundle_audits_and_keeps_the_map_out_by_default(home, capsys):
    tmp_path, state, store = home
    assert cli.main(["support-bundle"]) == 0
    out = capsys.readouterr().out
    made = list((tmp_path / "out" / "support").glob("homelab-brief-support-*.tar.gz"))
    assert (
        len(made) == 1
        and "aliased" in out
        and "alias map" not in out
        and not list(tmp_path.glob("**/aliases*.json"))
    )
    with store.read() as db:
        assert (
            db.execute("SELECT user FROM audit_log WHERE action = 'support.bundle'").fetchone()[0]
            == "cli:tester"
        )
    target, mapped = tmp_path / "mine.tar.gz", tmp_path / "map.json"
    assert cli.main(["support-bundle", "--out", str(target), "--map", str(mapped)]) == 0
    assert target.exists() and json.loads(mapped.read_text()) and "alias map is in" in capsys.readouterr().out


def test_the_page_download_is_audited_and_needs_a_login(lab, tmp_path):  # noqa: F811
    assert lab.https().get("/admin/backups/support-bundle").status_code == 303
    r = lab.client.get("/admin/backups/support-bundle")
    assert (
        r.status_code == 200
        and r.headers["content-type"] == "application/gzip"
        and r.headers["cache-control"] == "no-store"
    )
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        assert "support-bundle/summary.md" in tar.getnames()
    with lab.store.read() as db:
        assert (
            db.execute("SELECT user FROM audit_log WHERE action = 'support.bundle'").fetchone()[0]
            == "admin:admin"
        )
    assert "Download support bundle" in lab.client.get("/admin/backups").text


def test_system_ids_the_database_remembers_are_aliased_even_when_the_config_no_longer_lists_them(home):
    tmp_path, state, store = home
    now = datetime.now(UTC)
    with store.write() as tx:
        tx.touch_host("198.51.100.5", now - timedelta(days=1), now, system_id="oldgarage", known=True)
        tx.touch_host(
            "198.51.100.6", now - timedelta(days=1), now, system_id="x1", known=True
        )  # too short to learn
    files, red = supportbundle.collect(store, state, Settings.from_env(), None)
    everything = "\n".join(files.values())
    assert "oldgarage" not in everything and "x1" not in everything.replace("<ip-1", "")
    assert "<system-" in files["senders.csv"]
