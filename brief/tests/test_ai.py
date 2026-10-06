"""The AI provider layer: chains that end in the template, privacy, validation, budgets, the call log."""

import json
import shutil
from datetime import timedelta

import golden_day as g
import pytest

from brief import ai, outputs, render
from brief.store import Store

NOW = g.RUN_AT
GOOD = (
    "198.51.100.3 has been silent for two days\n\n"
    "Top stories\n\n"
    "The camera at 198.51.100.3 stopped sending logs two days ago and needs a look.\n\n"
    "Checked and clean\n\nThree hosts were checked."
)


@pytest.fixture
def day(tmp_path):
    store, digest, out = g.build_daily(tmp_path)
    yield store, digest, out, tmp_path
    store.close()


def config(tmp_path, providers, features=True, chain=("local", "template"), guard="", enabled=True):
    folder = tmp_path / "cfg" / "brief"
    (folder / "prompts").mkdir(parents=True, exist_ok=True)
    shutil.copy(g.BRIEF / "prompts" / "morning_brief.md", folder / "prompts" / "morning_brief.md")
    lines = ["schema: 1", f"enabled: {str(enabled).lower()}"]
    lines += ["features:", f"  morning_brief: {str(features).lower()}", "providers:"]
    lines += providers
    lines += [
        "tasks:",
        f"  morning_brief: {{ chain: [{', '.join(chain)}], prompt: prompts/morning_brief.md }}",
    ]
    lines += ["guardrails:", guard or "  max_calls_per_day: 50"]
    (folder / "ai.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "BRIEF_CONFIG_DIR": str(tmp_path / "cfg"),
        "BRIEF_DEFAULTS_DIR": str(tmp_path / "none"),
        "KEY": "k",
    }


def provider(pid="local", locality="lan", **extra):
    fields = {
        "id": pid, "type": "openai_compatible", "base_url": "http://models.invalid/v1", "model": "m",
        "locality": locality, "api_key_var": "KEY", "enabled": "true", **extra,
    }  # fmt: skip
    return ["  - " + ", ".join(f"{k}: {v}" for k, v in fields.items()).join(["{", "}"])]


class Fake:
    """A transport that answers with `reply` (text or an exception) and remembers every request."""

    def __init__(self, reply=GOOD, tokens=(100, 50)):
        self.reply, self.tokens, self.requests = reply, tokens, []

    def __call__(self, url, headers, body, timeout):
        self.requests.append((url, headers, body, timeout))
        if isinstance(self.reply, Exception):
            raise self.reply
        return {
            "choices": [{"message": {"content": self.reply}}],
            "usage": {"prompt_tokens": self.tokens[0], "completion_tokens": self.tokens[1]},
        }


def calls(store):
    with store.read() as db:
        return [dict(r) for r in db.execute("SELECT * FROM ai_call ORDER BY rowid")]


def brief(store, digest, env, post):
    return ai.morning_brief(store, digest, "2026-09-20", NOW, env, post=post)


# ---------------------------------------------------------------- the happy path


def test_a_good_reply_becomes_the_prose(day):
    store, digest, _, tmp = day
    fake = Fake()
    prose = brief(store, digest, config(tmp, provider()), fake)
    assert prose.headline == "198.51.100.3 has been silent for two days"
    assert any("camera" in p for p in prose.paragraphs) and prose.provider == "local"
    url, headers, body, _ = fake.requests[0]
    assert url == "http://models.invalid/v1/chat/completions" and headers["Authorization"] == "Bearer k"
    assert body["messages"][0]["role"] == "system" and "silent for 2 days" in body["messages"][1]["content"]
    row = calls(store)[0]
    assert (row["outcome"], row["tokens_in"], row["tokens_out"], row["locality"]) == ("ok", 100, 50, "lan")


def test_the_prose_lands_in_brief_html_and_the_template_stays_under_it(day):
    store, digest, out, tmp = day
    prose = brief(store, digest, config(tmp, provider()), Fake())
    render.write_views(digest, out, "2026-09-20", prose, only=("brief.html",))
    html = (out / "latest" / "brief.html").read_text(encoding="utf-8")
    assert "Rewritten from the rule-based findings" in html and "stopped sending logs" in html
    assert "Stories (5, most important first)" in html  # the detected findings are still all there


def test_thinking_text_is_stripped_and_the_switch_is_sent(day):
    store, digest, _, tmp = day
    fake = Fake("<think>maybe it is the router</think>" + GOOD)
    prose = brief(store, digest, config(tmp, provider(disable_thinking="true")), fake)
    assert prose and "router" not in " ".join(prose.paragraphs)
    assert fake.requests[0][2]["reasoning_effort"] == "none"


# ---------------------------------------------------------------- the chain falls through to the template


@pytest.mark.parametrize(
    "reply, outcome",
    [
        (ai.ProviderError("error", "HTTP 500"), "error"),
        (ai.ProviderError("timeout", "no answer in time"), "timeout"),
        ("", "invalid"),
        ("A fine headline\n\nThe host plex-server and 203.0.113.9 are down.", "invalid"),
        ("x" * 300, "invalid"),
    ],
)
def test_any_failure_means_no_prose_and_a_logged_reason(day, reply, outcome):
    store, digest, _, tmp = day
    assert brief(store, digest, config(tmp, provider()), Fake(reply)) is None
    assert [c["outcome"] for c in calls(store)] == [outcome]


def test_a_down_provider_falls_to_the_next_and_the_run_still_publishes(day):
    store, digest, out, tmp = day
    env = config(tmp, provider("a") + provider("b"), chain=("a", "b", "template"))
    seen = []

    def post(url, headers, body, timeout):
        seen.append(url)
        if len(seen) == 1:
            raise ai.ProviderError("error", "refused")
        return Fake()(url, headers, body, timeout)

    prose = brief(store, digest, env, post)
    assert prose and len(seen) == 2
    assert [c["outcome"] for c in calls(store)] == ["error", "ok"]


def test_the_run_publishes_the_template_when_every_provider_is_down(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "_post", Fake(ai.ProviderError("error", "refused")))
    env = {**config(tmp_path, provider()), "BRIEF_DEFAULTS_DIR": str(tmp_path / "d")}
    store = g.build_store(tmp_path / "work")
    out = tmp_path / "out"
    try:
        digest = outputs.run_daily(store, g.SYSTEMS, g.RUN_AT, out, env={**g.golden_env(tmp_path), **env})
        assert digest.run_ok
        html = (out / "latest" / "brief.html").read_text(encoding="utf-8")
        assert "Stories (5" in html and "Rewritten from" not in html
        assert [c["outcome"] for c in calls(store)] == ["error"]
    finally:
        store.close()


def test_an_unknown_provider_in_a_chain_is_skipped(day):
    store, digest, _, tmp = day
    assert brief(store, digest, config(tmp, provider(), chain=("ghost", "template")), Fake()) is None
    assert calls(store) == []


# ---------------------------------------------------------------- off means off


@pytest.mark.parametrize("kw", [{"enabled": False}, {"features": False}])
def test_off_makes_no_call(day, kw):
    store, digest, _, tmp = day
    fake = Fake()
    assert brief(store, digest, config(tmp, provider(), **kw), fake) is None
    assert fake.requests == [] and calls(store) == []


def test_the_shipped_config_has_ai_off():
    settings = ai.load_settings({"BRIEF_CONFIG_DIR": "/none", "BRIEF_DEFAULTS_DIR": str(g.BRIEF.parent)})
    assert settings.enabled is False and settings.providers == {}


def test_a_failed_run_is_never_sent_to_a_model(tmp_path):
    store = Store(tmp_path / "empty" / "baseline.db")
    fake = Fake()
    try:
        digest = outputs.run_daily(store, g.SYSTEMS, g.RUN_AT, tmp_path / "out", state_dir=tmp_path)
        assert not digest.run_ok
        assert brief(store, digest, config(tmp_path, provider()), fake) is None
    finally:
        store.close()
    assert fake.requests == []


def test_a_provider_that_is_unusable_is_left_out(day):
    store, digest, _, tmp = day
    placeholder = provider(base_url="<model-server-ip>")
    assert brief(store, digest, config(tmp, placeholder), Fake()) is None
    assert brief(store, digest, config(tmp, provider(enabled="false")), Fake()) is None
    assert brief(store, digest, config(tmp, provider(model='""')), Fake()) is None


# ---------------------------------------------------------------- privacy


def test_a_cloud_provider_is_blocked_unless_the_task_allows_it(day):
    store, digest, _, tmp = day
    fake = Fake()
    env = config(tmp, provider("cloud", "cloud"), chain=("cloud", "template"))
    assert brief(store, digest, env, fake) is None and fake.requests == []
    assert calls(store)[0]["outcome"] == "skipped"


def test_a_cloud_provider_never_receives_hosts_titles_or_log_text(day):
    store, digest, _, tmp = day
    fake = Fake("Two systems need a look\n\nOne system is yellow and one is red.")
    env = config(
        tmp,
        provider("cloud", "cloud"),
        chain=("cloud", "template"),
        guard="  allow_cloud_for: [morning_brief]",
    )
    prose = brief(store, digest, env, fake)
    assert prose is not None
    sent = json.dumps(fake.requests[0][2])
    for secret in ("198.51.100", "blk_update_request", "silent for 2 days", "omfwd", "firewall", "cron"):
        assert secret not in sent, secret
    assert "Camera" in sent and "red" in sent  # system-level facts do go
    assert calls(store)[0]["locality"] == "cloud"


def test_the_cloud_token_budget_stops_cloud_calls(day):
    store, digest, _, tmp = day
    guard = "  allow_cloud_for: [morning_brief]\n  max_cloud_tokens_per_day: 100"
    env = config(tmp, provider("cloud", "cloud"), chain=("cloud", "template"), guard=guard)
    ok = "Systems need a look\n\nOne is red."
    assert brief(store, digest, env, Fake(ok, tokens=(80, 40)))
    fake = Fake(ok)
    assert brief(store, digest, env, fake) is None and fake.requests == []
    assert [c["outcome"] for c in calls(store)] == ["ok", "budget"]


# ---------------------------------------------------------------- budgets and context


def test_the_daily_call_budget(day):
    store, digest, _, tmp = day
    env = config(tmp, provider(), guard="  max_calls_per_day: 2")
    fake = Fake()
    assert brief(store, digest, env, fake) and brief(store, digest, env, fake)
    assert brief(store, digest, env, fake) is None and len(fake.requests) == 2
    assert calls(store)[-1]["outcome"] == "budget"
    later = NOW + timedelta(days=1, minutes=1)  # yesterday's calls fall out of the window
    assert ai.morning_brief(store, digest, "2026-09-21", later, env, post=fake)


def test_the_input_is_trimmed_to_the_context_lowest_ranked_stories_first(day):
    _, digest, _, _ = day
    big = ai.Provider("p", context_tokens=1300, max_output_tokens=1000)  # about 900 characters of room
    text = ai.build_input(digest, "2026-09-20", big)
    assert len(text) <= 900 and "silent for 2 days" in text and "cron" not in text
    full = ai.build_input(digest, "2026-09-20", ai.Provider("p", context_tokens=8192))
    assert "cron" in full and full.count("\n") <= 150


# ---------------------------------------------------------------- validation


def test_unknown_hosts_fail_strict_and_are_dropped_lenient(day):
    store, digest, _, tmp = day
    reply = "198.51.100.3 is silent\n\nThe camera is down.\n\nAlso nas-9.example.net is broken."
    assert brief(store, digest, config(tmp, provider()), Fake(reply)) is None
    lenient = config(tmp, provider(), guard="  output_validation: lenient")
    prose = brief(store, digest, lenient, Fake(reply))
    assert prose and prose.paragraphs == ["The camera is down."]


def test_a_known_host_that_is_not_in_the_digest_fails(day):
    store, digest, _, tmp = day
    assert "198.51.100.1" in ai.known_hosts(store)
    trimmed = ai.unknown_terms("198.51.100.1 is busy", "only 198.51.100.3 here", {"198.51.100.1"})
    assert trimmed == {"198.51.100.1"}


def test_the_anthropic_adapter_reads_its_own_reply_shape():
    p = ai.Provider("a", type="anthropic", model="m", api_key="secret")
    seen = {}

    def post(url, headers, body, timeout):
        seen.update(url=url, headers=headers, body=body)
        return {"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 3, "output_tokens": 2}}

    assert ai.call(p, "sys", "user", post) == ("hi", 3, 2)
    assert seen["url"].endswith("/v1/messages") and seen["headers"]["x-api-key"] == "secret"
    assert seen["body"]["system"] == "sys"


def test_a_malformed_reply_is_an_error_not_a_crash():
    with pytest.raises(ai.ProviderError):
        ai.call(ai.Provider("p", base_url="http://x"), "s", "u", lambda *a: {"nope": 1})
