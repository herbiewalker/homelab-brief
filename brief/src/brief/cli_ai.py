"""`brief ai ...`: everything the admin GUI's AI page does, from a shell.

  brief ai status [--json]             master switch, features, providers, chains, today's usage
  brief ai enable | disable            the master switch
  brief ai feature NAME on|off         one feature switch
  brief ai provider ID on|off          switch a provider on or off
  brief ai provider ID model NAME      choose its model
  brief ai chain TASK ID [ID ...]      a task's fallback chain (it must end in template)
  brief ai guardrail KEY VALUE         an adjustable guardrail (a list is comma-separated)
  brief ai test ID                     connection, latency, model list, and a tool-calling check (a real call)
  brief ai models ID                   the model ids the server offers
  brief ai calls [--limit N]           the call log (no content)
  brief ai preview [--provider ID]     today's brief: the template next to a provider's version (a real call)
  brief ai explain FINDING_ID          a plain-language explanation of one finding (a real call)

Prompts are edited with `brief config show|set|history|rollback prompts/morning_brief.md`. Every change goes through the
config editor, so it is validated, versioned, audited, and can be undone.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from brief import ai, aiconfig, aitasks, aitools, configedit, outputs
from brief.config import ConfigError
from brief.systems import Systems


def add_parsers(sub) -> None:
    ai_ = sub.add_parser("ai", help="the AI settings, providers, and tests (off by default)").add_subparsers(
        dest="cmd", required=True
    )
    st = ai_.add_parser("status", help="what is on, which providers, and today's usage")
    st.add_argument("--json", action="store_true")
    ai_.add_parser("enable", help="the master switch on")
    ai_.add_parser("disable", help="the master switch off: no model is called anywhere")
    fe = ai_.add_parser("feature", help="one feature switch")
    fe.add_argument("name", choices=aiconfig.FEATURES)
    fe.add_argument("state", choices=("on", "off"))
    pr = ai_.add_parser("provider", help="switch a provider or choose its model")
    pr.add_argument("id")
    pr.add_argument("action", choices=("on", "off", "model"))
    pr.add_argument("value", nargs="?", default="")
    ch = ai_.add_parser("chain", help="a task's fallback chain")
    ch.add_argument("task", choices=aiconfig.TASKS)
    ch.add_argument("providers", nargs="+")
    gr = ai_.add_parser("guardrail", help="an adjustable guardrail")
    gr.add_argument("key", choices=sorted(aiconfig.ADJUSTABLE))
    gr.add_argument("value")
    for name, text in (("test", "test a provider (a real call)"), ("models", "list a provider's models")):
        p = ai_.add_parser(name, help=text)
        p.add_argument("id")
    ca = ai_.add_parser("calls", help="the call log")
    ca.add_argument("--limit", type=int, default=30)
    pv = ai_.add_parser("preview", help="the template brief next to a provider's version (a real call)")
    pv.add_argument("--provider", default="")
    ex = ai_.add_parser("explain", help="explain one finding (a real call)")
    ex.add_argument("finding_id")


# ---------------------------------------------------------------- shared with the admin page


def edit(store, state: Path, user: str, change) -> str:
    """Apply `change(text) -> text` to ai.yaml through the config editor. Returns its message."""
    text, _ = configedit.current("ai.yaml")
    try:
        new = change(text)
    except ConfigError as exc:
        raise configedit.EditError(str(exc)) from None
    return configedit.save(store, "ai.yaml", new, user, configedit.digest(text), state)


def status(store, env=None) -> dict:
    settings = ai.load_settings(env)
    calls, tokens = ai.usage_today(store, datetime.now(UTC))
    raw = aitools.raw_config(env)
    return {
        "enabled": bool(raw.get("enabled")),
        "features": {f: bool((raw.get("features") or {}).get(f)) for f in aiconfig.FEATURES},
        "providers": aitools.provider_rows(env),
        "chains": {t: list((cfg or {}).get("chain") or []) for t, cfg in (raw.get("tasks") or {}).items()},
        "guardrails": {k: (raw.get("guardrails") or {}).get(k) for k in aiconfig.ADJUSTABLE},
        "usage": {
            "calls_today": calls,
            "cloud_tokens_today": tokens,
            "max_calls": settings.max_calls_per_day,
            "max_cloud_tokens": settings.max_cloud_tokens_per_day,
        },
    }


def run(args, state: Path, store, out, err) -> int:
    from brief.cli_tools import actor

    user = actor()
    cmd = args.cmd
    if cmd == "status":
        data = status(store)
        if args.json:
            out(json.dumps(data, indent=2))
            return 0
        out(f"AI is {'ON' if data['enabled'] else 'off'} (the master switch).")
        out("features: " + ", ".join(f"{f} {'on' if on else 'off'}" for f, on in data["features"].items()))
        for p in data["providers"]:
            out(
                f"provider {p['id']}: {'on' if p['enabled'] else 'off'}, {p['locality']}, {p['type']}, "
                f"model {p['model'] or '(not chosen)'}, key {'set' if p['key_set'] else 'not set'}"
            )
        for task, chain in data["chains"].items():
            out(f"chain {task}: {' -> '.join(chain)}")
        u = data["usage"]
        out(
            f"today: {u['calls_today']} of {u['max_calls']} calls; {u['cloud_tokens_today']} of {u['max_cloud_tokens']} cloud tokens"
        )
        return 0
    if cmd in ("enable", "disable"):
        out(edit(store, state, user, lambda t: aiconfig.set_master(t, cmd == "enable")))
        return 0
    if cmd == "feature":
        out(edit(store, state, user, lambda t: aiconfig.set_feature(t, args.name, args.state == "on")))
        return 0
    if cmd == "provider":
        if args.action == "model":
            out(edit(store, state, user, lambda t: aiconfig.set_provider(t, args.id, "model", args.value)))
        else:
            out(
                edit(
                    store,
                    state,
                    user,
                    lambda t: aiconfig.set_provider(t, args.id, "enabled", args.action == "on"),
                )
            )
        return 0
    if cmd == "chain":
        out(edit(store, state, user, lambda t: aiconfig.set_chain(t, args.task, args.providers)))
        return 0
    if cmd == "guardrail":
        kind = aiconfig.ADJUSTABLE[args.key]
        value = (
            [x.strip() for x in args.value.split(",") if x.strip()]
            if kind is list
            else (
                args.value.lower() in ("true", "on", "yes", "1")
                if kind is bool
                else (int(args.value) if kind is int else args.value)
            )
        )
        out(edit(store, state, user, lambda t: aiconfig.set_guardrail(t, args.key, value)))
        return 0
    if cmd == "models":
        models = aitools.list_models(aitools.build_provider(args.id))
        out("\n".join(models) or "The server listed no models.")
        return 0
    if cmd == "test":
        result = aitools.test_provider(store, aitools.build_provider(args.id))
        out(
            f"connection: {'ok' if result['ok'] else 'FAILED'}"
            + (f" ({result['error']})" if result["error"] else "")
        )
        if result["latency_ms"] is not None:
            out(f"latency: {result['latency_ms']} ms for a one-word answer")
        out(
            f"models listed: {len(result['models'])}"
            + ("" if result["model_listed"] in (None, True) else " (the chosen model is NOT among them)")
        )
        out(
            "tool calling: "
            + {True: "works", False: "not supported", None: "could not be told"}[result["tools"]]
        )
        return 0 if result["ok"] else 1
    if cmd == "calls":
        for row in aitools.recent_calls(store, args.limit):
            out(
                f"{row['ts']} {row['task']:<16} {row['provider']:<10} {row['outcome']:<8} "
                f"{row['latency_ms'] or '-':>6} ms  {row['tokens_in'] or 0}/{row['tokens_out'] or 0} tokens  {row['detail'] or ''}"
            )
        return 0
    if cmd == "preview":
        raw = status(store)
        provider = args.provider or next((p["id"] for p in raw["providers"] if p["enabled"]), "")
        if not provider:
            err("No provider is switched on. Use `brief ai provider ID on`.")
            return 1
        now = datetime.now(UTC)
        systems = _systems()
        digest = outputs.run_daily(store, systems, now, "/dev/null", write=False)
        result = aitools.preview_morning_brief(store, digest, now.strftime("%Y-%m-%d"), provider, now)
        out("=== template ===")
        out("\n".join(result["template"]))
        out(f"=== {provider} ===")
        prose = result["prose"]
        out(result["note"] if prose is None else "\n\n".join([prose.headline, *prose.paragraphs]))
        return 0
    if cmd == "explain":
        now = datetime.now(UTC)
        finding = aitasks.find_finding(store, _systems(), args.finding_id, now)
        if finding is None:
            err("No finding with that id is open right now.")
            return 1
        explanation = aitasks.explain_finding(store, finding, now)
        if explanation is None:
            err(
                "No explanation: AI or the explain_finding feature is off, or no provider gave a usable answer (see `brief ai calls`)."
            )
            return 1
        out(explanation.text)
        out(f"-- {explanation.provider} ({explanation.model})")
        return 0
    err(f"unknown command {cmd}")
    return 2


def _systems() -> Systems:
    from brief.config import config_file

    try:
        return Systems.load(config_file("systems.yaml"))
    except (ConfigError, OSError):
        return Systems()
