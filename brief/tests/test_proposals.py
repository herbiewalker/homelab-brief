"""Task 28: proposals change nothing until a person approves; approval goes through the config editor."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_admin import lab  # noqa: F401  (the fixture: an HTTPS app with a signed-in admin)
from test_agents import Model, calls, final, go, rows, setup, status
from test_cli_tools import box  # noqa: F401  (the fixture: a state dir, config dir, and store)
from test_mcp import call, rpc, token, turn_on

from brief import agenttools, cli, configedit, mcpserver, mcptokens, proposals
from brief.mcptokens import Client

BRIEF = Path(__file__).resolve().parents[1]
IGNORE = (BRIEF / "ignore.yaml").read_text()
TRIAGE = """schema: 1
defaults: {max_steps: 5, max_tool_calls: 12, max_tokens_per_run: 9000, timeout_s: 60, max_runs_per_day: 5, chain: [CHAIN]}
agents:
  triage: {enabled: true, tools: [get_status, get_config, propose_change], prompt: prompts/agent_investigator.md}
"""


def edited(old: str) -> str:
    """The same file with one extra comment line: valid for every validator."""
    return old + "# proposed: a note\n"


def cfg(env):
    return env.settings.state_dir.parent / "cfg" / "brief"


def make(env, **kw):
    old, _ = configedit.current("ignore.yaml")
    args = {"source": "agent:triage", "file": "ignore.yaml", "new_text": edited(old), "reason": "noisy",
            "evidence": "seen a lot [ref:r1]"}  # fmt: skip
    args.update(kw)
    return proposals.create(
        env.store, args["source"], args["file"], args["new_text"], args["reason"], args["evidence"]
    )


def actions(env, prefixes):
    return [(a["user"], a["action"]) for a in rows(env, "audit_log") if a["action"].startswith(prefixes)]


# ---------------------------------------------------------------- creating changes nothing


def test_a_proposal_is_a_record_and_the_file_is_untouched(lab):  # noqa: F811
    setup(lab)
    before = configedit.current("ignore.yaml")
    pid = make(lab)
    assert configedit.current("ignore.yaml") == before and not (cfg(lab) / "ignore.yaml").exists()
    p = proposals.get(lab.store, pid)
    assert p.status == "pending" and p.base_digest == configedit.digest(before[0])
    assert actions(lab, "proposal") == [("agent:triage", "proposal.create")]


@pytest.mark.parametrize(
    "kw,why",
    [
        ({"file": "ai.yaml"}, "can only change"),
        ({"file": "agents.yaml"}, "can only change"),
        ({"file": "network.yaml"}, "can only change"),
        ({"file": "notify.yaml"}, "can only change"),
        ({"file": "../etc/passwd"}, "can only change"),
        ({"reason": ""}, "reason"),
        ({"evidence": ""}, "evidence"),
        ({"reason": "x" * 700}, "reason"),
        ({"new_text": "schema: [unclosed"}, "not valid"),
        ({"new_text": "schema: 99\n"}, "not valid"),
    ],
)
def test_a_proposal_that_is_not_allowed_or_not_valid_is_refused(lab, kw, why):  # noqa: F811
    setup(lab)
    with pytest.raises(proposals.ProposalError, match=why):
        make(lab, **kw)
    assert rows(lab, "proposal") == []


def test_no_change_a_duplicate_a_huge_change_and_too_many_are_refused(lab):  # noqa: F811
    setup(lab)
    old, _ = configedit.current("ignore.yaml")
    with pytest.raises(proposals.ProposalError, match="nothing to propose"):
        make(lab, new_text=old)
    make(lab)
    with pytest.raises(proposals.ProposalError, match="already waiting"):
        make(lab)
    with pytest.raises(proposals.ProposalError, match="at most 80"):
        make(lab, new_text=old + "".join(f"# line {i}\n" for i in range(100)))
    for i in range(proposals.MAX_PENDING - 1):
        make(lab, new_text=edited(old) + f"# {i}\n")
    with pytest.raises(proposals.ProposalError, match="already 20"):
        make(lab, new_text=edited(old) + "# one more\n")


def test_a_guard_can_refuse_a_proposal(lab, monkeypatch):  # noqa: F811
    setup(lab)
    monkeypatch.setattr(proposals, "GUARDS", [lambda f, old, new, who: "no, because of a safety rule"])
    with pytest.raises(proposals.ProposalError, match="safety rule"):
        make(lab)


# ---------------------------------------------------------------- deciding


def test_approval_applies_through_the_config_editor_with_a_version_and_audit(lab):  # noqa: F811
    setup(lab)
    old, _ = configedit.current("ignore.yaml")
    pid = make(lab)
    message = proposals.approve(lab.store, pid, "admin:me", lab.settings.state_dir)
    assert "Saved ignore.yaml" in message
    assert (cfg(lab) / "ignore.yaml").read_text() == edited(old)
    decided = proposals.get(lab.store, pid)
    assert decided.status == "approved" and decided.decided_by == "admin:me"
    kept = configedit.versions(lab.settings.state_dir, "ignore.yaml")
    assert (
        kept and configedit.version_text(lab.settings.state_dir, "ignore.yaml", kept[0].stamp) == old
    )  # rollback
    assert actions(lab, ("proposal", "config")) == [
        ("agent:triage", "proposal.create"),
        ("admin:me", "config.proposal"),
        ("admin:me", "proposal.approved"),
    ]
    with pytest.raises(proposals.ProposalError, match="already approved"):
        proposals.approve(lab.store, pid, "admin:me", lab.settings.state_dir)


def test_rejection_changes_nothing_and_is_logged(lab):  # noqa: F811
    setup(lab)
    pid = make(lab)
    proposals.reject(lab.store, pid, "admin:me", "not now")
    assert not (cfg(lab) / "ignore.yaml").exists()
    p = proposals.get(lab.store, pid)
    assert (p.status, p.note, p.decided_by) == ("rejected", "not now", "admin:me")
    assert ("admin:me", "proposal.rejected") in actions(lab, "proposal")
    with pytest.raises(proposals.ProposalError, match="already rejected"):
        proposals.reject(lab.store, pid, "admin:me")


def test_a_proposal_made_against_an_older_file_is_closed_not_applied(lab):  # noqa: F811
    setup(lab)
    pid = make(lab)
    (cfg(lab) / "ignore.yaml").write_text(IGNORE + "# someone edited it meanwhile\n")
    info = proposals.view(proposals.get(lab.store, pid))
    assert info["stale"] and not info["approvable"]
    with pytest.raises(proposals.ProposalError, match="changed after"):
        proposals.approve(lab.store, pid, "admin:me", lab.settings.state_dir)
    assert proposals.get(lab.store, pid).status == "stale"
    assert "proposed" not in (cfg(lab) / "ignore.yaml").read_text()


def test_a_proposal_that_stopped_being_valid_cannot_be_approved(lab, monkeypatch):  # noqa: F811
    setup(lab)
    pid = make(lab)
    monkeypatch.setattr(configedit, "validate_text", lambda name, text: "the rules changed under it")
    with pytest.raises(proposals.ProposalError, match="no longer valid"):
        proposals.approve(lab.store, pid, "admin:me", lab.settings.state_dir)
    assert proposals.get(lab.store, pid).status == "invalid" and not (cfg(lab) / "ignore.yaml").exists()


def test_unknown_ids_and_old_pending_proposals(lab):  # noqa: F811
    setup(lab)
    with pytest.raises(proposals.ProposalError, match="no such"):
        proposals.approve(lab.store, 99, "x", lab.settings.state_dir)
    pid = make(lab)
    assert proposals.sweep(lab.store, datetime.now(UTC) + timedelta(days=1)) == 0
    assert proposals.sweep(lab.store, datetime.now(UTC) + timedelta(days=15)) == 1
    assert proposals.get(lab.store, pid).status == "expired"
    with pytest.raises(proposals.ProposalError, match="already expired"):
        proposals.approve(lab.store, pid, "x", lab.settings.state_dir)


# ---------------------------------------------------------------- who may propose


def test_an_agent_proposes_through_its_tools_and_must_cite_real_results(lab):  # noqa: F811
    setup(lab, agents=TRIAGE.replace("CHAIN", "local"))
    old, _ = configedit.current("ignore.yaml")
    bad = {
        "file": "ignore.yaml",
        "new_text": edited(old),
        "reason": "noise",
        "evidence": "I just feel like it",
    }
    good = {**bad, "evidence": "The status was ok [ref:r1]."}
    fake = {**good, "evidence": "Made up [ref:r9]."}
    model = Model(
        calls(("get_status", {}), ("get_config", {"file": "ignore.yaml"})),
        calls(("propose_change", bad), ("propose_change", fake), ("propose_change", good)),
        final("I proposed a change [ref:r5]."),
    )
    r = go(lab, model, name="triage")
    assert r.outcome == "ok"
    assert [(s["tool"], s["outcome"]) for s in rows(lab, "agent_step")] == [
        ("get_status", "ok"),
        ("get_config", "ok"),
        ("propose_change", "error"),
        ("propose_change", "error"),
        ("propose_change", "ok"),
    ]
    made = proposals.listing(lab.store)
    assert len(made) == 1 and made[0].source == "agent:triage" and made[0].run_id == r.run_id
    assert "Results cited: r1 (get_status)" in made[0].evidence
    assert not (cfg(lab) / "ignore.yaml").exists()  # still only a proposal


def test_an_agent_makes_at_most_three_proposals_per_run(lab):  # noqa: F811
    setup(lab, agents=TRIAGE.replace("CHAIN", "local"))
    old, _ = configedit.current("ignore.yaml")
    tries = [
        ("propose_change", {"file": "ignore.yaml", "new_text": edited(old) + f"# {i}\n", "reason": "r",
                            "evidence": "ok [ref:r1]"})
        for i in range(5)
    ]  # fmt: skip
    r = go(lab, Model(calls(("get_status", {})), calls(*tries), final("done [ref:r1].")), name="triage")
    assert len(proposals.listing(lab.store)) == 3 and r.outcome == "ok"


def test_a_cloud_agent_can_neither_read_nor_propose_config(lab):  # noqa: F811
    cloud = "  - {id: far, type: openai_compatible, base_url: 'https://cloud.invalid/v1', model: m, enabled: true, locality: cloud}\n"
    setup(lab, providers=cloud, allow_cloud="[triage]", agents=TRIAGE.replace("CHAIN", "far"))
    model = Model(final("x [ref:r1]"))
    go(lab, model, name="triage")
    assert {t["function"]["name"] for t in model.bodies[0]["tools"]} == {"get_status"}


def test_only_a_propose_scoped_mcp_token_can_propose_and_it_is_never_default_or_cloud(lab):  # noqa: F811
    setup(lab)
    turn_on(lab)
    old, _ = configedit.current("ignore.yaml")
    client = lab.https()
    plain = token(lab, ("status", "findings", "history", "systems", "map", "logs"), name="plain")
    args = {"file": "ignore.yaml", "new_text": edited(old), "reason": "r", "evidence": "my own reasoning"}
    assert call(client, plain, "propose_change", args)["isError"]
    assert "propose_change" not in {
        t["name"] for t in rpc(client, plain, "tools/list").json()["result"]["tools"]
    }
    maker = token(lab, ("propose",), name="maker")
    result = call(client, maker, "propose_change", args)
    assert not result["isError"] and json.loads(result["content"][0]["text"])["status"] == "pending"
    assert proposals.listing(lab.store)[0].source == "mcp:maker" and not (cfg(lab) / "ignore.yaml").exists()
    with pytest.raises(mcptokens.TokenError, match="cloud client cannot"):
        token(lab, ("propose",), "cloud", name="cloudy")
    assert "propose" not in mcpserver.default_scopes()


def test_get_config_only_serves_the_proposable_files(lab):  # noqa: F811
    setup(lab)
    ctx = agenttools.Context(
        lab.store, lab.settings, Client("t", "t", frozenset({"logs"})), status, source="agent"
    )
    ok = agenttools.call_tool(ctx, "get_config", {"file": "ignore.yaml"})
    assert not ok["isError"] and "schema" in ok["content"][0]["text"]
    for name in ("ai.yaml", "notify.yaml", "../x"):
        assert agenttools.call_tool(ctx, "get_config", {"file": name})["isError"]
    assert agenttools.PROPOSABLE_FILES == proposals.PROPOSABLE


# ---------------------------------------------------------------- the page and the command line


def test_the_ai_page_shows_the_diff_and_approves_and_rejects(lab):  # noqa: F811
    setup(lab)
    first = make(lab)
    second = make(lab, new_text=edited(configedit.current("ignore.yaml")[0]) + "# second\n")
    page = lab.client.get("/admin/ai")
    assert "Nothing changes until you approve" in page.text and "# proposed: a note" in page.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    assert lab.client.post(f"/admin/ai/proposal/{first}/approve").status_code == 403  # no CSRF token
    done = lab.client.post(f"/admin/ai/proposal/{first}/approve", data={"csrf_token": csrf})
    assert done.status_code == 303 and "proposed: a note" in (cfg(lab) / "ignore.yaml").read_text()
    gone = lab.client.post(f"/admin/ai/proposal/{second}/approve", data={"csrf_token": csrf})  # now stale
    assert gone.status_code == 409 and "changed after" in gone.text
    third = make(lab, new_text=configedit.current("ignore.yaml")[0] + "# third\n")
    rejected = lab.client.post(f"/admin/ai/proposal/{third}/reject", data={"csrf_token": csrf, "note": "no"})
    assert rejected.status_code == 303 and proposals.get(lab.store, third).status == "rejected"
    assert lab.client.post("/admin/ai/proposal/999/approve", data={"csrf_token": csrf}).status_code == 404


def test_cli_lists_shows_approves_and_rejects(box, capsys):  # noqa: F811
    _, user, state, store = box
    old, _ = configedit.current("ignore.yaml")
    a = proposals.create(store, "agent:triage", "ignore.yaml", edited(old), "noisy", "seen [ref:r1]")
    b = proposals.create(
        store, "agent:triage", "ignore.yaml", edited(old) + "# b\n", "again", "seen [ref:r1]"
    )
    assert cli.main(["proposal", "list"]) == 0 and "noisy" in capsys.readouterr().out
    assert cli.main(["proposal", "show", str(a)]) == 0
    shown = capsys.readouterr().out
    assert "+# proposed: a note" in shown and "Why: noisy" in shown
    assert cli.main(["proposal", "approve", str(a)]) == 0 and (user / "ignore.yaml").exists()
    assert cli.main(["proposal", "approve", str(b)]) == 1 and "changed after" in capsys.readouterr().err
    assert cli.main(["proposal", "reject", str(a)]) == 1  # already decided
    c = proposals.create(
        store, "agent:triage", "ignore.yaml", (user / "ignore.yaml").read_text() + "# c\n", "c", "e"
    )
    assert cli.main(["proposal", "reject", str(c), "--note", "later"]) == 0
    assert cli.main(["proposal", "list", "--all"]) == 0 and "rejected" in capsys.readouterr().out
    assert cli.main(["proposal", "show", "99"]) == 1
