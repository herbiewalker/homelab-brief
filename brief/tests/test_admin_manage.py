"""15d (second half): the systems form editor, backups, config restore, and report downloads."""

import gzip
import io
import json
import re
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)

from brief import backup, configedit, sysedit
from brief.config import ConfigError
from brief.systems import Systems

BRIEF = Path(__file__).resolve().parents[1]


def token(client, path):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


def audit(env, like):
    with env.store.read() as db:
        return [
            dict(r)
            for r in db.execute("SELECT user, action, detail FROM audit_log WHERE action LIKE ?", (like,))
        ]


def loads(text, tmp_path):
    path = tmp_path / "systems.yaml"
    path.write_text(text)
    return Systems.load(path)


# ---------------------------------------------------------------- the form <-> file


def test_hosts_can_be_typed_in_any_separator_without_duplicates():
    assert sysedit.split_hosts("a, b  c\nB,d") == ["a", "b", "c", "d"] and sysedit.split_hosts("  ") == []


def test_rows_round_trip_and_unknown_keys_are_kept(tmp_path):
    original = (
        "schema: 1\ndefault_system: misc\nsystems:\n"
        "  - {id: gw, label: Gateway, kind: gateway, hosts: [198.51.100.1, gw.example], source_tz: UTC, expected: true, note: keep me}\n"
    )
    rows, default = sysedit.rows_from_text(original)
    assert (
        default == "misc"
        and rows[0]["hosts"] == "198.51.100.1, gw.example"
        and rows[0]["extra"] == {"note": "keep me"}
    )
    form = {"count": "1", "id-0": "gw", "label-0": "Gateway", "kind-0": "gateway", "hosts-0": "198.51.100.1, gw.example",
            "source_tz-0": "UTC", "expected-0": "on"}  # fmt: skip
    text = sysedit.build_text(sysedit.rows_from_form(form, rows), "misc")
    assert "note: keep me" in text and text.startswith("# systems.yaml, written by")
    systems = loads(text, tmp_path)
    assert [(s.id, s.hosts, s.expected) for s in systems.systems] == [
        ("gw", ("198.51.100.1", "gw.example"), True)
    ]
    assert systems.default_system == "misc"


def test_blank_rows_are_ignored_and_removed_rows_are_dropped():
    form = {
        "count": "3",
        "id-0": "keep",
        "hosts-0": "198.51.100.1",
        "id-1": "drop",
        "remove-1": "on",
        "label-2": "",
        "id-2": "",
    }
    rows = sysedit.rows_from_form(form, [])
    assert [r["id"] for r in rows] == ["keep", "drop"]
    text = sysedit.build_text(rows, "other")
    assert "keep" in text and "drop" not in text


def test_a_row_without_an_id_is_refused_and_a_huge_count_is_capped():
    with pytest.raises(ConfigError, match="needs an id"):
        sysedit.build_text(sysedit.rows_from_form({"count": "1", "label-0": "No id"}, []), "other")
    assert sysedit.rows_from_form({"count": "99999999"}, []) == []


def test_unreadable_text_gives_no_rows():
    assert sysedit.rows_from_text("not: [valid") == ([], "other")
    assert sysedit.rows_from_text("- a list\n") == ([], "other")


# ---------------------------------------------------------------- the systems page


def systems_form(csrf, base, rows, **extra):
    data = {"csrf_token": csrf, "base": base, "count": str(len(rows)), "default_system": "other", **extra}
    for i, row in enumerate(rows):
        for key, value in row.items():
            data[f"{key}-{i}"] = value
    return data


def page_base(client):
    return re.search(r'name="base" value="([^"]+)"', client.get("/admin/systems").text).group(1)


def test_the_systems_page_starts_empty_with_blank_rows_and_offers_unknown_senders(lab):  # noqa: F811
    now = datetime.now(UTC)
    with lab.store.write() as tx:
        tx.touch_host("198.51.100.77", now - timedelta(days=1), now, system_id="other", known=False)
    html = lab.client.get("/admin/systems").text
    assert (
        html.count('name="id-') == 3
        and "198.51.100.77" in html
        and "/admin/systems?add=198.51.100.77" in html
    )
    prefilled = lab.client.get("/admin/systems?add=198.51.100.77").text
    assert prefilled.count('name="id-') == 4 and 'value="198.51.100.77"' in prefilled
    assert (
        'value="&lt;script' not in lab.client.get("/admin/systems?add=%3Cscript%3E").text
    )  # a hostile name is not offered
    assert lab.client.get("/admin/systems?add=%3Cscript%3E").text.count('name="id-') == 3


def test_saving_a_system_writes_the_file_audits_and_keeps_history(lab, tmp_path):  # noqa: F811
    csrf, base = token(lab.client, "/admin/systems"), page_base(lab.client)
    rows = [
        {
            "id": "gw",
            "label": "Gateway",
            "kind": "gateway",
            "hosts": "198.51.100.1, gw.example",
            "source_tz": "UTC",
            "expected": "on",
        }
    ]
    r = lab.client.post("/admin/systems", data=systems_form(csrf, base, rows))
    assert r.status_code == 303 and r.headers["location"] == "/admin/systems?done=saved"
    saved = loads((tmp_path / "cfg" / "brief" / "systems.yaml").read_text(), tmp_path)
    assert [(s.id, s.label, s.hosts) for s in saved.systems] == [
        ("gw", "Gateway", ("198.51.100.1", "gw.example"))
    ]
    page = lab.client.get(r.headers["location"]).text
    assert "Saved. The pipeline picks the new systems up" in page and 'value="gw"' in page
    assert audit(lab, "config.save")[-1]["user"] == "admin"
    assert configedit.versions(lab.settings.state_dir, "systems.yaml")


@pytest.mark.parametrize(
    "rows, reason",
    [
        ([{"id": "Bad Id", "label": "x"}], "lowercase letters"),
        ([{"id": "a", "hosts": "198.51.100.1"}, {"id": "b", "hosts": "198.51.100.1"}], "listed under both"),
        ([{"id": "a", "source_tz": "Mars/Base"}], "unknown timezone"),
        ([{"id": "a"}, {"id": "a"}], "duplicate system id"),
        ([{"label": "no id"}], "needs an id"),
    ],
)
def test_a_bad_systems_form_is_refused_and_leaves_the_file_alone(lab, tmp_path, rows, reason):  # noqa: F811
    csrf, base = token(lab.client, "/admin/systems"), page_base(lab.client)
    r = lab.client.post("/admin/systems", data=systems_form(csrf, base, rows))
    assert r.status_code == 422 and "Not saved." in r.text and reason in r.text
    assert not (tmp_path / "cfg" / "brief" / "systems.yaml").exists()


def test_removing_and_editing_systems_through_the_form(lab, tmp_path):  # noqa: F811
    csrf, base = token(lab.client, "/admin/systems"), page_base(lab.client)
    two = [
        {"id": "a", "label": "A", "hosts": "198.51.100.1"},
        {"id": "b", "label": "B", "hosts": "198.51.100.2"},
    ]
    lab.client.post("/admin/systems", data=systems_form(csrf, base, two))
    base = page_base(lab.client)
    edit = [
        {"id": "a", "label": "Renamed", "hosts": "198.51.100.1"},
        {"id": "b", "label": "B", "hosts": "198.51.100.2", "remove": "on"},
    ]
    assert lab.client.post("/admin/systems", data=systems_form(csrf, base, edit)).status_code == 303
    saved = loads((tmp_path / "cfg" / "brief" / "systems.yaml").read_text(), tmp_path)
    assert [(s.id, s.label) for s in saved.systems] == [("a", "Renamed")]


def test_a_stale_systems_form_gets_a_conflict(lab):  # noqa: F811
    csrf, base = token(lab.client, "/admin/systems"), page_base(lab.client)
    other = lab.client.post(
        "/admin/systems", data=systems_form(csrf, base, [{"id": "first", "hosts": "198.51.100.1"}])
    )
    assert other.status_code == 303
    r = lab.client.post(
        "/admin/systems", data=systems_form(csrf, base, [{"id": "second", "hosts": "198.51.100.2"}])
    )  # old base
    assert r.status_code == 409 and "changed while you were editing" in r.text


def test_a_form_without_a_valid_count_is_refused(lab):  # noqa: F811
    csrf = token(lab.client, "/admin/systems")
    assert (
        lab.client.post("/admin/systems", data={"csrf_token": csrf, "count": "lots", "base": "x"}).status_code
        == 400
    )


# ---------------------------------------------------------------- backups


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A config dir with two files, and a state dir with a live database and miner state."""
    from brief.store import Store

    cfg = tmp_path / "cfg" / "brief"
    cfg.mkdir(parents=True)
    defaults = tmp_path / "defaults" / "brief"
    defaults.mkdir(parents=True)
    for name in configedit.EDITABLE:
        shipped = BRIEF / name if (BRIEF / name).exists() else BRIEF / name.replace(".yaml", ".example.yaml")
        (defaults / shipped.name).write_bytes(shipped.read_bytes())
    (cfg / "views.yaml").write_text("schema: 1\nboard: {stories: 7}\n")
    (cfg / "notify.yaml").write_text("schema: 1\napprise: {enabled: false}\n")
    monkeypatch.setenv("BRIEF_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("BRIEF_DEFAULTS_DIR", str(tmp_path / "defaults"))
    state = tmp_path / "state"
    state.mkdir()
    store = Store(state / "baseline.db")
    store.audit("admin", "marker", "in the snapshot")
    (state / "drain3.bin").write_bytes(b"miner")
    (state / "tls").mkdir()
    (state / "tls" / "self-signed.key").write_text("PRIVATE KEY")
    yield tmp_path, state, store
    store.close()


def members(path):
    with tarfile.open(path, "r:gz") as tar:
        return {m.name: m for m in tar}


def test_a_backup_holds_the_config_a_consistent_database_and_a_manifest_and_nothing_secret(home):
    tmp_path, state, store = home
    path = backup.create(state)
    try:
        found = members(path)
        assert set(found) == {
            "config/views.yaml",
            "config/notify.yaml",
            "state/baseline.db",
            "state/drain3.bin",
            "MANIFEST.json",
        }
        assert all(m.uid == 0 and m.mode == 0o600 for n, m in found.items() if n != "MANIFEST.json")
        with tarfile.open(path, "r:gz") as tar:
            manifest = json.loads(tar.extractfile("MANIFEST.json").read())
            assert manifest["product"] == "homelab-brief" and set(manifest["files"]) == set(found) - {
                "MANIFEST.json"
            }
            snapshot = tmp_path / "snap.db"
            snapshot.write_bytes(tar.extractfile("state/baseline.db").read())
        assert (
            sqlite3.connect(snapshot)
            .execute("SELECT detail FROM audit_log WHERE action = 'marker'")
            .fetchone()[0]
            == "in the snapshot"
        )
        assert all("tls" not in n for n in found) and b"PRIVATE KEY" not in gzip.decompress(
            path.read_bytes()
        )  # no TLS key
    finally:
        backup.discard(path)
    assert not path.exists()


def make_backup(files, extra=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode() if isinstance(text, str) else text
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    return buf.getvalue()


def test_restore_applies_each_config_file_through_the_editor_and_audits(home):
    tmp_path, state, store = home
    data = make_backup(
        {
            "config/views.yaml": "schema: 1\nboard: {stories: 9}\n",
            "config/notify.yaml": "schema: 1\napprise: {enabled: false}\n",
            "state/baseline.db": b"x",
        }
    )
    result = backup.restore_config(store, data, "admin:admin", state)
    assert (
        result.applied == ["views.yaml"]
        and result.unchanged == ["notify.yaml"]
        and result.skipped == ["state/baseline.db"]
    )
    assert "stories: 9" in (tmp_path / "cfg" / "brief" / "views.yaml").read_text()
    assert configedit.versions(state, "views.yaml")[0].action == "replaced by restore"
    assert [r["action"] for r in audit_rows(store)] == ["marker", "config.restore"]


def audit_rows(store):
    with store.read() as db:
        return [dict(r) for r in db.execute("SELECT action FROM audit_log ORDER BY rowid")]


def test_one_invalid_file_in_a_backup_stops_the_whole_restore(home):
    tmp_path, state, store = home
    data = make_backup(
        {
            "config/views.yaml": "schema: 1\nboard: {stories: 9}\n",
            "config/schedule.yaml": 'schema: 1\ndaily_run_utc: "never"\n',
        }
    )
    with pytest.raises(backup.BackupError, match="schedule.yaml in the backup is not valid"):
        backup.restore_config(store, data, "admin:admin", state)
    assert "stories: 7" in (tmp_path / "cfg" / "brief" / "views.yaml").read_text()  # nothing was applied


def test_a_backup_cannot_write_outside_the_config_files(home):
    tmp_path, state, store = home
    link = tarfile.TarInfo("config/ai.yaml")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    data = make_backup(
        {
            "config/views.yaml": "schema: 1\nboard: {stories: 8}\n",
            "config/../../escape.yaml": "x",
            "../escape.yaml": "x",
            "/abs.yaml": "x",
            "config/secrets.env": "TOKEN=1",
            "config/sub/views.yaml": "x",
        },
        extra=[link],
    )
    result = backup.restore_config(store, data, "admin:admin", state)
    assert result.applied == ["views.yaml"] and len(result.skipped) == 6
    assert (
        not (tmp_path / "escape.yaml").exists() and not (tmp_path / "cfg" / "brief" / "secrets.env").exists()
    )
    assert not (tmp_path / "cfg" / "brief" / "ai.yaml").exists()


@pytest.mark.parametrize(
    "data, reason",
    [
        (b"not a tarball", "not a backup file"),
        (b"", "not a backup file"),
        (b"x" * (backup.MAX_UPLOAD + 1), "larger than"),
        (make_backup({"state/baseline.db": b"x"}), "no config files"),
        (make_backup({"config/views.yaml": b"\xff\xfe\x00bad"}), "not text"),
    ],
)
def test_bad_backups_are_refused(home, data, reason):
    tmp_path, state, store = home
    with pytest.raises(backup.BackupError, match=reason):
        backup.restore_config(store, data, "admin:admin", state)


def test_a_gzip_bomb_of_config_text_is_capped(home):
    tmp_path, state, store = home
    data = make_backup({"config/views.yaml": "schema: 1\n#" + "x" * (configedit.MAX_BYTES + 10)})
    assert len(data) < backup.MAX_UPLOAD
    with pytest.raises(backup.BackupError):  # skipped as too large, so nothing is left to restore
        backup.restore_config(store, data, "admin:admin", state)


def test_report_paths_are_fixed_kinds_and_strict_names(tmp_path):
    out = tmp_path / "out"
    (out / "weekly").mkdir(parents=True)
    (out / "export").mkdir()
    (out / "2026-10-07").mkdir()
    (out / "weekly" / "2026-W40.md").write_text("w")
    (out / "export" / "gw-1.csv").write_text("c")
    (out / "2026-10-07" / "digest.md").write_text("d")
    assert backup.report_path(out, "weekly", "2026-W40.md").read_text() == "w"
    assert backup.report_path(out, "export", "gw-1.csv").read_text() == "c"
    assert backup.report_path(out, "brief", "2026-10-07").read_text() == "d"
    for kind, name in [
        ("weekly", "../x.md"),
        ("weekly", "2026-W40.csv"),
        ("export", "a/b.csv"),
        ("brief", "../../etc"),
        ("brief", "2026-10-08"),
        ("other", "x"),
        ("weekly", ""),
    ]:
        assert backup.report_path(out, kind, name) is None, (kind, name)
    listing = backup.reports(out)
    assert listing == {"weekly": ["2026-W40.md"], "export": ["gw-1.csv"], "brief": ["2026-10-07"]}


# ---------------------------------------------------------------- the pages


def test_the_new_pages_need_a_login_and_writes_need_a_token(lab):  # noqa: F811
    for path in ("/admin/systems", "/admin/backups", "/admin/backups/download", "/admin/files/weekly/x.md"):
        r = lab.https().get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/admin/login"), path
    for path in ("/admin/systems", "/admin/backups/restore"):
        assert lab.https().post(path, data={}).status_code == 401, path
        assert lab.client.post(path, data={}).status_code == 403, path


def test_the_backups_page_lists_reports_and_the_download_is_an_audited_attachment(lab):  # noqa: F811
    out = lab.settings.out_dir
    (out / "weekly").mkdir(parents=True, exist_ok=True)
    (out / "weekly" / "2026-W40.md").write_text("# week\n")
    page = lab.client.get("/admin/backups").text
    assert "/admin/files/weekly/2026-W40.md" in page and "Treat it like a password" in page
    r = lab.client.get("/admin/backups/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    assert "attachment" in r.headers["content-disposition"] and ".tar.gz" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        assert "MANIFEST.json" in tar.getnames() and "state/baseline.db" in tar.getnames()
    assert audit(lab, "backup.download")[0]["user"] == "admin:admin"


def test_report_downloads_are_attachments_and_refuse_anything_else(lab):  # noqa: F811
    out = lab.settings.out_dir
    (out / "export").mkdir(parents=True, exist_ok=True)
    (out / "export" / "gw-1.csv").write_text("day,events\n")
    r = lab.client.get("/admin/files/export/gw-1.csv")
    assert (
        r.status_code == 200
        and r.headers["content-type"].startswith("text/csv")
        and "attachment" in r.headers["content-disposition"]
    )
    for path in (
        "/admin/files/export/..%2f..%2fstate%2fbaseline.db",
        "/admin/files/weekly/none.md",
        "/admin/files/secret/x",
        "/admin/files/export/a.txt",
    ):
        assert lab.client.get(path).status_code == 404, path


def test_restoring_through_the_page_applies_audits_and_reports(lab, tmp_path):  # noqa: F811
    for name in configedit.EDITABLE:
        shipped = BRIEF / name if (BRIEF / name).exists() else BRIEF / name.replace(".yaml", ".example.yaml")
        (tmp_path / "defaults" / "brief" / shipped.name).write_bytes(shipped.read_bytes())
    data = make_backup({"config/views.yaml": "schema: 1\nboard: {stories: 6}\n", "state/baseline.db": b"x"})
    r = lab.client.post(
        "/admin/backups/restore",
        data={"csrf_token": token(lab.client, "/admin/backups")},
        files={"backup": ("backup.tar.gz", data, "application/gzip")},
    )
    assert (
        r.status_code == 200
        and "Restored the config files" in r.text
        and "views.yaml" in r.text
        and "state/baseline.db" in r.text
    )
    assert "stories: 6" in (tmp_path / "cfg" / "brief" / "views.yaml").read_text()
    assert audit(lab, "backup.restore") and audit(lab, "config.restore")


@pytest.mark.parametrize("upload", [None, ("x.tar.gz", b"junk", "application/gzip")])
def test_a_missing_or_bad_upload_is_refused_with_a_reason(lab, upload):  # noqa: F811
    kwargs = {"files": {"backup": upload}} if upload else {}
    r = lab.client.post(
        "/admin/backups/restore", data={"csrf_token": token(lab.client, "/admin/backups")}, **kwargs
    )
    assert r.status_code == 422 and ("Choose a backup file" in r.text or "not a backup file" in r.text)


def test_an_oversized_write_is_refused_before_it_is_read(lab):  # noqa: F811
    csrf = token(lab.client, "/admin/backups")
    r = lab.client.post(
        "/admin/backups/restore", data={"csrf_token": csrf}, headers={"Content-Length": str(50 * 1024 * 1024)}
    )
    assert r.status_code in (403, 400, 422)


def test_the_new_pages_have_no_inline_script_or_style(lab):  # noqa: F811
    for path in ("/admin/systems", "/admin/backups"):
        html = lab.client.get(path).text
        assert " style=" not in html and "<script>" not in html, path
    _ = gzip
