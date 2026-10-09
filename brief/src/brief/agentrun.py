"""The agent runtime (docs/guide/10-agents.md): a small tool-calling loop with hard limits, a background queue,
and an output that keeps only what cites evidence.

An agent looks things up with the read-only tools in `agenttools` (the same ones the MCP server offers) and
writes a short note. It never changes anything: a change would be a proposal for a person to approve (Task 28).
What this module enforces, in code and not by prompt:

  - nothing runs unless `ai.yaml` has `enabled: true` and `features.agents: true`, and the agent is `enabled`;
  - every run stops at its step, tool-call, token, and time limits, and each agent has a daily run cap;
  - background runs wait out quiet hours and a busy model server; a person's on-demand run does not;
  - a provider marked `cloud` is used only for an agent named in `allow_cloud_for`, is offered no tool that
    reads device detail, DNS, or raw lines, receives nothing the privacy scan flags, and sees only tool results
    that pass the scan (`agenttools.guard_cloud`);
  - the output is split into statements, and every statement without a valid `[ref:rN]` citation to a tool
    result that succeeded is deleted before anything is shown or stored;
  - every model call goes to the call log, every tool call and run is recorded, none with its content.
"""

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from brief import agentout, ai, lifecycle
from brief.agentout import check_citations  # noqa: F401  (re-exported)
from brief.agenttools import TOOLS, Context, call_tool
from brief.aiguard import privacy_leak, private_terms
from brief.config import ConfigError, config_file, load_mapping
from brief.jsonlog import log
from brief.mcptokens import SCOPES, SENSITIVE, Client
from brief.routing import Priorities
from brief.settings import Settings
from brief.store import Store, iso

LIMITS = {  # key: (lowest, highest) accepted in agents.yaml
    "max_steps": (1, 30),
    "max_tool_calls": (1, 60),
    "max_tokens_per_run": (500, 200_000),
    "timeout_s": (10, 3600),
    "max_runs_per_day": (1, 500),
}
BUSY_WINDOW = timedelta(minutes=2)  # a model call this recent by anything else means the server is busy
MAX_QUEUE = 50
MAX_SUBJECT = 600
CITE_RULES = (
    "\n\nLocked rules from the product: cite every factual statement with the id of the tool result it came "
    "from, written like [ref:r2] (each tool result starts with its id). Statements without a valid citation "
    "are deleted. You have at most {steps} steps and {calls} tool calls. You can only read; you cannot change "
    "anything."
)


@dataclass(frozen=True)
class Spec:
    name: str
    enabled: bool
    tools: tuple[str, ...]
    chain: tuple[str, ...]
    prompt: str
    output: str = "text"
    trigger: Mapping = field(default_factory=dict)
    max_steps: int = 8
    max_tool_calls: int = 12
    max_tokens_per_run: int = 12000
    timeout_s: int = 300
    max_runs_per_day: int = 20


@dataclass
class Result:
    outcome: str
    text: str = ""
    run_id: int | None = None
    provider: str = ""
    model: str = ""
    steps: int = 0
    tool_calls: int = 0
    kept: int = 0
    dropped: int = 0
    detail: str = ""


# ---------------------------------------------------------------- agents.yaml


def _output(name: str, value) -> str:
    value = str(value or "text")
    if value not in agentout.OUTPUTS:
        raise ConfigError(f"agents.yaml: agents.{name}.output must be one of {', '.join(agentout.OUTPUTS)}")
    return value


def parse(data: Mapping) -> dict[str, Spec]:
    """The agents in agents.yaml with their defaults filled in. Raises ConfigError for anything unusable."""
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, Mapping):
        raise ConfigError("agents.yaml: defaults must be a mapping")
    agents = data.get("agents") or {}
    if not isinstance(agents, Mapping):
        raise ConfigError("agents.yaml: agents must be a mapping of name to settings")
    out: dict[str, Spec] = {}
    for name, raw in agents.items():
        if not isinstance(raw, Mapping):
            raise ConfigError(f"agents.yaml: agents.{name} must be a mapping")
        if not re.match(r"^[a-z][a-z0-9_]{0,30}$", str(name)):
            raise ConfigError(f"agents.yaml: {name!r} is not a usable agent name")
        merged = {**defaults, **raw}
        if not isinstance(merged.get("enabled", False), bool):
            raise ConfigError(f"agents.yaml: agents.{name}.enabled must be true or false")
        tools = merged.get("tools") or []
        if not isinstance(tools, list) or not tools or not all(isinstance(t, str) for t in tools):
            raise ConfigError(f"agents.yaml: agents.{name}.tools must be a list of tool names")
        unknown = [t for t in tools if t not in TOOLS]
        if unknown:
            raise ConfigError(f"agents.yaml: agents.{name}.tools: no such tool {unknown[0]!r}")
        chain = merged.get("chain") or []
        if not isinstance(chain, list) or not all(isinstance(c, str) for c in chain):
            raise ConfigError(f"agents.yaml: agents.{name}.chain must be a list of provider ids")
        limits = {}
        for key, (low, high) in LIMITS.items():
            value = merged.get(key, getattr(Spec, key) if hasattr(Spec, key) else low)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ConfigError(
                    f"agents.yaml: agents.{name}.{key} must be a whole number from {low} to {high}"
                )
            limits[key] = value
        if merged.get("require_citations", True) is not True:
            raise ConfigError("agents.yaml: require_citations is locked on and cannot be switched off")
        out[str(name)] = Spec(
            name=str(name),
            enabled=bool(merged.get("enabled", False)),
            tools=tuple(dict.fromkeys(tools)),
            chain=tuple(chain),
            prompt=str(merged.get("prompt") or f"prompts/agent_{name}.md"),
            output=_output(name, merged.get("output")),
            trigger=merged.get("trigger") or {},
            **limits,
        )
    return out


def load(env: Mapping[str, str] | None = None) -> dict[str, Spec]:
    """The agents, or none when the file is unusable (the config guard reports it; nothing runs meanwhile)."""
    try:
        return parse(load_mapping(config_file("agents.yaml", env)))
    except (ConfigError, OSError) as exc:
        log("config_error", file="agents.yaml", error=str(exc), kept="no agents")
        return {}


# ---------------------------------------------------------------- talking to a provider with tools


def chat(
    p: ai.Provider, messages: list[dict], tools: list[dict], post: ai.Transport
) -> tuple[dict, int, int]:
    """One OpenAI-compatible chat completion with tools: (the assistant message, tokens in, tokens out)."""
    headers = {"Authorization": f"Bearer {p.api_key}"} if p.api_key else {}
    body = {"model": p.model, "messages": messages, "max_tokens": p.max_output_tokens, "temperature": 0.2}
    if tools:
        body["tools"], body["tool_choice"] = tools, "auto"
    if p.disable_thinking:
        body["reasoning_effort"] = "none"
        body["chat_template_kwargs"] = {"enable_thinking": False}
    reply = post(f"{p.base_url}/chat/completions", headers, body, p.timeout_s)
    try:
        message = reply["choices"][0]["message"]
        usage = reply.get("usage") or {}
        return message, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ai.ProviderError("error", "unexpected reply shape") from exc


def tool_specs(names: list[str]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.listing()["inputSchema"],
            },
        }
        for t in (TOOLS[n] for n in names)
    ]


# ---------------------------------------------------------------- gates


def runs_today(store: Store, agent: str, now: datetime) -> int:
    with store.read() as db:
        return db.execute(
            "SELECT COUNT(*) FROM agent_run WHERE agent = ? AND started >= ? AND outcome NOT IN ('off', 'skipped')",
            (agent, iso(now - timedelta(days=1))),
        ).fetchone()[0]


def server_busy(store: Store, p: ai.Provider, now: datetime) -> bool:
    """True if anything other than an agent called this provider within the last two minutes. (The product has
    no view of the GPU itself; its own recent calls are the signal it can see.)"""
    with store.read() as db:
        return (
            db.execute(
                "SELECT 1 FROM ai_call WHERE provider = ? AND ts >= ? AND task NOT LIKE 'agent:%' LIMIT 1",
                (p.id, iso(now - BUSY_WINDOW)),
            ).fetchone()
            is not None
        )


def in_quiet_hours(now: datetime, env: Mapping[str, str] | None = None) -> bool:
    policy = lifecycle.Policy.from_priorities(Priorities.load(env))
    return lifecycle.in_quiet_hours(now, policy.quiet_start, policy.quiet_end)


def gate(
    store: Store, spec: Spec, settings: ai.Settings, now: datetime, background: bool, env=None
) -> str | None:
    """Why this agent may not run now, or None. A reason starting with `wait:` means try again later."""
    if not ai.feature_on(settings, "agents"):
        return "off: AI or the agents feature is switched off"
    if not spec.enabled:
        return "off: this agent is switched off"
    if runs_today(store, spec.name, now) >= spec.max_runs_per_day:
        return "skipped: the daily run cap is used up"
    if background and settings.agents_off_in_quiet_hours and in_quiet_hours(now, env):
        return "wait: quiet hours"
    return None


# ---------------------------------------------------------------- one run


def _record_step(db, run_id: int, n: int, result: str | None, tool: str, outcome: str, ms: int) -> None:
    db.execute(
        "INSERT INTO agent_step (run_id, n, result, tool, outcome, ms) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, n, result, tool, outcome, ms),
    )


def run(
    store: Store,
    spec: Spec,
    subject: str,
    trigger: str,
    settings: ai.Settings,
    app_settings: Settings,
    status: Callable[[], tuple[dict, object, list]],
    now: datetime | None = None,
    background: bool = False,
    post: ai.Transport | None = None,
    env: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Result:
    """Run one agent on one subject and record it. Never raises."""
    now = now or datetime.now(UTC)
    post = post or ai._post
    subject = subject.strip()[:MAX_SUBJECT]
    why = gate(store, spec, settings, now, background, env)
    if why:
        outcome = why.split(":")[0]
        if outcome != "wait":
            _finish(store, _start(store, spec, subject, trigger, now), now, Result(outcome, detail=why))
        return Result(outcome, detail=why)
    run_id = _start(store, spec, subject, trigger, now)
    try:
        result = _loop(
            store, spec, subject, settings, app_settings, status, now, background, post, env, clock, run_id
        )
    except Exception as exc:  # an agent is never worth an error page
        log("agent_failed", agent=spec.name, error=type(exc).__name__)
        result = Result("failed", detail=f"internal error: {type(exc).__name__}")
    if result.outcome == "wait":  # nothing happened: leave no trace in the run list, the job stays queued
        with store.write() as tx:
            tx.db.execute("DELETE FROM agent_run WHERE run_id = ?", (run_id,))
        return result
    result.run_id = run_id
    _finish(store, run_id, datetime.now(UTC), result)
    return result


def _start(store: Store, spec: Spec, subject: str, trigger: str, now: datetime) -> int:
    with store.write() as tx:
        cur = tx.db.execute(
            "INSERT INTO agent_run (agent, trigger, subject, started, outcome) VALUES (?, ?, ?, ?, 'failed')",
            (spec.name, trigger, subject, iso(now)),
        )
        return int(cur.lastrowid)


def _finish(store: Store, run_id: int, now: datetime, r: Result) -> None:
    with store.write() as tx:
        tx.db.execute(
            "UPDATE agent_run SET finished = ?, provider = ?, model = ?, outcome = ?, steps = ?, tool_calls = ?, "
            "kept = ?, dropped = ?, output = ?, detail = ? WHERE run_id = ?",
            (iso(now), r.provider or None, r.model or None, r.outcome, r.steps, r.tool_calls, r.kept, r.dropped,
             r.text or None, r.detail[:200] or None, run_id),
        )  # fmt: skip


def _tokens(store: Store, run_id: int, t_in: int, t_out: int) -> None:
    with store.write() as tx:
        tx.db.execute(
            "UPDATE agent_run SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ? WHERE run_id = ?",
            (t_in, t_out, run_id),
        )


def _loop(store, spec, subject, settings, app, status, now, background, post, env, clock, run_id) -> Result:
    system = ai.system_prompt(spec.name, {"prompt": spec.prompt}, env)
    if system is None:
        return Result("failed", detail="the agent's prompt file is not readable")
    system += CITE_RULES.format(steps=spec.max_steps, calls=spec.max_tool_calls)
    task = f"agent:{spec.name}"
    terms = private_terms(store, env)
    last = "no provider in the chain could be used"
    for step in spec.chain:
        p = settings.providers.get(step)
        if p is None:
            continue
        if p.type != "openai_compatible":
            ai._record(store, now, task, p, "skipped", detail="agents need OpenAI-compatible tool calling")
            last = "no provider with tool calling"
            continue
        cloud = p.locality == "cloud"
        if cloud and spec.name not in settings.allow_cloud_for:
            ai._record(store, now, task, p, "skipped", detail="cloud not allowed for this agent")
            continue
        if background and settings.agents_when_idle and server_busy(store, p, now):
            return Result("wait", detail="wait: the model server is busy")
        names = [t for t in spec.tools if not (cloud and TOOLS[t].scope in SENSITIVE)]
        if not names:
            ai._record(
                store, now, task, p, "skipped", detail="every tool of this agent reads device or log detail"
            )
            continue
        user = agentout.user_message(spec.output, subject)
        if cloud:
            leak = privacy_leak(system + "\n" + user, terms)
            if leak:
                ai._record(store, now, task, p, "privacy", detail=f"blocked before sending: {leak}")
                continue
        scopes = frozenset(TOOLS[t].scope for t in names) & (
            frozenset(SCOPES) - (frozenset(SENSITIVE) if cloud else frozenset())
        )
        client = Client("agent", f"{spec.name}", scopes, "cloud" if cloud else "lan")
        ctx = Context(store, app, client, status, source="agent", run_id=run_id)
        try:
            return _converse(
                store,
                spec,
                p,
                system,
                user,
                names,
                ctx,
                settings,
                now,
                post,
                clock,
                run_id,
                task,
                subject,
                env,
            )
        except ai.ProviderError as exc:
            ai._record(store, now, task, p, exc.outcome, detail=exc.detail[:120])
            last = f"{p.id}: {exc.detail}"[:160]
    return Result("failed", detail=last)


def _converse(
    store, spec, p, system, user, names, ctx, settings, now, post, clock, run_id, task, subject, env
) -> Result:
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    allowed = set(names)
    started = clock()
    r = Result("limit", provider=p.id, model=p.model)
    evidence: dict[str, str] = {}  # result id -> tool, for results that succeeded
    ctx.evidence = evidence
    spent = 0
    final: str | None = None
    for turn in range(spec.max_steps):
        last_turn = turn == spec.max_steps - 1 or r.tool_calls >= spec.max_tool_calls
        if clock() - started > spec.timeout_s:
            r.detail = "the time limit"
            break
        if spent >= spec.max_tokens_per_run:
            r.detail = "the token limit"
            break
        calls_today, _ = ai.usage_today(store, now)
        if calls_today >= settings.max_calls_per_day:
            ai._record(store, now, task, p, "budget", detail="daily budget used up")
            r.detail = "the daily call budget"
            break
        if last_turn:
            messages.append(
                {"role": "user", "content": "Stop calling tools now and write the note from what you have."}
            )
        began = clock()
        message, t_in, t_out = chat(p, messages, [] if last_turn else tool_specs(names), post)
        spent += t_in + t_out
        _tokens(store, run_id, t_in, t_out)
        ai._record(
            store,
            now,
            task,
            p,
            "ok",
            latency_ms=int((clock() - began) * 1000),
            tokens_in=t_in,
            tokens_out=t_out,
        )
        r.steps += 1
        calls = message.get("tool_calls") or []
        if not calls or last_turn:
            final = str(message.get("content") or "")
            break
        messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
        for call in calls:
            r.tool_calls += 1
            number = f"r{r.tool_calls}"
            fn = (call.get("function") or {}) if isinstance(call, dict) else {}
            name, raw = str(fn.get("name") or ""), fn.get("arguments") or "{}"
            outcome, text = _run_tool(ctx, name, raw, allowed, r.tool_calls <= spec.max_tool_calls)
            with store.write() as tx:
                _record_step(tx.db, run_id, r.tool_calls, number, name[:60], outcome, 0)
            if outcome == "ok":
                evidence[number] = name
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or number),
                    "content": f"[{number}] {text}",
                }
            )
    if final is None:
        return r
    kept_text, r.kept, r.dropped = agentout.finish(
        store, spec, subject, final, evidence, run_id, env, datetime.now(UTC), systems=ctx.status()[1]
    )
    r.text = kept_text
    r.outcome = "ok" if r.kept else "no_evidence"
    r.detail = "" if r.kept else "nothing in the reply cited a tool result"
    return r


def _run_tool(ctx: Context, name: str, raw, allowed: set[str], within_limit: bool) -> tuple[str, str]:
    """(outcome, text for the model) for one requested tool call. A refusal is told to the model plainly."""
    if not within_limit:
        return "over limit", "No more tool calls are allowed in this run."
    if name not in allowed:
        return "unknown tool", f"There is no tool called {name[:40]!r} for you."
    try:
        args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        if not isinstance(args, dict):
            raise ValueError
    except (ValueError, TypeError):
        return "error", "The arguments were not a JSON object."
    result = call_tool(ctx, name, args)
    text = result["content"][0]["text"]
    if result["isError"]:
        return ("withheld" if text.startswith("Withheld") else "denied" if "scope" in text else "error"), text
    return "ok", text


# ---------------------------------------------------------------- the background queue


def enqueue(store: Store, agent: str, subject: str, trigger: str, by: str = "") -> int | None:
    """Queue a run. Returns the job id, or None if the same agent and subject is already waiting or the queue
    is full."""
    subject = subject.strip()[:MAX_SUBJECT]
    with store.write() as tx:
        waiting = tx.db.execute("SELECT agent, subject FROM agent_job WHERE status = 'queued'").fetchall()
        if len(waiting) >= MAX_QUEUE or any(w["agent"] == agent and w["subject"] == subject for w in waiting):
            return None
        cur = tx.db.execute(
            "INSERT INTO agent_job (agent, subject, trigger, created, requested_by) VALUES (?, ?, ?, ?, ?)",
            (agent, subject, trigger, iso(datetime.now(UTC)), by),
        )
        return int(cur.lastrowid)


def work_one(
    store: Store,
    app_settings: Settings,
    status: Callable[[], tuple[dict, object, list]],
    now: datetime | None = None,
    post: ai.Transport | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Take the oldest queued job and run it if the rules allow. Returns what happened, or None if the queue is
    empty. A job that must wait (quiet hours, busy server) stays queued; one for an agent that is off is
    marked skipped. Called by the worker after each cycle, so a run never delays ingest by more than itself."""
    now = now or datetime.now(UTC)
    post = post or ai._post
    settings = ai.load_settings(env)
    if not ai.feature_on(settings, "agents"):
        return None
    specs = load(env)
    with store.read() as db:
        jobs = db.execute(
            "SELECT * FROM agent_job WHERE status = 'queued' ORDER BY created, job_id"
        ).fetchall()
    for job in jobs:
        spec = specs.get(job["agent"])
        background = job["trigger"] != "on_demand"
        if spec is None:
            _mark(store, job["job_id"], "skipped", None)
            continue
        result = run(store, spec, job["subject"] or "", job["trigger"], settings, app_settings, status, now,
                     background, post, env)  # fmt: skip
        if result.outcome == "wait":
            continue  # try the next job (another agent may be allowed), leave this one queued
        _mark(
            store, job["job_id"], "skipped" if result.outcome in ("off", "skipped") else "done", result.run_id
        )
        return f"{spec.name}: {result.outcome}"
    return None


def _mark(store: Store, job_id: int, status: str, run_id: int | None) -> None:
    with store.write() as tx:
        tx.db.execute(
            "UPDATE agent_job SET status = ?, run_id = ? WHERE job_id = ?", (status, run_id, job_id)
        )


# ---------------------------------------------------------------- reading it back


def recent_runs(store: Store, limit: int = 20) -> list[dict]:
    with store.read() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT run_id, agent, trigger, subject, started, outcome, provider, steps, tool_calls, kept, dropped, "
                "tokens_in + tokens_out AS tokens, detail FROM agent_run ORDER BY run_id DESC LIMIT ?",
                (limit,),
            )
        ]


def queue(store: Store) -> list[dict]:
    with store.read() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT job_id, agent, subject, trigger, created, status FROM agent_job "
                "WHERE status IN ('queued', 'running') ORDER BY created"
            )
        ]


def ask_history(store: Store, limit: int = 5) -> tuple[list[dict], list[dict]]:
    """(recent Ask-the-lab runs with their answers, questions still waiting)."""
    with store.read() as db:
        done = [
            dict(r)
            for r in db.execute(
                "SELECT run_id, started, subject, outcome, output, detail FROM agent_run "
                "WHERE agent = 'ask_the_lab' ORDER BY run_id DESC LIMIT ?",
                (limit,),
            )
        ]
        waiting = [
            dict(r)
            for r in db.execute(
                "SELECT job_id, created, subject FROM agent_job WHERE agent = 'ask_the_lab' "
                "AND status IN ('queued', 'running') ORDER BY created"
            )
        ]
    return done, waiting


def verdicts(store: Store, limit: int = 20) -> list[dict]:
    with store.read() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT v.sig_id, v.verdict, v.reason, v.created, s.app FROM agent_verdict v "
                "LEFT JOIN signature s ON s.sig_id = v.sig_id ORDER BY v.created DESC, v.sig_id LIMIT ?",
                (limit,),
            )
        ]
