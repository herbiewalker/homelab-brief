"""The Collectors page: every collector as a form, saved into collectors.yaml through the settings framework."""

import json
import re
from pathlib import Path

import pytest
import yaml
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)
from test_collectors import Fake

from brief import collector_specs, configedit, settingsui
from brief.collectors import framework

SHIPPED = Path(__file__).resolve().parents[1] / "collectors.yaml"


@pytest.fixture
def page(lab, monkeypatch):  # noqa: F811
    root = lab.settings.state_dir.parent
    (root / "defaults" / "brief" / "collectors.yaml").write_bytes(SHIPPED.read_bytes())
    monkeypatch.setitem(framework.REGISTRY, "unifi", Fake)
    for var in ("UNIFI_API_KEY", "UNIFI_URL", "UNRAID_ONE_KEY", "UNRAID_VAULT_KEY"):
        monkeypatch.delenv(var, raising=False)
    lab.cfg = root / "cfg" / "brief"
    lab.shipped = SHIPPED.read_text()
    lab.csrf = lambda: re.search(
        r'name="csrf_token" value="([^"]+)"', lab.client.get("/admin/collectors").text
    ).group(1)
    lab.data = lambda: yaml.safe_load((lab.cfg / "collectors.yaml").read_text())
    return lab


def unraid_form(**extra):
    form = {
        "f.enabled": "on", "f.interval": "hourly", "f.smart_source": "api", "f.smart_files_path": "/import-smart",
        "f.timeout_s": "90", "f.request_timeout_s": "10", "f.slow_request_timeout_s": "60", "r.servers.count": "1", "r.servers.0.id": "vault",
        "r.servers.0.url": "https://198.51.100.5/graphql", "r.servers.0.key_var": "UNRAID_ONE_KEY", "r.servers.0.verify_tls": "",
        "r.servers.0.orig": "",
    }  # fmt: skip
    form.update(extra)
    return form


def save(page, name="unraid", **form):
    return page.client.post(f"/admin/collectors/{name}/save", data={"csrf_token": page.csrf(), **form})


def audit(env):
    with env.store.read() as db:
        return [dict(r) for r in db.execute("SELECT user, action, detail FROM audit_log")]


# ---------------------------------------------------------------- the page


def test_the_page_shows_every_collector_as_a_form_with_placeholders_as_empty_fields(page):
    text = page.client.get("/admin/collectors").text
    assert "<h1>Collectors</h1>" in text and 'id="card-unraid"' in text and 'id="card-snmp"' in text
    assert "Unraid servers" in text and "Home Assistant power" in text and "Edit as text" in text
    assert (
        "server-1-ip" not in text
    )  # the shipped address is a placeholder: shown as an empty field, not as text to edit
    assert (
        "Add a server" in text and 'name="r.servers.0.url"' not in text
    )  # the shipped example rows are not shown


def test_a_key_variable_shows_set_or_not_set_and_never_its_value(page, monkeypatch):
    assert save(page, **unraid_form()).status_code == 200
    assert "UNRAID_ONE_KEY is not set" in page.client.get("/admin/collectors").text
    monkeypatch.setenv("UNRAID_ONE_KEY", "super-secret-value-123")
    text = page.client.get("/admin/collectors").text
    assert "UNRAID_ONE_KEY is set" in text and "super-secret-value-123" not in text


# ---------------------------------------------------------------- saving


def test_saving_the_unraid_form_writes_the_server_without_touching_the_text_editor(page):
    r = save(page, **unraid_form(**{"r.servers.0.verify_tls": "on"}))
    assert r.status_code == 200 and 'id="card-unraid"' in r.text
    assert page.data()["collectors"]["unraid"]["servers"] == [
        {
            "id": "vault",
            "url": "https://198.51.100.5/graphql",
            "key_var": "UNRAID_ONE_KEY",
            "verify_tls": True,
        }
    ]
    assert page.data()["collectors"]["unraid"]["enabled"] is True
    assert framework.parse(page.data())["unraid"].enabled  # the worker's own validator accepts it
    toast = json.loads(r.headers["hx-trigger"])["toast"]
    assert (
        toast["kind"] == "ok"
        and "switched on and saved" in toast["text"]
        and toast["undo"].endswith("/restore")
    )
    assert [a["action"] for a in audit(page) if a["user"] == "admin:admin"][-1] == "config.save"


def test_the_text_editor_shows_the_same_file_afterwards_and_comments_survive(page):
    save(page, **unraid_form())
    text = (page.cfg / "collectors.yaml").read_text()
    assert "# One entry per Unraid server." in text and "# Phase 2 collectors" in text
    assert "vault" in page.client.get("/admin/config/collectors.yaml").text
    assert text.count("enabled: true") == 1 and "unifi:\n    enabled: false" in text


def test_undo_in_the_toast_restores_the_file_exactly(page):
    save(page, **unraid_form())
    stamp = configedit.versions(page.settings.state_dir, "collectors.yaml")[0].stamp
    new = (page.cfg / "collectors.yaml").read_text()
    undo = page.client.post(
        "/admin/config/collectors.yaml/restore",
        data={"csrf_token": page.csrf(), "stamp": stamp, "base": configedit.digest(new)},
    )
    assert undo.status_code == 303 and (page.cfg / "collectors.yaml").read_text() == page.shipped


def test_saving_again_with_no_change_says_so_and_writes_nothing(page):
    save(page, **unraid_form())
    before = len(configedit.versions(page.settings.state_dir, "collectors.yaml"))
    again = save(page, **unraid_form(**{"r.servers.0.orig": "0"}))
    assert (
        again.status_code == 200
        and "Nothing to save" in json.loads(again.headers["hx-trigger"])["toast"]["text"]
    )
    assert len(configedit.versions(page.settings.state_dir, "collectors.yaml")) == before


@pytest.mark.parametrize(
    "change,why",
    [
        ({"r.servers.0.url": "not an address"}, "address starting with https"),
        ({"r.servers.0.url": "https://<server-1-ip>/graphql"}, "placeholder"),
        ({"r.servers.0.key_var": "my key"}, "capital letters"),
        ({"r.servers.0.key_var": ""}, "Needed"),
        ({"r.servers.0.id": "!!!"}, "letters, digits, and hyphens"),
        ({"f.interval": "every minute"}, "Choose one of"),
        ({"f.timeout_s": "abc"}, "whole number"),
        ({"f.timeout_s": "5000"}, "at most"),
        ({"f.smart_source": "magic"}, "Choose one of"),
    ],
)
def test_a_refused_value_comes_back_with_the_form_marked_and_nothing_saved(page, change, why):
    r = save(page, **unraid_form(**change))
    assert r.status_code == 422 and "Some settings need attention" in r.text and why in r.text
    assert 'id="card-unraid"' in r.text  # the same card, so htmx can swap it in place
    assert not (page.cfg / "collectors.yaml").exists()


def test_switching_on_with_no_server_is_refused_with_the_reason(page):
    r = save(page, **{"f.enabled": "on", "r.servers.count": "0", "f.interval": "hourly"})
    assert r.status_code == 422 and "card-unraid" in r.text and "alert--bad" in r.text
    assert not (page.cfg / "collectors.yaml").exists()


def test_the_switch_can_be_turned_off_and_the_server_is_kept(page):
    save(page, **unraid_form())
    r = save(page, **unraid_form(**{"f.enabled": "", "r.servers.0.orig": "0"}))
    assert r.status_code == 200 and page.data()["collectors"]["unraid"]["enabled"] is False
    assert page.data()["collectors"]["unraid"]["servers"][0]["id"] == "vault"


# ---------------------------------------------------------------- the row list


def test_rows_can_be_added_and_removed_without_losing_what_was_typed(page):
    form = {"csrf_token": page.csrf(), **unraid_form(**{"r.servers.0.id": "typed-not-saved"})}
    added = page.client.post("/admin/collectors/unraid/rows", data={**form, "row_action": "add:servers"})
    assert (
        added.status_code == 200 and 'name="r.servers.1.id"' in added.text and "typed-not-saved" in added.text
    )
    assert not (page.cfg / "collectors.yaml").exists()  # a re-render saves nothing
    form["r.servers.count"] = "2"
    form.update(
        {
            "r.servers.1.id": "second",
            "r.servers.1.url": "https://198.51.100.6/graphql",
            "r.servers.1.key_var": "KEY_TWO",
        }
    )
    removed = page.client.post(
        "/admin/collectors/unraid/rows", data={**form, "row_action": "remove:servers:0"}
    )
    assert (
        'name="r.servers.1.id"' not in removed.text
        and "second" in removed.text
        and "typed-not-saved" not in removed.text
    )


def test_two_servers_are_saved_in_order_and_an_empty_added_row_is_dropped(page):
    form = unraid_form(**{"r.servers.count": "3", "r.servers.1.id": "second", "r.servers.1.url": "https://198.51.100.6/graphql",
                          "r.servers.1.key_var": "KEY_TWO", "r.servers.2.id": "", "r.servers.2.url": ""})  # fmt: skip
    assert save(page, **form).status_code == 200
    assert [s["id"] for s in page.data()["collectors"]["unraid"]["servers"]] == ["vault", "second"]


def test_a_row_that_is_half_filled_is_refused(page):
    r = save(page, **unraid_form(**{"r.servers.0.url": "", "r.servers.0.key_var": ""}))
    assert r.status_code == 422 and r.text.count("Needed.") >= 2


def test_settings_a_row_holds_that_the_form_has_no_column_for_are_kept(page):
    custom = '    targets:\n      - { id: pdu-1, host: "198.51.100.9", profile: custom, oids: [{ name: x, oid: "1.2.3", scale: 0.1 }] }'
    (page.cfg / "collectors.yaml").write_text(page.shipped.replace("    targets: []", custom))
    form = {"f.enabled": "", "f.interval": "hourly", "f.version": "v3", "r.targets.count": "1", "r.targets.0.id": "pdu-1",
            "r.targets.0.host": "198.51.100.10", "r.targets.0.profile": "custom", "r.targets.0.orig": "0"}  # fmt: skip
    assert save(page, "snmp", **form).status_code == 200
    target = page.data()["collectors"]["snmp"]["targets"][0]
    assert target["host"] == "198.51.100.10" and target["oids"] == [
        {"name": "x", "oid": "1.2.3", "scale": 0.1}
    ]


# ---------------------------------------------------------------- the other collectors, access, and the buttons


def test_every_collector_in_the_shipped_file_has_a_form_and_saving_its_own_values_changes_nothing_real(page):
    assert set(yaml.safe_load(page.shipped)["collectors"]) == set(collector_specs.BY_COLLECTOR)
    for name, section in collector_specs.BY_COLLECTOR.items():
        loaded = settingsui.load(section, page.shipped, page.shipped)
        form = {
            f"f.{k}": ("on" if v is True else "" if v in (False, None) else str(v))
            for k, v in loaded["values"].items()
        }
        form["f.enabled"] = ""
        for r in section.row_lists:
            form[f"r.{r.key}.count"] = str(len(loaded["rows"][r.key]))
            for i, row in enumerate(loaded["rows"][r.key]):
                form.update(
                    {
                        f"r.{r.key}.{i}.{k}": ("on" if v is True else "" if v in (False, None) else str(v))
                        for k, v in row.items()
                    }
                )
        values, errors, on = settingsui.parse(section, form)
        assert not errors, (name, errors)
        new = settingsui.apply(section, page.shipped, values, on)
        before, after = (
            yaml.safe_load(page.shipped)["collectors"][name],
            yaml.safe_load(new)["collectors"][name],
        )
        for key in before:
            if any(r.key == key for r in section.row_lists):
                assert after[key] == [row for row in before[key] if "<" not in str(row)], (
                    name,
                    key,
                )  # only the examples go
            else:
                assert after[key] == before[key], (name, key)
        if not any(before.get(r.key) for r in section.row_lists):
            assert new == page.shipped, name  # nothing to drop: the text is byte for byte the same


def test_saving_needs_csrf_a_real_collector_and_a_login(page):
    assert page.client.post("/admin/collectors/unraid/save", data=unraid_form()).status_code == 403
    assert (
        page.client.post("/admin/collectors/nope/save", data={"csrf_token": page.csrf()}).status_code == 404
    )
    assert (
        page.client.post("/admin/collectors/nope/rows", data={"csrf_token": page.csrf()}).status_code == 404
    )
    assert page.https().post("/admin/collectors/unraid/save", data=unraid_form()).status_code in (
        303,
        401,
        403,
    )


def test_a_broken_collectors_file_is_reported_not_crashed_on(page):
    (page.cfg / "collectors.yaml").write_text("schema: 1\ncollectors:\n  unifi:\n    enabled: maybe\n")
    r = page.client.get("/admin/collectors")
    assert r.status_code == 200 and "collectors.yaml cannot be used" in r.text


def test_test_asks_the_source_and_says_what_happened(page, monkeypatch):
    save(
        page,
        "unifi",
        **{"f.enabled": "on", "f.interval": "hourly", "f.url_var": "UNIFI_URL", "f.key_var": "UNIFI_API_KEY"},
    )
    token = page.csrf()
    failed = page.client.post("/admin/collectors/unifi/test", data={"csrf_token": token})
    assert "Did not work." in failed.text and "UNIFI_API_KEY" in failed.text
    monkeypatch.setenv("UNIFI_API_KEY", "x")
    monkeypatch.setenv("UNIFI_URL", "https://198.51.100.1")
    assert "Worked." in page.client.post("/admin/collectors/unifi/test", data={"csrf_token": token}).text
    assert page.client.post("/admin/collectors/nope/test", data={"csrf_token": token}).status_code == 404
    assert [a["detail"] for a in audit(page) if a["action"] == "collector.test"] == [
        "unifi: failed",
        "unifi: ok",
    ]


def test_run_now_needs_it_switched_on_then_runs(page, monkeypatch):
    token = page.csrf()
    assert "switched off" in page.client.post("/admin/collectors/unifi/run", data={"csrf_token": token}).text
    save(
        page,
        "unifi",
        **{"f.enabled": "on", "f.interval": "hourly", "f.url_var": "UNIFI_URL", "f.key_var": "UNIFI_API_KEY"},
    )
    monkeypatch.setenv("UNIFI_API_KEY", "x")
    monkeypatch.setenv("UNIFI_URL", "https://198.51.100.1")
    assert (
        "ran and stored its numbers"
        in page.client.post("/admin/collectors/unifi/run", data={"csrf_token": token}).text
    )
    assert "Working" in page.client.get("/admin/collectors").text


def test_a_key_typed_into_the_variable_field_is_refused_and_never_echoed(page):
    secret = "x" * 8 + "abcdef0123456789" * 3  # a made-up key-shaped string
    r = save(page, **unraid_form(**{"r.servers.0.key_var": secret}))
    assert r.status_code == 422 and "looks like a key" in r.text
    assert secret not in r.text  # not in the field, not in the badge
    assert not (page.cfg / "collectors.yaml").exists()


def test_a_name_with_capitals_and_spaces_is_saved_as_a_lowercase_id(page):
    r = save(page, **unraid_form(**{"r.servers.0.id": "Storage Server 2"}))
    assert r.status_code == 200
    assert [s["id"] for s in page.data()["collectors"]["unraid"]["servers"]] == ["storage-server-2"]
