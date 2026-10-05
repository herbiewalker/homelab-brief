"""Where a finding goes: its rank and route from brief/priorities.yaml.

Lookup order is rule id, then finding kind, then the default (docs/guide/03-brief.md, section 3g).
Detector findings have no rule id yet (that arrives with the playbook rules), so they route by kind.

Routes: push_now | daily_headline | digest_only | suppress. `urgent` lets a push bypass quiet hours
and the daily budget.

This module only answers "which route and how important". The alert lifecycle (dedupe, re-notify,
escalate, resolve, quiet hours, the daily budget) is separate and builds on it.
"""

from dataclasses import dataclass

from brief.config import ConfigError, config_file, load_mapping
from brief.detectors import Finding

ROUTES = ("push_now", "daily_headline", "digest_only", "suppress")
FALLBACK = {"rank": 10, "route": "digest_only"}


@dataclass(frozen=True)
class Route:
    rank: int
    route: str
    urgent: bool = False


def _route(entry: dict | None, base: Route) -> Route:
    entry = entry or {}
    route = entry.get("route", base.route)
    if route not in ROUTES:
        raise ConfigError(f"priorities.yaml: unknown route {route!r} (use one of {', '.join(ROUTES)})")
    return Route(int(entry.get("rank", base.rank)), route, bool(entry.get("urgent", base.urgent)))


class Priorities:
    def __init__(self, data: dict | None = None):
        data = data or {}
        self.default = _route(data.get("default") or FALLBACK, Route(FALLBACK["rank"], FALLBACK["route"]))
        self.kinds = {k: _route(v, self.default) for k, v in (data.get("kinds") or {}).items()}
        self.rules = {k: _route(v, self.default) for k, v in (data.get("rules") or {}).items()}
        self.lifecycle = data.get("lifecycle") or {}
        self.quiet_hours = data.get("quiet_hours") or {}

    @classmethod
    def load(cls, env=None) -> "Priorities":
        """priorities.yaml, or the built-in default when it is missing or broken (so a bad edit never
        stops findings from being reported; it is only less well ranked)."""
        try:
            return cls(load_mapping(config_file("priorities.yaml", env)))
        except (ConfigError, OSError, ValueError, TypeError):
            return cls({})

    def route_for(self, finding: Finding, rule: str | None = None) -> Route:
        if rule and rule in self.rules:
            return self.rules[rule]
        return self.kinds.get(finding.kind, self.default)

    def ranked(self, findings: list[Finding]) -> list[tuple[Finding, Route]]:
        """Most important first: story findings before count-only ones, then rank, then size, then id."""
        pairs = [(f, self.route_for(f)) for f in findings]
        return sorted(pairs, key=lambda p: (not p[0].story, -p[1].rank, -(p[0].value or 0), p[0].id))

    def pushes(self, findings: list[Finding]) -> list[tuple[Finding, Route]]:
        """The findings that would go out right now: stories routed push_now (count-only findings
        and suppressed routes never push)."""
        return [(f, r) for f, r in self.ranked(findings) if f.story and r.route == "push_now"]
