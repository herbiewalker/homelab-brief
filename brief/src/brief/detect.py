"""Run the detectors against the baseline database.

`brief.detectors` holds the rules as pure functions; this module reads the rows they need, applies the
learning period, and learns each host's normal gap. Reads use a read-only connection, so it is safe
to run next to the worker. Only `learn_gaps` writes (one UPDATE per host).

Windows are rolling and counted in whole UTC hours: "today" is the last 24 hour buckets up to and
including the current (partial) hour, and each earlier window is the 24 buckets before it.
"""

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from brief import detectors as d
from brief.config import ConfigError, config_file, load_mapping
from brief.learning import DEFAULT_DAYS, learning_status
from brief.store import Store, iso, parse_time
from brief.systems import Systems

DEFAULTS = {
    "learning": {"days": DEFAULT_DAYS},
    "novelty": {"quiet_days_before_return": 7},
    "deviation": {"window_days": 14, "min_count": 20, "mad_multiple": 4},
    "off_hours": {"window_days": 14, "min_count": 3},
    "absence": {"gap_multiple": 3, "min_minutes": 30, "max_hours": 24},
    "clock_skew": {"max_minutes": 3},
}
DAILY = ("novelty", "deviation", "off_hours", "clock_skew")
HOURLY = ("absence",)
ALL = HOURLY + DAILY

# the hour bucket as one number (hours since 1970), so windows are plain arithmetic
HIDX = "(CAST(strftime('%s', day) AS INTEGER) / 3600 + hour)"


def thresholds(env=None) -> dict:
    """thresholds from playbook.yaml over the defaults; a missing or broken file means the defaults."""
    try:
        found = load_mapping(config_file("playbook.yaml", env)).get("thresholds") or {}
    except (ConfigError, OSError):
        found = {}
    out = {}
    for name, values in DEFAULTS.items():
        given = found.get(name) if isinstance(found.get(name), dict) else {}
        out[name] = {k: type(v)(given.get(k, v)) for k, v in values.items()}
    return out


def hour_index(now: datetime) -> int:
    return int(now.timestamp()) // 3600


def host_rows(store: Store) -> list[d.HostRow]:
    with store.read() as db:
        rows = db.execute(
            "SELECT host, system_id, known, last_seen, expected_gap_min, clock_skew_s, skew_updated FROM host"
        ).fetchall()
    return [
        d.HostRow(
            r["host"],
            r["system_id"],
            bool(r["known"]),
            r["last_seen"],
            r["expected_gap_min"],
            r["clock_skew_s"],
            r["skew_updated"],
        )  # fmt: skip
        for r in rows
    ]


def absence_findings(store: Store, systems: Systems, rules: dict, now: datetime) -> list[d.Finding]:
    expected = {s.id for s in systems.systems if s.expected}
    return d.absence(host_rows(store), now, rules, expected)


def is_learning(store: Store, days: int, now: datetime) -> bool:
    with store.read() as db:
        row = db.execute("SELECT value FROM meta WHERE key = 'live_since'").fetchone()
    return learning_status(row["value"] if row else None, now, days)["active"]


# ---------------------------------------------------------------- row gathering


def _labels(db, sig_ids) -> dict[int, str]:
    ids = sorted(set(sig_ids))
    out: dict[int, str] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        for r in db.execute(f"SELECT sig_id, app, template FROM signature WHERE sig_id IN ({marks})", chunk):
            out[r["sig_id"]] = d.short_label(r["app"], r["template"])
    return out


def novelty_rows(store: Store, now: datetime, window_hours: int = 24) -> list[d.SignatureRow]:
    since = iso(now - timedelta(hours=window_hours))
    now_h = hour_index(now)
    lo = now_h - window_hours + 1
    with store.read() as db:
        cands = db.execute(
            "SELECT sig_id, app, template, first_seen, last_seen, prev_last_seen, ignored FROM signature "
            "WHERE ignored = 0 AND (first_seen >= ? OR (prev_last_seen IS NOT NULL AND last_seen >= ?))",
            (since, since),
        ).fetchall()
        if not cands:
            return []
        ids = [r["sig_id"] for r in cands]
        worst: dict[int, str] = {}
        busy: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            query = (
                f"SELECT sig_id, host, severity, SUM(n) AS n FROM hourly WHERE sig_id IN ({marks}) "
                f"AND {HIDX} BETWEEN ? AND ? GROUP BY sig_id, host, severity"
            )
            for r in db.execute(query, [*chunk, lo, now_h]):
                busy[r["sig_id"]][r["host"]] += r["n"]
                cur = worst.get(r["sig_id"], "debug")
                if d.SEVERITY_RANK.get(r["severity"], 7) < d.SEVERITY_RANK[cur]:
                    cur = r["severity"]
                worst[r["sig_id"]] = cur
        systems = {r["host"]: r["system_id"] for r in db.execute("SELECT host, system_id FROM host")}
        rows = []
        for r in cands:
            returned_at = None
            if r["prev_last_seen"]:
                found = db.execute(
                    "SELECT MIN(day || 'T' || printf('%02d', hour) || ':00:00Z') FROM hourly "
                    "WHERE sig_id = ? AND day || 'T' || printf('%02d', hour) || ':00:00Z' > ?",
                    (r["sig_id"], r["prev_last_seen"]),
                ).fetchone()[0]
                returned_at = found
            hosts = tuple(h for h, _ in sorted(busy[r["sig_id"]].items(), key=lambda kv: -kv[1])[:3])
            rows.append(
                d.SignatureRow(
                    r["sig_id"],
                    r["app"],
                    r["template"],
                    r["first_seen"],
                    r["last_seen"],
                    r["prev_last_seen"],
                    bool(r["ignored"]),
                    worst.get(r["sig_id"], "info"),
                    hosts,
                    tuple(dict.fromkeys(s for s in (systems.get(h) for h in hosts) if s)),
                    returned_at,
                )  # fmt: skip
            )
    return rows


def series_rows(store: Store, now: datetime, days: int, min_count: int) -> list[d.SeriesRow]:
    now_h = hour_index(now)
    with store.read() as db:
        windows: dict[tuple[str, int], dict[int, int]] = defaultdict(dict)
        query = (
            f"SELECT host, sig_id, ({now_h} - {HIDX}) / 24 AS k, SUM(n) AS n FROM hourly "
            f"WHERE {HIDX} BETWEEN ? AND ? GROUP BY host, sig_id, k"
        )
        for r in db.execute(query, (now_h - 24 * (days + 1) + 1, now_h)):
            windows[(r["host"], r["sig_id"])][r["k"]] = r["n"]
        first = {r["host"]: r["first_seen"] for r in db.execute("SELECT host, first_seen FROM host")}
        systems = {r["host"]: r["system_id"] for r in db.execute("SELECT host, system_id FROM host")}
        busy = {key: w for key, w in windows.items() if w.get(0, 0) >= min_count}
        labels = _labels(db, (sig for _, sig in busy))
    rows = []
    for (host, sig), w in busy.items():
        # only the days the host has existed count as baseline, so a new host is not "usually zero"
        seen_h = int(parse_time(first[host]).timestamp()) // 3600 if host in first else now_h
        available = min(days, max(0, (now_h - seen_h) // 24))
        history = tuple(w.get(k, 0) for k in range(1, available + 1))
        rows.append(d.SeriesRow(host, sig, systems.get(host), w[0], history, labels.get(sig, "")))
    return rows


def hour_rows(store: Store, now: datetime, days: int, min_count: int) -> list[d.HourRow]:
    now_h = hour_index(now)
    lo0 = now_h - 23
    with store.read() as db:
        today = db.execute(
            f"SELECT host, sig_id, hour, SUM(n) AS n FROM hourly WHERE {HIDX} BETWEEN ? AND ? "
            "GROUP BY host, sig_id, hour HAVING SUM(n) >= ?",
            (lo0, now_h, min_count),
        ).fetchall()
        if not today:
            return []
        history: dict[tuple[str, int], set[int]] = defaultdict(set)
        wanted = {(r["host"], r["sig_id"]) for r in today}
        for r in db.execute(
            f"SELECT host, sig_id, hour FROM hourly WHERE {HIDX} BETWEEN ? AND ? GROUP BY host, sig_id, hour",
            (now_h - 24 * (days + 1) + 1, lo0 - 1),
        ):
            if (r["host"], r["sig_id"]) in wanted:
                history[(r["host"], r["sig_id"])].add(r["hour"])
        systems = {r["host"]: r["system_id"] for r in db.execute("SELECT host, system_id FROM host")}
        labels = _labels(db, (r["sig_id"] for r in today))
    return [
        d.HourRow(
            r["host"],
            r["sig_id"],
            systems.get(r["host"]),
            r["hour"],
            r["n"],
            frozenset(history[(r["host"], r["sig_id"])]),
            labels.get(r["sig_id"], ""),
        )  # fmt: skip
        for r in today
    ]


# ---------------------------------------------------------------- entry points


def run(
    store: Store,
    systems: Systems,
    now: datetime | None = None,
    env=None,
    kinds: tuple[str, ...] = ALL,
    ignore_learning: bool = False,
) -> dict:
    """{"learning": bool, "findings": [Finding...]} for the chosen detector kinds."""
    now = now or datetime.now(UTC)
    t = thresholds(env)
    learning = False if ignore_learning else is_learning(store, t["learning"]["days"], now)
    findings: list[d.Finding] = []
    if "absence" in kinds:
        findings += absence_findings(store, systems, t["absence"], now)
    if "clock_skew" in kinds:
        findings += d.clock_skew(host_rows(store), now, t["clock_skew"]["max_minutes"])
    if "novelty" in kinds and not learning:
        findings += d.novelty(
            novelty_rows(store, now), now, learning, t["novelty"]["quiet_days_before_return"]
        )
    if "deviation" in kinds and not learning:
        dev = t["deviation"]
        rows = series_rows(store, now, dev["window_days"], dev["min_count"])
        findings += d.deviation(rows, learning, dev["min_count"], dev["mad_multiple"])
    if "off_hours" in kinds and not learning:
        oh = t["off_hours"]
        findings += d.off_hours(
            hour_rows(store, now, oh["window_days"], oh["min_count"]), learning, oh["min_count"]
        )
    return {"learning": learning, "findings": findings}


def learn_gaps(store: Store, now: datetime | None = None, days: int = 14) -> int:
    """Set each host's normal gap from the hours it was active (hourly and suppressed counts both
    prove a host was alive). Returns how many hosts have a learned gap."""
    now = now or datetime.now(UTC)
    now_h = hour_index(now)
    lo = now_h - 24 * days
    with store.read() as db:
        active: dict[str, set[int]] = defaultdict(set)
        for table in ("hourly", "suppressed"):
            for r in db.execute(
                f"SELECT DISTINCT host, {HIDX} AS h FROM {table} WHERE {HIDX} BETWEEN ? AND ?", (lo, now_h)
            ):
                active[r["host"]].add(r["h"])
        hosts = [r["host"] for r in db.execute("SELECT host FROM host")]
    learned = 0
    with store.write() as tx:
        for host in hosts:
            gap = d.expected_gap(sorted(active.get(host, ())))
            tx.set_expected_gap(host, gap)
            learned += gap is not None
    return learned


def as_json(result: dict) -> str:
    return json.dumps(
        {"learning": result["learning"], "findings": [f.__dict__ for f in result["findings"]]}, indent=2
    )
