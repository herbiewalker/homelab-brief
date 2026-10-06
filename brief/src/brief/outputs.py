"""The daily run: detect, compose the digest, and write the files in /out.

  latest/status.json           machine-readable state for Home Assistant and dashboards
  latest/digest.md             ranked stories for people and AI assistants
  latest/brief.html, board.html, status.png   the static views (render.py); brief.html may carry AI prose
  YYYY-MM-DD/digest.md         the same, dated
  YYYY-MM-DD/hosts/<host>.md   one page per host with findings (or all hosts, see HOST_PAGES)
  state.json                   developing issues carried between days

Every Markdown file is at most 150 lines. Files are written atomically (a tablet or an assistant never
reads half a file). A run that fails writes `run_ok: false` and a "No data today" digest, and keeps the
previous state.json so a failed night does not resolve real issues.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from brief import ai, alerts, detect, grouping, issues, render, status
from brief import digest as dg
from brief.detectors import Finding
from brief.jsonlog import log
from brief.render_md import HostFacts, digest_markdown, host_markdown
from brief.routing import Priorities
from brief.store import Store, iso
from brief.systems import Systems

TOP_SIGNATURES = 10
SAMPLES_PER_SIGNATURE = 3
HOST_PAGES = "findings"  # which hosts get a page: those with findings (the digest links to them)


# ---------------------------------------------------------------- files


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_lines(path: Path, lines: list[str]) -> None:
    atomic_write(path, "\n".join(lines) + "\n")


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, doc: dict) -> None:
    atomic_write(path, json.dumps(doc, indent=2, sort_keys=False) + "\n")


# ---------------------------------------------------------------- numbers and per-host facts


def pipeline_numbers(
    store: Store, systems: Systems, now: datetime, env=None, state_dir: Path | None = None
) -> dict:
    nums = status.derive(store, systems, status.absence_thresholds(env), now)
    cutoff = iso(now - timedelta(hours=24))
    with store.read() as db:
        failed = db.execute(
            "SELECT COALESCE(SUM(value), 0) FROM metric WHERE metric_id = 'ingest.parse_failed' AND ts >= ?",
            (cutoff,),
        ).fetchone()[0]
        lag = db.execute(
            "SELECT value FROM metric WHERE metric_id = 'ingest.lag_seconds' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    size = None
    path = Path(state_dir or os.environ.get("BRIEF_STATE", "/state")) / "baseline.db"
    if path.exists():
        size = round(path.stat().st_size / 1e6, 1)
    return {
        "events_24h": nums["events_24h"],
        "new_signatures_24h": nums["new_signatures_24h"],
        "hosts_silent": nums["hosts_silent"],
        "hosts_checked": nums["hosts_checked"],
        "parse_failed_24h": int(failed),
        "ingest_lag_s": int(lag["value"]) if lag else None,
        "db_mb": size,
    }


SEVERITIES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")
_RANK = " ".join(f"WHEN '{name}' THEN {i}" for i, name in enumerate(SEVERITIES))
WORST_SQL = f"MIN(CASE h.severity {_RANK} ELSE 7 END)"
HOURLY_IDX = "(CAST(strftime('%s', h.day) AS INTEGER) / 3600 + h.hour)"


def host_facts(
    store: Store, host: str, now: datetime, findings: list[Finding], labels: dict[str, str]
) -> HostFacts:
    now_h = detect.hour_index(now)
    lo = now_h - 23
    with store.read() as db:
        row = db.execute(
            "SELECT system_id, last_seen, expected_gap_min FROM host WHERE host = ?", (host,)
        ).fetchone()
        counted = db.execute(
            f"SELECT COALESCE(SUM(n), 0) FROM hourly WHERE host = ? AND {detect.HIDX} BETWEEN ? AND ?",
            (host, lo, now_h),
        ).fetchone()[0]
        suppressed = db.execute(
            f"SELECT COALESCE(SUM(n), 0) FROM suppressed WHERE host = ? AND {detect.HIDX} BETWEEN ? AND ?",
            (host, lo, now_h),
        ).fetchone()[0]
        top = []
        query = (
            f"SELECT h.sig_id, s.app, s.template, SUM(h.n) AS n, {WORST_SQL} AS worst "
            "FROM hourly h JOIN signature s ON s.sig_id = h.sig_id "
            f"WHERE h.host = ? AND {HOURLY_IDX} BETWEEN ? AND ? GROUP BY h.sig_id ORDER BY n DESC LIMIT ?"
        )
        for r in db.execute(query, (host, lo, now_h, TOP_SIGNATURES)):
            label = f"{r['app'] + ': ' if r['app'] else ''}{r['template'] or ''}"[:140]
            top.append(
                {"sig_id": r["sig_id"], "label": label, "count": r["n"], "worst": SEVERITIES[r["worst"]]}
            )
        samples = {}
        for f in findings:
            if f.sig_id is not None and f.sig_id not in samples:
                found = db.execute(
                    "SELECT ts, msg FROM sample WHERE sig_id = ? AND host = ? ORDER BY ts DESC LIMIT ?",
                    (f.sig_id, host, SAMPLES_PER_SIGNATURE),
                )
                samples[f.sig_id] = [(r["ts"], r["msg"] or "") for r in found]
    system = row["system_id"] if row else None
    return HostFacts(
        host=host,
        system=system,
        system_name=labels.get(system or ""),
        last_seen=row["last_seen"] if row else None,
        events_24h=int(counted + suppressed),
        expected_gap_min=row["expected_gap_min"] if row else None,
        top=top,
        samples=samples,
    )


# ---------------------------------------------------------------- the daily run


def run_daily(
    store: Store,
    systems: Systems,
    now: datetime | None = None,
    out_dir: Path | str = "/out",
    env=None,
    state_dir: Path | None = None,
    extra_problems: list[str] | None = None,
    network: dict | None = None,
    write: bool = True,
    ignore_learning: bool = False,
    notifier=None,
    alerts_on: bool = False,
) -> dg.Digest:
    """Run every detector, compose the digest, and write the files. Never raises: a failure becomes a
    "No data today" digest (and the files say so), because that is better than no files at all."""
    now = now or datetime.now(UTC)
    out = Path(out_dir)
    problems = list(extra_problems or [])
    findings: list[Finding] = []
    learning = {"active": True, "day": 0, "of": detect.thresholds(env)["learning"]["days"]}
    try:
        result = detect.run(store, systems, now, env, ignore_learning=ignore_learning)
        findings = result["findings"]
        learning = status.learning_status_for(store, now, env)
        pipeline = pipeline_numbers(store, systems, now, env, state_dir)
    except Exception as exc:  # the database or a detector failed: say so, never go quiet
        log("daily_failed", error=f"{type(exc).__name__}: {exc}")
        problems.append(f"the detectors failed ({type(exc).__name__})")
        pipeline = {"events_24h": 0}
    prev_state = read_json(out / "state.json")
    ok_so_far = not problems and pipeline.get("events_24h", 0) > 0
    state = (
        issues.update(prev_state, [f for f in findings if f.story], now.date()) if ok_so_far else prev_state
    )
    d = dg.compose(
        findings, systems, Priorities.load(env), pipeline, learning, now, state=state, problems=problems
    )
    if write:
        write_daily(store, systems, d, state if d.run_ok else prev_state, out, now, env, network)
        if alerts_on:
            run_daily_alerts(store, d, Priorities.load(env), now, notifier)
        # last, so a slow or broken model can only delay the prose, never the files, status, or alerts
        prose = ai.morning_brief(store, d, now.strftime("%Y-%m-%d"), now, env)
        if prose:
            render.write_views(d, out, now.strftime("%Y-%m-%d"), prose, env, only=("brief.html",))
    return d


def write_daily(
    store, systems, d: dg.Digest, state: dict | None, out: Path, now: datetime, env=None, network=None
) -> None:
    day = now.strftime("%Y-%m-%d")
    labels = {s.id: s.label for s in systems.systems}
    md = digest_markdown(d, day)
    write_lines(out / "latest" / "digest.md", md)
    write_lines(out / day / "digest.md", md)
    if d.run_ok:
        by_host: dict[str, list[Finding]] = {}
        for f in d.findings:
            if f.host:
                by_host.setdefault(f.host, []).append(f)
        for host, found in by_host.items():
            facts = host_facts(store, host, now, found, labels)
            write_lines(out / day / "hosts" / dg.host_file(host), host_markdown(facts, found, True))
        if state is not None:
            write_json(out / "state.json", state)
    render.write_views(d, out, day, env=env)  # the template views; a prose rewrite replaces brief.html later
    doc = status.status_document(d, network=network)
    write_json(out / "latest" / "status.json", doc)
    log("daily_written", run_ok=d.run_ok, stories=len(d.stories), headline=d.headline)


# ---------------------------------------------------------------- alerts: the lifecycle around the runs

PIPELINE_KIND = "pipeline_error"


def pipeline_story(headline: str, priorities: Priorities) -> list[grouping.Story]:
    """A failed run is itself something to be told about (kind pipeline_error, routed in priorities.yaml)."""
    finding = Finding("pipeline_error-daily", PIPELINE_KIND, headline, story=True)
    return grouping.build_stories([finding], priorities)


def run_daily_alerts(store: Store, d: dg.Digest, priorities: Priorities, now: datetime, notifier=None):
    """The alert lifecycle for the daily run, which is also when the daily headline goes out."""
    if d.run_ok:
        stories, evaluated = (
            d.stories,
            {*detect.ALL, PIPELINE_KIND},
        )  # a good run also clears a pipeline alert
    else:
        stories, evaluated = pipeline_story(d.headline, priorities), {PIPELINE_KIND}
    return alerts.run_lifecycle(
        store,
        priorities,
        stories,
        evaluated,
        now,
        headline_run=True,
        base_headline=d.headline,
        notifier=notifier,
    )


def run_hourly_alerts(
    store: Store, systems: Systems, now: datetime, env=None, status_doc: dict | None = None, notifier=None
):
    """The alert lifecycle for the hourly run: absence, and whether the pipeline itself is healthy."""
    priorities = Priorities.load(env)
    rules = status.absence_thresholds(env)
    findings = detect.absence_findings(store, systems, rules, now)
    labels = {s.id: s.label for s in systems.systems}
    stories = grouping.build_stories(findings, priorities, labels)
    if status_doc is not None and not status_doc.get("run_ok", True):
        stories += pipeline_story(status_doc.get("headline", "the pipeline is not healthy"), priorities)
    return alerts.run_lifecycle(
        store, priorities, stories, {"absence", PIPELINE_KIND}, now, headline_run=False, notifier=notifier
    )
