"""ai.yaml: the rules for a valid file, and small comment-preserving edits for the AI page and `brief ai set`.

The page and the command never rewrite the whole file. They change one value in place (the master switch, a
feature switch, a provider's switch or model, a task's chain, a guardrail) and hand the text to the config editor
(configedit.save), which validates it with `validate` below, keeps the old version for rollback, and audits it.
Comments, ordering, and everything not touched stay exactly as the user wrote them.

Locked guardrails are not in this file and have no setting: they are listed in LOCKED for the page to show.
"""

import re
from collections.abc import Mapping
from pathlib import Path

from brief.config import ConfigError, load_mapping

FEATURES = ("morning_brief", "explain_finding", "weekly_summary", "agents", "ask_the_lab", "mcp_server")
TASKS = ("morning_brief", "explain_finding", "weekly_summary", "ask_the_lab")
NO_TEMPLATE_TASKS = ("ask_the_lab",)  # a conversation has no template to fall back to
PROVIDER_TYPES = ("openai_compatible", "anthropic")
LOCALITIES = ("lan", "cloud")
VALIDATION_MODES = ("strict", "lenient")
ID = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,120}$")
ADJUSTABLE = {
    "max_calls_per_day": int,
    "max_cloud_tokens_per_day": int,
    "output_validation": str,
    "allow_cloud_for": list,
    "run_agents_only_when_gpu_idle": bool,
    "quiet_hours_no_agents": bool,
}
LOCKED = (
    ("The pipeline never waits on AI", "Every chain ends in the template; an error, a timeout, a budget limit, or a failed check moves on."),
    ("AI never decides or acts", "Rules decide findings, severity, and routing. A model only rewrites or explains, and takes no action."),
    ("Cloud privacy", "A cloud model never receives device-level detail, raw log lines, DNS data, or devices.yaml; outgoing text is scanned and blocked if it holds an address or a private name."),
    ("Read-only tools", "Agents and outside AI clients can only read. Changes are proposals that need your approval."),
    ("Citations required", "Every claim in an agent's output must cite a tool result; uncited claims are dropped."),
    ("Everything is logged", "Every model call, agent run, MCP call, and proposal decision is recorded, without the content."),
)  # fmt: skip


# ---------------------------------------------------------------- validation


def validate(path: Path) -> dict:
    """Raises ConfigError for an ai.yaml the product could not use safely."""
    data = load_mapping(path)
    name = path.name
    if "enabled" in data and not isinstance(data["enabled"], bool):
        raise ConfigError(f"{name}: enabled must be true or false")
    features = data.get("features") or {}
    if not isinstance(features, dict) or not all(isinstance(v, bool) for v in features.values()):
        raise ConfigError(f"{name}: features must be a mapping of name to true or false")
    ids = _providers(data, name)
    tasks = data.get("tasks") or {}
    if not isinstance(tasks, dict):
        raise ConfigError(f"{name}: tasks must be a mapping")
    for task, cfg in tasks.items():
        chain = (cfg or {}).get("chain") if isinstance(cfg, dict) else None
        if not isinstance(chain, list) or not chain or not all(isinstance(x, str) for x in chain):
            raise ConfigError(f"{name}: tasks.{task}.chain must be a list of provider ids")
        for step in chain:
            if step != "template" and step not in ids:
                raise ConfigError(f"{name}: tasks.{task}.chain names {step!r}, which is not a provider")
        if "template" in chain and chain[-1] != "template":
            raise ConfigError(f"{name}: tasks.{task}.chain must end with template (nothing comes after it)")
        if task not in NO_TEMPLATE_TASKS and chain[-1] != "template":
            raise ConfigError(
                f"{name}: tasks.{task}.chain must end with template, so the product works without AI"
            )
    guard = data.get("guardrails") or {}
    if not isinstance(guard, dict):
        raise ConfigError(f"{name}: guardrails must be a mapping")
    for key, kind in ADJUSTABLE.items():
        if key in guard:
            value = guard[key]
            if not isinstance(value, kind) or (kind is int and (isinstance(value, bool) or value < 0)):
                raise ConfigError(f"{name}: guardrails.{key} has the wrong type or a negative number")
    if guard.get("output_validation", "strict") not in VALIDATION_MODES:
        raise ConfigError(f"{name}: guardrails.output_validation must be strict or lenient")
    unknown = [f for f in guard.get("allow_cloud_for") or [] if f not in FEATURES]
    if unknown:
        raise ConfigError(f"{name}: guardrails.allow_cloud_for names {unknown[0]!r}, which is not a feature")
    return data


def _providers(data: Mapping, name: str) -> set[str]:
    ids: set[str] = set()
    providers = data.get("providers") or []
    if not isinstance(providers, list):
        raise ConfigError(f"{name}: providers must be a list")
    for raw in providers:
        if not isinstance(raw, dict) or not ID.match(str(raw.get("id", ""))):
            raise ConfigError(f"{name}: every provider needs an id of lowercase letters, digits, - and _")
        pid = str(raw["id"])
        if pid in ids or pid == "template":
            raise ConfigError(f"{name}: provider id {pid!r} is repeated or reserved")
        ids.add(pid)
        if raw.get("type", "openai_compatible") not in PROVIDER_TYPES:
            raise ConfigError(f"{name}: provider {pid}: type must be one of {', '.join(PROVIDER_TYPES)}")
        if raw.get("locality", "lan") not in LOCALITIES:
            raise ConfigError(f"{name}: provider {pid}: locality must be lan or cloud")
        base = str(raw.get("base_url") or "")
        if base and not base.startswith(("http://", "https://", "<")):
            raise ConfigError(f"{name}: provider {pid}: base_url must start with http:// or https://")
        for field in ("timeout_s", "max_output_tokens", "context_tokens"):
            if field in raw and (
                isinstance(raw[field], bool) or not isinstance(raw[field], (int, float)) or raw[field] <= 0
            ):
                raise ConfigError(f"{name}: provider {pid}: {field} must be a number above zero")
        if (
            raw.get("enabled")
            and raw.get("type", "openai_compatible") == "openai_compatible"
            and (not base or "<" in base)
        ):
            raise ConfigError(f"{name}: provider {pid} is switched on but has no real base_url")
        if "model" in raw and raw["model"] and not MODEL.match(str(raw["model"])):
            raise ConfigError(
                f"{name}: provider {pid}: the model name has characters a model name does not use"
            )
        key_var = str(raw.get("api_key_var") or "")
        if key_var and not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", key_var):
            raise ConfigError(
                f"{name}: provider {pid}: api_key_var must name a variable like AI_LOCAL_API_KEY"
            )
    return ids


# ---------------------------------------------------------------- edits in place


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _replace(text: str, pattern: str, value: str, what: str) -> str:
    new, n = re.subn(
        rf"(?m)(?P<head>{pattern})[^#\n]*?(?P<tail>[ \t]*(?:#.*)?)$",
        rf"\g<head>{value}\g<tail>",
        text,
        count=1,
    )
    if not n:
        raise ConfigError(f"ai.yaml has no {what} line to change")
    return new


def set_master(text: str, on: bool) -> str:
    return _replace(text, r"^enabled:[ \t]*", _bool(on), "enabled")


def set_feature(text: str, name: str, on: bool) -> str:
    if name not in FEATURES:
        raise ConfigError(f"{name} is not a feature")
    block = _block(text, "features")
    new = _replace(block, rf"^[ \t]+{name}:[ \t]*", _bool(on), f"features.{name}")
    return text.replace(block, new, 1)


def _block(text: str, key: str) -> str:
    """The lines of a top-level mapping `key:` up to the next top-level key."""
    match = re.search(rf"(?m)^{key}:[ \t]*(?:#.*)?\n(?P<body>(?:[ \t]+.*\n|[ \t]*\n|[ \t]*#.*\n)*)", text)
    if not match:
        raise ConfigError(f"ai.yaml has no {key} section")
    return match.group(0)


def _provider_block(text: str, pid: str) -> str:
    # the provider's own lines: indented ones, until the next "- id:" or a line with no indent
    match = re.search(
        rf"(?m)^[ \t]*- id:[ \t]*{re.escape(pid)}[ \t]*(?:#.*)?\n(?:(?![ \t]*- id:)[ \t]+.*\n|[ \t]*\n)*",
        text,
    )
    if not match:
        raise ConfigError(f"ai.yaml has no provider {pid}")
    return match.group(0)


def set_provider(text: str, pid: str, field: str, value) -> str:
    """Change `enabled` (bool) or `model` (text) of one provider."""
    block = _provider_block(text, pid)
    if field == "enabled":
        new = _replace(block, r"^[ \t]+enabled:[ \t]*", _bool(bool(value)), f"provider {pid} enabled")
    elif field == "model":
        if value and not MODEL.match(str(value)):
            raise ConfigError("that model name has characters a model name does not use")
        new = _replace(block, r"^[ \t]+model:[ \t]*", f'"{value}"', f"provider {pid} model")
    else:
        raise ConfigError(f"{field} cannot be changed here")
    return text.replace(block, new, 1)


def set_chain(text: str, task: str, chain: list[str]) -> str:
    if task not in TASKS:
        raise ConfigError(f"{task} is not a task")
    if not all(re.fullmatch(r"[a-z][a-z0-9_-]*", x) for x in chain):
        raise ConfigError("a chain is a list of provider ids")
    line = re.search(rf"(?m)^[ \t]+{task}:[ \t]*\{{.*chain:[ \t]*\[[^\]\n]*\].*$", text)
    if not line:
        raise ConfigError(f"ai.yaml has no tasks.{task} line to change")
    changed = re.sub(
        r"(chain:[ \t]*\[)[^\]\n]*(\])",
        lambda m: m.group(1) + ", ".join(chain) + m.group(2),
        line.group(0),
        count=1,
    )
    return text.replace(line.group(0), changed, 1)


def set_guardrail(text: str, key: str, value) -> str:
    if key not in ADJUSTABLE:
        raise ConfigError(f"{key} is not an adjustable guardrail")
    kind = ADJUSTABLE[key]
    if kind is list:
        rendered = "[" + ", ".join(str(x) for x in value) + "]"
    elif kind is bool:
        rendered = _bool(bool(value))
    elif kind is int:
        rendered = str(int(value))
    else:
        rendered = str(value)
    block = _block(text, "guardrails")
    new = _replace(block, rf"^[ \t]+{key}:[ \t]*", rendered, f"guardrails.{key}")
    return text.replace(block, new, 1)
