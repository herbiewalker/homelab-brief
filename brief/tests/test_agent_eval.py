"""Task 30: agent fixtures, scores, live metrics, and the check before a prompt or model change takes effect."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)
from test_agents import calls, final, rows
from test_agents29 import FOUR, four
from test_cli_tools import box  # noqa: F401  (the fixture: a state dir, config dir, and store)

from brief import agenteval, agentrun, ai, cli, configedit, proposals
from brief.store import iso

BRIEF = Path(__file__).resolve().parents[1]
FINDING_SUBJECTS = {
    "parity-slow-nas": "parity check speed slow",
    "drift-ap-temp": "temperature hot drift",
    "absence-cam": "silent absence stopped",
}


ASK_WORDS = {"Is any of my network equipment waiting for a firmware update?": "firmware switch update"}


class Brain:
    """A scripted model that behaves well, or sloppily when the prompt says BE SLOPPY or the model is bad-model."""

    def __init__(self):
        self.bodies = []

    def __call__(self, url, headers, body, timeout):
        self.bodies.append(body)
        messages = body["messages"]
        sloppy = "BE SLOPPY" in messages[0]["content"] or body["model"] == "bad-model"
        user = messages[1]["content"]
        subject = user.removeprefix("Subject: ")
        if "tools" in body and not any(m["role"] == "tool" for m in messages):
            if user.startswith("Review these signature ids"):
                ids = [int(i) for i in re.findall(r"\d+", user)]
                return calls(*[("get_signature", {"id": i}) for i in ids])
            if user.startswith("Weekly review"):
                return calls(("run_replay", {"days": 3}))
            if subject in FINDING_SUBJECTS:
                return calls(("get_finding", {"finding_id": subject}))
            return calls(("get_status", {}))
        if user.startswith("Review these signature ids"):
            ids = [int(i) for i in re.findall(r"\d+", user)]
            wrong = {1: "concern", 2: "noise", 3: "noise"}
            right = {1: "noise", 2: "concern", 3: "concern"}
            items = [
                {
                    "sig_id": i,
                    "verdict": (wrong if sloppy else right)[i],
                    "reason": f"Reason {i}.",
                    "citations": ["r1"],
                }
                for i in ids
            ]
            if not sloppy:
                items[0]["proposal"] = {"kind": "ignore", "pattern": "link flapped"}
            return final(json.dumps(items))
        if user.startswith("Weekly review"):
            return final("[]")
        if sloppy:
            return final("Everything is probably fine, no need to look closer.")
        words = (FINDING_SUBJECTS.get(subject) or ASK_WORDS.get(subject) or "parity yellow slow").split()
        return final(f"The {words[0]} is the main point [ref:r1]. The {words[1]} showed it too [ref:r1].")


@pytest.fixture
def brain(monkeypatch):
    b = Brain()
    monkeypatch.setattr(ai, "_post", b)
    return b


def prepare(lab, **kw):  # noqa: F811
    """Four agents on, the shipped fixtures in place, and a roomy daily budget."""
    four(lab, **kw)
    root = lab.settings.state_dir.parent
    (root / "defaults" / "brief" / "fixtures" / "agents").mkdir(parents=True, exist_ok=True)
    for f in (BRIEF / "fixtures" / "agents").glob("*.yaml"):
        (root / "defaults" / "brief" / "fixtures" / "agents" / f.name).write_bytes(f.read_bytes())
    path = root / "cfg" / "brief" / "ai.yaml"
    path.write_text(path.read_text().replace("guardrails: {", "guardrails: {max_calls_per_day: 5000, "))


def cfg(lab):  # noqa: F811
    return lab.settings.state_dir.parent / "cfg" / "brief"


def sloppy_prompt(lab):  # noqa: F811
    old, _ = configedit.current("prompts/agent_investigator.md")
    return old + "\nBE SLOPPY and skip the checking.\n"


# ---------------------------------------------------------------- fixtures


def test_the_shipped_fixtures_cover_the_four_agents_and_the_named_cases():
    ids = {}
    for path in (BRIEF / "fixtures" / "agents").glob("*.yaml"):
        data = __import__("yaml").safe_load(path.read_text())
        ids[data["id"]] = data["agent"]
        assert data["expect"] and data["title"], path.name
    assert {"slow_parity", "hot_access_point", "collection_outage", "noisy_signature"} <= set(ids)
    assert set(ids.values()) == {"investigator", "triage", "ask_the_lab", "tuner"}
    text = "".join(p.read_text() for p in (BRIEF / "fixtures" / "agents").glob("*"))
    assert not re.search(
        r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+\.?\d*\b", text
    )  # only documentation addresses


def test_your_own_fixture_replaces_a_shipped_one_with_the_same_id_and_a_broken_file_is_skipped(lab):  # noqa: F811
    prepare(lab)
    mine = cfg(lab) / "fixtures" / "agents"
    mine.mkdir(parents=True)
    (mine / "slow.yaml").write_text(
        "id: slow_parity\nagent: investigator\nsubject: x\ntitle: mine\nexpect: {min_kept: 1}\n"
    )
    (mine / "broken.yaml").write_text("id: [unclosed")
    (mine / "empty.yaml").write_text("title: no id\n")
    found = agenteval.load_fixtures(None)
    assert found["slow_parity"].title == "mine" and "noisy_signature" in found and len(found) == 7


# ---------------------------------------------------------------- scoring


def test_a_good_agent_passes_its_cases_and_scores_high_and_nothing_real_is_touched(lab, brain):  # noqa: F811
    prepare(lab)
    scores = {s.agent: s for s in agenteval.evaluate(lab.store, label="manual")}
    assert set(scores) == {"investigator", "triage", "tuner", "ask_the_lab"}
    for s in scores.values():
        assert all(c.passed for c in s.cases), [(c.id, c.failed) for c in s.cases if not c.passed]
        assert s.score >= 95 and s.citation_rate == 1.0
    assert len(scores["investigator"].cases) == 3
    stored = {r["agent"]: r for r in rows(lab, "agent_eval")}
    assert stored["triage"]["score"] == scores["triage"].score and stored["triage"]["label"] == "manual"
    assert (
        "model" not in json.loads(stored["triage"]["detail"])[0]
        and "Reason" not in stored["triage"]["detail"]
    )
    assert (
        rows(lab, "agent_run") == [] and rows(lab, "proposal") == [] and rows(lab, "agent_verdict") == []
    )  # scratch only
    assert not (cfg(lab) / "ignore.yaml").exists()
    spent = [c for c in rows(lab, "ai_call") if c["task"] == "agent_eval"]
    assert len(spent) == len(brain.bodies) > 8  # every model call is in the real call log


def test_a_sloppy_agent_scores_lower_and_the_failed_checks_say_why(lab, monkeypatch, brain):  # noqa: F811
    prepare(lab)
    good = agenteval.evaluate(lab.store, ["investigator", "triage"])
    (cfg(lab) / "prompts" / "agent_investigator.md").write_text(sloppy_prompt(lab))
    bad = agenteval.evaluate(lab.store, ["investigator"])[0]
    assert bad.score < good[0].score - 30 and not any(c.passed for c in bad.cases)
    assert {"outcome no_evidence", "looked nothing up"} & set(bad.cases[0].failed)
    brain_bad = agenteval.evaluate(
        lab.store,
        ["triage"],
        overrides={"ai.yaml": (cfg(lab) / "ai.yaml").read_text().replace("model: m", "model: bad-model")},
    )[0]
    assert brain_bad.score < good[1].score and any("verdict" in f for f in brain_bad.cases[0].failed)


def test_the_score_is_sixty_thirty_ten(lab, brain):  # noqa: F811
    prepare(lab)
    s = agenteval.evaluate(lab.store, ["investigator"])[0]
    assert s.score == round(60 * s.pass_rate + 30 * s.citation_rate + 10, 1) and s.pass_rate == 1.0


def test_an_agent_without_fixtures_or_unknown_is_not_run(lab, brain):  # noqa: F811
    prepare(lab)
    assert agenteval.evaluate(lab.store, ["nobody"]) == [] and brain.bodies == []


def test_evaluation_refuses_to_overspend_the_days_model_call_budget(lab, brain):  # noqa: F811
    prepare(lab)
    path = cfg(lab) / "ai.yaml"
    path.write_text(path.read_text().replace("max_calls_per_day: 5000", "max_calls_per_day: 3"))
    with pytest.raises(agenteval.EvalError, match="budget"):
        agenteval.evaluate(lab.store, ["investigator"])
    assert brain.bodies == []


def test_cases_run_even_if_an_agent_is_switched_off_but_the_feature_must_be_configured(lab, brain):  # noqa: F811
    prepare(lab, agents=FOUR.replace("investigator:\n    enabled: true", "investigator:\n    enabled: false"))
    assert agenteval.evaluate(lab.store, ["investigator"])[0].score > 90


# ---------------------------------------------------------------- live metrics


def test_live_metrics_come_from_real_runs_and_decided_proposals(lab):  # noqa: F811
    prepare(lab)
    now = iso(datetime.now(UTC))
    with lab.store.write() as tx:
        tx.db.execute("INSERT INTO agent_run (agent, trigger, subject, started, finished, outcome, steps, kept, dropped) VALUES "
                      "('triage', 'q', 's', ?, ?, 'ok', 3, 3, 1)", (now, now))  # fmt: skip
        tx.db.execute("INSERT INTO agent_run (agent, trigger, subject, started, finished, outcome, steps, kept, dropped) VALUES "
                      "('triage', 'q', 's', ?, ?, 'no_evidence', 5, 0, 4)", (now, now))  # fmt: skip
        tx.db.execute(
            "INSERT INTO agent_run (agent, trigger, subject, started, outcome) VALUES ('triage', 'q', 's', ?, 'off')",
            (now,),
        )
        for status in ("approved", "approved", "rejected", "pending"):
            tx.db.execute("INSERT INTO proposal (created, source, file, base_digest, new_text, reason, evidence, status) VALUES "
                          "(?, 'agent:triage', 'ignore.yaml', 'd', 't', 'r', 'e', ?)", (now, status))  # fmt: skip
    live = agenteval.live_metrics(lab.store)["triage"]
    assert (live["runs"], live["ok"], live["approved"], live["rejected"]) == (2, 1, 2, 1)
    assert live["citation_rate"] == 3 / 8 and live["steps"] == 4


# ---------------------------------------------------------------- the check before a change takes effect


def test_a_deliberately_worse_prompt_is_flagged_and_not_saved(lab, brain):  # noqa: F811
    prepare(lab)
    name = "prompts/agent_investigator.md"
    old, _ = configedit.current(name)
    with pytest.raises(configedit.EditError, match="makes an agent worse") as caught:
        configedit.save(
            lab.store, name, sloppy_prompt(lab), "admin:me", configedit.digest(old), lab.settings.state_dir
        )
    assert caught.value.status == 409 and "Save even though the score is lower" in str(caught.value)
    assert configedit.current(name)[0] == old  # nothing changed
    flagged = agenteval.flagged_changes(lab.store)
    assert (
        flagged
        and flagged[0]["agent"] in ("investigator", "ask_the_lab")
        and flagged[0]["score"] < flagged[0]["baseline_score"] - 5
    )
    assert any(r["label"] == "baseline" for r in rows(lab, "agent_eval"))


def test_the_person_can_accept_a_lower_score_and_that_is_audited(lab, brain):  # noqa: F811
    prepare(lab)
    name = "prompts/agent_investigator.md"
    old, _ = configedit.current(name)
    agenteval.OVERRIDE.set(True)
    try:
        configedit.save(
            lab.store, name, sloppy_prompt(lab), "admin:me", configedit.digest(old), lab.settings.state_dir
        )
    finally:
        agenteval.OVERRIDE.set(False)
    assert "BE SLOPPY" in configedit.current(name)[0]
    assert [a for a in rows(lab, "audit_log") if a["action"] == "agent.eval.override"][0][
        "user"
    ] == "admin:me"


def test_a_harmless_prompt_change_goes_through_and_the_baseline_is_reused(lab, brain):  # noqa: F811
    prepare(lab)
    name = "prompts/agent_investigator.md"
    old, _ = configedit.current(name)
    configedit.save(
        lab.store,
        name,
        old + "\nPlease be brief.\n",
        "admin:me",
        configedit.digest(old),
        lab.settings.state_dir,
    )
    assert "Please be brief." in configedit.current(name)[0]
    first = len(brain.bodies)
    new, _ = configedit.current(name)
    configedit.save(
        lab.store, name, new + "\nAnd polite.\n", "admin:me", configedit.digest(new), lab.settings.state_dir
    )
    labels = [r["label"] for r in rows(lab, "agent_eval")]
    assert (
        labels.count("baseline") == 2
    )  # investigator and ask_the_lab, once each: the second change reused them
    assert (
        labels.count("candidate") == 4
        and len(brain.bodies) > first
        and not agenteval.flagged_changes(lab.store)
    )


def test_changing_the_model_of_a_provider_an_agent_uses_is_tested_too(lab, brain):  # noqa: F811
    prepare(lab)
    path = cfg(lab) / "ai.yaml"
    old = path.read_text()
    new = old.replace("model: m", "model: bad-model")
    with pytest.raises(configedit.EditError, match="makes an agent worse"):
        configedit.save(lab.store, "ai.yaml", new, "admin:me", configedit.digest(old), lab.settings.state_dir)
    assert path.read_text() == old
    assert {f["agent"] for f in agenteval.flagged_changes(lab.store)} >= {"triage"}


def test_a_change_that_does_not_touch_any_agent_is_not_tested(lab, brain):  # noqa: F811
    prepare(lab)
    old, _ = configedit.current("ai.yaml")
    configedit.save(lab.store, "ai.yaml", old.replace("run_agents_only_when_gpu_idle: true", "run_agents_only_when_gpu_idle: false"),
                    "admin:me", configedit.digest(old), lab.settings.state_dir)  # fmt: skip
    assert brain.bodies == [] and rows(lab, "agent_eval") == []


def test_nothing_is_tested_while_agents_are_off_or_when_the_test_run_cannot_happen(lab, brain, monkeypatch):  # noqa: F811
    prepare(lab, features="agents: false")
    name = "prompts/agent_investigator.md"
    old, _ = configedit.current(name)
    configedit.save(
        lab.store, name, sloppy_prompt(lab), "admin:me", configedit.digest(old), lab.settings.state_dir
    )
    assert brain.bodies == []
    prepare(lab)  # on again; now the budget is too small to test
    path = cfg(lab) / "ai.yaml"
    path.write_text(path.read_text().replace("max_calls_per_day: 5000", "max_calls_per_day: 2"))
    new, _ = configedit.current(name)
    configedit.save(
        lab.store, name, new + "\nmore\n", "admin:me", configedit.digest(new), lab.settings.state_dir
    )  # let through
    monkeypatch.setattr(agenteval, "evaluate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    path.write_text(path.read_text().replace("max_calls_per_day: 2", "max_calls_per_day: 5000"))
    again, _ = configedit.current(name)
    configedit.save(
        lab.store, name, again + "\neven more\n", "admin:me", configedit.digest(again), lab.settings.state_dir
    )
    assert "even more" in configedit.current(name)[0]  # a crashing test run never locks the configuration


def test_resetting_a_prompt_to_the_shipped_default_is_tested_as_well(lab, brain):  # noqa: F811
    prepare(lab)
    name = "prompts/agent_investigator.md"
    agenteval.OVERRIDE.set(True)
    try:
        old, _ = configedit.current(name)
        configedit.save(
            lab.store, name, sloppy_prompt(lab), "admin:me", configedit.digest(old), lab.settings.state_dir
        )
    finally:
        agenteval.OVERRIDE.set(False)
    configedit.reset(
        lab.store, name, "admin:me", lab.settings.state_dir
    )  # back to the shipped (better) prompt: allowed
    assert "BE SLOPPY" not in configedit.current(name)[0]


# ---------------------------------------------------------------- the page, the editor, and the command line


def test_the_ai_page_runs_the_cases_and_shows_scores_and_held_changes(lab, brain):  # noqa: F811
    prepare(lab)
    page = lab.client.get("/admin/ai/agents")
    assert "<h2>Agent evaluation</h2>" in page.text and "Not run yet." in page.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    assert lab.client.post("/admin/ai/eval", data={"agent": "triage"}).status_code == 403
    assert lab.client.post("/admin/ai/eval", data={"csrf_token": csrf, "agent": "nobody"}).status_code == 404
    done = lab.client.post("/admin/ai/eval", data={"csrf_token": csrf, "agent": "triage"})
    assert done.status_code == 303
    after = lab.client.get("/admin/ai/agents").text
    assert "triage" in after and "Not run yet." not in after and "3/3" in after or "1/1" in after
    assert [a["action"] for a in rows(lab, "audit_log") if a["action"] == "agent.eval"] == ["agent.eval"]
    with pytest.raises(configedit.EditError):
        old, _ = configedit.current("prompts/agent_investigator.md")
        configedit.save(
            lab.store,
            "prompts/agent_investigator.md",
            sloppy_prompt(lab),
            "x",
            configedit.digest(old),
            lab.settings.state_dir,
        )
    assert "Changes held for a lower score" in lab.client.get("/admin/ai/agents").text


def test_the_editor_blocks_a_worse_prompt_until_the_box_is_ticked(lab, brain):  # noqa: F811
    prepare(lab)
    page = lab.client.get("/admin/ai/prompt/agent_investigator")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    base = re.search(r'name="base" value="([^"]+)"', page.text).group(1)
    assert "accept_eval" in page.text
    blocked = lab.client.post(
        "/admin/ai/prompt/agent_investigator",
        data={"csrf_token": csrf, "base": base, "text": sloppy_prompt(lab)},
    )
    assert blocked.status_code == 409 and "makes an agent worse" in blocked.text
    assert "BE SLOPPY" not in configedit.current("prompts/agent_investigator.md")[0]
    ok = lab.client.post(
        "/admin/ai/prompt/agent_investigator",
        data={"csrf_token": csrf, "base": base, "text": sloppy_prompt(lab), "accept_eval": "on"},
    )
    assert ok.status_code == 303 and "BE SLOPPY" in configedit.current("prompts/agent_investigator.md")[0]
    assert agenteval.OVERRIDE.get() in (True, False)


def test_cli_evaluates_scores_and_can_accept_a_lower_score(box, capsys, monkeypatch, tmp_path):  # noqa: F811
    _, user, state, store = box
    root = tmp_path / "defaults" / "brief"
    (root / "fixtures" / "agents").mkdir(parents=True)
    for f in (BRIEF / "fixtures" / "agents").glob("*.yaml"):
        (root / "fixtures" / "agents" / f.name).write_bytes(f.read_bytes())
    (user / "prompts").mkdir()
    for name in ("agent_investigator", "agent_triage", "agent_tuner"):
        (user / "prompts" / f"{name}.md").write_text((BRIEF / "prompts" / f"{name}.md").read_text())
    (user / "agents.yaml").write_text(FOUR)
    (user / "ai.yaml").write_text(
        "schema: 1\nenabled: true\nfeatures: {agents: true}\nproviders:\n  - {id: local, type: openai_compatible, base_url: 'http://x.invalid/v1', model: m, enabled: true}\n"
        "guardrails: {max_calls_per_day: 5000}\n"
    )
    (user / "priorities.yaml").write_bytes((BRIEF / "priorities.yaml").read_bytes())
    monkeypatch.setenv("BRIEF_DEFAULTS_DIR", str(tmp_path / "defaults"))
    monkeypatch.setattr(ai, "_post", Brain())
    assert cli.main(["agent", "scores"]) == 0 and "No evaluation yet" in capsys.readouterr().out
    assert cli.main(["agent", "eval", "triage"]) == 0
    out = capsys.readouterr().out
    assert "triage: score" in out and "pass  noisy_signature" in out
    assert cli.main(["agent", "scores"]) == 0 and "triage" in capsys.readouterr().out
    bad = tmp_path / "bad.md"
    bad.write_text(sloppy_prompt_text(user))
    assert cli.main(["config", "set", "prompts/agent_investigator.md", "--from", str(bad)]) == 1
    assert "makes an agent worse" in capsys.readouterr().err
    assert (
        cli.main(
            ["config", "set", "prompts/agent_investigator.md", "--from", str(bad), "--accept-lower-score"]
        )
        == 0
    )
    assert "BE SLOPPY" in (user / "prompts" / "agent_investigator.md").read_text()


def sloppy_prompt_text(user_dir):
    return (
        user_dir / "prompts" / "agent_investigator.md"
    ).read_text() + "\nBE SLOPPY and skip the checking.\n"


def test_agent_eval_is_a_known_sensitive_file_set():
    assert (
        "agents.yaml" in agenteval.GATED
        and "prompts/agent_triage.md" in agenteval.GATED
        and "ai.yaml" in agenteval.GATED
    )
    assert proposals.PROPOSABLE and agentrun.LIMITS
