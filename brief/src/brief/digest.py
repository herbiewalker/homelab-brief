"""The digest: what a person (or an AI assistant) should read this morning, as plain data.

`compose` turns the findings of one run into ranked stories, a state for each system, the issues that
are still developing, and a few "quiet" lines, with a rule-based headline (no model needed). Rendering
to files is separate (render_md.py, outputs.py).

The failure path matters most. If the run failed or no events arrived in 24 hours, the digest says
"No data today" with the reason, `run_ok` is false, and nothing is described as quiet. A silent
pipeline must never read as a calm network.
"""

from dataclasses import dataclass, field
from datetime import datetime

from brief import grouping, issues
from brief.detectors import Finding
from brief.routing import Priorities
from brief.store import iso
from brief.systems import Systems

SCHEMA = 1
HEADLINE_TITLE_CHARS = 90
SELF_NAMES = {"brief": "Homelab Brief"}  # the pseudo-system for findings about the product itself


@dataclass
class SystemState:
    id: str
    name: str
    state: str  # green | yellow | red
    summary: str


@dataclass
class Digest:
    generated_at: datetime
    run_ok: bool
    problems: list[str]
    learning: dict
    headline: str
    systems: list[SystemState] = field(default_factory=list)
    stories: list[grouping.Story] = field(default_factory=list)
    developing: list[dict] = field(default_factory=list)
    quiet: list[str] = field(default_factory=list)
    pipeline: dict = field(default_factory=dict)
    counted: int = 0  # info-level findings counted but not listed
    findings: list[Finding] = field(default_factory=list)  # every finding, for the host files

    def to_status(self) -> dict:
        """The schema-1 status.json fields that come from the daily run (docs/guide/03-brief.md)."""
        return {
            "systems": [
                {"id": s.id, "name": s.name, "state": s.state, "summary": s.summary} for s in self.systems
            ],
            "stories": [
                {
                    "id": s.id,
                    "rank": i + 1,
                    "system": s.system,
                    "kind": s.kind,
                    "title": s.title,
                    "route": s.route,
                    "count": len(s.members),
                    "detail": f"hosts/{host_file(s.host)}" if s.host else None,
                }
                for i, s in enumerate(self.stories)
            ],
            "developing": [{"id": d["id"], "days": d["days"], "trend": d["trend"]} for d in self.developing],
            "quiet": list(self.quiet),
        }


def host_file(host: str | None) -> str:
    """The file name for a host's detail page (an IPv6 address has colons, which not every share allows)."""
    return f"{(host or 'unknown').replace(':', '-').replace('/', '-')}.md"


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _headline(
    run_ok: bool, problems: list[str], stories: list[grouping.Story], pipeline: dict, learning: dict
) -> str:
    if not run_ok:
        return "No data today: " + ("; ".join(problems) if problems else "the run did not complete")
    if not stories:
        checked = pipeline.get("hosts_checked", 0)
        text = f"Quiet: {plural(checked, 'host')} checked, nothing needs a look"
    else:
        top = stories[0].title
        if len(top) > HEADLINE_TITLE_CHARS:  # cut at a word, not in the middle of one
            top = top[: HEADLINE_TITLE_CHARS - 3].rsplit(" ", 1)[0].rstrip(",:;") + "..."
        count = len(stories)
        text = f"{plural(count, 'thing needs', 'things need')} a look; first: {top}"
    if learning.get("active"):
        text += f" (learning, day {learning['day']} of {learning['of']})"
    return text


def _system_states(
    systems: Systems, stories: list[grouping.Story], findings: list[Finding]
) -> list[SystemState]:
    by_system: dict[str, list[grouping.Story]] = {}
    for s in stories:
        by_system.setdefault(s.system or systems.default_system, []).append(s)
    out = []
    for system in [*systems.systems]:
        mine = by_system.pop(system.id, [])
        out.append(_state_for(system.id, system.label, mine))
    for sid, mine in by_system.items():  # findings for a system that is not in systems.yaml (the default one)
        out.append(_state_for(sid, SELF_NAMES.get(sid, sid), mine))
    return out


def _state_for(sid: str, name: str, mine: list[grouping.Story]) -> SystemState:
    if not mine:
        return SystemState(sid, name, "green", "no findings")
    state = "red" if any(s.pushes or s.urgent for s in mine) else "yellow"
    kinds: dict[str, int] = {}
    for s in mine:
        kinds[s.kind] = kinds.get(s.kind, 0) + len(s.members)
    summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
    return SystemState(sid, name, state, summary)


def compose(
    findings: list[Finding],
    systems: Systems,
    priorities: Priorities,
    pipeline: dict,
    learning: dict,
    now: datetime,
    state: dict | None = None,
    problems: list[str] | None = None,
    min_members: int | None = None,
) -> Digest:
    """The digest for one run. `pipeline` carries events_24h, hosts_checked and friends; `state` is the
    previous state.json (pass the NEW one from issues.update to show developing issues)."""
    problems = list(problems or [])
    if pipeline.get("events_24h", 0) == 0:
        problems.append("no events were received in the last 24 hours")
    run_ok = not problems
    labels = {s.id: s.label for s in systems.systems}
    if not run_ok:
        return Digest(
            generated_at=now,
            run_ok=False,
            problems=problems,
            learning=learning,
            headline=_headline(False, problems, [], pipeline, learning),
            systems=[SystemState(s.id, s.label, "unknown", "no data") for s in systems.systems],
            pipeline=pipeline,
        )
    cfg = priorities.grouping
    stories = grouping.build_stories(
        findings,
        priorities,
        labels,
        min_members if min_members is not None else int(cfg.get("min_members", grouping.DEFAULT_MIN_MEMBERS)),
        int(cfg.get("show", grouping.DEFAULT_SHOW)),
    )
    counted = len(grouping.counted_only(findings))
    story_hosts = {m.host for s in stories for m in s.members if m.host}
    expected_hosts = pipeline.get("hosts_checked", 0)
    clean = max(0, expected_hosts - len(story_hosts))
    quiet = [f"{plural(expected_hosts, 'host')} checked, {clean} clean"]
    if counted:
        quiet.append(f"{plural(counted, 'info-level finding')} counted, not listed")
    if learning.get("active"):
        quiet.append(
            f"Learning period, day {learning['day']} of {learning['of']}: "
            "novelty, deviation and off-hours findings are held back"
        )
    return Digest(
        generated_at=now,
        run_ok=True,
        problems=[],
        learning=learning,
        headline=_headline(True, [], stories, pipeline, learning),
        systems=_system_states(systems, stories, findings),
        stories=stories,
        developing=issues.developing(state) if state else [],
        quiet=quiet,
        pipeline=pipeline,
        counted=counted,
        findings=findings,
    )


def generated_at(d: Digest) -> str:
    return iso(d.generated_at)
