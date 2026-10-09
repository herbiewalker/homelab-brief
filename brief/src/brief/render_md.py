"""Markdown for people and AI assistants: the daily digest and one page per host.

Every generated file stays at or under 150 lines, because assistants read them through limited
context and some reads over SMB stop around 150 to 180 lines. `clip` enforces the limit as the last
step, and the writers also keep the important things first, so what gets cut is the tail.
"""

from dataclasses import dataclass, field

from brief.digest import Digest, generated_at, host_file, plural
from brief.grouping import Story

LIMIT = 150
MAX_STORIES = 20
MAX_MEMBERS = 5
MAX_SAMPLE_CHARS = 160
ICON = {"green": "green", "yellow": "yellow", "red": "RED", "unknown": "unknown"}


def clip(lines: list[str], limit: int = LIMIT) -> list[str]:
    """At most `limit` lines; when cut, the last line says how much was left out."""
    if len(lines) <= limit:
        return lines
    kept = lines[: limit - 2]
    return [*kept, "", f"... {len(lines) - len(kept)} more lines left out to stay under {limit} lines."]


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def digest_markdown(d: Digest, day: str) -> list[str]:
    lines = [
        f"# Homelab brief for {day}",
        "",
        f"Generated {generated_at(d)} (UTC). {'Run ok.' if d.run_ok else 'RUN FAILED.'}",
        "",
    ]
    lines += [f"**{d.headline}**", ""]
    if not d.run_ok:
        lines += ["## What went wrong", *[f"- {p}" for p in d.problems], ""]
        lines += ["Nothing below is a statement that the network is fine: there is not enough data to say."]
        if d.pipeline:
            lines += ["", "## Pipeline", *_pipeline(d)]
        return clip(lines)
    lines += ["## Pipeline", *_pipeline(d), ""]
    lines += ["## Systems", "", "| System | State | Summary |", "| --- | --- | --- |"]
    lines += [f"| {_cell(s.name)} | {ICON.get(s.state, s.state)} | {_cell(s.summary)} |" for s in d.systems]
    lines += ["", f"## Stories ({len(d.stories)}, most important first)", ""]
    if not d.stories:
        lines += ["Nothing needs a look."]
    for i, s in enumerate(d.stories[:MAX_STORIES], 1):
        lines += _story(i, s)
    if len(d.stories) > MAX_STORIES:
        lines += [f"... and {len(d.stories) - MAX_STORIES} more stories; see the host files."]
    if d.developing:
        lines += ["", "## Developing (open more than one day)", ""]
        lines += [f"- {x['title']}: day {x['days']}, {x['trend']}" for x in d.developing[:15]]
    lines += ["", "## Quiet", "", *[f"- {q}" for q in d.quiet]]
    return clip(lines)


def _pipeline(d: Digest) -> list[str]:
    p = d.pipeline
    out = []
    if "events_24h" in p:
        out.append(
            f"- {p['events_24h']:,} events in 24 hours; "
            f"{plural(p.get('new_signatures_24h', 0), 'new signature')}"
        )
    extra = []
    if p.get("parse_failed_24h") is not None:
        extra.append(plural(p["parse_failed_24h"], "parse failure"))
    if p.get("ingest_lag_s") is not None:
        extra.append(f"ingest lag {p['ingest_lag_s']} s")
    if p.get("db_mb") is not None:
        extra.append(f"{p['db_mb']} MB database")
    if extra:
        out.append("- " + "; ".join(extra))
    if p.get("hosts_silent"):
        out.append(f"- {plural(p['hosts_silent'], 'silent host')}")
    if p.get("power"):
        shown = [
            f"{u['label']} {u['watts']:.0f} W"
            + (f", {u['kwh_24h']:.1f} kWh in 24 h" if u.get("kwh_24h") is not None else "")
            for u in p["power"][:4]
            if u.get("watts") is not None
        ]
        if shown:
            out.append("- power: " + "; ".join(shown))
    return out or ["- no pipeline numbers"]


def _story(i: int, s: Story) -> list[str]:
    where = f" ({s.system})" if s.system else ""
    lines = [f"{i}. [{s.route}, rank {s.rank}] {s.title}{where}"]
    if s.grouped:
        lines += [f"   - biggest: {t}" for t in s.top]
        if len(s.members) > len(s.top):
            lines.append(f"   - and {len(s.members) - len(s.top)} more")
    for key in ("state", "worst_severity", "today", "median", "silent_minutes", "last_seen", "hour_utc"):
        if not s.grouped and key in s.detail and s.detail[key] is not None:
            value = s.detail[key]
            value = f"{value:g}" if isinstance(value, float) else value
            lines.append(f"   - {key.replace('_', ' ')}: {value}")
    if not s.grouped and s.detail.get("advice"):
        bucket = f" ({s.detail['bucket'].replace('_', ' ')})" if s.detail.get("bucket") else ""
        lines.append(f"   - advice{bucket}: {s.detail['advice']}")
    if s.host:
        lines.append(f"   - detail: hosts/{host_file(s.host)}")
    return lines


# ---------------------------------------------------------------- one host


@dataclass
class HostFacts:
    host: str
    system: str | None = None
    system_name: str | None = None
    last_seen: str | None = None
    events_24h: int = 0
    expected_gap_min: int | None = None
    top: list[dict] = field(default_factory=list)  # {sig_id, label, count, worst}
    samples: dict[int, list[tuple[str, str]]] = field(default_factory=dict)  # sig_id -> [(ts, msg)]


def host_markdown(facts: HostFacts, findings: list, run_ok: bool = True) -> list[str]:
    name = f" ({facts.system_name or facts.system})" if (facts.system_name or facts.system) else ""
    lines = [f"# {facts.host}{name}", ""]
    seen = f"last heard from {facts.last_seen}" if facts.last_seen else "never heard from"
    gap = f"; normal gap about {facts.expected_gap_min} min" if facts.expected_gap_min else ""
    lines += [f"{seen}{gap}. {facts.events_24h:,} events in the last 24 hours.", ""]
    if not run_ok:
        lines += ["The run failed, so there is no assessment of this host today."]
        return clip(lines)
    lines += [f"## Findings ({len(findings)})", ""]
    if not findings:
        lines += ["None."]
    for f in findings:
        lines.append(f"- {f.kind}{'' if f.story else ' (count only)'}: {f.title}")
        for key, value in f.detail.items():
            if isinstance(value, (str, int, float)) and value is not None:
                lines.append(f"  - {key.replace('_', ' ')}: {value}")
    if facts.top:
        lines += [
            "",
            "## Busiest signatures, last 24 hours",
            "",
            "| Count | Worst | Signature |",
            "| ---: | --- | --- |",
        ]
        lines += [f"| {t['count']:,} | {t['worst']} | {_cell(t['label'])} |" for t in facts.top]
    flagged = {f.sig_id: f for f in findings if f.sig_id is not None}
    shown = [(sid, rows) for sid, rows in facts.samples.items() if sid in flagged and rows]
    if shown:
        lines += ["", "## Sample lines for the flagged signatures"]
        for sid, rows in shown:
            lines += ["", f"{flagged[sid].title}"]
            lines += [f"    {ts} {msg[:MAX_SAMPLE_CHARS]}" for ts, msg in rows]
    return clip(lines)
