"""The playbook: findings become recommendations by plain templating (brief/playbook.yaml `rules`).

A rule names a finding `kind`, optional `match` conditions, a `bucket`, and `advice` text:

    smart_pending_nonzero:
      kind: smart_trend
      match:
        - { field: attr, op: in, value: ["197:Current_Pending_Sector", "198:Offline_Uncorrectable"] }
        - { field: raw, op: gt, value: 0 }
      bucket: act_now
      advice: "{disk} reports {raw} unreadable sectors. ..."

The first rule (in file order) whose kind matches and whose conditions all hold is attached to the finding:
`detail["rule"]` (the id, which priorities.yaml `rules:` can route), `detail["bucket"]`, `detail["advice"]`.

Nothing here is code. A condition is (field, operator, value) and the operators are a fixed list; a regex
is matched with `re.search` against at most 500 characters; the advice may only contain `{name}`
placeholders (no format specs, no attribute or index access), filled from the finding's facts. A field a
finding does not have makes the condition false, and a placeholder it does not have reads "unknown".
Facts are the finding's `kind`, `system`, `host`, `value`, `title`, plus everything in its `detail`.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

from brief.config import ConfigError, config_file, load_mapping
from brief.detectors import Finding

OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "regex")
BUCKETS = ("watch", "plan", "act_now")
MAX_REGEX = 200
MAX_SUBJECT = 500
MAX_ADVICE = 1200
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# a group that itself repeats, then repeats again: the classic catastrophic-backtracking shape
_NESTED_REPEAT = re.compile(r"\([^)]*(?:[+*]|\{\d*,\d*\})[^)]*\)\s*(?:[+*]|\{\d+,?\d*\})")
_RULE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: object
    pattern: re.Pattern | None = None


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    bucket: str | None
    match: tuple[Condition, ...]
    advice: str


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _same(a, b) -> bool:
    x, y = _number(a), _number(b)
    return x == y if x is not None and y is not None else str(a) == str(b)


def holds(cond: Condition, facts: Mapping) -> bool:
    if cond.field not in facts or facts[cond.field] is None:
        return False
    have = facts[cond.field]
    op = cond.op
    if op == "eq":
        return _same(have, cond.value)
    if op == "ne":
        return not _same(have, cond.value)
    if op in ("gt", "gte", "lt", "lte"):
        a, b = _number(have), _number(cond.value)
        if a is None or b is None:
            return False
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    if op == "in":
        return any(_same(have, item) for item in cond.value)  # type: ignore[union-attr]
    if op == "contains":
        if isinstance(have, (list, tuple, set)):
            return any(_same(item, cond.value) for item in have)
        return str(cond.value) in str(have)
    if op == "regex":
        return cond.pattern is not None and cond.pattern.search(str(have)[:MAX_SUBJECT]) is not None
    return False


def _condition(rule_id: str, index: int, raw) -> Condition:
    where = f"playbook.yaml: rules.{rule_id}.match[{index}]"
    if not isinstance(raw, dict) or set(raw) - {"field", "op", "value"}:
        raise ConfigError(f"{where} must be {{ field, op, value }}")
    field_name, op = raw.get("field"), raw.get("op")
    if not isinstance(field_name, str) or not re.fullmatch(r"\w+", field_name):
        raise ConfigError(f"{where}: field must be a plain name")
    if op not in OPS:
        raise ConfigError(f"{where}: op must be one of {', '.join(OPS)}")
    if "value" not in raw:
        raise ConfigError(f"{where}: value is missing")
    value = raw["value"]
    if op == "in" and (not isinstance(value, list) or not value):
        raise ConfigError(f"{where}: value must be a non-empty list for in")
    if op in ("gt", "gte", "lt", "lte") and _number(value) is None:
        raise ConfigError(f"{where}: value must be a number for {op}")
    pattern = None
    if op == "regex":
        if not isinstance(value, str) or len(value) > MAX_REGEX:
            raise ConfigError(f"{where}: the pattern must be text of at most {MAX_REGEX} characters")
        if _NESTED_REPEAT.search(value):
            raise ConfigError(
                f"{where}: nested repetition like (a+)+ is not allowed (it can hang the matcher)"
            )
        try:
            pattern = re.compile(value)
        except re.error as exc:
            raise ConfigError(f"{where}: not a valid pattern ({exc})") from None
    return Condition(field_name, op, value, pattern)


def check_advice(rule_id: str, advice) -> str:
    where = f"playbook.yaml: rules.{rule_id}.advice"
    if not isinstance(advice, str) or not advice.strip():
        raise ConfigError(f"{where} must be text")
    if len(advice) > MAX_ADVICE:
        raise ConfigError(f"{where} is longer than {MAX_ADVICE} characters")
    if "{" in _PLACEHOLDER.sub("", advice) or "}" in _PLACEHOLDER.sub("", advice):
        raise ConfigError(f"{where}: only plain {{name}} placeholders are allowed")
    return " ".join(advice.split())


def parse(data: Mapping) -> list[Rule]:
    """The rules of playbook.yaml (already loaded), or ConfigError naming the rule and the problem."""
    block = data.get("rules") or {}
    if not isinstance(block, dict):
        raise ConfigError("playbook.yaml: rules must be a mapping of rule id to rule")
    rules = []
    for rule_id, raw in block.items():
        rule_id = str(rule_id)
        if not _RULE_ID.match(rule_id):
            raise ConfigError(f"playbook.yaml: rule id {rule_id!r} may use letters, digits, _ . - only")
        if not isinstance(raw, dict):
            raise ConfigError(f"playbook.yaml: rules.{rule_id} must be a mapping")
        unknown = set(raw) - {"kind", "match", "bucket", "advice"}
        if unknown:
            raise ConfigError(
                f"playbook.yaml: rules.{rule_id} has unknown keys: {', '.join(sorted(unknown))}"
            )
        kind = raw.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ConfigError(f"playbook.yaml: rules.{rule_id}.kind is missing")
        bucket = raw.get("bucket")
        if bucket is not None and bucket not in BUCKETS:
            raise ConfigError(f"playbook.yaml: rules.{rule_id}.bucket must be one of {', '.join(BUCKETS)}")
        match = raw.get("match") or []
        if not isinstance(match, list):
            raise ConfigError(f"playbook.yaml: rules.{rule_id}.match must be a list")
        rules.append(
            Rule(
                rule_id,
                kind,
                bucket,
                tuple(_condition(rule_id, i, m) for i, m in enumerate(match)),
                check_advice(rule_id, raw.get("advice")),
            )
        )
    return rules


def validate(path) -> list[Rule]:
    """For the config guard and the editor."""
    return parse(load_mapping(path))


def load(env: Mapping[str, str] | None = None) -> list[Rule]:
    """The rules, or none when the file cannot be read (a bad edit loses only the advice text)."""
    try:
        return parse(load_mapping(config_file("playbook.yaml", env)))
    except (ConfigError, OSError):
        return []


def facts_of(finding: Finding) -> dict:
    return {
        **finding.detail,
        "kind": finding.kind,
        "system": finding.system,
        "host": finding.host,
        "value": finding.value,
        "title": finding.title,
    }


def _show(value) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def render(advice: str, facts: Mapping) -> str:
    return _PLACEHOLDER.sub(lambda m: _show(facts.get(m.group(1))), advice)


def advise(finding: Finding, rules: list[Rule]) -> dict | None:
    facts = facts_of(finding)
    for rule in rules:
        if rule.kind == finding.kind and all(holds(c, facts) for c in rule.match):
            out = {"rule": rule.id, "advice": render(rule.advice, facts)}
            if rule.bucket:
                out["bucket"] = rule.bucket
            return out
    return None


def apply(findings: list[Finding], rules: list[Rule]) -> list[Finding]:
    """The findings with `rule`, `bucket`, and `advice` added to the detail of those a rule matches."""
    if not rules:
        return findings
    out = []
    for f in findings:
        extra = advise(f, rules)
        out.append(replace(f, detail={**f.detail, **extra}) if extra else f)
    return out
