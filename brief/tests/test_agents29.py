"""Task 29: the four agents, their triggers and structured outputs, notes on findings, and the locked safety rules."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)
from test_agents import DAY, Model, calls, final, go, rows, setup, status
from test_cli_tools import box  # noqa: F401  (the fixture: a state dir, config dir, and store)

from brief import (
    agentguards,
    agentnotes,
    agentrun,
    agenttools,
    agenttriggers,
    ai,
    cli,
    configedit,
    proposals,
    yamlscalar,
)
from brief.detectors import Finding
from brief.mcptokens import Client
from brief.store import iso

BRIEF = Path(__file__).resolve().parents[1]
FOUR = """schema: 1
defaults: {max_steps: 6, max_tool_calls: 10, max_tokens_per_run: 20000, timeout_s: 60, max_runs_per_day: 20, chain: [local]}
agents:
  investigator:
    enabled: true
    trigger: {on_finding_rank_at_least: 60, on_demand: true}
    tools: [get_status, list_findings, get_finding, get_history, get_signature, list_systems]
    output: investigation_note
    prompt: prompts/agent_investigator.md
  triage:
    enabled: true
    trigger: {on_new_signatures: true, after_learning_period: true}
    tools: [get_signature, get_history, list_systems]
    output: verdicts
    prompt: prompts/agent_triage.md
  tuner:
    enabled: true
    trigger: {schedule: weekly}
    tools: [run_replay, list_findings, get_status]
    output: proposals
    prompt: prompts/agent_tuner.md
  ask_the_lab:
    enabled: true
    trigger: {on_demand: true}
    tools: [get_status, list_findings, list_systems]
    output: chat_answer
    prompt: prompts/agent_investigator.md
"""


def four(env, **kw):
    setup(env, agents=FOUR, **kw)
    cfg = env.settings.state_dir.parent / "cfg" / "brief"
    shipped = env.settings.state_dir.parent / "defaults" / "brief"
    for name in ("ignore.yaml", "playbook.yaml"):  # as shipped: the lab fixture has no defaults of its own
        (shipped / name).write_bytes((BRIEF / name).read_bytes())
    for name in ("agent_triage", "agent_tuner"):
        (cfg / "prompts" / f"{name}.md").write_text((BRIEF / "prompts" / f"{name}.md").read_text())


def cfg(env):
    return env.settings.state_dir.parent / "cfg" / "brief"


def sigs(env, *lines):
    """Signatures with a sample line each (app, message); returns their ids."""
    now = datetime.now(UTC)
    ids = []
    with env.store.write() as tx:
        for i, (app, msg) in enumerate(lines, 1):
            sid = tx.signature_for(0, i, app, msg, now)
            tx.add_hourly([(f"{now:%Y-%m-%d}", now.hour, "198.51.100.1", sid, "info", 10 * i)])
            tx.db.execute(
                "INSERT INTO sample (sig_id, ts, host, msg) VALUES (?, ?, ?, ?)", (sid, iso(now), "gw", msg)
            )
            ids.append(sid)
    return ids


def verdict_reply(items):
    return final(json.dumps(items))


# ---------------------------------------------------------------- yamlscalar


YAML = """# top comment
schema: 1
default: { rank: 10, route: digest_only }
lifecycle:
  renotify_hours: 24          # repeat at most this often
  send_resolved: true
  snooze_options_h: [24, 168]
kinds:
  absence:            { rank: 80, route: push_now }
  deviation:          { rank: 50, route: digest_only }
thresholds:
  deviation: { window_days: 14, min_ratio: 3, nested: { depth: 2 } }
"""


@pytest.mark.parametrize(
    "path,value,expect",
    [
        (["lifecycle", "renotify_hours"], 48, "  renotify_hours: 48          # repeat at most this often"),
        (["lifecycle", "send_resolved"], False, "  send_resolved: false"),
        (["kinds", "deviation", "rank"], 55, "  deviation:          { rank: 55, route: digest_only }"),
        (["kinds", "deviation", "route"], "daily_headline", "{ rank: 50, route: daily_headline }"),
        (["thresholds", "deviation", "min_ratio"], 4.5, "min_ratio: 4.5,"),
        (["thresholds", "deviation", "nested", "depth"], 3, "nested: { depth: 3 }"),
        (["default", "rank"], 20, "default: { rank: 20, route: digest_only }"),
    ],
)
def test_set_path_changes_one_value_and_nothing_else(path, value, expect):
    new = yamlscalar.set_path(YAML, path, value)
    assert expect in new
    changed = [a for a, b in zip(YAML.split("\n"), new.split("\n"), strict=True) if a != b]
    assert len(changed) == 1 and new.startswith("# top comment")


@pytest.mark.parametrize(
    "path,value",
    [
        (["lifecycle", "nope"], 1),
        (["nope", "x"], 1),
        (["lifecycle", "snooze_options_h"], 5),  # a list is not a single value
        (["kinds", "absence", "rank"], "Has Spaces"),
        (["kinds", "absence", "rank"], None),
        (["kinds", "absence", "rank"], [1]),
        (["kinds", "deviation", "nope"], 1),
        (["thresholds", "deviation", "nested", "nope"], 1),
    ],
)
def test_set_path_refuses_what_it_cannot_change_exactly(path, value):
    with pytest.raises(yamlscalar.EditError):
        yamlscalar.set_path(YAML, path, value)


def test_a_key_in_another_block_is_not_confused_for_the_one_asked_for():
    text = "a:\n  x: 1\nb:\n  x: 2\n"
    assert yamlscalar.set_path(text, ["b", "x"], 9) == "a:\n  x: 1\nb:\n  x: 9\n"


def test_the_shipped_files_can_be_edited_at_the_paths_the_tuner_is_told_about():
    for file, path in (("priorities.yaml", ["lifecycle", "renotify_hours"]), ("priorities.yaml", ["kinds", "deviation", "rank"]),
                       ("playbook.yaml", ["thresholds", "deviation", "min_ratio"])):  # fmt: skip
        old = (BRIEF / file).read_text()
        assert configedit.validate_text(file, yamlscalar.set_path(old, path, 5)) is None


# ---------------------------------------------------------------- configuration


def test_the_shipped_agents_use_the_structured_outputs_and_no_proposal_tool():
    shipped = agentrun.parse(__import__("yaml").safe_load((BRIEF / "agents.yaml").read_text()))
    assert {n: s.output for n, s in shipped.items()} == {
        "investigator": "investigation_note", "triage": "verdicts", "tuner": "proposals", "ask_the_lab": "chat_answer",
    }  # fmt: skip
    assert not any("propose_change" in s.tools for s in shipped.values())
    with pytest.raises(Exception, match="output must be one of"):
        agentrun.parse({"agents": {"x": {"tools": ["get_status"], "output": "banana"}}})


def test_the_two_structured_agents_cannot_use_the_proposal_tool(lab):  # noqa: F811
    four(lab)
    for name in agenttools.STRUCTURED_AGENTS:
        ctx = agenttools.Context(lab.store, lab.settings, Client("agent", name, frozenset({"propose"})), status, source="agent",
                                 run_id=1, evidence={"r1": "get_status"})  # fmt: skip
        out = agenttools.call_tool(ctx, "propose_change", {"file": "ignore.yaml", "new_text": "schema: 1\n", "reason": "r",
                                                          "evidence": "x [ref:r1]"})  # fmt: skip
        assert out["isError"] and "structured answer" in out["content"][0]["text"]


# ---------------------------------------------------------------- triage


def test_triage_stores_cited_verdicts_and_makes_checked_proposals(lab):  # noqa: F811
    four(lab)
    a, b, c = sigs(
        lab, ("camera", "link flapped on port 3"), ("app", "cache warmed in 12 ms"), ("app", "something else")
    )
    model = Model(
        calls(("get_signature", {"id": a}), ("get_signature", {"id": b})),
        verdict_reply([
            {"sig_id": a, "verdict": "noise", "reason": "A port that flaps when a device sleeps.", "citations": ["r1"],
             "proposal": {"kind": "ignore", "host": "gw", "pattern": "link flapped on port"}},
            {"sig_id": b, "verdict": "concern", "reason": "Cache is being rebuilt every hour.", "citations": ["r2"],
             "proposal": {"kind": "playbook", "for_kind": "novelty", "bucket": "watch", "advice": "Check why the cache restarts."}},
            {"sig_id": c, "verdict": "noise", "reason": "Not one of the asked ones.", "citations": ["r1"]},
            {"sig_id": a, "verdict": "noise", "reason": "A second verdict for the same one.", "citations": ["r1"]},
            {"sig_id": b + 1000, "verdict": "noise", "reason": "Made up id.", "citations": ["r1"]},
        ]),
    )  # fmt: skip
    r = go(lab, model, subject=f"signatures:{a},{b}", name="triage")
    assert r.outcome == "ok" and r.kept == 2 and r.dropped == 3
    assert (
        "user" in [m["role"] for m in model.bodies[0]["messages"]]
        and f"{a}, {b}" in model.bodies[0]["messages"][1]["content"]
    )
    got = {v["sig_id"]: v["verdict"] for v in rows(lab, "agent_verdict")}
    assert got == {a: "noise", b: "concern"}
    made = {p.file: p for p in proposals.listing(lab.store)}
    assert set(made) == {"ignore.yaml", "playbook.yaml"} and all(
        p.source == "agent:triage" for p in made.values()
    )
    assert (
        f"triage_sig_{a}" in made["ignore.yaml"].new_text
        and "link flapped on port" in made["ignore.yaml"].new_text
    )
    assert f"triage_sig_{b}" in made["playbook.yaml"].new_text
    assert (
        not (cfg(lab) / "ignore.yaml").exists() and not (cfg(lab) / "playbook.yaml").exists()
    )  # only proposals
    assert (
        "Triage verdict" in made["ignore.yaml"].evidence
        and "r1 (get_signature)" in made["ignore.yaml"].evidence
    )


def test_a_verdict_without_a_real_citation_or_with_a_bad_value_is_dropped(lab):  # noqa: F811
    four(lab)
    (a,) = sigs(lab, ("app", "hello"))
    model = Model(
        calls(("get_signature", {"id": a})),
        verdict_reply([
            {"sig_id": a, "verdict": "noise", "reason": "no citation", "citations": []},
            {"sig_id": a, "verdict": "noise", "reason": "fake citation", "citations": ["r9"]},
            {"sig_id": a, "verdict": "harmless", "reason": "not a verdict", "citations": ["r1"]},
            {"sig_id": a, "verdict": "noise", "reason": "", "citations": ["r1"]},
            "not an object",
        ]),
    )  # fmt: skip
    r = go(lab, model, subject=f"signatures:{a}", name="triage")
    assert r.outcome == "no_evidence" and r.dropped == 5 and rows(lab, "agent_verdict") == []


def test_a_reply_that_is_not_json_is_no_evidence(lab):  # noqa: F811
    four(lab)
    (a,) = sigs(lab, ("app", "hello"))
    r = go(
        lab,
        Model(calls(("get_signature", {"id": a})), final("I think it is fine [ref:r1].")),
        subject=f"signatures:{a}",
        name="triage",
    )
    assert r.outcome == "no_evidence" and rows(lab, "agent_verdict") == []


def test_a_json_list_wrapped_in_a_fence_and_prose_is_still_read(lab):  # noqa: F811
    four(lab)
    (a,) = sigs(lab, ("app", "hello"))
    body = json.dumps(
        [{"sig_id": a, "verdict": "watch", "reason": "New today.", "citations": ["r1"], "proposal": None}]
    )
    r = go(lab, Model(calls(("get_signature", {"id": a})), final(f"Here you go:\n```json\n{body}\n```\nDone.")),
           subject=f"signatures:{a}", name="triage")  # fmt: skip
    assert r.outcome == "ok" and rows(lab, "agent_verdict")[0]["verdict"] == "watch"


@pytest.mark.parametrize(
    "pattern,why",
    [
        ("disk (error|failure)", "disks, authentication, or security"),
        ("sshd.*Failed password", "disks, authentication, or security"),
        ("(?i)fail2ban", "disks, authentication, or security"),
        ("Failed password", "disks, authentication, or security"),
        (
            "sector 5",
            "also ignore recent lines",
        ),  # no deny word in the rule, yet it matches a risky recent line
        (".*", "also ignore recent lines"),
        (r"line \d+", "too broad"),  # matches most of the harmless lines and none of the risky ones
    ],
)
def test_triage_can_never_ignore_disk_authentication_or_security_events(lab, pattern, why):  # noqa: F811
    four(lab)
    base = [("sshd", "Failed password for root from 198.51.100.7"), ("kernel", "I/O error on sda sector 5")]
    sigs(lab, *base, *[("app", f"line {i} a") for i in range(30)])
    old, _ = configedit.current("ignore.yaml")
    new = old.rstrip("\n") + f"\n  - name: triage_x\n    match: {json.dumps(pattern)}\n    note: x\n"
    with pytest.raises(proposals.ProposalError, match=why):
        proposals.create(lab.store, "agent:triage", "ignore.yaml", new, "r", "e")
    assert proposals.listing(lab.store) == []


def test_triage_may_only_add_rules_and_only_to_two_files(lab):  # noqa: F811
    four(lab)
    sigs(lab, ("app", "quiet line"))
    old, _ = configedit.current("ignore.yaml")
    ok = old.rstrip("\n") + '\n  - name: triage_ok\n    match: "quiet line"\n    note: fine\n'
    assert proposals.create(lab.store, "agent:triage", "ignore.yaml", ok, "r", "e")
    with pytest.raises(proposals.ProposalError, match="Triage may only"):
        proposals.create(
            lab.store,
            "agent:triage",
            "priorities.yaml",
            configedit.current("priorities.yaml")[0] + "# x\n",
            "r",
            "e",
        )
    play, _ = configedit.current("playbook.yaml")
    edited = yamlscalar.set_path(play, ["thresholds", "deviation", "min_ratio"], 9)
    with pytest.raises(proposals.ProposalError, match="may only add playbook rules"):
        proposals.create(lab.store, "agent:triage", "playbook.yaml", edited, "r", "e")


def test_a_proposal_the_triage_agent_asks_for_wrongly_is_reported_not_made(lab):  # noqa: F811
    four(lab)
    a, b = sigs(lab, ("sshd", "Failed password for root"), ("app", "hello"))
    model = Model(
        calls(("get_signature", {"id": a}), ("get_signature", {"id": b})),
        verdict_reply([
            {"sig_id": a, "verdict": "noise", "reason": "Looks routine.", "citations": ["r1"],
             "proposal": {"kind": "ignore", "pattern": "Failed password"}},
            {"sig_id": b, "verdict": "watch", "reason": "New.", "citations": ["r2"],
             "proposal": {"kind": "ignore", "pattern": "hello"}},  # an ignore needs a noise verdict
        ]),
    )  # fmt: skip
    r = go(lab, model, subject=f"signatures:{a},{b}", name="triage")
    assert r.outcome == "ok" and len(rows(lab, "agent_verdict")) == 2 and proposals.listing(lab.store) == []
    assert r.text.count("proposal not made") == 2 and "authentication" in r.text


# ---------------------------------------------------------------- the tuner


def tuner_reply(items):
    return final(json.dumps(items))


def test_the_tuner_proposes_single_values_with_the_products_own_replay(lab):  # noqa: F811
    four(lab)
    items = [
        {"file": "priorities.yaml", "path": "lifecycle.renotify_hours", "value": 48, "reason": "Repeats were ignored.", "citations": ["r1"]},
        {"file": "playbook.yaml", "path": "thresholds.deviation.min_ratio", "value": 4, "reason": "Too chatty.", "citations": ["r1"]},
        {"file": "priorities.yaml", "path": "kinds.absence.rank", "value": 10, "reason": "Never mind.", "citations": ["r1"]},
        {"file": "ai.yaml", "path": "enabled", "value": True, "reason": "x", "citations": ["r1"]},
        {"file": "priorities.yaml", "path": "lifecycle.renotify_hours", "value": 12, "reason": "no citation", "citations": []},
        {"file": "priorities.yaml", "path": "lifecycle.daily_push_budget", "value": 3, "reason": "A fourth would be over the limit.", "citations": ["r1"]},
    ]  # fmt: skip
    model = Model(calls(("run_replay", {"days": 3})), tuner_reply(items))
    r = go(lab, model, subject="weekly review", name="tuner")
    assert r.outcome == "ok" and r.kept == 3
    made = proposals.listing(lab.store, "pending")
    assert {(p.file) for p in made} == {"priorities.yaml", "playbook.yaml"} and len(made) == 3
    first = next(p for p in made if "renotify_hours = 48" in p.evidence)
    assert (
        "Replay of the last 7 days, run by the product" in first.evidence
        and "pushes before" in first.evidence
    )
    assert "r1 (run_replay)" in first.evidence and "renotify_hours: 48" in first.new_text
    assert (cfg(lab) / "priorities.yaml").read_text() == (BRIEF / "priorities.yaml").read_text()  # untouched
    assert (
        "not proposed (priorities.yaml kinds.absence.rank)" in r.text
        and "disk, parity, SMART, storage, or absence" in r.text
    )


def test_the_tuner_proposal_is_refused_if_it_would_hide_an_acknowledged_urgent_or_protected_finding(
    lab, monkeypatch
):  # noqa: F811
    four(lab)
    old, _ = configedit.current("priorities.yaml")
    new = yamlscalar.set_path(old, ["lifecycle", "renotify_hours"], 48)
    lost = agentguards.Replayed("x", "Replay of the last 7 days: ...", hidden=["smart-sdb"])
    monkeypatch.setattr(agentguards, "replay_evidence", lambda *a, **k: lost)
    with pytest.raises(
        proposals.ProposalError, match="hidden an acknowledged, snoozed, urgent, or protected finding"
    ):
        proposals.create(
            lab.store, "agent:tuner", "priorities.yaml", new, "r", "Replay of the last 7 days: ..."
        )


def test_a_tuner_proposal_without_the_products_replay_is_refused(lab):  # noqa: F811
    four(lab)
    old, _ = configedit.current("priorities.yaml")
    new = yamlscalar.set_path(old, ["lifecycle", "renotify_hours"], 48)
    with pytest.raises(proposals.ProposalError, match="must carry the replay"):
        proposals.create(lab.store, "agent:tuner", "priorities.yaml", new, "r", "I checked, trust me")


@pytest.mark.parametrize(
    "file,path,value,why",
    [
        (
            "priorities.yaml",
            ["kinds", "smart_failing", "rank"],
            1,
            "disk, parity, SMART, storage, or absence",
        ),
        (
            "priorities.yaml",
            ["kinds", "storage_low", "route"],
            "digest_only",
            "disk, parity, SMART, storage, or absence",
        ),
        (
            "playbook.yaml",
            ["thresholds", "absence", "gap_multiple"],
            9,
            "disk, parity, SMART, storage, or absence",
        ),
        (
            "playbook.yaml",
            ["thresholds", "parity_slow", "floor_mb_s"],
            1,
            "disk, parity, SMART, storage, or absence",
        ),
        ("priorities.yaml", ["kinds", "deviation", "route"], "suppress", "suppresses a kind"),
    ],
)
def test_the_tuner_never_touches_disk_parity_smart_storage_or_absence_and_never_suppresses(
    lab, file, path, value, why
):  # noqa: F811
    four(lab)
    old, _ = configedit.current(file)
    new = yamlscalar.set_path(old, path, value)
    with pytest.raises(
        proposals.ProposalError, match=why.split(" or ")[0] if "suppress" not in why else "never suppresses"
    ):
        proposals.create(lab.store, "agent:tuner", file, new, "r", "Replay of the last 7 days: x")


def test_protected_findings_are_the_acknowledged_the_snoozed_the_urgent_and_the_protected_kinds(lab):  # noqa: F811
    with lab.store.write() as tx:
        tx.upsert_alert("a1", kind="deviation", state="acked")
        tx.upsert_alert("s1", kind="deviation", state="snoozed")
        tx.upsert_alert("o1", kind="deviation", state="open")
        tx.add_push("u1", "new", "alert", "critical", "t", True, datetime.now(UTC))
        tx.add_push("n1", "new", "alert", "warning", "t", False, datetime.now(UTC))
    kinds = {
        "a1": "deviation",
        "s1": "deviation",
        "o1": "deviation",
        "u1": "deviation",
        "n1": "deviation",
        "d1": "smart_failing",
    }
    assert agentguards._protected_ids(lab.store, kinds) == {"a1", "s1", "u1", "d1"}


def test_replay_evidence_runs_on_copies_and_leaves_the_real_config_alone(lab):  # noqa: F811
    four(lab)
    old, _ = configedit.current("priorities.yaml")
    new = yamlscalar.set_path(old, ["lifecycle", "renotify_hours"], 48)
    result = agentguards.replay_evidence(lab.store, None, "priorities.yaml", old, new, None)
    assert result.text.startswith("Replay of the last 7 days, run by the product") and result.hidden == []
    assert (cfg(lab) / "priorities.yaml").read_text() == (BRIEF / "priorities.yaml").read_text()


# ---------------------------------------------------------------- triggers


STATUS = {
    "run_ok": True,
    "learning": {"active": False},
    "stories": [
        {"id": "f-absence", "kind": "absence", "detail": "hosts/198.51.100.3.md"},
        {"id": "f-deviation", "kind": "deviation", "detail": "hosts/198.51.100.1.md"},
        {"id": "f-new-device", "kind": "new_device", "detail": None},
        {"id": "f-smart", "kind": "smart_failing", "detail": None},
        {"id": "f-storage", "kind": "storage_low", "detail": None},
        {"id": "f-unraid", "kind": "unraid_warning", "detail": None},
    ],
}


def jobs(env):
    return [
        (j["agent"], j["subject"], j["trigger"]) for j in rows(env, "agent_job") if j["status"] == "queued"
    ]


def test_after_the_daily_run_the_high_ranked_stories_are_queued_for_the_investigator(lab):  # noqa: F811
    four(lab)
    queued = agenttriggers.after_daily(lab.store, STATUS, DAY)
    assert len(queued) == agenttriggers.MAX_INVESTIGATIONS
    assert [j for j in jobs(lab) if j[0] == "investigator"] == [
        ("investigator", "f-absence", "finding"),
        ("investigator", "f-new-device", "finding"),
        ("investigator", "f-smart", "finding"),
    ]  # fmt: skip  (deviation ranks 50, below the 60 threshold; the cap leaves the rest for tomorrow)
    assert agenttriggers.after_daily(lab.store, STATUS, DAY) == [
        "investigator:f-storage",
        "investigator:f-unraid",
    ]  # the rest
    assert (
        agenttriggers.after_daily(lab.store, STATUS, DAY) == []
    )  # everything is waiting now: nothing doubled


def test_a_story_investigated_recently_is_not_queued_again(lab):  # noqa: F811
    four(lab)
    go(
        lab,
        Model(calls(("get_status", {})), final("fine [ref:r1].")),
        subject="f-absence",
        name="investigator",
    )
    agenttriggers.after_daily(lab.store, STATUS, DAY)
    assert "f-absence" not in {j[1] for j in jobs(lab)}


def test_triage_is_queued_for_new_signatures_only_after_learning_and_only_once(lab):  # noqa: F811
    four(lab)
    a, b = sigs(lab, ("app", "one"), ("app", "two"))
    now = datetime.now(UTC)
    agenttriggers.after_daily(lab.store, {**STATUS, "stories": [], "learning": {"active": True}}, now)
    assert jobs(lab) == []
    agenttriggers.after_daily(lab.store, {**STATUS, "stories": []}, now)
    assert jobs(lab) == [("triage", f"signatures:{b},{a}", "new_signatures")]  # the busier one first
    with lab.store.write() as tx:
        tx.db.execute(
            "INSERT INTO agent_verdict (sig_id, verdict, reason, run_id, created) VALUES (?, 'noise', 'x', 1, ?)",
            (a, iso(now)),
        )
    assert agenttriggers.new_signatures(lab.store, now) == [b]


def test_a_failed_daily_run_queues_nothing(lab):  # noqa: F811
    four(lab)
    assert agenttriggers.after_daily(lab.store, {**STATUS, "run_ok": False}, DAY) == []


def test_the_weekly_tuner_is_queued_once_a_week_and_nothing_is_queued_while_off(lab):  # noqa: F811
    four(lab)
    assert (
        agenttriggers.after_weekly(lab.store, DAY) == ["tuner"]
        and agenttriggers.after_weekly(lab.store, DAY) == []
    )
    for kwargs in ({"features": "agents: false"}, {"enabled": "false"}):
        four(lab, **kwargs)
        assert (
            agenttriggers.after_daily(lab.store, STATUS, DAY) == []
            and agenttriggers.after_weekly(lab.store, DAY) == []
        )
    four(lab)
    off = FOUR.replace("investigator:\n    enabled: true", "investigator:\n    enabled: false")
    setup(lab, agents=off)
    assert not any(q.startswith("investigator") for q in agenttriggers.after_daily(lab.store, STATUS, DAY))


# ---------------------------------------------------------------- notes


def investigate(lab, story="f-absence"):
    return go(lab, Model(calls(("get_status", {})), final("## Summary\nThe run was ok [ref:r1].\n## Evidence\n- Nothing else was found [ref:r1].")),
              subject=story, name="investigator")  # fmt: skip


def test_the_investigators_note_is_found_by_story_id_and_ages_out(lab):  # noqa: F811
    four(lab)
    r = investigate(lab)
    note = agentnotes.note_for(lab.store, "f-absence")
    assert note["run_id"] == r.run_id and "The run was ok [ref:r1]." in note["output"]
    assert agentnotes.note_for(lab.store, "other") is None
    assert agentnotes.note_for(lab.store, "f-absence", datetime.now(UTC) + timedelta(days=30)) is None


def test_the_note_is_put_on_the_hosts_detail_page_once_and_within_the_line_limit(lab):  # noqa: F811
    four(lab)
    investigate(lab)
    day = DAY.date().isoformat()
    page = lab.settings.out_dir / day / "hosts" / "198.51.100.3.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Host\n" + "\n".join(f"line {i}" for i in range(20)) + "\n")
    doc = {**STATUS, "daily_generated_at": f"{day}T06:00:00Z"}
    assert agentnotes.attach(lab.store, lab.settings.out_dir, doc) == 1
    assert agentnotes.attach(lab.store, lab.settings.out_dir, doc) == 1  # again: replaced, not doubled
    text = page.read_text()
    assert (
        text.count("<!-- agent-note:f-absence -->") == 1
        and "The run was ok [ref:r1]." in text
        and "advice only" in text
    )
    full = "\n".join(f"line {i}" for i in range(147)) + "\n"
    page.write_text(full)
    assert (
        agentnotes.attach(lab.store, lab.settings.out_dir, doc) == 0 and page.read_text() == full
    )  # no room: skipped
    page.write_text("\n".join(f"line {i}" for i in range(135)) + "\n")
    agentnotes.attach(lab.store, lab.settings.out_dir, doc)
    assert len(page.read_text().splitlines()) <= agentnotes.MAX_PAGE_LINES
    assert (
        agentnotes.attach(lab.store, lab.settings.out_dir, {"stories": STATUS["stories"]}) == 0
    )  # no daily stamp


def test_the_findings_page_shows_the_note(lab):  # noqa: F811
    four(lab)
    investigate(
        lab,
        story=next(
            s["id"]
            for s in json.loads((lab.settings.out_dir / "latest" / "status.json").read_text())["stories"]
        ),
    )
    page = lab.client.get("/admin/findings").text
    assert "Investigation note" in page and "The run was ok" in page


def test_the_mcp_finding_tool_returns_the_note_and_the_list_flags_it(lab, monkeypatch):  # noqa: F811
    four(lab)
    investigate(lab, story="f-absence")
    found = Finding(id="f-absence", kind="absence", title="silent", system="cam")
    monkeypatch.setattr("brief.aitasks.find_finding", lambda *a, **k: found)
    doc = {**STATUS}

    def ctx(*scopes):
        return agenttools.Context(
            lab.store, lab.settings, Client("t", "t", frozenset(scopes)), lambda: (doc, status()[1], [])
        )

    out = json.loads(
        agenttools.call_tool(ctx("findings"), "get_finding", {"finding_id": "f-absence"})["content"][0][
            "text"
        ]
    )
    assert out["investigation"]["note"].startswith("## Summary")
    listed = json.loads(agenttools.call_tool(ctx("findings"), "list_findings", {})["content"][0]["text"])
    assert [s.get("has_investigation") for s in listed["stories"] if s["id"] == "f-absence"] == [True]
    investigate(lab, story="group-deviation-198.51.100.1")
    monkeypatch.setattr("brief.aitasks.find_finding", lambda *a, **k: None)
    grouped = json.loads(
        agenttools.call_tool(ctx("findings"), "get_finding", {"finding_id": "group-deviation-198.51.100.1"})[
            "content"
        ][0]["text"]
    )
    assert grouped["grouped"] and "investigation" in grouped
    investigate(lab, story="group-new_device-198.51.100.9")  # device-level: needs the devices scope
    hidden = agenttools.call_tool(
        ctx("findings"), "get_finding", {"finding_id": "group-new_device-198.51.100.9"}
    )
    assert hidden["isError"]
    shown = agenttools.call_tool(
        ctx("findings", "devices"), "get_finding", {"finding_id": "group-new_device-198.51.100.9"}
    )
    assert not shown["isError"]


# ---------------------------------------------------------------- ask the lab and the page


def test_ask_the_lab_queues_a_question_and_shows_the_cited_answer(lab, monkeypatch):  # noqa: F811
    four(lab)
    page = lab.client.get("/admin/ai")
    assert "<h2>Ask the lab</h2>" in page.text and "<h2>Triage verdicts</h2>" in page.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    assert lab.client.post("/admin/ai/ask", data={"question": "why?"}).status_code == 403  # no CSRF token
    assert lab.client.post("/admin/ai/ask", data={"csrf_token": csrf, "question": "  "}).status_code == 422
    done = lab.client.post("/admin/ai/ask", data={"csrf_token": csrf, "question": "Is the gateway healthy?"})
    assert done.status_code == 303
    assert (
        lab.client.post(
            "/admin/ai/ask", data={"csrf_token": csrf, "question": "Is the gateway healthy?"}
        ).status_code
        == 422
    )  # waiting
    waiting = lab.client.get("/admin/ai/ask/recent").text
    assert "Waiting for the worker: Is the gateway healthy?" in waiting and "hx-trigger" in waiting
    monkeypatch.setattr(
        ai,
        "_post",
        Model(calls(("get_status", {})), final("Yes, the last run was ok [ref:r1]. Also it is fast.")),
    )
    assert agentrun.work_one(lab.store, lab.settings, status, DAY) == "ask_the_lab: ok"
    answered = lab.client.get("/admin/ai/ask/recent").text
    assert (
        "Yes, the last run was ok [ref:r1]." in answered
        and "it is fast" not in answered
        and "hx-trigger" not in answered
    )


def test_ask_the_lab_says_so_when_it_is_off(lab):  # noqa: F811
    setup(lab, features="agents: false")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', lab.client.get("/admin/ai").text).group(1)
    r = lab.client.post("/admin/ai/ask", data={"csrf_token": csrf, "question": "hello?"})
    assert r.status_code == 422 and "Ask the lab is off" in r.text and rows(lab, "agent_job") == []


def test_cli_shows_triage_verdicts(box, capsys):  # noqa: F811
    _, user, state, store = box
    with store.write() as tx:
        sid = tx.signature_for(0, 1, "app", "t", datetime.now(UTC))
        tx.db.execute("INSERT INTO agent_verdict (sig_id, verdict, reason, run_id, created) VALUES (?, 'watch', 'new today', 1, ?)",
                      (sid, iso(datetime.now(UTC))))  # fmt: skip
    assert cli.main(["agent", "verdicts"]) == 0
    shown = capsys.readouterr().out
    assert "watch" in shown and "new today" in shown


# ---------------------------------------------------------------- the worker's part


def test_the_worker_queues_agent_work_after_the_daily_and_weekly_runs_and_attaches_notes(tmp_path, capsys):
    from test_worker import make_env

    from brief import worker as w

    env, user = make_env(tmp_path)
    (user / "ai.yaml").write_text("schema: 1\nenabled: true\nfeatures: {agents: true}\n")
    (user / "agents.yaml").write_text(FOUR)
    (user / "priorities.yaml").write_bytes((BRIEF / "priorities.yaml").read_bytes())
    worker = w.Worker(tmp_path / "state", env)
    latest = worker.out_dir / "latest"
    latest.mkdir(parents=True)
    now = datetime.now(UTC)
    (latest / "status.json").write_text(json.dumps({**STATUS, "daily_generated_at": iso(now)}))
    worker._agent_follow_up(now)
    queued = {(j["agent"], j["subject"]) for j in rows_of(worker.store, "agent_job")}
    logged = capsys.readouterr().out
    assert ("investigator", "f-absence") in queued, logged
    worker.run_weekly(now)
    assert ("tuner", "weekly review") in {
        (j["agent"], j["subject"]) for j in rows_of(worker.store, "agent_job")
    }


def rows_of(store, table):
    with store.read() as db:
        return [dict(r) for r in db.execute(f"SELECT * FROM {table}")]  # noqa: S608 (a fixed name)
