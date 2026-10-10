"""The Collectors page: state, variables set or not (never their values), switching on and off, test, and run now."""

import re

import pytest
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)
from test_collectors import Fake

from brief import yamlscalar
from brief.collectors import framework

YAML = """schema: 1
collectors:
  unifi:
    enabled: false          # a comment that must survive
    interval: hourly
    key_var: UNIFI_API_KEY
  unraid:
    enabled: false
    interval: hourly
    servers:
      - { id: one, url: "https://198.51.100.5/graphql", key_var: UNRAID_ONE_KEY, verify_tls: false }
"""


@pytest.fixture
def page(lab, monkeypatch):  # noqa: F811
    cfg = lab.settings.state_dir.parent / "cfg" / "brief"
    (cfg / "collectors.yaml").write_text(YAML)
    monkeypatch.setitem(framework.REGISTRY, "unifi", Fake)
    monkeypatch.delenv("UNIFI_API_KEY", raising=False)
    monkeypatch.delenv("UNRAID_ONE_KEY", raising=False)
    lab.cfg = cfg
    lab.csrf = lambda: re.search(
        r'name="csrf_token" value="([^"]+)"', lab.client.get("/admin/collectors").text
    ).group(1)
    return lab


def test_the_page_lists_every_collector_with_its_state_and_variables(page):
    text = page.client.get("/admin/collectors").text
    assert "<h1>Collectors</h1>" in text and "unifi" in text and "unraid" in text
    assert "UNIFI_API_KEY" in text and "UNRAID_ONE_KEY" in text and text.count("not set") >= 2
    assert "not in this version" not in text  # both are real collectors in this version


def test_a_variable_is_shown_as_set_but_its_value_never_appears(page, monkeypatch):
    monkeypatch.setenv("UNIFI_API_KEY", "super-secret-value-123")
    text = page.client.get("/admin/collectors").text
    assert "super-secret-value-123" not in text and re.search(
        r"UNIFI_API_KEY</code>\s*<span class=\"badge ok\">set", text
    )


def test_switching_on_edits_only_the_enabled_line_through_the_config_editor(page):
    token = page.csrf()
    r = page.client.post("/admin/collectors/unifi/switch", data={"csrf_token": token, "state": "on"})
    assert r.status_code == 303
    new = (page.cfg / "collectors.yaml").read_text()
    assert "enabled: true          # a comment that must survive" in new
    assert new.count("enabled: true") == 1 and "UNRAID_ONE_KEY" in new
    assert any(a["action"] == "config.save" for a in _audit(page))
    page.client.post("/admin/collectors/unifi/switch", data={"csrf_token": token, "state": "off"})
    assert (page.cfg / "collectors.yaml").read_text() == YAML  # and back, byte for byte
    assert (
        len(
            __import__("brief.configedit", fromlist=["x"]).versions(
                page.settings.state_dir, "collectors.yaml"
            )
        )
        == 2
    )


def test_the_switch_needs_csrf_a_real_name_and_a_login(page):
    assert page.client.post("/admin/collectors/unifi/switch", data={"state": "on"}).status_code == 403
    token = page.csrf()
    assert (
        page.client.post(
            "/admin/collectors/nope/switch", data={"csrf_token": token, "state": "on"}
        ).status_code
        == 404
    )
    assert page.https().post("/admin/collectors/unifi/switch", data={"state": "on"}).status_code in (
        303,
        401,
        403,
    )


def test_a_broken_collectors_file_is_reported_not_crashed_on(page):
    (page.cfg / "collectors.yaml").write_text("schema: 1\ncollectors:\n  unifi:\n    enabled: maybe\n")
    r = page.client.get("/admin/collectors")
    assert r.status_code == 200 and "collectors.yaml cannot be used" in r.text


def test_test_asks_the_source_and_says_what_happened(page, monkeypatch):
    token = page.csrf()
    failed = page.client.post(
        "/admin/collectors/unifi/test", data={"csrf_token": token}, headers={"hx-request": "true"}
    )
    assert failed.status_code == 200 and "Did not work." in failed.text and "UNIFI_API_KEY" in failed.text
    monkeypatch.setenv("UNIFI_API_KEY", "x")
    worked = page.client.post("/admin/collectors/unifi/test", data={"csrf_token": token})
    assert "Worked." in worked.text and "reached the controller" in worked.text
    assert page.client.post("/admin/collectors/nope/test", data={"csrf_token": token}).status_code == 404
    assert [a["detail"] for a in _audit(page) if a["action"] == "collector.test"] == [
        "unifi: failed",
        "unifi: ok",
    ]


def test_run_now_needs_it_switched_on_then_runs_and_the_page_shows_the_result(page, monkeypatch):
    token = page.csrf()
    assert "switched off" in page.client.post("/admin/collectors/unifi/run", data={"csrf_token": token}).text
    page.client.post("/admin/collectors/unifi/switch", data={"csrf_token": token, "state": "on"})
    assert (
        "Did not work." in page.client.post("/admin/collectors/unifi/run", data={"csrf_token": token}).text
    )  # no key yet
    monkeypatch.setenv("UNIFI_API_KEY", "x")
    assert (
        "ran and stored its numbers"
        in page.client.post("/admin/collectors/unifi/run", data={"csrf_token": token}).text
    )
    assert re.search(r"20\d\d-.*: ok", page.client.get("/admin/collectors").text)
    assert "Run now" in page.client.get("/admin/collectors").text


def test_yamlscalar_can_flip_a_collector_inside_a_list_free_block():
    assert "enabled: true" in yamlscalar.set_path(YAML, ["collectors", "unraid", "enabled"], True)


def _audit(env):
    with env.store.read() as db:
        return [dict(r) for r in db.execute("SELECT user, action, detail FROM audit_log")]
