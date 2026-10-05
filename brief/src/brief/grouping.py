"""From findings to stories: one story per thing worth a person's attention.

Detectors report one finding per signature, which is right for tracking and alert dedupe but wrong
for reading: twelve signatures jumping at once on one gateway (a firmware update, a restarted
service) are one event. So findings of one kind on one host are merged into a single story once there
are `min_members` of them (brief/priorities.yaml `grouping`); fewer stay separate. The members are
kept, so detail files and the alert lifecycle still see every finding with its stable id.

Count-only findings (info-level novelty) are not stories; the digest only counts them.
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from brief.detectors import Finding
from brief.routing import Priorities

NOUN = {
    "deviation": "signatures far above normal",
    "novelty": "new or returning signatures",
    "off_hours": "signatures at unusual hours",
    "clock_skew": "sources with clock drift",
    "absence": "silent hosts",
}
DEFAULT_MIN_MEMBERS = 3
DEFAULT_SHOW = 3


@dataclass(frozen=True)
class Story:
    id: str
    kind: str
    title: str
    system: str | None
    host: str | None
    rank: int
    route: str
    urgent: bool
    value: float | None
    members: tuple[Finding, ...]
    grouped: bool = False
    top: tuple[str, ...] = ()  # the biggest member titles, for a grouped story
    detail: dict = field(default_factory=dict)

    @property
    def pushes(self) -> bool:
        return self.route == "push_now"


def _label(system: str | None, host: str | None, labels: dict[str, str]) -> str:
    if system and system in labels:
        return labels[system]
    return host or system or "an unknown source"


def build_stories(
    findings: list[Finding],
    priorities: Priorities,
    labels: dict[str, str] | None = None,
    min_members: int = DEFAULT_MIN_MEMBERS,
    show: int = DEFAULT_SHOW,
    on_suppress: Callable[[Finding], None] | None = None,
) -> list[Story]:
    """Ranked stories, most important first. `labels` maps a system id to its display name."""
    labels = labels or {}
    by_key: dict[tuple, list[Finding]] = defaultdict(list)
    for f in findings:
        if not f.story:
            continue
        route = priorities.route_for(f)
        if route.route == "suppress":
            if on_suppress:
                on_suppress(f)
            continue
        by_key[(f.host or f.system or "", f.kind)].append(f)
    stories: list[Story] = []
    hosts_of: dict[str, set[str]] = defaultdict(set)
    for f in findings:
        if f.story and f.system and f.host:
            hosts_of[f.system].add(f.host)
    for (_, kind), group in by_key.items():
        group.sort(key=lambda f: (-(f.value or 0), f.id))
        route = priorities.route_for(group[0])
        if len(group) >= min_members:
            first = group[0]
            where = _label(first.system, first.host, labels)
            if first.system and len(hosts_of.get(first.system, ())) > 1 and first.host:
                where += f" ({first.host})"  # several hosts share this system: say which one
            noun = NOUN.get(kind, f"{kind} findings")
            stories.append(
                Story(
                    id=f"group-{kind}-{first.host or first.system}",
                    kind=kind,
                    title=f"{len(group)} {noun} on {where}",
                    system=first.system,
                    host=first.host,
                    rank=route.rank + max(priorities.boost(m) for m in group),
                    route=route.route,
                    urgent=route.urgent,
                    value=max((f.value or 0 for f in group), default=None) or None,
                    members=tuple(group),
                    grouped=True,
                    top=tuple(f.title for f in group[:show]),
                    detail={"count": len(group)},
                )
            )
        else:
            for f in group:
                stories.append(
                    Story(
                        id=f.id,
                        kind=kind,
                        title=f.title,
                        system=f.system,
                        host=f.host,
                        rank=route.rank + priorities.boost(f),
                        route=route.route,
                        urgent=route.urgent,
                        value=f.value,
                        members=(f,),
                        detail=f.detail,
                    )
                )
    return sorted(stories, key=lambda s: (-s.rank, not s.urgent, -(s.value or 0), s.id))


def counted_only(findings: list[Finding]) -> list[Finding]:
    """The findings that are counted in the pipeline summary but not listed as stories."""
    return [f for f in findings if not f.story]
