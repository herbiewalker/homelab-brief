"""The AI provider layer: a prose rewrite of the digest, always optional, never in the critical path.

Rules decide what happened; a model may only put the existing findings into words. So:

- every task has a chain from brief/ai.yaml that ends in "template" (no model), and any error, timeout,
  budget limit, privacy block, or failed validation moves to the next step;
- a failed run is never sent to a model: "No data today" is the template's job and nothing may soften it;
- a cloud provider receives system-level facts only (no host names, addresses, log text, or story titles);
- the output is checked against the input: a host or address the digest did not mention fails it;
- every attempt is logged (provider, model, latency, tokens, outcome, never the content).

Providers speak OpenAI-compatible Chat Completions (Ollama, llama.cpp, vLLM, LM Studio, a LiteLLM gateway,
hosted services), plus an optional native Anthropic adapter. Nothing here names a vendor or a model.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from brief.aiguard import DEVICE_KINDS, known_names, privacy_leak, private_terms
from brief.config import ConfigError, config_file, load_mapping
from brief.digest import Digest
from brief.jsonlog import log
from brief.render_md import digest_markdown
from brief.store import Store, iso

TEMPLATE = "template"
CHARS_PER_TOKEN = 3  # a pessimistic estimate, so the input is trimmed early rather than late
MAX_HEADLINE = 120
ANTHROPIC_URL = "https://api.anthropic.com"
Transport = Callable[[str, dict, dict, float], dict]  # url, headers, json body, timeout -> json reply


@dataclass(frozen=True)
class Provider:
    id: str
    type: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    locality: str = "lan"
    timeout_s: float = 60
    max_output_tokens: int = 1200
    context_tokens: int = 8192
    disable_thinking: bool = False


@dataclass
class Prose:
    headline: str
    paragraphs: list[str]
    provider: str
    model: str


@dataclass
class Settings:
    enabled: bool = False
    features: dict = field(default_factory=dict)
    providers: dict[str, Provider] = field(default_factory=dict)
    tasks: dict = field(default_factory=dict)
    max_calls_per_day: int = 50
    max_cloud_tokens_per_day: int = 200000
    allow_cloud_for: tuple[str, ...] = ()
    validation: str = "strict"
    agents_when_idle: bool = True  # background agents wait while the model server is busy
    agents_off_in_quiet_hours: bool = True


class ProviderError(Exception):
    """A model call that did not give a usable answer; `outcome` is the label for the call log."""

    def __init__(self, outcome: str, detail: str):
        super().__init__(detail)
        self.outcome, self.detail = outcome, detail


# ---------------------------------------------------------------- config


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """ai.yaml as settings. Anything unusable (a bad file, a provider without an address) leaves AI off or
    that provider out: the template brief does not depend on any of it."""
    env = os.environ if env is None else env
    try:
        data = load_mapping(config_file("ai.yaml", env))
    except (ConfigError, OSError) as exc:
        log("config_error", file="ai.yaml", error=str(exc), kept="ai off")
        return Settings()
    guard = data.get("guardrails") or {}
    providers = {}
    for raw in data.get("providers") or []:
        if not isinstance(raw, dict) or not raw.get("enabled") or not raw.get("id"):
            continue
        base = str(raw.get("base_url") or "").strip().rstrip("/")
        if (not base and raw.get("type") != "anthropic") or "<" in base or not raw.get("model"):
            continue
        key_var = str(raw.get("api_key_var") or "")
        providers[str(raw["id"])] = Provider(
            id=str(raw["id"]),
            type=str(raw.get("type") or "openai_compatible"),
            base_url=base,
            api_key=env.get(key_var, "") if key_var else "",
            model=str(raw["model"]),
            locality="cloud" if raw.get("locality") == "cloud" else "lan",
            timeout_s=float(raw.get("timeout_s") or 60),
            max_output_tokens=int(raw.get("max_output_tokens") or 1200),
            context_tokens=int(raw.get("context_tokens") or 8192),
            disable_thinking=bool(raw.get("disable_thinking")),
        )
    return Settings(
        enabled=bool(data.get("enabled")),
        features={k: bool(v) for k, v in (data.get("features") or {}).items()},
        providers=providers,
        tasks=data.get("tasks") or {},
        max_calls_per_day=int(guard.get("max_calls_per_day", 50)),
        max_cloud_tokens_per_day=int(guard.get("max_cloud_tokens_per_day", 200000)),
        allow_cloud_for=tuple(guard.get("allow_cloud_for") or ()),
        validation="lenient" if guard.get("output_validation") == "lenient" else "strict",
        agents_when_idle=guard.get("run_agents_only_when_gpu_idle", True) is not False,
        agents_off_in_quiet_hours=guard.get("quiet_hours_no_agents", True) is not False,
    )


# ---------------------------------------------------------------- what the model is given


def _lan_input(d: Digest, day: str, budget_chars: int) -> str:
    """The digest as Markdown, with the lowest-ranked stories dropped until it fits the context."""
    stories = list(d.stories)
    while True:
        text = "\n".join(digest_markdown(_with(d, stories), day))
        if len(text) <= budget_chars or not stories:
            return text[:budget_chars]
        stories.pop()


def _with(d: Digest, stories: list) -> Digest:
    clone = Digest(**{**d.__dict__, "stories": stories})
    return clone


def _cloud_input(d: Digest, day: str) -> str:
    """System-level facts only: no host names or addresses, no log text, no story titles."""
    kinds: dict[str, int] = {}
    for s in d.stories:
        if s.kind in DEVICE_KINDS:
            continue  # one household device's behaviour: never leaves the LAN, not even as a count
        kinds[s.kind] = kinds.get(s.kind, 0) + 1
    lines = [f"Digest for {day}. Run ok.", "", "Systems:"]
    lines += [f"- {s.name}: {s.state} ({s.summary})" for s in d.systems]
    lines += ["", f"Stories: {len(d.stories)}"]
    lines += [f"- {n} of kind {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])]
    if d.developing:
        lines += ["", "Developing:", *[f"- open {x['days']} days, {x['trend']}" for x in d.developing[:10]]]
    lines += ["", *[f"Note: {q}" for q in d.quiet]]
    return "\n".join(lines)


def build_input(d: Digest, day: str, provider: Provider, prompt_chars: int = 0) -> str:
    if provider.locality == "cloud":
        return _cloud_input(d, day)
    room = provider.context_tokens - provider.max_output_tokens
    return _lan_input(d, day, max(room * CHARS_PER_TOKEN - prompt_chars, 1000))


# ---------------------------------------------------------------- checking what comes back

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[a-z][a-z0-9-]*(?:\.[a-z0-9-]+)+\b", re.IGNORECASE)
_MAC = re.compile(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", re.IGNORECASE)
_HEADING = re.compile(r"^[#*\s]*(headline|top stories|developing|checked and clean)[:*\s]*$", re.IGNORECASE)


def clean(text: str) -> str:
    """The reply without any reasoning block a model left in."""
    return _THINK.sub("", text).strip()


def unknown_terms(text: str, source: str, known_hosts: set[str]) -> set[str]:
    """Hosts and addresses the output names that the input never mentioned (the model made them up)."""
    seen = source.lower()
    bad = {m.group(0) for m in _ADDRESS.finditer(text) if m.group(0).lower() not in seen}
    bad |= {m.group(0) for m in _MAC.finditer(text) if m.group(0).lower() not in seen}
    low = text.lower()
    for host in known_hosts:
        name = host.lower()
        if len(name) >= 3 and name not in seen and re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", low):
            bad.add(host)
    return bad


def parse(text: str, source: str, known_hosts: set[str], mode: str) -> tuple[str, list[str]]:
    """(headline, paragraphs) from a reply, or ProviderError("invalid", why). In lenient mode a paragraph
    that names an unknown host or address is dropped; in strict mode it rejects the whole reply."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", clean(text)) if b.strip()]
    lines = [ln.strip() for b in blocks for ln in b.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not _HEADING.match(ln)]
    if not lines:
        raise ProviderError("invalid", "empty reply")
    headline = re.sub(r"^[#*\s]+|[*\s]+$", "", re.sub(r"^headline:\s*", "", lines[0], flags=re.I))
    if not headline or len(headline) > MAX_HEADLINE:
        raise ProviderError("invalid", "the headline is empty or too long")
    body = [b for b in blocks if not _HEADING.match(b)]
    body = [b for b in body if b.strip("#* \n") != lines[0].strip("#* \n")]
    kept = []
    for block in [headline, *body]:
        bad = unknown_terms(block, source, known_hosts)
        if bad and mode == "strict":
            raise ProviderError("invalid", f"names something not in the digest: {sorted(bad)[0]}")
        if not bad:
            kept.append(block)
    if len(kept) < 1 or kept[0] != headline:
        raise ProviderError("invalid", "the headline names something not in the digest")
    return headline, kept[1:]


# ---------------------------------------------------------------- talking to a provider


def _post(url: str, headers: dict, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:  # noqa: S310 (a configured URL)
            return json.loads(reply.read().decode("utf-8"))
    except TimeoutError as exc:
        raise ProviderError("timeout", "no answer in time") from exc
    except urllib.error.HTTPError as exc:
        raise ProviderError("error", f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        outcome = "timeout" if isinstance(reason, TimeoutError) else "error"
        raise ProviderError(outcome, f"{type(reason).__name__}") from exc
    except ValueError as exc:
        raise ProviderError("error", "the reply was not JSON") from exc


def call(p: Provider, system: str, user: str, post: Transport = _post) -> tuple[str, int, int]:
    """One chat completion: (text, tokens in, tokens out)."""
    if p.type == "anthropic":
        headers = {"x-api-key": p.api_key, "anthropic-version": "2023-06-01"}
        body = {
            "model": p.model,
            "max_tokens": p.max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        reply = post(f"{p.base_url or ANTHROPIC_URL}/v1/messages", headers, body, p.timeout_s)
        try:
            text = "".join(b.get("text", "") for b in reply["content"] if b.get("type") == "text")
            usage = reply.get("usage") or {}
            return text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        except (KeyError, TypeError) as exc:
            raise ProviderError("error", "unexpected reply shape") from exc
    headers = {"Authorization": f"Bearer {p.api_key}"} if p.api_key else {}
    body = {
        "model": p.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": p.max_output_tokens,
        "temperature": 0.2,
    }
    if p.disable_thinking:  # servers that do not know these fields ignore them
        body["reasoning_effort"] = "none"
        body["chat_template_kwargs"] = {"enable_thinking": False}
    reply = post(f"{p.base_url}/chat/completions", headers, body, p.timeout_s)
    try:
        text = reply["choices"][0]["message"]["content"] or ""
        usage = reply.get("usage") or {}
        return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError("error", "unexpected reply shape") from exc


# ---------------------------------------------------------------- budgets and the log


def usage_today(store: Store, now: datetime) -> tuple[int, int]:
    """(model calls, cloud tokens) in the last 24 hours."""
    with store.read() as db:
        calls = db.execute(
            "SELECT COUNT(*) FROM ai_call WHERE ts >= ? AND outcome IN ('ok', 'error', 'timeout', 'invalid')",
            (iso(now - timedelta(days=1)),),
        ).fetchone()[0]
        tokens = db.execute(
            "SELECT COALESCE(SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)), 0) FROM ai_call "
            "WHERE ts >= ? AND locality = 'cloud'",
            (iso(now - timedelta(days=1)),),
        ).fetchone()[0]
    return calls, tokens


def _record(store: Store, now: datetime, task: str, p: Provider | None, outcome: str, **fields) -> None:
    with store.write() as tx:
        tx.add_ai_call(
            now,
            task,
            p.id if p else "?",
            p.model if p else None,
            p.locality if p else None,
            outcome,
            **fields,
        )


def known_hosts(store: Store) -> set[str]:
    with store.read() as db:
        return {r["host"] for r in db.execute("SELECT host FROM host")}


# ---------------------------------------------------------------- one chain runner for every task


def read_prompt(path: str, env: Mapping[str, str] | None = None) -> str:
    return config_file(path, env).read_text(encoding="utf-8")


def system_prompt(task: str, cfg: dict, env, **fill: str) -> str | None:
    """The task prompt after its `---` line, with {placeholders} filled; None if the file is unreadable."""
    try:
        text = read_prompt(str(cfg.get("prompt") or f"prompts/{task}.md"), env)
    except OSError as exc:
        log("ai_failed", task=task, error=f"prompt not readable: {type(exc).__name__}")
        return None
    out = text.split("\n---\n", 1)[-1].strip()
    for key, value in fill.items():
        out = out.replace("{" + key + "}", value)
    return out


@dataclass
class Job:
    """What one task needs from the chain runner."""

    task: str
    system: str
    lan: Callable[[Provider, int], str]  # (provider, prompt chars) -> the input, fitted to that provider
    cloud: Callable[
        [], str | None
    ]  # system-level facts only; None = this subject never goes to a cloud model
    check: Callable[[str, str], object]  # (reply, input sent) -> the result, or ProviderError("invalid", why)
    private: frozenset[str] = frozenset()


def run_chain(
    store: Store, job: Job, settings: Settings, now: datetime, post: Transport = _post
) -> tuple[object, Provider] | None:
    """Try the task's providers in order. Returns (result, provider), or None to publish the template.
    Every step that is skipped, refused, or fails is written to the call log (no content)."""
    cfg = settings.tasks.get(job.task) or {}
    chain = [str(x) for x in cfg.get("chain") or [TEMPLATE]]
    for step in chain:
        if step == TEMPLATE:
            return None
        p = settings.providers.get(step)
        if p is None:
            continue  # not configured or not enabled: the next step
        if p.locality == "cloud" and job.task not in settings.allow_cloud_for:
            _record(store, now, job.task, p, "skipped", detail="cloud not allowed for this task")
            continue
        calls, tokens = usage_today(store, now)
        if calls >= settings.max_calls_per_day or (
            p.locality == "cloud" and tokens >= settings.max_cloud_tokens_per_day
        ):
            _record(store, now, job.task, p, "budget", detail="daily budget used up")
            continue
        if p.locality == "cloud":
            user = job.cloud()
            if user is None:
                _record(store, now, job.task, p, "skipped", detail="device-level subject never goes to cloud")
                continue
            leak = privacy_leak(job.system + "\n" + user, job.private)
            if leak:
                _record(store, now, job.task, p, "privacy", detail=f"blocked before sending: {leak}")
                continue
        else:
            user = job.lan(p, len(job.system))
        started = time.monotonic()
        t_in = t_out = None
        try:
            text, t_in, t_out = call(p, job.system, user, post)
            ms = int((time.monotonic() - started) * 1000)
            result = job.check(text, user)
        except ProviderError as exc:
            ms = int((time.monotonic() - started) * 1000)  # tokens still count against a cloud budget
            _record(
                store, now, job.task, p, exc.outcome,
                latency_ms=ms, tokens_in=t_in, tokens_out=t_out, detail=exc.detail[:120],
            )  # fmt: skip
            continue
        _record(store, now, job.task, p, "ok", latency_ms=ms, tokens_in=t_in, tokens_out=t_out)
        return result, p
    return None


def feature_on(settings: Settings, task: str) -> bool:
    return settings.enabled and bool(settings.features.get(task))


# ---------------------------------------------------------------- the morning brief


def morning_brief(
    store: Store,
    d: Digest,
    day: str,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    settings: Settings | None = None,
    post: Transport = _post,
    task: str = "morning_brief",
) -> Prose | None:
    """The prose version of today's digest, or None (publish the template). Never raises."""
    now = now or datetime.now(UTC)
    try:
        return _morning_brief(store, d, day, now, env, settings or load_settings(env), post, task)
    except Exception as exc:  # AI is never worth a failed run
        log("ai_failed", task=task, error=f"{type(exc).__name__}: {exc}")
        return None


def _morning_brief(store, d, day, now, env, settings: Settings, post, task) -> Prose | None:
    if not feature_on(settings, task) or not d.run_ok:
        return None  # off, or a failed run: the template says "No data today" and nothing may soften it
    system = system_prompt(
        task, settings.tasks.get(task) or {}, env, max_stories="5", date=day, timezone="UTC"
    )
    if system is None:
        return None
    hosts = known_hosts(store)
    names = known_names(store, env, [f.id for f in d.findings])

    def check(reply: str, sent: str) -> tuple[str, list[str]]:
        return parse(reply, sent, hosts | names, settings.validation)

    job = Job(
        task, system,
        lan=lambda p, prompt_chars: build_input(d, day, p, prompt_chars),
        cloud=lambda: _cloud_input(d, day),
        check=check,
        private=private_terms(store, env),
    )  # fmt: skip
    found = run_chain(store, job, settings, now, post)
    if found is None:
        return None
    (headline, paragraphs), p = found
    return Prose(headline, paragraphs, p.id, p.model)
