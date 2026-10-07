"""15b: the config editor. Validation before saving, history, rollback, reset, audit, and its security."""

import html
import json
import re
from pathlib import Path

import pytest
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)

from brief import configedit, configguard
from brief.store import Store

BRIEF = Path(__file__).resolve().parents[1]
GOOD_SYSTEMS = "schema: 1\nsystems:\n  - id: gw\n    label: Gateway\n    hosts: [198.51.100.1]\n"
OTHER_SYSTEMS = "schema: 1\nsystems:\n  - id: nas\n    label: Storage\n    hosts: [198.51.100.2]\n"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A config dir with every shipped file as the default, and a store for the audit log."""
    user = tmp_path / "cfg" / "brief"
    defaults = tmp_path / "defaults" / "brief"
    user.mkdir(parents=True, exist_ok=True)
    defaults.mkdir(parents=True, exist_ok=True)
    for name in configedit.EDITABLE:
        shipped = BRIEF / name if (BRIEF / name).exists() else BRIEF / name.replace(".yaml", ".example.yaml")
        (defaults / shipped.name).write_bytes(shipped.read_bytes())
    monkeypatch.setenv("BRIEF_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("BRIEF_DEFAULTS_DIR", str(tmp_path / "defaults"))
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    store = Store(state / "baseline.db")
    yield user, state, store
    store.close()


def base_of(name):
    return configedit.digest(configedit.current(name)[0])


def audit_rows(store, only="config."):
    with store.read() as db:
        rows = db.execute(
            "SELECT user, action, detail FROM audit_log WHERE action LIKE ? ORDER BY rowid", (only + "%",)
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- the rules


@pytest.mark.parametrize("name", configedit.EDITABLE)
def test_every_shipped_default_is_a_valid_edit_of_itself(cfg, name):
    assert configedit.validate_text(name, configedit.starting_text(name)) is None


@pytest.mark.parametrize(
    "name, text",
    [
        ("systems.yaml", "schema: 1\nsystems: [oops: : broken\n"),
        ("systems.yaml", "schema: 1\nsystems:\n  - id: Bad Id\n"),
        ("priorities.yaml", "schema: 1\nkinds:\n  absence: { route: shout }\n"),
        ("schedule.yaml", 'schema: 1\ndaily_run_utc: "25:99"\n'),
        ("notify.yaml", "schema: 9\n"),
        ("views.yaml", "- a\n- list\n"),
        ("playbook.yaml", "schema: 1\nthresholds:\n  absence: { max_hours: soon }\n"),
        ("ignore.yaml", "schema: 1\nignore:\n  - name: x\n    match: '(unclosed'\n"),
        ("registry.yaml", "schema: 1\nmetrics:\n  - name: no id\n"),
        ("ai.yaml", "schema: 1\nenabled: true\n\x00"),
    ],
)
def test_an_invalid_edit_is_refused_with_a_reason(cfg, name, text):
    assert configedit.validate_text(name, text)


def test_a_huge_file_is_refused(cfg):
    assert "larger than" in configedit.validate_text(
        "views.yaml", "schema: 1\n# " + "x" * configedit.MAX_BYTES
    )


def test_save_writes_the_file_keeps_what_it_replaces_and_audits(cfg):
    user, state, store = cfg
    msg = configedit.save(store, "systems.yaml", GOOD_SYSTEMS, "admin", base_of("systems.yaml"), state)
    assert "Saved systems.yaml" in msg and (user / "systems.yaml").read_text() == GOOD_SYSTEMS
    (v,) = configedit.versions(state, "systems.yaml")
    assert v.user == "admin" and "save" in v.action
    assert configedit.version_text(state, "systems.yaml", v.stamp) == configedit.starting_text("systems.yaml")
    (row,) = audit_rows(store)
    assert row["user"] == "admin" and row["action"] == "config.save" and "systems.yaml" in row["detail"]
    assert v.stamp in row["detail"]


def test_browser_line_ends_are_normalised_and_a_final_newline_is_added(cfg):
    user, state, store = cfg
    configedit.save(
        store,
        "systems.yaml",
        GOOD_SYSTEMS.replace("\n", "\r\n").rstrip(),
        "admin",
        base_of("systems.yaml"),
        state,
    )
    text = (user / "systems.yaml").read_bytes()
    assert b"\r" not in text and text.endswith(b"\n") and not text.endswith(b"\n\n")


def test_an_invalid_edit_is_never_written_and_leaves_no_trace(cfg):
    user, state, store = cfg
    with pytest.raises(configedit.EditError):
        configedit.save(store, "systems.yaml", "not: [valid", "admin", base_of("systems.yaml"), state)
    assert not (user / "systems.yaml").exists() and configedit.versions(state, "systems.yaml") == []
    assert audit_rows(store) == []


def test_two_people_cannot_overwrite_each_other(cfg):
    user, state, store = cfg
    mine = base_of("systems.yaml")
    configedit.save(store, "systems.yaml", GOOD_SYSTEMS, "first", mine, state)
    with pytest.raises(configedit.EditError) as err:
        configedit.save(
            store, "systems.yaml", OTHER_SYSTEMS, "second", mine, state
        )  # started from the old text
    assert err.value.status == 409 and (user / "systems.yaml").read_text() == GOOD_SYSTEMS


def test_saving_the_same_text_is_refused_as_no_change(cfg):
    user, state, store = cfg
    text, _ = configedit.current("systems.yaml")
    with pytest.raises(configedit.EditError, match="Nothing changed"):
        configedit.save(store, "systems.yaml", text, "admin", base_of("systems.yaml"), state)


def test_rollback_restores_a_version_keeps_the_current_one_and_audits(cfg):
    user, state, store = cfg
    configedit.save(store, "systems.yaml", GOOD_SYSTEMS, "admin", base_of("systems.yaml"), state)
    configedit.save(store, "systems.yaml", OTHER_SYSTEMS, "admin", base_of("systems.yaml"), state)
    older = configedit.versions(state, "systems.yaml")[0]  # the GOOD_SYSTEMS text, replaced by the 2nd save
    assert configedit.version_text(state, "systems.yaml", older.stamp) == GOOD_SYSTEMS
    msg = configedit.rollback(store, "systems.yaml", older.stamp, "admin", base_of("systems.yaml"), state)
    assert msg.startswith("Restored version") and (user / "systems.yaml").read_text() == GOOD_SYSTEMS
    assert len(configedit.versions(state, "systems.yaml")) == 3  # nothing was lost
    assert audit_rows(store)[-1]["action"] == "config.rollback"


def test_a_tampered_history_file_cannot_be_restored(cfg):
    user, state, store = cfg
    configedit.save(store, "systems.yaml", GOOD_SYSTEMS, "admin", base_of("systems.yaml"), state)
    (v,) = configedit.versions(state, "systems.yaml")
    (state / "config-history" / "systems.yaml" / f"{v.stamp}.yaml").write_text(
        "schema: 1\nsystems: [: broken\n"
    )
    with pytest.raises(configedit.EditError):
        configedit.rollback(store, "systems.yaml", v.stamp, "admin", base_of("systems.yaml"), state)
    assert (user / "systems.yaml").read_text() == GOOD_SYSTEMS


def test_reset_removes_the_users_copy_keeps_it_in_history_and_audits(cfg):
    user, state, store = cfg
    configedit.save(store, "systems.yaml", GOOD_SYSTEMS, "admin", base_of("systems.yaml"), state)
    assert "shipped default" in configedit.reset(store, "systems.yaml", "admin", state)
    assert not (user / "systems.yaml").exists() and configedit.current("systems.yaml")[1] is False
    assert (
        configedit.version_text(state, "systems.yaml", configedit.versions(state, "systems.yaml")[0].stamp)
        == GOOD_SYSTEMS
    )
    assert audit_rows(store)[-1]["action"] == "config.reset"
    with pytest.raises(configedit.EditError, match="already"):
        configedit.reset(store, "systems.yaml", "admin", state)


def test_history_is_pruned_to_a_limit(cfg, monkeypatch):
    user, state, store = cfg
    monkeypatch.setattr(configedit, "KEEP_VERSIONS", 3)
    for i in range(6):
        text = f"schema: 1\nsystems:\n  - id: s{i}\n    label: S{i}\n    hosts: []\n"
        configedit.save(store, "systems.yaml", text, "admin", base_of("systems.yaml"), state)
    kept = configedit.versions(state, "systems.yaml")
    assert len(kept) == 3 and len(list((state / "config-history" / "systems.yaml").glob("*.json"))) == 3


@pytest.mark.parametrize(
    "name", ["../secrets.env", "systems.yaml/../../x", "/etc/passwd", "baseline.db", "", "drain3.ini"]
)
def test_only_the_listed_files_can_be_touched(cfg, name):
    user, state, store = cfg
    with pytest.raises(configedit.EditError) as err:
        configedit.save(store, name, "schema: 1\n", "admin", "x", state)
    assert err.value.status == 404
    with pytest.raises(configedit.EditError):
        configedit.current(name)


@pytest.mark.parametrize("stamp", ["../../etc/passwd", "20261006T231500Z/../x", "latest", "", "2026"])
def test_a_version_name_is_never_a_path(cfg, stamp):
    user, state, store = cfg
    with pytest.raises(configedit.EditError):
        configedit.version_text(state, "systems.yaml", stamp)


def test_saving_a_fixed_file_clears_the_config_error(cfg):
    user, state, store = cfg
    (user / "systems.yaml").write_text("schema: 1\nsystems: [: broken\n")
    guard = configguard.ConfigGuard(state)
    assert "systems.yaml" in guard.check()
    configedit.save(store, "systems.yaml", GOOD_SYSTEMS, "admin", base_of("systems.yaml"), state)
    assert configguard.read_errors(state) == {}


# ---------------------------------------------------------------- the pages


def token(client, path="/admin/config"):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


def page_base(client, name):
    return re.search(r'name="base" value="([^"]+)"', client.get(f"/admin/config/{name}").text).group(1)


def test_the_config_pages_need_a_login(lab, cfg):  # noqa: F811
    for path in (
        "/admin/config",
        "/admin/config/systems.yaml",
        "/admin/config/systems.yaml/versions/20260101T000000Z",
    ):
        r = lab.https().get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/admin/login"), path
    anon = lab.https()
    assert anon.post("/admin/config/systems.yaml", data={"text": "x", "base": "y"}).status_code == 401


def test_the_list_shows_every_file_with_its_state(lab, cfg):  # noqa: F811
    user, state, store = cfg
    (user / "views.yaml").write_text((BRIEF / "views.yaml").read_text())
    html = lab.client.get("/admin/config").text
    for name in configedit.EDITABLE:
        assert f'href="/admin/config/{name}"' in html
    assert "customised" in html and "default" in html


def test_review_shows_the_diff_and_saves_nothing(lab, cfg):  # noqa: F811
    user, state, store = cfg
    name = "systems.yaml"
    r = lab.client.post(
        f"/admin/config/{name}",
        data={
            "csrf_token": token(lab.client),
            "base": page_base(lab.client, name),
            "action": "review",
            "text": GOOD_SYSTEMS,
        },
    )
    assert r.status_code == 200 and "Changes to save" in r.text and "Save these changes" in r.text
    assert 'class="add"' in r.text and "Gateway" in r.text
    assert not (user / name).exists() and audit_rows(store) == []


def test_saving_through_the_page_writes_audits_and_redirects(lab, cfg):  # noqa: F811
    user, state, store = cfg
    name = "systems.yaml"
    data = {"csrf_token": token(lab.client), "base": page_base(lab.client, name), "action": "save"}
    r = lab.client.post(f"/admin/config/{name}", data={**data, "text": GOOD_SYSTEMS.replace("\n", "\r\n")})
    assert r.status_code == 303 and r.headers["location"] == f"/admin/config/{name}?done=saved"
    assert (user / name).read_text() == GOOD_SYSTEMS
    done = lab.client.get(r.headers["location"]).text
    assert "Saved. The pipeline picks it up" in done and "<code>2" in done  # a history row exists
    assert (
        audit_rows(lab.store)[-1]["user"] == "admin" and audit_rows(lab.store)[-1]["action"] == "config.save"
    )
    assert "config.save" in lab.client.get("/admin/audit").text


def test_an_invalid_edit_cannot_be_saved_from_the_page(lab, cfg):  # noqa: F811
    user, state, store = cfg
    name = "schedule.yaml"
    data = {
        "csrf_token": token(lab.client),
        "base": page_base(lab.client, name),
        "text": 'schema: 1\ndaily_run_utc: "nope"\n',
    }
    for action in ("review", "save"):
        r = lab.client.post(f"/admin/config/{name}", data={**data, "action": action})
        assert r.status_code == 422 and "daily_run_utc must look like" in r.text, action
        assert 'daily_run_utc: "nope"' in html.unescape(r.text)  # the edit is still in the box, to fix
    assert not (user / name).exists() and audit_rows(lab.store) == []


def test_a_stale_edit_gets_a_conflict_not_an_overwrite(lab, cfg):  # noqa: F811
    user, state, store = cfg
    name = "systems.yaml"
    csrf, mine = token(lab.client), page_base(lab.client, name)
    configedit.save(lab.store, name, OTHER_SYSTEMS, "someone-else", mine, state)
    r = lab.client.post(
        f"/admin/config/{name}",
        data={"csrf_token": csrf, "base": mine, "action": "save", "text": GOOD_SYSTEMS},
    )
    assert r.status_code == 409 and "changed while you were editing" in r.text
    assert (user / name).read_text() == OTHER_SYSTEMS


def test_history_compare_and_restore_through_the_pages(lab, cfg):  # noqa: F811
    user, state, store = cfg
    name = "systems.yaml"
    configedit.save(lab.store, name, GOOD_SYSTEMS, "admin", base_of(name), state)
    configedit.save(lab.store, name, OTHER_SYSTEMS, "admin", base_of(name), state)
    page = lab.client.get(f"/admin/config/{name}").text
    stamp = re.search(r'/versions/(\d{8}T\d{6}Z(?:-\d+)?)"', page).group(1)  # the newest kept version
    version = lab.client.get(f"/admin/config/{name}/versions/{stamp}")
    assert (
        version.status_code == 200
        and "Restore this version" in version.text
        and 'class="add"' in version.text
    )
    r = lab.client.post(
        f"/admin/config/{name}/restore",
        data={"csrf_token": token(lab.client), "stamp": stamp, "base": page_base(lab.client, name)},
    )
    assert r.status_code == 303 and (user / name).read_text() == GOOD_SYSTEMS
    assert audit_rows(lab.store)[-1]["action"] == "config.rollback"


def test_reset_needs_the_csrf_token_and_then_returns_to_the_default(lab, cfg):  # noqa: F811
    user, state, store = cfg
    name = "systems.yaml"
    configedit.save(lab.store, name, GOOD_SYSTEMS, "admin", base_of(name), state)
    assert lab.client.post(f"/admin/config/{name}/reset", data={}).status_code == 403
    assert (user / name).exists()
    r = lab.client.post(f"/admin/config/{name}/reset", data={"csrf_token": token(lab.client)})
    assert r.status_code == 303 and not (user / name).exists()


def test_unknown_files_and_versions_are_404_on_every_route(lab, cfg):  # noqa: F811
    csrf = token(lab.client)
    for path in (
        "/admin/config/notes.txt",
        "/admin/config/systems.yaml/versions/nope",
        "/admin/config/drain3.ini",
    ):
        assert lab.client.get(path).status_code == 404, path
    for path in (
        "/admin/config/notes.txt",
        "/admin/config/notes.txt/reset",
        "/admin/config/notes.txt/restore",
    ):
        r = lab.client.post(
            path, data={"csrf_token": csrf, "text": "x", "base": "y", "stamp": "z", "action": "save"}
        )
        assert r.status_code == 404, path


def test_file_contents_are_escaped_in_the_editor(lab, cfg):  # noqa: F811
    user, state, store = cfg
    (user / "views.yaml").write_text("schema: 1\n# </textarea><script>alert(1)</script>\n")
    html = lab.client.get("/admin/config/views.yaml").text
    assert "</textarea><script>" not in html and "&lt;/textarea&gt;&lt;script&gt;" in html


def test_the_editor_page_has_no_inline_script_or_style(lab, cfg):  # noqa: F811
    for path in ("/admin/config", "/admin/config/systems.yaml"):
        html = lab.client.get(path).text
        assert " style=" not in html and "<script>" not in html, path


def test_a_file_the_pipeline_cannot_load_is_flagged_on_its_editor_page(lab, cfg):  # noqa: F811
    user, state, store = cfg
    (state / configguard.ERRORS_FILE).write_text(
        json.dumps({"errors": {"notify.yaml": {"error": "schema 9", "using": "the last good copy"}}})
    )
    html = lab.client.get("/admin/config/notify.yaml").text
    assert "could not load the current file" in html and "the last good copy" in html
