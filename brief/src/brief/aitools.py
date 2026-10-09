"""What the AI page and `brief ai` do with a provider besides running a task: list models, test it, and show
today's brief written by it next to the template.

A test or a preview is a real model call, so it is written to the call log like any other and counts toward
daily budget. Neither can reach a cloud provider with anything more than the brief's own system-level input.
"""

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime

from brief import ai
from brief.config import ConfigError, config_file, load_mapping
from brief.digest import Digest
from brief.render_md import digest_markdown
from brief.store import Store

Getter = Callable[[str, dict, float], dict]
PING = "Reply with the single word: ok"
TOOL_NAME = "get_time"
TOOL_PROMPT = "What time is it? You must use the get_time tool to find out."


def _get(url: str, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:  # noqa: S310 (a configured URL)
            return json.loads(reply.read().decode("utf-8"))
    except TimeoutError as exc:
        raise ai.ProviderError("timeout", "no answer in time") from exc
    except urllib.error.HTTPError as exc:
        raise ai.ProviderError("error", f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ai.ProviderError(
            "timeout" if isinstance(reason, TimeoutError) else "error", type(reason).__name__
        ) from exc
    except ValueError as exc:
        raise ai.ProviderError("error", "the reply was not JSON") from exc


# ---------------------------------------------------------------- providers as configured


def raw_config(env: Mapping[str, str] | None = None) -> dict:
    try:
        return load_mapping(config_file("ai.yaml", env))
    except (ConfigError, OSError):
        return {}


def provider_rows(env: Mapping[str, str] | None = None) -> list[dict]:
    """Every provider in ai.yaml, on or off, for display. Never a key, only whether one is set."""
    env = os.environ if env is None else env
    out = []
    for raw in raw_config(env).get("providers") or []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        key_var = str(raw.get("api_key_var") or "")
        out.append(
            {
                "id": str(raw["id"]),
                "type": str(raw.get("type") or "openai_compatible"),
                "base_url": str(raw.get("base_url") or ""),
                "model": str(raw.get("model") or ""),
                "locality": "cloud" if raw.get("locality") == "cloud" else "lan",
                "enabled": bool(raw.get("enabled")),
                "key_set": bool(key_var and env.get(key_var)),
                "key_var": key_var,
                "usable": bool(raw.get("base_url") or raw.get("type") == "anthropic")
                and "<" not in str(raw.get("base_url") or ""),
            }
        )
    return out


def build_provider(provider_id: str, env: Mapping[str, str] | None = None) -> ai.Provider:
    """A Provider for testing, whether or not it is switched on. Raises ConfigError when it cannot be used."""
    env = os.environ if env is None else env
    for raw in raw_config(env).get("providers") or []:
        if isinstance(raw, dict) and str(raw.get("id")) == provider_id:
            base = str(raw.get("base_url") or "").strip().rstrip("/")
            if (not base and raw.get("type") != "anthropic") or "<" in base:
                raise ConfigError(f"provider {provider_id} has no address yet (base_url)")
            key_var = str(raw.get("api_key_var") or "")
            return ai.Provider(
                id=provider_id,
                type=str(raw.get("type") or "openai_compatible"),
                base_url=base,
                api_key=env.get(key_var, "") if key_var else "",
                model=str(raw.get("model") or ""),
                locality="cloud" if raw.get("locality") == "cloud" else "lan",
                timeout_s=float(raw.get("timeout_s") or 60),
                max_output_tokens=int(raw.get("max_output_tokens") or 1200),
                context_tokens=int(raw.get("context_tokens") or 8192),
                disable_thinking=bool(raw.get("disable_thinking")),
            )
    raise ConfigError(f"there is no provider called {provider_id}")


# ---------------------------------------------------------------- models and tests


def list_models(p: ai.Provider, get: Getter = _get) -> list[str]:
    """The model ids the server offers (`/v1/models`)."""
    if p.type == "anthropic":
        reply = get(
            f"{p.base_url or ai.ANTHROPIC_URL}/v1/models",
            {"x-api-key": p.api_key, "anthropic-version": "2023-06-01"},
            p.timeout_s,
        )
    else:
        headers = {"Authorization": f"Bearer {p.api_key}"} if p.api_key else {}
        reply = get(f"{p.base_url}/models", headers, p.timeout_s)
    try:
        return sorted({str(m["id"]) for m in reply["data"] if m.get("id")})
    except (KeyError, TypeError) as exc:
        raise ai.ProviderError("error", "unexpected reply shape") from exc


def _tool_check(p: ai.Provider, post: ai.Transport) -> bool | None:
    """True if the model answers a request with a tool call, False if the server says tools are not supported,
    None if it could not be told."""
    if p.type == "anthropic":
        headers = {"x-api-key": p.api_key, "anthropic-version": "2023-06-01"}
        body = {
            "model": p.model,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": TOOL_PROMPT}],
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": "Return the current time.",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        }
        url = f"{p.base_url or ai.ANTHROPIC_URL}/v1/messages"
    else:
        headers = {"Authorization": f"Bearer {p.api_key}"} if p.api_key else {}
        body = {
            "model": p.model,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": TOOL_PROMPT}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": TOOL_NAME,
                        "description": "Return the current time.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": "auto",
        }
        url = f"{p.base_url}/chat/completions"
    try:
        reply = post(url, headers, body, p.timeout_s)
    except ai.ProviderError as exc:
        return False if exc.detail in ("HTTP 400", "HTTP 422", "HTTP 501") else None
    if p.type == "anthropic":
        return any(b.get("type") == "tool_use" for b in reply.get("content") or [])
    try:
        return bool(reply["choices"][0]["message"].get("tool_calls"))
    except (KeyError, IndexError, TypeError, AttributeError):
        return False


def test_provider(
    store: Store,
    p: ai.Provider,
    now: datetime | None = None,
    post: ai.Transport = ai._post,
    get: Getter = _get,
) -> dict:
    """{ok, models, model_listed, latency_ms, tools, error}. Written to the call log as task provider_test."""
    now = now or datetime.now(UTC)
    result: dict = {
        "ok": False,
        "models": [],
        "model_listed": None,
        "latency_ms": None,
        "tools": None,
        "error": None,
    }
    started = time.monotonic()
    try:
        result["models"] = list_models(p, get)
        result["model_listed"] = (p.model in result["models"]) if p.model else None
    except ai.ProviderError as exc:  # a server may not list models; the chat test below decides
        result["models_error"] = exc.detail
    if not p.model:
        result["error"] = "no model is set for this provider yet (pick one from the list)"
        return result
    t_in = t_out = None
    try:
        began = time.monotonic()
        text, t_in, t_out = ai.call(replace(p, max_output_tokens=16), "You answer in one word.", PING, post)
        result["latency_ms"] = int((time.monotonic() - began) * 1000)
        result["ok"] = bool(ai.clean(text))
        if not result["ok"]:
            result["error"] = "the model answered with nothing"
    except ai.ProviderError as exc:
        result["error"] = f"{exc.outcome}: {exc.detail}"
    ai._record(
        store, now, "provider_test", p, "ok" if result["ok"] else "error",
        latency_ms=result["latency_ms"] or int((time.monotonic() - started) * 1000),
        tokens_in=t_in, tokens_out=t_out, detail=(result["error"] or "")[:120],
    )  # fmt: skip
    if result["ok"]:
        result["tools"] = _tool_check(p, post)
    return result


# ---------------------------------------------------------------- side by side


def preview_morning_brief(
    store: Store,
    digest: Digest,
    day: str,
    provider_id: str,
    now: datetime | None = None,
    env: Mapping[str, str] | None = None,
    post: ai.Transport = ai._post,
) -> dict:
    """{template: [lines], prose: {headline, paragraphs, provider, model} | None, note}. The checks of the
    real task apply (privacy scan, validation, budget); the feature switch is not needed to preview."""
    now = now or datetime.now(UTC)
    settings = ai.load_settings(env)
    cfg = dict(settings.tasks.get("morning_brief") or {})
    cfg["chain"] = [provider_id, ai.TEMPLATE]
    forced = replace(
        settings,
        enabled=True,
        features={**settings.features, "morning_brief": True},
        tasks={**settings.tasks, "morning_brief": cfg},
    )
    template = digest_markdown(digest, day)
    if provider_id not in forced.providers:
        return {
            "template": template,
            "prose": None,
            "note": "that provider is not switched on, or is incomplete",
        }
    prose = ai.morning_brief(store, digest, day, now, env, forced, post)
    note = "" if prose else "The provider gave nothing usable; see the call log for why. The template stands."
    return {"template": template, "prose": prose, "note": note}


def recent_calls(store: Store, limit: int = 50) -> list[dict]:
    with store.read() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT ts, task, provider, model, locality, latency_ms, tokens_in, tokens_out, "
                "outcome, detail "
                "FROM ai_call ORDER BY ts DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        ]
