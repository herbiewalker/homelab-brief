"""Agent evaluation (docs/guide/10-agents.md): fixtures, scores, and the check before a change takes effect.

A fixture is a past case with known good outcomes (`brief/fixtures/agents/*.yaml`, plus your own under
`/config/brief/fixtures/agents/`). To score an agent, each of its fixtures is built into a scratch database in a
temporary folder (the real data and the real config files are never touched), the agent runs against it with the
real model and the real prompt, and its output is checked against the fixture's `expect`. The score is 0 to 100:

    60 x the share of fixtures passed  +  30 x the citation rate  +  10 x efficiency (steps within 75% of the limit)

Changing an agent's prompt, model, tools, limits, or chain goes through `gate` (called by the config editor, so
every route to a change is covered): the agent's fixtures run with the old and with the new setting, and a change
that scores more than five points lower is refused until the person saves it again and accepts the lower score
(which is audited). Evaluation calls count against the day's model-call budget and the call log, as `agent_eval`.
"""

import contextvars
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from brief import agentrun, ai, configedit, proposals
from brief.config import config_file, default_file, user_file
from brief.detectors import Finding
from brief.jsonlog import log
from brief.store import Store, iso
from brief.systems import System, Systems

TOLERANCE = 5.0  # a candidate may score this many points lower before it is flagged
CACHE_DAYS = 14  # a stored score for the same setting is reused this long
GATED = ("agents.yaml", "ai.yaml", *configedit.PROMPTS)
OVERRIDE: contextvars.ContextVar[bool] = contextvars.ContextVar("eval_override", default=False)


class EvalError(Exception):
    pass


@dataclass
class Fixture:
    id: str
    agent: str
    subject: str
    title: str = ""
    status: dict = field(default_factory=dict)
    systems: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    signatures: list = field(default_factory=list)
    inventory: list = field(default_factory=list)
    notices: list = field(default_factory=list)
    metrics: list = field(default_factory=list)
    expect: dict = field(default_factory=dict)


@dataclass
class Case:
    id: str
    passed: bool
    failed: list[str]
    outcome: str
    kept: int
    dropped: int
    steps: int
    ms: int


@dataclass
class Score:
    agent: str
    label: str
    fingerprint: str
    model: str
    cases: list[Case]
    score: float
    pass_rate: float
    citation_rate: float
    avg_steps: float
    avg_ms: float
    eval_id: int | None = None
    flagged: bool = False
    baseline_score: float | None = None


# ---------------------------------------------------------------- fixtures


def load_fixtures(env: Mapping[str, str] | None = None) -> dict[str, Fixture]:
    """Every fixture, shipped ones first; a file of yours with the same id replaces the shipped one."""
    found: dict[str, Fixture] = {}
    for folder in (default_file("fixtures/agents", env), user_file("fixtures/agents", env)):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                fx = Fixture(**{k: data[k] for k in data if k in Fixture.__dataclass_fields__})
                if not fx.id or not fx.agent or not isinstance(fx.expect, dict):
                    raise ValueError("needs id, agent, and expect")
            except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
                log("fixture_unreadable", file=path.name, error=type(exc).__name__)
                continue
            found[fx.id] = fx
    return found


# ---------------------------------------------------------------- the scratch world for one case


def scratch_env(env: Mapping[str, str] | None, overrides: Mapping[str, str], folder: Path) -> dict[str, str]:
    """A temporary config folder holding the effective config files and prompts, with `overrides` applied."""
    (folder / "brief").mkdir(parents=True, exist_ok=True)
    for name in (*configedit.EDITABLE, *configedit.PROMPTS):
        src = config_file(name, env)
        if src.exists():
            (folder / "brief" / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, folder / "brief" / name)
    for name, text in overrides.items():
        (folder / "brief" / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / "brief" / name).write_text(text, encoding="utf-8")
    return {
        **(os.environ if env is None else env),
        "BRIEF_CONFIG_DIR": str(folder),
        "BRIEF_DEFAULTS_DIR": str(folder),
    }


def _seed(store: Store, fx: Fixture, now: datetime) -> tuple[Systems, dict[str, Finding]]:
    day = f"{now:%Y-%m-%d}"
    with store.write() as tx:
        for i, s in enumerate(fx.signatures, 1):
            sid = tx.signature_for(0, i, s.get("app", ""), s.get("message", ""), now)
            tx.add_hourly(
                [
                    (
                        day,
                        now.hour,
                        s.get("host", "198.51.100.1"),
                        sid,
                        s.get("severity", "info"),
                        int(s.get("count", 1)),
                    )
                ]
            )
            tx.db.execute(
                "INSERT INTO sample (sig_id, ts, host, msg) VALUES (?, ?, ?, ?)",
                (sid, iso(now), s.get("host", "198.51.100.1"), s.get("message", "")),
            )
        for item in fx.inventory:
            tx.db.execute(
                "INSERT INTO inventory (item_id, kind, name, model, current_version, available_version, checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item.get("item_id"), item.get("kind"), item.get("name"), item.get("model"),
                 item.get("current_version"), item.get("available_version"), iso(now)),
            )  # fmt: skip
        for n, notice in enumerate(fx.notices, 1):
            tx.db.execute(
                "INSERT INTO server_notice (server, notice_id, ts, importance, subject, description) VALUES (?, ?, ?, ?, ?, ?)",
                (notice.get("server", "nas"), f"fx{n}", iso(now), notice.get("importance", "warning"),
                 notice.get("subject", ""), notice.get("description", "")),
            )  # fmt: skip
        for m in fx.metrics:
            values = m.get("values") or []
            for i, v in enumerate(values):
                tx.db.execute(
                    "INSERT INTO metric (ts, metric_id, value) VALUES (?, ?, ?)",
                    (iso(now - timedelta(days=len(values) - 1 - i)), m["metric_id"], v),
                )
    systems = Systems(
        [
            System(
                s["id"],
                s.get("label", s["id"]),
                s.get("kind", "other"),
                tuple(s.get("hosts") or ()),
                s.get("source_tz", "UTC"),
                True,
            )
            for s in fx.systems
        ]
    )
    findings = {
        f["id"]: Finding(id=f["id"], kind=f["kind"], title=f["title"], system=f.get("system"), host=f.get("host"),
                         detail=dict(f.get("detail") or {}))
        for f in fx.findings
    }  # fmt: skip
    return systems, findings


def _checks(fx: Fixture, r: agentrun.Result, store: Store) -> list[str]:
    """The names of the checks this run failed (empty = passed)."""
    e = fx.expect
    text = r.text.lower()
    failed = []
    if r.outcome not in (e.get("outcome_any") or ["ok"]):
        failed.append(f"outcome {r.outcome}")
    with store.read() as db:
        steps = [(s["tool"], s["outcome"]) for s in db.execute("SELECT tool, outcome FROM agent_step")]
        verdicts = {
            v["sig_id"]: v["verdict"] for v in db.execute("SELECT sig_id, verdict FROM agent_verdict")
        }
    used = {t for t, o in steps if o == "ok"}
    if e.get("tools_any") and not used & set(e["tools_any"]):
        failed.append("looked nothing up")
    if e.get("tools_all") and not set(e["tools_all"]) <= used:
        failed.append("missed a required tool")
    if any(str(w).lower() not in text for w in e.get("mention") or []):
        failed.append("missing a required point")
    if e.get("mention_any") and not any(str(w).lower() in text for w in e["mention_any"]):
        failed.append("missing every expected point")
    if any(str(w).lower() in text for w in e.get("forbid") or []):
        failed.append("said something it must not")
    if r.kept < int(e.get("min_kept", 0)):
        failed.append("too little cited")
    for sig, allowed in (e.get("verdicts") or {}).items():
        if verdicts.get(int(sig)) not in allowed:
            failed.append(f"verdict for signature {sig}")
    rule = e.get("proposals")
    if rule is not None:
        made = proposals.listing(store, None)
        limit = 0 if rule == "none" else int((rule or {}).get("max", 0))
        files = set((rule or {}).get("files") or []) if isinstance(rule, dict) else set()
        if len(made) > limit or (files and any(p.file not in files for p in made)):
            failed.append("proposals outside what was expected")
    return failed


def run_case(
    fx: Fixture,
    env: Mapping[str, str],
    post: ai.Transport | None,
    clock: Callable[[], float],
    real: Store | None = None,
) -> Case:
    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder) / "baseline.db")
        try:
            specs = agentrun.load(env)
            spec = specs.get(fx.agent)
            if spec is None:
                return Case(fx.id, False, ["the agent is not defined"], "failed", 0, 0, 0, 0)
            spec = replace(spec, enabled=True)
            settings = ai.load_settings(env)
            settings = replace(settings, enabled=True, features={**settings.features, "agents": True})
            systems, findings = _seed(store, fx, now)
            doc = {
                "run_ok": True,
                "generated_at": iso(now),
                "daily_generated_at": iso(now),
                "stories": [],
                "systems": [],
                **fx.status,
            }
            from brief.settings import Settings

            app = Settings(state_dir=Path(folder), announce_setup=False)
            began = clock()
            r = agentrun.run(store, spec, fx.subject, "evaluation", settings, app, lambda: (doc, systems, []), now,
                             False, post, env, clock, findings)  # fmt: skip
            ms = int((clock() - began) * 1000)
            failed = _checks(fx, r, store)
            if real is not None:  # the calls are real: count them against the day's budget
                with store.read() as db:
                    calls = [dict(c) for c in db.execute("SELECT * FROM ai_call WHERE task LIKE 'agent:%'")]
                with real.write() as tx:
                    for c in calls:
                        tx.add_ai_call(c["ts"], "agent_eval", c["provider"], c["model"], c["locality"], c["outcome"],
                                       c["latency_ms"], c["tokens_in"], c["tokens_out"], c["detail"])  # fmt: skip
            return Case(fx.id, not failed, failed, r.outcome, r.kept, r.dropped, r.steps, ms)
        finally:
            store.close()


# ---------------------------------------------------------------- scoring


def fingerprint(env: Mapping[str, str] | None, name: str) -> str:
    """What was tested: the agent's prompt, tools, limits, chain, output, and each chain provider's model."""
    spec = agentrun.load(env).get(name)
    if spec is None:
        return ""
    settings = ai.load_settings(env)
    try:
        prompt = config_file(spec.prompt, env).read_text(encoding="utf-8")
    except OSError:
        prompt = ""
    providers = [
        [
            p.id,
            p.type,
            p.base_url,
            p.model,
            p.locality,
            p.max_output_tokens,
            p.context_tokens,
            p.disable_thinking,
        ]
        for p in (settings.providers.get(c) for c in spec.chain)
        if p is not None
    ]
    blob = json.dumps([prompt, sorted(spec.tools), spec.chain, spec.output, spec.max_steps, spec.max_tool_calls,
                       spec.max_tokens_per_run, spec.timeout_s, providers], sort_keys=True)  # fmt: skip
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _model_name(env, name: str) -> str:
    spec = agentrun.load(env).get(name)
    settings = ai.load_settings(env)
    first = next(
        (settings.providers[c] for c in (spec.chain if spec else ()) if c in settings.providers), None
    )
    return first.model if first else ""


def _aggregate(agent: str, label: str, env, cases: list[Case]) -> Score:
    spec = agentrun.load(env).get(agent)
    kept, dropped = sum(c.kept for c in cases), sum(c.dropped for c in cases)
    citation = kept / (kept + dropped) if kept + dropped else 1.0
    pass_rate = sum(c.passed for c in cases) / len(cases) if cases else 0.0
    avg_steps = sum(c.steps for c in cases) / len(cases) if cases else 0.0
    efficient = spec is not None and avg_steps <= 0.75 * spec.max_steps
    score = round(60 * pass_rate + 30 * citation + 10 * (1.0 if efficient else 0.5), 1)
    return Score(agent, label, fingerprint(env, agent), _model_name(env, agent), cases, score, pass_rate, citation,
                 avg_steps, sum(c.ms for c in cases) / len(cases) if cases else 0.0)  # fmt: skip


def _save(store: Store, s: Score, note: str = "") -> int:
    detail = json.dumps([{"id": c.id, "passed": c.passed, "failed": c.failed, "outcome": c.outcome, "kept": c.kept,
                          "dropped": c.dropped, "steps": c.steps} for c in s.cases])  # fmt: skip
    with store.write() as tx:
        cur = tx.db.execute(
            "INSERT INTO agent_eval (agent, started, label, fingerprint, model, cases, passed, score, citation_rate, "
            "avg_steps, avg_ms, detail, flagged, baseline_score, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (s.agent, iso(datetime.now(UTC)), s.label, s.fingerprint, s.model, len(s.cases), sum(c.passed for c in s.cases),
             s.score, s.citation_rate, s.avg_steps, s.avg_ms, detail, int(s.flagged), s.baseline_score, note or None),
        )  # fmt: skip
        s.eval_id = int(cur.lastrowid)
        return s.eval_id


def _budget_check(store: Store, settings: ai.Settings, planned_calls: int) -> None:
    calls, _ = ai.usage_today(store, datetime.now(UTC))
    if calls + planned_calls > settings.max_calls_per_day:
        raise EvalError(
            f"Not enough of today's model-call budget ({calls} of {settings.max_calls_per_day} used, up to {planned_calls} needed)."
        )


def evaluate(
    store: Store,
    names: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    label: str = "manual",
    overrides: Mapping[str, str] | None = None,
    post: ai.Transport | None = None,
    clock: Callable[[], float] = time.monotonic,
    save: bool = True,
) -> list[Score]:
    """Run the fixtures of the named agents (all that have any) and score them. `overrides` are config texts that
    replace the real ones for this run only (a candidate prompt, model, or limit)."""
    fixtures = load_fixtures(env)
    with tempfile.TemporaryDirectory() as folder:
        scratch = scratch_env(env, overrides or {}, Path(folder))
        specs = agentrun.load(scratch)
        wanted = [n for n in (names or specs) if n in specs and any(f.agent == n for f in fixtures.values())]
        planned = sum(specs[n].max_steps for n in wanted for f in fixtures.values() if f.agent == n)
        _budget_check(store, ai.load_settings(scratch), planned)
        out = []
        for name in wanted:
            cases = [run_case(f, scratch, post, clock, store) for f in fixtures.values() if f.agent == name]
            s = _aggregate(name, label, scratch, cases)
            if save:
                _save(store, s)
            out.append(s)
        return out


# ---------------------------------------------------------------- the gate on a change


def _affected(env, scratch, specs_now: Mapping[str, agentrun.Spec]) -> list[str]:
    return [n for n, s in specs_now.items() if s.enabled and fingerprint(env, n) != fingerprint(scratch, n)]


def _cached(store: Store, agent: str, fp: str) -> Score | None:
    with store.read() as db:
        row = db.execute(
            "SELECT * FROM agent_eval WHERE agent = ? AND fingerprint = ? AND started >= ? AND flagged = 0 "
            "ORDER BY eval_id DESC LIMIT 1",
            (agent, fp, iso(datetime.now(UTC) - timedelta(days=CACHE_DAYS))),
        ).fetchone()
    if row is None:
        return None
    return Score(agent, row["label"], fp, row["model"] or "", [], row["score"], row["passed"] / max(row["cases"], 1),
                 row["citation_rate"], row["avg_steps"], row["avg_ms"], row["eval_id"])  # fmt: skip


def gate(
    store: Store,
    name: str,
    new_text: str,
    user: str,
    env: Mapping[str, str] | None = None,
    post: ai.Transport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """A reason to hold back saving `name` as `new_text`, or None. Never raises: evaluation trouble lets the
    change through (and is logged), because a broken test must not lock the configuration."""
    if name not in GATED:
        return None
    try:
        if not ai.feature_on(ai.load_settings(env), "agents"):
            return None
        with tempfile.TemporaryDirectory() as folder:
            scratch = scratch_env(env, {name: new_text}, Path(folder))
            affected = _affected(env, scratch, agentrun.load(env))
        fixtures = load_fixtures(env)
        affected = [n for n in affected if any(f.agent == n for f in fixtures.values())]
        if not affected:
            return None
        lines, flagged = [], False
        for agent in affected:
            base = _cached(store, agent, fingerprint(env, agent))
            if base is None:
                base = evaluate(store, [agent], env, "baseline", None, post, clock)[0]
            cand = evaluate(store, [agent], env, "candidate", {name: new_text}, post, clock, save=False)[0]
            cand.baseline_score = base.score
            cand.flagged = cand.score < base.score - TOLERANCE
            _save(store, cand, note=f"change to {name} by {user}")
            flagged = flagged or cand.flagged
            lines.append(
                f"{agent}: {base.score:.1f} now, {cand.score:.1f} with this change ({sum(c.passed for c in cand.cases)} of {len(cand.cases)} cases passed)"
            )
        if not flagged:
            return None
        return (
            "This change makes an agent worse on its test cases, so it was not saved. "
            + "; ".join(lines)
            + ". To save it anyway, tick 'Save even though the score is lower' and save again."
        )
    except EvalError as exc:
        log("agent_eval_skipped", reason=str(exc)[:120])
        return None
    except Exception as exc:  # a failing test run must not block a config change
        log("agent_eval_failed", error=f"{type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------- what the AI page shows


def latest(store: Store) -> list[dict]:
    """The newest non-candidate score per agent."""
    with store.read() as db:
        rows = db.execute(
            "SELECT e.* FROM agent_eval e JOIN (SELECT agent, MAX(eval_id) m FROM agent_eval WHERE flagged = 0 "
            "GROUP BY agent) x ON x.m = e.eval_id ORDER BY e.agent"
        ).fetchall()
    return [dict(r) for r in rows]


def flagged_changes(store: Store, limit: int = 5) -> list[dict]:
    with store.read() as db:
        return [dict(r) for r in db.execute(
            "SELECT agent, started, score, baseline_score, note FROM agent_eval WHERE flagged = 1 ORDER BY eval_id DESC LIMIT ?",
            (limit,),
        )]  # fmt: skip


def live_metrics(store: Store, days: int = 30) -> dict[str, dict]:
    """Per agent, from real runs: runs, how many were ok, citation rate, steps, seconds, proposals accepted."""
    since = iso(datetime.now(UTC) - timedelta(days=days))
    out: dict[str, dict] = {}
    with store.read() as db:
        for r in db.execute(
            "SELECT agent, COUNT(*) runs, SUM(outcome = 'ok') ok, SUM(kept) kept, SUM(dropped) dropped, "
            "AVG(steps) steps, AVG((julianday(finished) - julianday(started)) * 86400) secs "
            "FROM agent_run WHERE started >= ? AND outcome NOT IN ('off', 'skipped') GROUP BY agent",
            (since,),
        ):
            kept, dropped = r["kept"] or 0, r["dropped"] or 0
            out[r["agent"]] = {
                "runs": r["runs"], "ok": r["ok"] or 0, "steps": r["steps"] or 0, "secs": r["secs"] or 0,
                "citation_rate": kept / (kept + dropped) if kept + dropped else None, "approved": 0, "rejected": 0,
            }  # fmt: skip
        for r in db.execute(
            "SELECT source, status, COUNT(*) n FROM proposal WHERE status IN ('approved', 'rejected') AND created >= ? GROUP BY source, status",
            (since,),
        ):
            agent = str(r["source"]).removeprefix("agent:")
            if agent in out:
                out[agent][r["status"]] = r["n"]
    return out
