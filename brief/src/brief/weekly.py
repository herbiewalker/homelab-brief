"""`brief weekly`: the weekly trend report, `weekly/YYYY-Www.md`, at most 150 lines.

For one ISO week (Monday to Sunday, UTC) against the week before it: how much each system logged,
which signatures moved most, what is new this week, what went quiet, which issues kept coming back
(found by replaying the week's daily runs, so it uses the same detectors and thresholds as every other
report), and which hosts went silent. Everything comes from the baseline database.
"""

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from brief import power, replay
from brief.render_md import clip
from brief.store import Store
from brief.systems import Systems

TOP = 8
RECURRING_MIN_DAYS = 3


def week_of(day: date) -> tuple[date, date, str]:
    monday = day - timedelta(days=day.weekday())
    year, week, _ = monday.isocalendar()
    return monday, monday + timedelta(days=6), f"{year}-W{week:02d}"


def parse_week(text: str) -> date:
    """The Monday of an ISO week written like 2026-W40."""
    year, week = text.upper().split("-W")
    return date.fromisocalendar(int(year), int(week), 1)


def _pct(now: int, before: int) -> str:
    if before == 0:
        return "new" if now else "-"
    change = (now - before) / before * 100
    return f"{change:+.0f}%"


def _pct_float(now: float, before: float) -> str:
    return f"{(now - before) / before * 100:+.0f}%"


def _label(app, template) -> str:
    text = f"{app + ': ' if app else ''}{template or '(no template)'}"
    return text if len(text) <= 100 else text[:97] + "..."


def weekly_markdown(store: Store, systems: Systems, monday: date, env=None) -> list[str]:
    sunday = monday + timedelta(days=6)
    prev_monday = monday - timedelta(days=7)
    w0, w1, p0, p1 = (d.isoformat() for d in (monday, sunday, prev_monday, monday - timedelta(days=1)))
    label = week_of(monday)[2]
    labels = {s.id: s.label for s in systems.systems}
    lines = [
        f"# Weekly report {label}",
        "",
        f"{monday:%a %Y-%m-%d} to {sunday:%a %Y-%m-%d} (UTC), against the week before.",
        "",
    ]
    with store.read() as db:
        host_system = {r["host"]: r["system_id"] for r in db.execute("SELECT host, system_id FROM host")}
        volume: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for table in ("hourly", "suppressed"):
            for r in db.execute(
                f"SELECT host, SUM(CASE WHEN day >= ? THEN n ELSE 0 END) AS this, "
                f"SUM(CASE WHEN day < ? THEN n ELSE 0 END) AS prev FROM {table} "
                "WHERE day BETWEEN ? AND ? GROUP BY host",
                (w0, w0, p0, w1),
            ):
                system = host_system.get(r["host"]) or "other"
                volume[system][0] += r["this"]
                volume[system][1] += r["prev"]
        movers = []
        for r in db.execute(MOVERS_SQL, (w0, w0, p0, w1)):
            movers.append(
                (r["this"] - r["prev"], r["this"], r["prev"], r["host"], _label(r["app"], r["template"]))
            )
        new_sigs = db.execute(NEW_SQL, (f"{w0}T00:00:00Z", f"{w1}T23:59:59Z", w0, w1, TOP)).fetchall()
        quiet = db.execute(QUIET_SQL, (p0, p1, w0, w1, TOP)).fetchall()
        total = db.execute(
            "SELECT COALESCE(SUM(n), 0) FROM hourly WHERE day BETWEEN ? AND ?", (w0, w1)
        ).fetchone()[0]
        failed = db.execute(
            "SELECT COALESCE(SUM(value), 0) FROM metric "
            "WHERE metric_id = 'ingest.parse_failed' AND ts BETWEEN ? AND ?",
            (f"{w0}T00:00:00Z", f"{w1}T23:59:59Z"),
        ).fetchone()[0]

    lines += [
        "## Volume by system",
        "",
        "| System | This week | Last week | Change |",
        "| --- | ---: | ---: | ---: |",
    ]
    for system, (this, prev) in sorted(volume.items(), key=lambda kv: -kv[1][0]):
        lines.append(f"| {labels.get(system, system)} | {this:,} | {prev:,} | {_pct(this, prev)} |")
    if not volume:
        lines.append("| (no data) | 0 | 0 | - |")
    lines += ["", f"{total:,} counted events this week; {int(failed)} parse failures."]

    with store.read() as db:
        usage = power.weekly(db, labels, datetime.combine(monday, time(0, 0), tzinfo=UTC))
    if usage:  # only when Home Assistant power data exists, so a plain install is unchanged
        lines += [
            "",
            "## Power and energy",
            "",
            "| System | kWh this week | Last week | Change | Average draw |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for u in sorted(usage, key=lambda x: -(x["kwh"] or 0)):
            kwh, before = u["kwh"], u["kwh_before"]
            change = _pct_float(kwh, before) if kwh is not None and before else "-"
            avg = f"{u['avg_w']:.0f} W" if u["avg_w"] is not None else "-"
            lines.append(
                f"| {u['label']} | {f'{kwh:.1f}' if kwh is not None else '-'} | "
                f"{f'{before:.1f}' if before is not None else '-'} | {change} | {avg} |"
            )

    lines += ["", "## Biggest movers", ""]
    movers.sort(key=lambda m: (-abs(m[0]), m[3], m[4]))
    shown = [m for m in movers if m[0] != 0][:TOP]
    lines += [f"- {m[4]} ({m[3]}): {m[1]:,} vs {m[2]:,} ({_pct(m[1], m[2])})" for m in shown] or [
        "- nothing moved"
    ]

    lines += ["", "## New this week", ""]
    lines += [f"- {_label(r['app'], r['template'])}: {r['n']:,} events" for r in new_sigs] or [
        "- no new signatures"
    ]

    lines += ["", "## Went quiet (active on 3 or more days last week, nothing this week)", ""]
    lines += [
        f"- {_label(r['app'], r['template'])} ({r['host']}): {r['days']} days last week" for r in quiet
    ] or ["- nothing"]

    rep = replay.replay(store, systems, monday, sunday, time(6, 0), hourly=True, env=env)
    days_open: dict[str, set[date]] = defaultdict(set)
    titles: dict[str, str] = {}
    for run in rep.runs:
        for f in run.findings:
            if f.kind != "absence" and f.story:
                days_open[f.id].add(run.at.date())
                titles[f.id] = f.title
    lines += ["", f"## Recurring issues (open on {RECURRING_MIN_DAYS} or more days)", ""]
    recurring = sorted(
        ((len(d), fid) for fid, d in days_open.items() if len(d) >= RECURRING_MIN_DAYS), reverse=True
    )
    lines += [f"- {titles[fid][:110]}: {n} days" for n, fid in recurring[:TOP]] or ["- none"]
    if len(recurring) > TOP:
        lines.append(f"- and {len(recurring) - TOP} more")

    silent = [(run.at, f) for run in rep.runs for f in run.new if f.kind == "absence"]
    lines += ["", "## Hosts that went silent", ""]
    lines += [f"- {f.title} (from {at:%a %H:%M} UTC)" for at, f in silent[:TOP]] or ["- none"]
    lines += ["", "Per-host detail pages are in each day's folder, under hosts/."]
    return clip(lines)


def write_weekly(store: Store, systems: Systems, out_dir: Path, monday: date, env=None) -> Path:
    path = Path(out_dir) / "weekly" / f"{week_of(monday)[2]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(weekly_markdown(store, systems, monday, env)) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def current_monday(now: datetime | None = None) -> date:
    return week_of((now or datetime.now(UTC)).date())[0]


MOVERS_SQL = (
    "SELECT h.sig_id, h.host, s.app, s.template, "
    "SUM(CASE WHEN h.day >= ? THEN h.n ELSE 0 END) AS this, "
    "SUM(CASE WHEN h.day < ? THEN h.n ELSE 0 END) AS prev "
    "FROM hourly h JOIN signature s ON s.sig_id = h.sig_id "
    "WHERE h.day BETWEEN ? AND ? GROUP BY h.sig_id, h.host"
)
NEW_SQL = (
    "SELECT s.sig_id, s.app, s.template, SUM(h.n) AS n "
    "FROM signature s JOIN hourly h ON h.sig_id = s.sig_id "
    "WHERE s.first_seen BETWEEN ? AND ? AND h.day BETWEEN ? AND ? "
    "GROUP BY s.sig_id ORDER BY n DESC, s.sig_id LIMIT ?"
)
QUIET_SQL = (
    "SELECT s.app, s.template, h.host, COUNT(DISTINCT h.day) AS days "
    "FROM hourly h JOIN signature s ON s.sig_id = h.sig_id WHERE h.day BETWEEN ? AND ? "
    "AND NOT EXISTS (SELECT 1 FROM hourly x WHERE x.sig_id = h.sig_id AND x.host = h.host "
    "AND x.day BETWEEN ? AND ?) "
    "GROUP BY h.sig_id, h.host HAVING days >= 3 ORDER BY days DESC, s.sig_id LIMIT ?"
)
