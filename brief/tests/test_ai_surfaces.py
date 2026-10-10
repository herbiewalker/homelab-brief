"""Task 24: the AI settings from a shell (`brief ai`) and from the admin page, and the acceptance cases that need a
real HTTP transport: the same task against two different OpenAI-compatible servers by changing only ai.yaml."""

import json
import os
import re
import shutil
from pathlib import Path

import golden_day as g
from test_admin import lab  # noqa: F401  (an HTTPS app with a signed-in admin)
from test_ai import GOOD
from test_cli_tools import audit, box, run  # noqa: F401  (a state dir, a config dir, and the command)
from test_unraid_api import Server, serve  # noqa: F401  (a real local HTTP server)

from brief import admin_ai, ai, aitools, cli_ai
from brief.store import Store

BRIEF = Path(__file__).resolve().parents[1]
NOW = g.RUN_AT


def chat_reply(text=GOOD):
    """One body that is a valid answer to /chat/completions and to /models."""
    return {
        "data": [{"id": "m"}, {"id": "other"}],
        "choices": [{"message": {"content": text, "tool_calls": [{"function": {"name": "get_time"}}]}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }


def point_at(url, tmp_cfg_env):
    """The shipped ai.yaml with its first provider aimed at `url` and switched on."""
    path = Path(tmp_cfg_env) / "brief" / "ai.yaml"
    (path.parent / "prompts").mkdir(parents=True, exist_ok=True)
    shutil.copy(BRIEF / "prompts" / "morning_brief.md", path.parent / "prompts" / "morning_brief.md")
    text = (BRIEF / "ai.yaml").read_text(encoding="utf-8")
    text = text.replace("http://<model-server-ip>:11434/v1", url).replace('model: ""', 'model: "m"', 1)
    text = text.replace("enabled: false\n\n  - id: cloud", "enabled: true\n\n  - id: cloud", 1)
    text = text.replace("\nenabled: false\n", "\nenabled: true\n", 1)
    text = text.replace("  morning_brief: false", "  morning_brief: true", 1)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- the acceptance cases over real HTTP


def test_the_same_task_runs_against_two_different_servers_by_changing_only_ai_yaml(serve, tmp_path):  # noqa: F811
    day_dir = tmp_path / "day"
    day_dir.mkdir()
    store, digest, _ = g.build_daily(day_dir)
    one = serve((200, chat_reply("First server headline\n\nFrom the first server.")))
    two = serve((200, chat_reply("Second server headline\n\nFrom the second server.")))
    cfg = tmp_path / "cfg"
    (cfg / "brief" / "prompts").mkdir(parents=True)
    shutil.copy(BRIEF / "prompts" / "morning_brief.md", cfg / "brief" / "prompts" / "morning_brief.md")
    env = {
        "BRIEF_CONFIG_DIR": str(cfg),
        "BRIEF_DEFAULTS_DIR": str(tmp_path / "none"),
        "AI_LOCAL_API_KEY": "k",
    }
    results = []
    for server in (one, two):
        point_at(server.url.removesuffix("/graphql") + "/v1", cfg)
        prose = ai.morning_brief(store, digest, "2026-09-20", NOW, env)
        results.append(prose.headline if prose else None)
    assert results == ["First server headline", "Second server headline"]
    assert one.seen and two.seen and one.seen[0][1]["model"] == "m"
    store.close()


def test_with_the_provider_down_the_template_publishes_and_the_page_data_says_why(tmp_path):
    day_dir = tmp_path / "day"
    day_dir.mkdir()
    store, digest, _ = g.build_daily(day_dir)
    cfg = tmp_path / "cfg"
    (cfg / "brief" / "prompts").mkdir(parents=True)
    shutil.copy(BRIEF / "prompts" / "morning_brief.md", cfg / "brief" / "prompts" / "morning_brief.md")
    env = {
        "BRIEF_CONFIG_DIR": str(cfg),
        "BRIEF_DEFAULTS_DIR": str(tmp_path / "none"),
        "AI_LOCAL_API_KEY": "k",
    }
    point_at("http://127.0.0.1:9/v1", cfg)  # nothing listens here
    assert ai.morning_brief(store, digest, "2026-09-20", NOW, env) is None
    (row,) = aitools.recent_calls(store)
    assert row["outcome"] in ("error", "timeout") and row["task"] == "morning_brief"
    store.close()


def test_the_real_transport_lists_models_and_tests_a_provider(serve, tmp_path):  # noqa: F811
    server = serve((200, chat_reply("ok")))
    store = Store(tmp_path / "baseline.db")
    p = ai.Provider(
        "local", base_url=server.url.removesuffix("/graphql") + "/v1", model="m", api_key="secret"
    )
    assert aitools.list_models(p) == ["m", "other"]
    result = aitools.test_provider(store, p)
    assert result["ok"] and result["tools"] is True and result["model_listed"] is True
    assert server.seen
    store.close()


# ---------------------------------------------------------------- brief ai


def say(capsys):
    out = capsys.readouterr()
    return out.out + out.err


def test_status_says_everything_is_off_as_shipped(box, capsys):  # noqa: F811
    assert run("ai", "status") == 0
    text = say(capsys)
    assert (
        "AI is off" in text
        and "provider local: off, lan" in text
        and "chain morning_brief: local -> template" in text
    )
    assert run("ai", "status", "--json") == 0
    data = json.loads(say(capsys))
    assert (
        data["enabled"] is False
        and data["usage"]["calls_today"] == 0
        and data["features"]["morning_brief"] is False
    )


def test_switches_are_changed_in_place_through_the_config_editor(box, capsys):  # noqa: F811
    tmp, user, state, store = box
    assert run("ai", "enable") == 0 and run("ai", "feature", "morning_brief", "on") == 0
    path = tmp / "cfg" / "brief" / "ai.yaml"
    text = path.read_text(encoding="utf-8")
    assert "\nenabled: true\n" in text and "  morning_brief: true" in text and "# Master switch." in text
    assert [r["action"] for r in audit(store, "config.%")] == ["config.save", "config.save"]
    assert run("ai", "disable") == 0
    assert "\nenabled: false\n" in path.read_text(encoding="utf-8")


def test_an_edit_the_validator_refuses_changes_nothing_and_says_why(box, capsys):  # noqa: F811
    tmp, user, state, store = box
    path = tmp / "cfg" / "brief" / "ai.yaml"
    before = path.read_text(encoding="utf-8") if path.exists() else None
    assert run("ai", "provider", "local", "on") == 1  # the shipped address is still a placeholder
    assert "no real base_url" in say(capsys)
    assert run("ai", "chain", "morning_brief", "local") == 1  # no template at the end
    assert "must end with template" in say(capsys)
    assert run("ai", "guardrail", "max_calls_per_day", "-3") == 1
    assert (path.read_text(encoding="utf-8") if path.exists() else None) == before


def test_chains_and_guardrails(box, capsys):  # noqa: F811
    tmp, user, state, store = box
    assert run("ai", "chain", "morning_brief", "local", "cloud", "template") == 0
    assert run("ai", "guardrail", "max_calls_per_day", "12") == 0
    assert run("ai", "guardrail", "allow_cloud_for", "morning_brief, explain_finding") == 0
    assert run("ai", "guardrail", "quiet_hours_no_agents", "off") == 0
    say(capsys)
    assert run("ai", "status", "--json") == 0
    data = json.loads(say(capsys))
    assert data["chains"]["morning_brief"] == ["local", "cloud", "template"]
    assert (
        data["guardrails"]["max_calls_per_day"] == 12 and data["guardrails"]["quiet_hours_no_agents"] is False
    )
    assert data["guardrails"]["allow_cloud_for"] == ["morning_brief", "explain_finding"]
    assert run("ai", "guardrail", "allow_cloud_for", "telepathy") == 1


def test_test_models_calls_and_preview_against_a_server(box, capsys, serve, monkeypatch, tmp_path):  # noqa: F811
    tmp, user, state, store = box
    server = serve((200, chat_reply("Quiet night\n\nNothing needs attention.")))
    point_at(server.url.removesuffix("/graphql") + "/v1", tmp / "cfg")
    day_store, digest, _ = g.build_daily(tmp_path / "golden")
    monkeypatch.setattr(cli_ai.outputs, "run_daily", lambda *a, **k: digest)
    say(capsys)  # the golden day logs while it is built; keep the output below clean
    os.environ["AI_LOCAL_API_KEY"] = "k"
    try:
        assert run("ai", "models", "local") == 0
        assert say(capsys).split() == ["m", "other"]
        assert run("ai", "test", "local") == 0
        text = say(capsys)
        assert "connection: ok" in text and "tool calling: works" in text and "models listed: 2" in text
        assert run("ai", "calls") == 0
        assert "provider_test" in say(capsys)
        assert run("ai", "preview") == 0
        text = say(capsys)
        assert "=== template ===" in text and "=== local ===" in text and "Quiet night" in text
    finally:
        os.environ.pop("AI_LOCAL_API_KEY", None)


def test_a_provider_that_does_not_exist_is_a_clear_error(box, capsys):  # noqa: F811
    assert run("ai", "test", "ghost") == 1
    assert "no provider called ghost" in say(capsys)


# ---------------------------------------------------------------- the admin page


def csrf(client, path="/admin/ai"):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


def ai_file():
    return Path(os.environ["BRIEF_CONFIG_DIR"]) / "brief" / "ai.yaml"


def put_shipped_ai():
    ai_file().parent.mkdir(parents=True, exist_ok=True)
    ai_file().write_text((BRIEF / "ai.yaml").read_text(encoding="utf-8"), encoding="utf-8")


def test_the_page_needs_a_login_and_shows_everything_off(lab):  # noqa: F811
    put_shipped_ai()
    assert lab.https().get("/admin/ai").status_code == 303
    page = lab.client.get("/admin/ai")
    assert page.status_code == 200
    for text in (
        "Master switch",
        "OFF",
        "Locked (always on)",
        "Adjustable",
        "Call log",
        "Fallback chains",
        "Side by side",
    ):
        assert text in page.text, text
    assert "AI_LOCAL_API_KEY" in page.text and "super-secret" not in page.text
    assert 'href="/admin/ai"' in lab.client.get("/admin").text  # in the navigation


def test_every_change_needs_the_csrf_token(lab):  # noqa: F811
    for path, data in (
        ("/admin/ai/master", {"state": "on"}),
        ("/admin/ai/feature", {"name": "morning_brief", "state": "on"}),
        ("/admin/ai/provider", {"id": "local", "action": "off"}),
        ("/admin/ai/chain", {"task": "morning_brief", "chain": "local, template"}),
        ("/admin/ai/guardrail", {"key": "max_calls_per_day", "value": "5"}),
        ("/admin/ai/test/local", {}),
        ("/admin/ai/preview", {"provider": "local"}),
        ("/admin/ai/explain", {"finding_id": "x"}),
    ):
        assert lab.client.post(path, data=data).status_code == 403, path


def test_switches_chains_and_guardrails_are_saved_audited_and_validated(lab):  # noqa: F811
    put_shipped_ai()
    token = csrf(lab.client)
    r = lab.client.post("/admin/ai/master", data={"csrf_token": token, "state": "on"})
    assert r.status_code == 303 and "\nenabled: true\n" in ai_file().read_text(encoding="utf-8")
    page = lab.client.get("/admin/ai?done=saved").text
    assert "Saved." in page and "ON" in page
    lab.client.post("/admin/ai/feature", data={"csrf_token": token, "name": "weekly_summary", "state": "on"})
    lab.client.post(
        "/admin/ai/guardrail", data={"csrf_token": token, "key": "max_calls_per_day", "value": "7"}
    )
    lab.client.post(
        "/admin/ai/chain",
        data={"csrf_token": token, "task": "morning_brief", "chain": "local cloud template"},
    )
    data = ai_file().read_text(encoding="utf-8")
    assert (
        "  weekly_summary: true" in data
        and "max_calls_per_day: 7" in data
        and "[local, cloud, template]" in data
    )
    with lab.store.read() as db:
        actions = [
            r["action"] for r in db.execute("SELECT action FROM audit_log WHERE action LIKE 'config.%'")
        ]
    assert actions.count("config.save") >= 4

    before = data
    bad = lab.client.post("/admin/ai/provider", data={"csrf_token": token, "id": "local", "action": "on"})
    assert (
        bad.status_code == 422
        and "no real base_url" in bad.text
        and ai_file().read_text(encoding="utf-8") == before
    )
    bad = lab.client.post(
        "/admin/ai/chain", data={"csrf_token": token, "task": "morning_brief", "chain": "local"}
    )
    assert bad.status_code == 422 and "must end with template" in bad.text
    bad = lab.client.post(
        "/admin/ai/guardrail", data={"csrf_token": token, "key": "max_calls_per_day", "value": "lots"}
    )
    assert bad.status_code == 422 and "whole number" in bad.text
    bad = lab.client.post(
        "/admin/ai/guardrail", data={"csrf_token": token, "key": "locked_thing", "value": "1"}
    )
    assert bad.status_code == 422
    assert ai_file().read_text(encoding="utf-8") == before


def test_test_connection_and_list_models_show_their_results_in_place(lab, serve):  # noqa: F811
    server = serve((200, chat_reply("ok")))
    put_shipped_ai()
    point_at(server.url.removesuffix("/graphql") + "/v1", os.environ["BRIEF_CONFIG_DIR"])
    token = csrf(lab.client)
    r = lab.client.post("/admin/ai/test/local", data={"csrf_token": token})
    assert r.status_code == 200 and "The connection works." in r.text and "works" in r.text
    r = lab.client.post("/admin/ai/models/local", data={"csrf_token": token})
    assert "2 model(s)" in r.text and 'name="value" value="other"' in r.text
    r = lab.client.post("/admin/ai/test/ghost", data={"csrf_token": token})
    assert "no provider called ghost" in r.text
    with lab.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'ai.test'").fetchone()[0] == 1


def test_the_preview_shows_the_template_beside_the_providers_version(lab, serve, monkeypatch, tmp_path):  # noqa: F811
    server = serve((200, chat_reply("Quiet night\n\nNothing needs attention.")))
    day_store, digest, _ = g.build_daily(tmp_path / "golden")
    monkeypatch.setattr(admin_ai.outputs, "run_daily", lambda *a, **k: digest)
    put_shipped_ai()
    point_at(server.url.removesuffix("/graphql") + "/v1", os.environ["BRIEF_CONFIG_DIR"])
    os.environ["AI_LOCAL_API_KEY"] = "k"
    try:
        r = lab.client.post("/admin/ai/preview", data={"csrf_token": csrf(lab.client), "provider": "local"})
    finally:
        os.environ.pop("AI_LOCAL_API_KEY", None)
    assert r.status_code == 200 and "Template" in r.text and "Quiet night" in r.text
    assert (
        "Call log" in lab.client.get("/admin/ai").text and "morning_brief" in lab.client.get("/admin/ai").text
    )


def test_explain_says_so_plainly_when_nothing_can_be_explained(lab):  # noqa: F811
    put_shipped_ai()
    token = csrf(lab.client)
    r = lab.client.post("/admin/ai/explain", data={"csrf_token": token, "finding_id": "no-such-finding"})
    assert "No finding with that id is open right now." in r.text


def test_a_prompt_is_edited_versioned_and_restored_on_the_page(lab):  # noqa: F811
    token = csrf(lab.client)
    page = lab.client.get("/admin/ai/prompt/weekly_summary")
    assert page.status_code == 200 and "Prompt: weekly summary" in page.text
    base = re.search(r'name="base" value="([^"]+)"', page.text).group(1)
    text = "Notes for me.\n---\nYou write a very short weekly summary.\n"
    saved = lab.client.post(
        "/admin/ai/prompt/weekly_summary", data={"csrf_token": token, "base": base, "text": text}
    )
    assert saved.status_code == 303
    assert (
        Path(os.environ["BRIEF_CONFIG_DIR"]) / "brief" / "prompts" / "weekly_summary.md"
    ).read_text() == text
    bad = lab.client.post(
        "/admin/ai/prompt/weekly_summary", data={"csrf_token": token, "base": "stale", "text": text}
    )
    assert bad.status_code == 409
    second = "Notes again.\n---\nYou write a very, very short weekly summary.\n"
    base = re.search(
        r'name="base" value="([^"]+)"', lab.client.get("/admin/ai/prompt/weekly_summary").text
    ).group(1)
    assert (
        lab.client.post(
            "/admin/ai/prompt/weekly_summary", data={"csrf_token": token, "base": base, "text": second}
        ).status_code
        == 303
    )
    refused = lab.client.post(
        "/admin/ai/prompt/weekly_summary",
        data={
            "csrf_token": token,
            "base": re.search(
                r'name="base" value="([^"]+)"', lab.client.get("/admin/ai/prompt/weekly_summary").text
            ).group(1),
            "text": "no separator",
        },
    )
    assert (
        refused.status_code == 422 and "line with only ---" in refused.text and "no separator" in refused.text
    )
    page = lab.client.get("/admin/ai/prompt/weekly_summary")
    stamp = re.search(r'name="stamp" value="([^"]+)"', page.text)
    assert stamp, "the replaced version is listed"
    restored = lab.client.post(
        "/admin/ai/prompt/weekly_summary/restore", data={"csrf_token": token, "stamp": stamp.group(1)}
    )
    assert restored.status_code == 303
    assert (
        lab.client.post("/admin/ai/prompt/weekly_summary/reset", data={"csrf_token": token}).status_code
        == 303
    )
    assert lab.client.get("/admin/ai/prompt/not_a_prompt").status_code == 404
    assert lab.client.post("/admin/ai/prompt/not_a_prompt", data={"csrf_token": token}).status_code == 404
