"""Task 24: the AI provider layer beyond the morning brief: privacy, explain and weekly tasks, provider tools,
ai.yaml rules and edits, and the prompt editor."""

import json
import shutil
from dataclasses import replace
from pathlib import Path

import golden_day as g
import pytest
from test_ai import GOOD, Fake, calls, config  # noqa: F401  (the day fixture comes with it)
from test_ai import day as day  # noqa: F401
from test_ai import provider as provider_line

from brief import ai, aiconfig, aiguard, aitasks, aitools, configedit
from brief.config import ConfigError
from brief.detectors import Finding
from brief.store import Store

BRIEF = Path(__file__).resolve().parents[1]
NOW = g.RUN_AT
DEVICE_LABEL, OWNER, MAC = "Alice's laptop", "Alice", "00:00:5e:00:53:77"
EXPLAIN = "A drive is reporting sectors it cannot read.\n\nBack up first and plan a replacement."


def ai_yaml(tmp_path, providers, allow_cloud="[]", extra_guard="", enabled=True, tasks=None, features=None):
    """A config with all three text tasks wired up and every shipped prompt in place."""
    folder = tmp_path / "cfg" / "brief"
    (folder / "prompts").mkdir(parents=True, exist_ok=True)
    shutil.copy(BRIEF / "playbook.yaml", folder / "playbook.yaml")
    for name in ("morning_brief", "explain_finding", "weekly_summary"):
        shutil.copy(BRIEF / "prompts" / f"{name}.md", folder / "prompts" / f"{name}.md")
    features = features or ("morning_brief", "explain_finding", "weekly_summary")
    chains = tasks or {t: "[local, template]" for t in ("morning_brief", "explain_finding", "weekly_summary")}
    lines = ["schema: 1", f"enabled: {str(enabled).lower()}", "features:"]
    lines += [f"  {f}: true" for f in features]
    lines += ["providers:", *providers, "tasks:"]
    lines += [f"  {t}: {{ chain: {c}, prompt: prompts/{t}.md }}" for t, c in chains.items()]
    lines += ["guardrails:", "  max_calls_per_day: 50", f"  allow_cloud_for: {allow_cloud}", extra_guard]
    (folder / "ai.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "BRIEF_CONFIG_DIR": str(tmp_path / "cfg"),
        "BRIEF_DEFAULTS_DIR": str(tmp_path / "none"),
        "KEY": "k",
    }


def private_data(store, tmp_path):
    """A device the household owns: in devices.yaml, as a client, and as a sender."""
    folder = tmp_path / "cfg" / "brief"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "devices.yaml").write_text(
        f'schema: 1\ndevices:\n  - {{ mac: "{MAC}", label: "{DEVICE_LABEL}", owner: "{OWNER}", profile: full }}\n',
        encoding="utf-8",
    )
    with store.write() as tx:
        tx.db.execute(
            "INSERT INTO client (mac, first_seen, last_seen, hostname) VALUES (?, ?, ?, ?)",
            (MAC, "2026-09-01T00:00:00Z", "2026-09-20T00:00:00Z", "alice-macbook"),
        )


def sent_text(fake):
    return json.dumps([request[2] for request in fake.requests])


def smart_finding():
    f = Finding(
        "smart_trend:server-1:ABC:197", "smart_trend", "server-1 Test HDD …0003: 8 current pending sector",
        detail={"disk": "server-1 Test HDD …0003", "server": "server-1", "attr": "197:Current_Pending_Sector",
                "raw": 8, "delta_14d": 8, "bucket": "act_now", "rule": "smart_pending_nonzero",
                "advice": "server-1 Test HDD …0003 reports 8 unreadable sectors. Back up and replace."},
    )  # fmt: skip
    return f


def device_finding():
    return Finding(
        "traffic_spike:" + MAC,
        "traffic_spike",
        f"{DEVICE_LABEL} moved 9.0 GB in 24 hours",
        detail={"device": DEVICE_LABEL, "gb": 9.0, "multiple": 12, "bucket": "watch"},
    )


# ---------------------------------------------------------------- the privacy scan


@pytest.mark.parametrize(
    "text, expected",
    [
        ("camera at 198.51.100.3 is silent", "an IP address"),
        ("it ended at 198.51.100.3.", "an IP address"),
        ("laptop 00:00:5E:00:53:77 joined", "a MAC address"),
        ("firmware 7.5.10.17129 is out", None),
        ("version 1.2.3 of the plugin", None),
        ("3 systems are fine", None),
        ("Alice's laptop was busy", "a device or host name"),
        ("the alice-macbook host", "a device or host name"),
        ("the malicestudio team", None),
    ],
)
def test_the_privacy_scan_finds_addresses_and_private_names_and_not_version_numbers(text, expected):
    terms = {"alice's laptop", "alice-macbook", "alice"}
    assert aiguard.privacy_leak(text, terms) == expected


def test_private_terms_come_from_devices_senders_and_clients(day):
    store, _, _, tmp = day
    private_data(store, tmp)
    terms = aiguard.private_terms(
        store, {"BRIEF_CONFIG_DIR": str(tmp / "cfg"), "BRIEF_DEFAULTS_DIR": str(tmp / "none")}
    )
    assert {DEVICE_LABEL.lower(), OWNER.lower(), MAC, "alice-macbook"} <= terms
    assert any(t.startswith("198.51.100.") for t in terms)  # the senders


def test_a_cloud_brief_never_carries_device_detail_even_when_the_digest_has_it(day):
    store, digest, _, tmp = day
    private_data(store, tmp)
    story = replace(
        digest.stories[0], kind="new_device", title=f"A new device joined: {DEVICE_LABEL}", host=None,
        detail={"label": DEVICE_LABEL, "mac": MAC, "owner": OWNER},
    )  # fmt: skip
    risky = replace(digest, stories=[story, *digest.stories[1:]])
    env = ai_yaml(tmp, provider_line("cloud", locality="cloud"), allow_cloud="[morning_brief]",
                  tasks={"morning_brief": "[cloud, template]"})  # fmt: skip
    fake = Fake()
    ai.morning_brief(store, risky, "2026-09-20", NOW, env, post=fake)
    assert len(fake.requests) == 1
    body = sent_text(fake)
    for private in (DEVICE_LABEL, OWNER, MAC, "alice-macbook", "198.51.100.", "new_device"):
        assert private not in body, private
    assert aiguard.privacy_leak(body, aiguard.private_terms(store, env)) is None


def test_a_cloud_input_that_slips_a_private_value_through_is_stopped_before_sending(day):
    store, _, _, tmp = day
    private_data(store, tmp)
    env = ai_yaml(tmp, provider_line("cloud", locality="cloud"), allow_cloud="[explain_finding]",
                  tasks={"explain_finding": "[cloud, template]"})  # fmt: skip
    settings = ai.load_settings(env)
    fake = Fake()
    job = ai.Job(
        "explain_finding", "system prompt", lan=lambda p, n: "x", cloud=lambda: f"the owner is {OWNER}, host 198.51.100.9",
        check=lambda reply, sent: reply, private=aiguard.private_terms(store, env),
    )  # fmt: skip
    assert ai.run_chain(store, job, settings, NOW, fake) is None
    assert fake.requests == []
    assert [r["outcome"] for r in calls(store)] == ["privacy"]
    assert "198.51.100.9" not in json.dumps(calls(store)) and OWNER not in json.dumps(
        calls(store)
    )  # the log keeps no value


# ---------------------------------------------------------------- explain a finding


def explain(store, tmp, finding, fake, providers=None, **kw):
    env = ai_yaml(tmp, providers or provider_line(), **kw)
    return aitasks.explain_finding(store, finding, NOW, env, post=fake), env


def test_a_lan_provider_gets_the_whole_finding_and_the_reply_is_shown(day):
    store, _, _, tmp = day
    fake = Fake(EXPLAIN)
    result, _ = explain(store, tmp, smart_finding(), fake)
    assert result.text.startswith("A drive is reporting") and result.provider == "local"
    sent = sent_text(fake)
    assert "unreadable sectors" in sent and "Test HDD" in sent and "act_now" in sent
    assert calls(store)[0]["task"] == "explain_finding" and calls(store)[0]["outcome"] == "ok"


def test_a_cloud_provider_gets_only_the_kind_the_bucket_and_the_generic_wording(day):
    store, _, _, tmp = day
    finding = replace(smart_finding(), detail={**smart_finding().detail, "rule": "smart_pending_nonzero"})
    fake = Fake("A drive is reporting sectors it cannot read.\n\nBack up first.")
    result, env = explain(
        store, tmp, finding, fake, provider_line("cloud", locality="cloud"),
        allow_cloud="[explain_finding]", tasks={"explain_finding": "[cloud, template]"},
    )  # fmt: skip
    assert result is not None
    body = sent_text(fake)
    assert (
        "smart_trend" in body and "act_now" in body and "{disk}" in body
    )  # the rule's own wording, unfilled
    assert "Test HDD" not in body and "server-1" not in body and "…0003" not in body


def test_a_finding_about_one_device_never_goes_to_a_cloud_provider(day):
    store, _, _, tmp = day
    private_data(store, tmp)
    fake = Fake("Something about a device.")
    result, _ = explain(
        store, tmp, device_finding(), fake, provider_line("cloud", locality="cloud"),
        allow_cloud="[explain_finding]", tasks={"explain_finding": "[cloud, template]"},
    )  # fmt: skip
    assert result is None and fake.requests == []
    assert [(r["outcome"], r["detail"]) for r in calls(store)] == [
        ("skipped", "device-level subject never goes to cloud")
    ]


def test_the_same_device_finding_is_explained_on_the_lan(day):
    store, _, _, tmp = day
    private_data(store, tmp)
    fake = Fake("The laptop moved much more data than usual today.\n\nCheck for a large update.")
    result, _ = explain(store, tmp, device_finding(), fake)
    assert result is not None and DEVICE_LABEL in sent_text(fake)


@pytest.mark.parametrize(
    "reply",
    [
        "The problem is on 198.51.100.77, not the camera.",  # an address that was never mentioned
        "The camera at 198.51.100.3 and 00:00:5e:00:53:99 are linked.",  # a MAC that was never mentioned
        "",  # nothing
        "word " * 400,  # far longer than asked
    ],
)
def test_a_reply_that_invents_or_rambles_is_rejected_in_strict_mode(day, reply):
    store, _, _, tmp = day
    finding = Finding(
        "absence-cam", "absence", "Camera is silent", host="198.51.100.3", detail={"host": "198.51.100.3"}
    )
    fake = Fake(reply)
    result, _ = explain(store, tmp, finding, fake)
    assert result is None and calls(store)[-1]["outcome"] == "invalid"


def test_lenient_mode_drops_only_the_paragraph_that_invents_something(day):
    store, _, _, tmp = day
    finding = Finding(
        "absence-cam", "absence", "Camera is silent", host="198.51.100.3", detail={"host": "198.51.100.3"}
    )
    fake = Fake(
        "The camera at 198.51.100.3 went quiet.\n\nIt may be linked to 198.51.100.88.\n\nCheck its cable."
    )
    result, _ = explain(store, tmp, finding, fake, extra_guard="  output_validation: lenient")
    assert result.text == "The camera at 198.51.100.3 went quiet.\n\nCheck its cable."


def test_a_thinking_block_is_removed_from_an_explanation(day):
    store, _, _, tmp = day
    fake = Fake("<think>it is probably the cable</think>Check the cable.")
    result, _ = explain(store, tmp, smart_finding(), fake)
    assert result.text == "Check the cable."


@pytest.mark.parametrize("kw", [{"enabled": False}, {"features": ("morning_brief",)}])
def test_explain_makes_no_call_when_it_is_off(day, kw):
    store, _, _, tmp = day
    fake = Fake(EXPLAIN)
    result, _ = explain(store, tmp, smart_finding(), fake, **kw)
    assert result is None and fake.requests == [] and calls(store) == []


def test_every_provider_down_means_no_explanation_and_a_logged_reason(day):
    store, _, _, tmp = day
    fake = Fake(ai.ProviderError("timeout", "no answer in time"))
    result, _ = explain(store, tmp, smart_finding(), fake)
    assert result is None and calls(store)[0]["outcome"] == "timeout"


def test_the_daily_call_budget_covers_every_task(day):
    store, digest, _, tmp = day
    env = ai_yaml(tmp, provider_line(), extra_guard="  max_calls_per_day: 1")
    env_one = {**env}
    assert ai.morning_brief(store, digest, "2026-09-20", NOW, env_one, post=Fake()) is not None
    fake = Fake(EXPLAIN)
    assert aitasks.explain_finding(store, smart_finding(), NOW, env_one, post=fake) is None
    assert fake.requests == [] and calls(store)[-1]["outcome"] == "budget"


# ---------------------------------------------------------------- the weekly summary


REPORT = [
    "# Weekly report 2026-W40", "",
    "## Volume by system", "", "| System | This week |", "| --- | ---: |", "| Gateway | 1,200 |", "",
    "## Biggest movers", "", "- noisy app (198.51.100.3): 900 vs 100", "",
    "## Power and energy", "", "| System | kWh |", "| --- | ---: |", "| Server Two | 16.8 |",
]  # fmt: skip


def test_the_weekly_summary_on_the_lan_sees_the_whole_report(day):
    store, _, _, tmp = day
    fake = Fake("A quiet week\n\nTraffic on the gateway rose a little.")
    env = ai_yaml(tmp, provider_line())
    summary = aitasks.weekly_summary(store, REPORT, NOW, env, post=fake)
    assert summary.title == "A quiet week" and summary.paragraphs == ["Traffic on the gateway rose a little."]
    assert "Biggest movers" in sent_text(fake)
    md = aitasks.summary_markdown(summary, "2026-W40.md")
    assert md[0] == "# A quiet week" and "2026-W40.md" in md[-1]


def test_a_cloud_weekly_summary_gets_only_the_system_level_tables(day):
    store, _, _, tmp = day
    fake = Fake("A quiet week\n\nPower use was steady.")
    env = ai_yaml(tmp, provider_line("cloud", locality="cloud"), allow_cloud="[weekly_summary]",
                  tasks={"weekly_summary": "[cloud, template]"})  # fmt: skip
    summary = aitasks.weekly_summary(store, REPORT, NOW, env, post=fake)
    assert summary is not None
    body = sent_text(fake)
    assert "Volume by system" in body and "Power and energy" in body
    assert "Biggest movers" not in body and "noisy app" not in body and "198.51.100.3" not in body


def test_the_weekly_summary_is_off_unless_switched_on(day):
    store, _, _, tmp = day
    fake = Fake("A title\n\nA paragraph.")
    env = ai_yaml(tmp, provider_line(), features=("morning_brief",))
    assert aitasks.weekly_summary(store, REPORT, NOW, env, post=fake) is None and fake.requests == []


# ---------------------------------------------------------------- listing and testing providers


class Reply:
    """A transport and a getter that answer like an OpenAI-compatible server."""

    def __init__(self, tools=True, models=("m", "other"), error=None):
        self.tools, self.models, self.error, self.posts = tools, models, error, []

    def post(self, url, headers, body, timeout):
        self.posts.append(body)
        if self.error and "tools" in body:
            raise self.error
        message = {"content": "ok"}
        if "tools" in body and self.tools:
            message["tool_calls"] = [{"function": {"name": "get_time"}}]
        return {"choices": [{"message": message}], "usage": {"prompt_tokens": 7, "completion_tokens": 1}}

    def get(self, url, headers, timeout):
        return {"data": [{"id": m} for m in self.models]}


def lan_provider(**kw):
    return ai.Provider("local", base_url="http://models.invalid/v1", model="m", **kw)


def test_models_are_listed_for_both_provider_types():
    assert aitools.list_models(lan_provider(), Reply().get) == ["m", "other"]
    anthropic = ai.Provider("a", type="anthropic", model="x", api_key="k")
    seen = []
    aitools.list_models(
        anthropic, lambda url, headers, t: seen.append((url, headers)) or {"data": [{"id": "x"}]}
    )
    assert seen[0][0] == "https://api.anthropic.com/v1/models" and seen[0][1]["x-api-key"] == "k"
    with pytest.raises(ai.ProviderError):
        aitools.list_models(lan_provider(), lambda *a: {"nope": 1})


def test_a_good_provider_test_reports_latency_models_and_tools_and_is_logged(day):
    store, *_ = day
    r = Reply()
    result = aitools.test_provider(store, lan_provider(), NOW, r.post, r.get)
    assert result["ok"] and result["models"] == ["m", "other"] and result["model_listed"] is True
    assert result["tools"] is True and result["latency_ms"] is not None
    row = calls(store)[0]
    assert (row["task"], row["outcome"], row["tokens_in"]) == ("provider_test", "ok", 7)


def test_tool_calling_is_false_when_the_server_refuses_tools_and_unknown_otherwise(day):
    store, *_ = day
    refuses = Reply(error=ai.ProviderError("error", "HTTP 400"))
    assert aitools.test_provider(store, lan_provider(), NOW, refuses.post, refuses.get)["tools"] is False
    broken = Reply(error=ai.ProviderError("timeout", "no answer in time"))
    assert aitools.test_provider(store, lan_provider(), NOW, broken.post, broken.get)["tools"] is None
    plain = Reply(tools=False)
    assert aitools.test_provider(store, lan_provider(), NOW, plain.post, plain.get)["tools"] is False


def test_a_failing_provider_test_says_why_and_a_missing_model_is_called_out(day):
    store, *_ = day
    down = Reply(error=None)

    def dead(url, headers, body, timeout):
        raise ai.ProviderError("timeout", "no answer in time")

    result = aitools.test_provider(store, lan_provider(), NOW, dead, down.get)
    assert not result["ok"] and "timeout" in result["error"] and calls(store)[-1]["outcome"] == "error"
    unset = aitools.test_provider(
        store, ai.Provider("local", base_url="http://x/v1"), NOW, down.post, down.get
    )
    assert not unset["ok"] and "no model is set" in unset["error"]
    missing = aitools.test_provider(store, replace(lan_provider(), model="gone"), NOW, down.post, down.get)
    assert missing["ok"] and missing["model_listed"] is False


def test_an_anthropic_tool_check_reads_tool_use_blocks(day):
    store, *_ = day

    def post(url, headers, body, timeout):
        if "tools" in body:
            return {"content": [{"type": "tool_use", "name": "get_time"}], "usage": {}}
        return {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 3, "output_tokens": 1}}

    p = ai.Provider("a", type="anthropic", model="x", api_key="k")
    result = aitools.test_provider(store, p, NOW, post, lambda u, h, t: {"data": [{"id": "x"}]})
    assert result["ok"] and result["tools"] is True


def test_the_provider_rows_never_contain_a_key(day):
    _, _, _, tmp = day
    env = ai_yaml(tmp, provider_line())
    env["KEY"] = "super-secret-key-value"
    rows = aitools.provider_rows(env)
    assert rows[0]["key_set"] is True and "super-secret-key-value" not in json.dumps(rows)
    with pytest.raises(ConfigError, match="no provider called"):
        aitools.build_provider("nope", env)


def test_a_placeholder_address_cannot_be_tested(day):
    _, _, _, tmp = day
    env = ai_yaml(tmp, provider_line(base_url="http://<model-server-ip>:11434/v1"))
    with pytest.raises(ConfigError, match="no address yet"):
        aitools.build_provider("local", env)


def test_the_preview_puts_the_template_next_to_a_providers_version(day):
    store, digest, _, tmp = day
    env = ai_yaml(tmp, provider_line(), features=())  # the feature switch is not needed to preview
    result = aitools.preview_morning_brief(store, digest, "2026-09-20", "local", NOW, env, Fake())
    assert result["prose"].headline == "198.51.100.3 has been silent for two days" and result["template"]
    none = aitools.preview_morning_brief(store, digest, "2026-09-20", "ghost", NOW, env, Fake())
    assert none["prose"] is None and "not switched on" in none["note"]
    bad = aitools.preview_morning_brief(
        store, digest, "2026-09-20", "local", NOW, env, Fake("a made up host x.invalid")
    )
    assert bad["prose"] is None and "see the call log" in bad["note"]


# ---------------------------------------------------------------- the rules for ai.yaml and edits in place


SHIPPED = (BRIEF / "ai.yaml").read_text(encoding="utf-8")


def write(tmp_path, text):
    path = tmp_path / "ai.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_shipped_file_is_valid_and_all_ai_is_off(tmp_path):
    data = aiconfig.validate(write(tmp_path, SHIPPED))
    assert data["enabled"] is False and not any(data["features"].values())


@pytest.mark.parametrize(
    "old, new, message",
    [
        ("enabled: false\n", "enabled: maybe\n", "enabled must be"),
        (
            "chain: [local, template], prompt: prompts/morning_brief.md",
            "chain: [local], prompt: prompts/morning_brief.md",
            "must end with template",
        ),
        (
            "chain: [local, template], prompt: prompts/explain_finding.md",
            "chain: [nope, template], prompt: prompts/explain_finding.md",
            "not a provider",
        ),
        (
            "chain: [local, template], prompt: prompts/weekly_summary.md",
            "chain: [template, local], prompt: prompts/weekly_summary.md",
            "must end with template",
        ),
        ("locality: lan ", "locality: moon ", "locality must be"),
        ("max_calls_per_day: 50 ", "max_calls_per_day: -5 ", "wrong type or a negative"),
        ("output_validation: strict", "output_validation: sloppy", "strict or lenient"),
        ("allow_cloud_for: []", "allow_cloud_for: [nonsense]", "not a feature"),
        ("api_key_var: AI_LOCAL_API_KEY", "api_key_var: my key", "api_key_var must name"),
        ("timeout_s: 120", "timeout_s: 0", "above zero"),
    ],
)
def test_a_bad_ai_file_is_refused(tmp_path, old, new, message):
    assert old in SHIPPED, old
    with pytest.raises(ConfigError, match=message):
        aiconfig.validate(write(tmp_path, SHIPPED.replace(old, new, 1)))


def test_a_provider_switched_on_without_a_real_address_is_refused(tmp_path):
    text = SHIPPED.replace("    enabled: false\n\n  - id: cloud", "    enabled: true\n\n  - id: cloud", 1)
    with pytest.raises(ConfigError, match="no real base_url"):
        aiconfig.validate(write(tmp_path, text))


def test_the_conversation_task_needs_no_template_and_the_others_do(tmp_path):
    assert "ask_the_lab:     { chain: [local]," in SHIPPED
    aiconfig.validate(write(tmp_path, SHIPPED))  # valid as shipped


def test_edits_in_place_change_one_value_and_keep_everything_else(tmp_path):
    text = SHIPPED
    text = aiconfig.set_master(text, True)
    text = aiconfig.set_feature(text, "morning_brief", True)
    text = aiconfig.set_provider(text, "local", "model", "qwen3.5:9b")
    text = aiconfig.set_chain(text, "morning_brief", ["local", "cloud", "template"])
    text = aiconfig.set_guardrail(text, "max_calls_per_day", 10)
    text = aiconfig.set_guardrail(text, "allow_cloud_for", ["explain_finding"])
    text = aiconfig.set_guardrail(text, "quiet_hours_no_agents", False)
    data = aiconfig.validate(write(tmp_path, text))
    assert (
        data["enabled"] is True
        and data["features"]["morning_brief"] is True
        and data["features"]["weekly_summary"] is False
    )
    assert data["providers"][0]["model"] == "qwen3.5:9b" and data["providers"][0]["enabled"] is False
    assert data["tasks"]["morning_brief"]["chain"] == ["local", "cloud", "template"]
    assert (
        data["tasks"]["morning_brief"]["prompt"] == "prompts/morning_brief.md"
    )  # the rest of that line is untouched
    assert data["guardrails"]["max_calls_per_day"] == 10 and data["guardrails"]["allow_cloud_for"] == [
        "explain_finding"
    ]
    assert data["guardrails"]["quiet_hours_no_agents"] is False
    for comment in ("# Master switch.", "# Locked guardrails (always on", "match OLLAMA_CONTEXT_LENGTH"):
        assert comment in text  # comments survive
    assert text.count("\n") == SHIPPED.count("\n")  # no line was added or lost


def test_a_provider_is_switched_by_its_own_block(tmp_path):
    on = aiconfig.set_provider(
        SHIPPED.replace('base_url: ""', 'base_url: "https://models.example/v1"'), "cloud", "enabled", True
    )
    data = aiconfig.validate(write(tmp_path, on))
    assert [p["enabled"] for p in data["providers"]] == [False, True]


@pytest.mark.parametrize(
    "call",
    [
        lambda t: aiconfig.set_feature(t, "telepathy", True),
        lambda t: aiconfig.set_provider(t, "ghost", "enabled", True),
        lambda t: aiconfig.set_provider(t, "local", "base_url", "http://x"),
        lambda t: aiconfig.set_provider(t, "local", "model", "bad model; rm -rf"),
        lambda t: aiconfig.set_chain(t, "no_such_task", ["local"]),
        lambda t: aiconfig.set_chain(t, "morning_brief", ["local; evil"]),
        lambda t: aiconfig.set_guardrail(t, "not_a_guardrail", 1),
    ],
)
def test_an_edit_that_names_something_unknown_is_refused(call):
    with pytest.raises(ConfigError):
        call(SHIPPED)


# ---------------------------------------------------------------- prompts share the config editor


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    user, defaults = tmp_path / "cfg" / "brief", tmp_path / "defaults" / "brief"
    (defaults / "prompts").mkdir(parents=True)
    user.mkdir(parents=True)
    shutil.copy(BRIEF / "prompts" / "morning_brief.md", defaults / "prompts" / "morning_brief.md")
    monkeypatch.setenv("BRIEF_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("BRIEF_DEFAULTS_DIR", str(tmp_path / "defaults"))
    (tmp_path / "state").mkdir()
    store = Store(tmp_path / "state" / "baseline.db")
    yield tmp_path, store
    store.close()


def test_a_prompt_is_edited_versioned_and_rolled_back_like_a_config_file(cfg):
    tmp, store = cfg
    name = "prompts/morning_brief.md"
    text, customised = configedit.current(name)
    assert not customised and "\n---\n" in text
    new = text.replace("calm, news-style brief", "calm, news-style brief in short sentences")
    message = configedit.save(store, name, new, "tester", configedit.digest(text), tmp / "state")
    assert "Saved" in message and (tmp / "cfg" / "brief" / name).read_text().count("short sentences") == 1
    (version,) = configedit.versions(tmp / "state", name)
    assert version.user == "tester"
    configedit.rollback(
        store, name, version.stamp, "tester", configedit.digest(configedit.current(name)[0]), tmp / "state"
    )
    assert configedit.current(name)[0] == text
    configedit.reset(store, name, "tester", tmp / "state")
    assert not (tmp / "cfg" / "brief" / name).exists()
    with store.read() as db:
        actions = [r["action"] for r in db.execute("SELECT action FROM audit_log ORDER BY rowid")]
    assert actions == ["config.save", "config.rollback", "config.reset"]


@pytest.mark.parametrize(
    "text, message",
    [
        ("no separator here\n", "line with only ---"),
        ("notes\n---\n   \n", "line with only ---"),
        ("notes\n---\n" + "x" * 30000, "will not fit"),
        ("notes\n---\nbad\x00byte\n", "NUL"),
    ],
)
def test_a_bad_prompt_is_refused(cfg, text, message):
    tmp, store = cfg
    name = "prompts/morning_brief.md"
    base = configedit.digest(configedit.current(name)[0])
    with pytest.raises(configedit.EditError, match=message):
        configedit.save(store, name, text, "tester", base, tmp / "state")


@pytest.mark.parametrize(
    "name", ["prompts/../ai.yaml", "prompts/other.md", "../prompts/morning_brief.md", "prompts/agent_x.md"]
)
def test_only_the_known_prompt_files_can_be_edited(cfg, name):
    with pytest.raises(configedit.EditError):
        configedit.checked_name(name)
