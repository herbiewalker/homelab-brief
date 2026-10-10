"""Task 27: the shared tool registry, the agent loop and its limits, citations, privacy, gates, and the queue."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)
from test_cli_tools import box  # noqa: F401  (the fixture: a state dir, config dir, and store)
from test_interfaces import SYSTEMS_YAML

from brief import agentrun, agenttools, ai, cli
from brief.config import ConfigError
from brief.store import iso
from brief.systems import Systems

BRIEF = Path(__file__).resolve().parents[1]
DAY = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)  # outside the default quiet hours (22:00 to 06:30)
NIGHT = datetime(2026, 10, 9, 23, 0, tzinfo=UTC)
AGENTS = """schema: 1
defaults: {max_steps: 4, max_tool_calls: 5, max_tokens_per_run: 5000, timeout_s: 60, max_runs_per_day: 3, chain: [local]}
agents:
  investigator:
    enabled: true
    tools: [get_status, list_findings, get_finding, get_history, search_logs, get_inventory, list_systems]
    prompt: prompts/agent_investigator.md
  off_one:
    enabled: false
    tools: [get_status]
    prompt: prompts/agent_investigator.md
"""
PROVIDER = (
    "  - {id: local, type: openai_compatible, base_url: 'http://model.invalid/v1', model: m, enabled: true}\n"
)
CLOUD = "  - {id: far, type: openai_compatible, base_url: 'https://cloud.invalid/v1', model: m, enabled: true, locality: cloud}\n"


def setup(
    env,
    providers=PROVIDER,
    allow_cloud="[]",
    features="agents: true",
    enabled="true",
    agents=AGENTS,
    chain=None,
):
    cfg = env.settings.state_dir.parent / "cfg" / "brief"
    (cfg / "prompts").mkdir(exist_ok=True)
    (cfg / "prompts" / "agent_investigator.md").write_text(
        (BRIEF / "prompts" / "agent_investigator.md").read_text()
    )
    (cfg / "systems.yaml").write_text(SYSTEMS_YAML)
    (cfg / "priorities.yaml").write_bytes((BRIEF / "priorities.yaml").read_bytes())
    (cfg / "ai.yaml").write_text(
        f"schema: 1\nenabled: {enabled}\nfeatures: {{{features}}}\nproviders:\n{providers}"
        f"guardrails: {{allow_cloud_for: {allow_cloud}, run_agents_only_when_gpu_idle: true, quiet_hours_no_agents: true}}\n"
    )
    (cfg / "agents.yaml").write_text(
        agents if chain is None else agents.replace("chain: [local]", f"chain: {chain}")
    )


class Model:
    """A scripted model server: replies in order, and keeps every request body it was sent."""

    def __init__(self, *replies):
        self.replies, self.bodies = list(replies), []

    def __call__(self, url, headers, body, timeout):
        self.bodies.append(body)
        reply = self.replies.pop(0) if self.replies else final("(nothing more scripted) [ref:r1]")
        if isinstance(reply, Exception):
            raise reply
        return reply


def calls(*pairs, content=None):
    tool_calls = [
        {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
        for i, (n, a) in enumerate(pairs)
    ]
    return {"choices": [{"message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}}  # fmt: skip


def final(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30}}  # fmt: skip


STATUS = {
    "run_ok": True,
    "headline": "all quiet",
    "stories": [{"id": "s1", "kind": "deviation", "title": "gateway busy"}],
    "systems": [],
}


def status():
    return STATUS, Systems(), []


def go(env, model, subject="s1", name="investigator", now=DAY, background=False, **kw):
    specs = agentrun.load()
    return agentrun.run(env.store, specs[name], subject, "on_demand", ai.load_settings(), env.settings, status,
                        now, background, model, **kw)  # fmt: skip


def rows(env, table):
    with env.store.read() as db:
        return [dict(r) for r in db.execute(f"SELECT * FROM {table}")]  # noqa: S608 (a fixed name)


# ---------------------------------------------------------------- the shared registry


def test_the_mcp_server_and_the_agents_use_one_registry():
    from brief import mcpserver

    assert mcpserver.TOOLS is agenttools.TOOLS and mcpserver.call_tool is agenttools.call_tool
    shipped = agentrun.parse(__import__("yaml").safe_load((BRIEF / "agents.yaml").read_text()))
    assert set(shipped) == {"investigator", "triage", "tuner", "ask_the_lab"}
    assert all(t in agenttools.TOOLS for s in shipped.values() for t in s.tools)  # every shipped tool exists
    assert not any(s.enabled for s in shipped.values())  # and every shipped agent is off


@pytest.mark.parametrize(
    "edit,why",
    [
        ("tools: [get_status, delete_all]", "no such tool"),
        ("max_steps: 0", "max_steps"),
        ("max_steps: 99", "max_steps"),
        ("max_runs_per_day: true", "max_runs_per_day"),
        ("require_citations: false", "locked on"),
        ("tools: []", "list of tool names"),
        ("chain: local", "chain"),
    ],
)
def test_a_bad_agents_file_is_refused(edit, why):
    data = __import__("yaml").safe_load(AGENTS)
    key, _, value = edit.partition(": ")
    data["agents"]["investigator"][key] = __import__("yaml").safe_load(value)
    with pytest.raises(ConfigError, match=why):
        agentrun.parse(data)


# ---------------------------------------------------------------- switches


def test_nothing_runs_unless_ai_and_the_feature_and_the_agent_are_on(lab):  # noqa: F811
    for kwargs in ({"enabled": "false"}, {"features": "agents: false"}):
        setup(lab, **kwargs)
        model = Model()
        r = go(lab, model)
        assert r.outcome == "off" and model.bodies == []
    setup(lab)
    model = Model()
    assert go(lab, model, name="off_one").outcome == "off" and model.bodies == []
    assert {r["outcome"] for r in rows(lab, "agent_run")} == {"off"}
    assert rows(lab, "ai_call") == []  # no model call anywhere


# ---------------------------------------------------------------- the loop


def test_a_run_calls_tools_then_keeps_only_cited_statements(lab):  # noqa: F811
    setup(lab)
    model = Model(
        calls(("get_status", {}), ("list_systems", {})),
        final(
            "## Summary\nThe gateway was busy overnight [ref:r1]. It will fix itself.\n\n"
            "## Evidence\n- Three systems are tracked [ref:r2].\n- The disk is dying [ref:r9].\n\n## Guesses\n- Probably nothing."
        ),
    )
    r = go(lab, model)
    assert r.outcome == "ok" and (r.kept, r.dropped) == (2, 3) and (r.steps, r.tool_calls) == (2, 2)
    assert (
        r.text
        == "## Summary\nThe gateway was busy overnight [ref:r1].\n## Evidence\n- Three systems are tracked [ref:r2]."
    )
    assert (
        "dying" not in r.text and "Probably" not in r.text and "Guesses" not in r.text
    )  # no heading without content
    second = model.bodies[1][
        "messages"
    ]  # what the model saw the second time: its own calls and tagged results
    tool_msgs = [m for m in second if m["role"] == "tool"]
    assert [m["content"][:9] for m in tool_msgs] == ["[ref:r1] ", "[ref:r2] "] and second[-3][
        "role"
    ] == "assistant"
    assert "tools" in model.bodies[0] and model.bodies[0]["tool_choice"] == "auto"
    run = rows(lab, "agent_run")[0]
    assert (
        run["output"] == r.text
        and run["tokens_in"] == 200
        and run["provider"] == "local"
        and run["subject"] == "s1"
    )
    steps = rows(lab, "agent_step")
    assert [(s["result"], s["tool"], s["outcome"]) for s in steps] == [
        ("r1", "get_status", "ok"),
        ("r2", "list_systems", "ok"),
    ]
    assert {c["task"] for c in rows(lab, "ai_call")} == {"agent:investigator"}
    audits = [a for a in rows(lab, "audit_log") if a["action"] == "agent.call"]
    assert [a["user"] for a in audits] == ["agent:investigator"] * 2 and "s1" not in json.dumps(audits)


def test_a_reply_with_no_citation_at_all_is_no_evidence_not_a_note(lab):  # noqa: F811
    setup(lab)
    r = go(lab, Model(final("Everything is fine, trust me.")))
    assert r.outcome == "no_evidence" and r.text == "" and r.dropped == 1


def test_citing_a_tool_that_failed_or_was_refused_does_not_count(lab):  # noqa: F811
    setup(lab)
    model = Model(
        calls(("get_finding", {"finding_id": "no-such"}), ("get_status", {}), ("delete_everything", {})),
        final("It is broken [ref:r1]. The run was ok [ref:r2]. Something else [ref:r3]."),
    )
    r = go(lab, model)
    assert r.text == "The run was ok [ref:r2]." and (r.kept, r.dropped) == (1, 2)
    assert [s["outcome"] for s in rows(lab, "agent_step")] == ["error", "ok", "unknown tool"]


def test_a_tool_outside_the_agents_list_is_refused_even_if_the_registry_has_it(lab):  # noqa: F811
    setup(lab)
    model = Model(calls(("get_map", {}), ("run_replay", {"days": 1})), final("x [ref:r9]"))
    r = go(lab, model)
    assert [s["outcome"] for s in rows(lab, "agent_step")] == [
        "unknown tool",
        "unknown tool",
    ] and r.outcome == "no_evidence"
    offered = {t["function"]["name"] for t in model.bodies[0]["tools"]}
    assert "get_map" not in offered and "run_replay" not in offered and "get_status" in offered


def test_the_loop_stops_at_its_step_and_tool_call_limits(lab):  # noqa: F811
    setup(lab)
    forever = [calls(("get_status", {}), ("list_systems", {})) for _ in range(20)]
    model = Model(*forever)
    r = go(lab, model)
    assert (
        r.steps <= 4 and r.tool_calls <= 6
    )  # max_steps 4; max_tool_calls 5, the last batch may overshoot by its size
    assert "tools" not in model.bodies[-1]  # the last turn offers no tools: the model must write its note
    assert "Stop calling tools" in model.bodies[-1]["messages"][-1]["content"]
    assert r.outcome in ("limit", "no_evidence") and len(model.bodies) <= 4
    over = [s for s in rows(lab, "agent_step") if s["outcome"] == "over limit"]
    assert all(s["result"] for s in rows(lab, "agent_step")) and len(rows(lab, "agent_step")) == r.tool_calls
    assert len(over) == max(0, r.tool_calls - 5)


def test_the_time_and_token_limits_stop_a_run(lab):  # noqa: F811
    setup(lab)
    ticks = iter(range(0, 10_000, 100))  # every clock read is 100 s later: past the 60 s limit at once
    r = go(lab, Model(calls(("get_status", {}))), clock=lambda: next(ticks))
    assert r.outcome == "limit" and "time" in r.detail
    agents = AGENTS.replace("max_tokens_per_run: 5000", "max_tokens_per_run: 500")
    setup(lab, agents=agents)
    big = {**calls(("get_status", {})), "usage": {"prompt_tokens": 400, "completion_tokens": 200}}
    r = go(lab, Model(big, calls(("get_status", {})), final("a [ref:r1]")))
    assert r.outcome == "limit" and "token" in r.detail and r.steps == 1


def test_a_daily_run_cap_and_the_daily_call_budget(lab):  # noqa: F811
    setup(lab)
    for _ in range(3):
        assert go(lab, Model(final("ok [ref:r1]"))).outcome == "no_evidence"  # (no tool ran: nothing to cite)
    model = Model()
    capped = go(lab, model)
    assert capped.outcome == "skipped" and "cap" in capped.detail and model.bodies == []


def test_a_provider_that_fails_moves_on_and_a_dead_chain_fails_honestly(lab):  # noqa: F811
    setup(lab, providers=PROVIDER + "  - {id: second, type: openai_compatible, base_url: 'http://two.invalid/v1', model: m, enabled: true}\n",
          chain="[local, second]")  # fmt: skip
    model = Model(ai.ProviderError("timeout", "no answer"), calls(("get_status", {})), final("fine [ref:r1]"))
    r = go(lab, model)
    assert r.outcome == "ok" and r.provider == "second"
    assert [c["outcome"] for c in rows(lab, "ai_call")] == ["timeout", "ok", "ok"]
    r = go(lab, Model(ai.ProviderError("error", "HTTP 500"), ai.ProviderError("error", "HTTP 500")))
    assert r.outcome == "failed" and "HTTP 500" in r.detail


def test_an_anthropic_provider_is_skipped_because_agents_need_openai_style_tool_calling(lab):  # noqa: F811
    setup(lab, providers="  - {id: local, type: anthropic, model: m, enabled: true}\n")
    model = Model()
    r = go(lab, model)
    assert r.outcome == "failed" and model.bodies == []
    assert rows(lab, "ai_call")[0]["outcome"] == "skipped"


def test_a_missing_prompt_fails_the_run_not_the_server(lab):  # noqa: F811
    setup(lab)
    (lab.settings.state_dir.parent / "cfg" / "brief" / "prompts" / "agent_investigator.md").unlink()
    (lab.settings.state_dir.parent / "defaults" / "brief" / "prompts").mkdir(parents=True, exist_ok=True)
    assert go(lab, Model()).outcome == "failed"


# ---------------------------------------------------------------- privacy


def test_a_cloud_provider_is_unused_unless_the_agent_is_allowed_it(lab):  # noqa: F811
    setup(lab, providers=CLOUD, chain="[far]")
    model = Model()
    assert go(lab, model).outcome == "failed" and model.bodies == []
    assert rows(lab, "ai_call")[0]["detail"] == "cloud not allowed for this agent"


def test_a_cloud_agent_is_offered_no_device_log_or_dns_tool_and_sees_no_private_result(lab):  # noqa: F811
    setup(lab, providers=CLOUD, chain="[far]", allow_cloud="[investigator]")
    model = Model(
        calls(("get_status", {}), ("search_logs", {"text": "x"})), final("ok [ref:r1]. also [ref:r2].")
    )
    r = go(lab, model)
    offered = {t["function"]["name"] for t in model.bodies[0]["tools"]}
    assert "search_logs" not in offered and "get_status" in offered
    assert offered <= {n for n, t in agenttools.TOOLS.items() if t.scope not in ("logs", "devices", "dns")}
    status_doc = {**STATUS, "headline": "198.51.100.3 has been silent"}  # a sender address in a tool result
    model = Model(calls(("get_status", {})), final("ok [ref:r1]"))
    r = agentrun.run(lab.store, agentrun.load()["investigator"], "s1", "on_demand", ai.load_settings(), lab.settings,
                     lambda: (status_doc, Systems(), []), DAY, False, model)  # fmt: skip
    told = [m["content"] for m in model.bodies[1]["messages"] if m["role"] == "tool"][0]
    assert "198.51.100.3" not in told and "Withheld from a cloud client" in told
    assert r.outcome == "no_evidence"  # a withheld result is not evidence


def test_a_subject_that_names_an_address_never_reaches_a_cloud_model(lab):  # noqa: F811
    setup(lab, providers=CLOUD, chain="[far]", allow_cloud="[investigator]")
    model = Model()
    r = go(lab, model, subject="why is 198.51.100.7 noisy?")
    assert r.outcome == "failed" and model.bodies == []
    assert rows(lab, "ai_call")[0]["outcome"] == "privacy"


def test_a_lan_agent_may_read_logs_when_its_tool_list_has_them(lab):  # noqa: F811
    setup(lab)
    day = DAY.date().isoformat()
    folder = lab.settings.archive_dir / day
    folder.mkdir(parents=True)
    (folder / "198.51.100.9.log").write_text("a needle line\n")
    model = Model(calls(("search_logs", {"text": "needle", "days": 1})), final("found it [ref:r1]"))
    # the archive search runs for 'today' (real clock); the point here is the tool is offered and runs on the LAN
    r = go(lab, model)
    assert "search_logs" in {t["function"]["name"] for t in model.bodies[0]["tools"]} and r.outcome == "ok"


# ---------------------------------------------------------------- gates and the queue


def test_background_runs_wait_out_quiet_hours_but_a_person_does_not(lab):  # noqa: F811
    setup(lab)
    model = Model()
    waiting = go(lab, model, now=NIGHT, background=True)
    assert waiting.outcome == "wait" and model.bodies == [] and rows(lab, "agent_run") == []
    assert (
        go(lab, Model(calls(("get_status", {})), final("ok [ref:r1]")), now=NIGHT, background=False).outcome
        == "ok"
    )


def test_background_runs_wait_while_the_model_server_is_busy(lab):  # noqa: F811
    setup(lab)
    with lab.store.write() as tx:
        tx.add_ai_call(DAY - timedelta(seconds=30), "morning_brief", "local", "m", "lan", "ok")
    model = Model()
    assert go(lab, model, background=True).outcome == "wait" and model.bodies == []
    assert go(lab, Model(calls(("get_status", {})), final("ok [ref:r1]")), background=False).outcome == "ok"
    with lab.store.write() as tx:
        tx.db.execute("DELETE FROM ai_call")
        tx.add_ai_call(DAY - timedelta(minutes=10), "morning_brief", "local", "m", "lan", "ok")
    assert go(lab, Model(calls(("get_status", {})), final("ok [ref:r1]")), background=True).outcome == "ok"


def test_the_guardrail_switches_turn_the_waiting_rules_off(lab):  # noqa: F811
    setup(lab)
    path = lab.settings.state_dir.parent / "cfg" / "brief" / "ai.yaml"
    path.write_text(path.read_text().replace("run_agents_only_when_gpu_idle: true", "run_agents_only_when_gpu_idle: false")
                    .replace("quiet_hours_no_agents: true", "quiet_hours_no_agents: false"))  # fmt: skip
    with lab.store.write() as tx:
        tx.add_ai_call(NIGHT - timedelta(seconds=5), "morning_brief", "local", "m", "lan", "ok")
    assert (
        go(lab, Model(calls(("get_status", {})), final("ok [ref:r1]")), now=NIGHT, background=True).outcome
        == "ok"
    )


def test_the_queue_dedupes_runs_one_job_and_keeps_waiting_ones(lab, monkeypatch):  # noqa: F811
    setup(lab)
    monkeypatch.setattr(ai, "_post", Model(calls(("get_status", {})), final("ok [ref:r1]")))
    assert agentrun.enqueue(lab.store, "investigator", "s1", "finding", "x") == 1
    assert agentrun.enqueue(lab.store, "investigator", "s1", "finding") is None  # already waiting
    assert agentrun.enqueue(lab.store, "off_one", "s1", "finding") == 2
    agentrun.work_one(lab.store, lab.settings, status, NIGHT)
    jobs = {j["job_id"]: j["status"] for j in rows(lab, "agent_job")}
    assert jobs[1] == "queued"  # quiet hours: it waits, nothing consumed
    assert jobs[2] == "skipped"  # the off agent's job is dropped
    assert agentrun.work_one(lab.store, lab.settings, status, DAY) == "investigator: ok"
    assert {j["job_id"]: j["status"] for j in rows(lab, "agent_job")}[1] == "done"
    assert agentrun.work_one(lab.store, lab.settings, status, DAY) is None
    assert agentrun.queue(lab.store) == []


def test_the_queue_is_ignored_while_agents_are_off(lab):  # noqa: F811
    setup(lab, features="agents: false")
    agentrun.enqueue(lab.store, "investigator", "s1", "finding")
    assert agentrun.work_one(lab.store, lab.settings, status, DAY) is None
    assert rows(lab, "agent_job")[0]["status"] == "queued" and rows(lab, "agent_run") == []


def test_the_queue_is_capped(lab):  # noqa: F811
    for i in range(agentrun.MAX_QUEUE):
        assert agentrun.enqueue(lab.store, "investigator", f"s{i}", "finding")
    assert agentrun.enqueue(lab.store, "investigator", "one-more", "finding") is None


# ---------------------------------------------------------------- citation checking on its own


def test_check_citations_handles_bullets_numbers_and_several_sentences_per_line():
    evidence = {"r1", "r2"}
    text = (
        "Overview:\n"
        "1. First thing happened [ref:r1]. Second thing is a guess.\n"
        "2) Both [ref:r1] and [ref:r2] agree.\n"
        "* Cites a result that does not exist [ref:r7].\n"
        "Unreferenced closing remark.\n"
        "<think>hidden [ref:r1]</think>"
    )
    kept, n, dropped = agentrun.check_citations(text, evidence)
    assert kept.splitlines() == [
        "Overview:",
        "1. First thing happened [ref:r1].",
        "2) Both [ref:r1] and [ref:r2] agree.",
    ]
    assert (n, dropped) == (2, 3)
    assert agentrun.check_citations("", evidence) == ("", 0, 0)


# ---------------------------------------------------------------- the new tools


def test_the_new_tools_read_what_they_say_and_keep_the_privacy_rules(lab):  # noqa: F811
    from brief.mcptokens import SCOPES, Client

    now = datetime.now(UTC)
    with lab.store.write() as tx:
        sid = tx.signature_for(0, 1, "app", "a generalised template", now)
        tx.add_hourly([(f"{now:%Y-%m-%d}", now.hour, "198.51.100.1", sid, "info", 25)])
        tx.db.execute(
            "INSERT INTO sample (sig_id, ts, host, msg) VALUES (?, ?, ?, ?)",
            (sid, iso(now), "gw", "a raw line"),
        )
        tx.db.execute("INSERT INTO inventory (item_id, kind, name, model, current_version, available_version, checked_at) "
                      "VALUES ('u:1', 'switch', 'Switch', 'M', '1', '2', ?)", (iso(now),))  # fmt: skip

    def ctx(*scopes):
        client = Client("t", "t", frozenset(scopes))
        return agenttools.Context(lab.store, lab.settings, client, status, source="agent")

    def run(c, tool, args=None):
        return json.loads(agenttools.call_tool(c, tool, args or {})["content"][0]["text"])

    plain = run(ctx("history"), "get_signature", {"id": sid})
    assert plain["senders_7d"] == 1 and "template" not in plain and "recent_lines" not in plain
    full = run(ctx("history", "logs"), "get_signature", {"id": sid})
    assert full["template"] == "a generalised template" and full["recent_lines"][0]["msg"] == "a raw line"
    assert (
        run(ctx("systems"), "get_inventory", {"pending_only": True})["items"][0]["available_version"] == "2"
    )
    denied = agenttools.call_tool(ctx("status"), "get_unraid_notifications", {})
    assert denied["isError"] and "scope" in denied["content"][0]["text"]
    assert run(ctx(*SCOPES), "get_unraid_notifications")["notices"] == []
    assert (
        "daily" not in run(ctx("history"), "run_replay", {"days": 1})
        and agenttools.call_tool(ctx("history"), "run_replay", {"days": 30})["isError"]
    )


# ---------------------------------------------------------------- the command line


def test_cli_lists_queues_runs_and_shows(box, capsys, monkeypatch):  # noqa: F811
    _, user, state, store = box
    (user / "prompts").mkdir()
    (user / "prompts" / "agent_investigator.md").write_text(
        (BRIEF / "prompts" / "agent_investigator.md").read_text()
    )
    (user / "agents.yaml").write_text(AGENTS)
    (user / "ai.yaml").write_text(
        f"schema: 1\nenabled: true\nfeatures: {{agents: true}}\nproviders:\n{PROVIDER}"
    )
    assert cli.main(["agent", "list"]) == 0
    out = capsys.readouterr().out
    assert "Agents are ON" in out and "investigator" in out
    assert (
        cli.main(["agent", "run", "investigator", "s1"]) == 0 and "Queued as job 1" in capsys.readouterr().out
    )
    assert cli.main(["agent", "run", "investigator", "s1"]) == 1  # already queued
    assert cli.main(["agent", "run", "nobody", "x"]) == 1
    capsys.readouterr()
    assert cli.main(["agent", "queue"]) == 0 and "investigator" in capsys.readouterr().out
    monkeypatch.setattr(ai, "_post", Model(calls(("get_status", {})), final("The run is fine [ref:r1].")))
    assert cli.main(["agent", "run", "investigator", "s1", "--now"]) == 0
    assert "The run is fine [ref:r1]." in capsys.readouterr().out
    assert cli.main(["agent", "runs"]) == 0 and "ok" in capsys.readouterr().out
    assert cli.main(["agent", "show", "1"]) == 0 and "get_status" in capsys.readouterr().out
    assert cli.main(["agent", "show", "99"]) == 1


def test_the_ai_page_lists_agents_and_their_runs(lab):  # noqa: F811
    setup(lab)
    go(
        lab, Model(calls(("get_status", {})), final("ok [ref:r1]")), now=datetime.now(UTC)
    )  # the page counts the real last 24 hours
    page = lab.client.get("/admin/ai").text
    assert "<h2>Agents</h2>" in page and "investigator" in page and "off_one" in page and "1 of 3" in page


def test_the_daily_call_budget_stops_a_run_before_it_calls_the_model(lab):  # noqa: F811
    setup(lab)
    with lab.store.write() as tx:
        for _ in range(50):
            tx.add_ai_call(DAY - timedelta(hours=1), "morning_brief", "other", "m", "lan", "ok")
    model = Model()
    r = go(lab, model)
    assert r.outcome == "limit" and "budget" in r.detail and model.bodies == []
