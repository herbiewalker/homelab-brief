"""ignore.yaml: known noise that is filtered before detection but still counted.

A rule applies to events from `host` (a sender IP, a header hostname, or a system id, any case;
leave it out for any host) whose "<app>: <message>" text matches `match` (a regular expression).
The first matching rule wins. Ignored events are not mined into signatures or counted in the
hourly table, but they are counted per rule so a change in their volume stays visible.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from brief.config import ConfigError, load_mapping

MAX_PATTERN = 500


@dataclass(frozen=True)
class Rule:
    name: str
    host: str | None
    pattern: re.Pattern
    note: str = ""


class IgnoreRules:
    def __init__(self, rules: list[Rule] | None = None):
        self.rules = tuple(rules or [])
        names = [r.name for r in self.rules]
        if len(set(names)) != len(names):
            raise ConfigError("ignore.yaml: rule names must be unique")

    @classmethod
    def from_mapping(cls, data: dict) -> "IgnoreRules":
        raw = data.get("ignore") or []
        if not isinstance(raw, list):
            raise ConfigError("ignore.yaml: ignore must be a list")
        rules = []
        for i, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                raise ConfigError(f"ignore.yaml: rule #{i} must be a mapping")
            name = str(item.get("name") or "").strip()
            match = item.get("match")
            if not name:
                raise ConfigError(f"ignore.yaml: rule #{i} needs a name")
            if not isinstance(match, str) or not match:
                raise ConfigError(f"ignore.yaml: rule {name!r} needs a match pattern")
            if len(match) > MAX_PATTERN:
                raise ConfigError(
                    f"ignore.yaml: rule {name!r}: pattern is longer than {MAX_PATTERN} characters"
                )
            try:
                pattern = re.compile(match)
            except re.error as exc:
                raise ConfigError(f"ignore.yaml: rule {name!r}: bad pattern: {exc}") from None
            host = item.get("host")
            rules.append(
                Rule(name, str(host).strip().lower() if host else None, pattern, str(item.get("note") or ""))
            )
        return cls(rules)

    @classmethod
    def load(cls, path: Path) -> "IgnoreRules":
        return cls.from_mapping(load_mapping(path))

    def match(
        self, *, source_ip: str, header_host: str | None, system_id: str, app: str, msg: str
    ) -> Rule | None:
        text = f"{app}: {msg}" if app else msg
        names = {source_ip.lower(), (header_host or "").lower(), system_id.lower()}
        for rule in self.rules:
            if (rule.host is None or rule.host in names) and rule.pattern.search(text):
                return rule
        return None
