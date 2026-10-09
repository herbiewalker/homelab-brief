"""Run the detectors against the baseline database.

`brief.detectors` holds the rules as pure functions; this module reads the rows they need, applies the
learning period, and learns each host's normal gap. Reads use a read-only connection, so it is safe
to run next to the worker. Only `learn_gaps` writes (one UPDATE per host).

Windows are rolling and counted in whole UTC hours: "today" is the last 24 hour buckets up to and
including the current (partial) hour, and each earlier window is the 24 buckets before it.
"""

import json
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from brief import detect_hw
from brief import detectors as d
from brief.config import ConfigError, config_file, load_mapping
from brief.learning import DEFAULT_DAYS, learning_status
from brief.store import Store, iso
from brief.systems import Systems

DEFAULTS = {
    "learning": {"days": DEFAULT_DAYS},
    "novelty": {"quiet_days_before_return": 7, "host_active_hours_in_gap": 3, "min_prior_days": 5},
    "deviation": {
        "window_days": 14,
        "min_count": 20,
        "mad_multiple": 4,
        "min_ratio": 3,
        "min_history_days": 3,
    },
    "off_hours": {
        "window_days": 14,
        "min_count": 3,
        "min_history_days": 3,
        "min_history_events": 10,
        "max_usual_hours": 8,
    },
    "absence": {"gap_multiple": 3, "min_minutes": 30, "max_hours": 24},
    "clock_skew": {"max_minutes": 3},
    "metric_drift": {"recent_days": 7, "baseline_days": 21, "min_change_pct": 20, "min_samples": 24},
    "smart_trend": {"window_days": 14},
    "disk_temp_hdd_c": {"warn": 50, "days": 3},
    "parity_slow": {"floor_mb_s": 40, "drop_vs_previous_pct": 50, "max_age_days": 40, "history": 5},
    "version_behind": {"days": 30},
    "new_infrastructure": {"settle_hours": 24, "show_days": 7},
    "storage_low": {"min_free_pct": 15, "min_days_until_full": 14},
}
DAILY = ("novelty", "deviation", "off_hours", "clock_skew", "metric_drift", *detect_hw.KINDS)
HOURLY = ("absence",)
ALL = HOURLY + DAILY

# the hour bucket as one number (hours since 1970), so windows are plain arithmetic
HIDX = "(CAST(strftime('%s', day) AS INTEGER) / 3600 + hour)"


def _merged(found: dict) -> dict:
    out = {}
    for name, values in DEFAULTS.items():
        given = found.get(name) if isinstance(found.get(name), dict) else {}
        out[name] = {k: type(v)(given.get(k, v)) for k, v in values.items()}
    return out


def validate_thresholds(found) -> None:
    """Raise ConfigError when playbook.yaml `thresholds` is not a mapping of mappings of numbers."""
    if found is None:
        return
    if not isinstance(found, dict):
        raise ConfigError("playbook.yaml: thresholds must be a mapping")
    for name in DEFAULTS:
        if name in found and not isinstance(found[name], dict):
            raise ConfigError(f"playbook.yaml: thresholds.{name} must be a mapping")
    try:
        _merged(found)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"playbook.yaml: a threshold is not a number ({exc})") from None


def thresholds(env=None) -> dict:
    """thresholds from playbook.yaml over the defaults; a missing or broken file means the defaults."""
    try:
        found = load_mapping(config_file("playbook.yaml", env)).get("thresholds") or {}
        return _merged(found)
    except (ConfigError, OSError, TypeError, ValueError):
        return _merged({})


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


class HostActivity:
    """When each host was last heard from, as of any past moment, from the hourly and suppressed counts.

    `host.last_seen` only holds the latest value, so replaying an earlier time needs this instead. It
    is built once (one pass over the counts) and answers any number of "as of" questions in memory.
    The precision is the hour bucket; the host's system, known flag, and learned gap are the current
    ones. Clock skew is a live measurement and cannot be replayed."""

    def __init__(self, hours: dict[str, list[int]], hosts: list[d.HostRow]):
        self._hours = hours  # host -> sorted active hour indexes
        self._hosts = hosts

    @classmethod
    def load(cls, store: Store) -> "HostActivity":
        hours: dict[str, set[int]] = defaultdict(set)
        with store.read() as db:
            for table in ("hourly", "suppressed"):
                for r in db.execute(f"SELECT DISTINCT host, {HIDX} AS h FROM {table}"):
                    hours[r["host"]].add(r["h"])
        return cls({h: sorted(v) for h, v in hours.items()}, host_rows(store))

    def host_rows(self, now: datetime) -> list[d.HostRow]:
        """Host rows as they would have looked at `now`: last_seen is the end of the last active hour
        up to then (never later than `now`), and hosts first heard from after `now` are absent."""
        import bisect

        now_h = hour_index(now)
        out = []
        for h in self._hosts:
            hours = self._hours.get(h.host, [])
            i = bisect.bisect_right(hours, now_h)
            if i == 0:
                continue  # not heard from yet at that time
            last = min(_bucket_end(hours[i - 1]), now)
            out.append(d.HostRow(h.host, h.system, h.known, iso(last), h.expected_gap_min))
        return out

    def last_data_hour(self) -> int | None:
        latest = [v[-1] for v in self._hours.values() if v]
        return max(latest) if latest else None


def _bucket_end(hidx: int) -> datetime:
    return datetime.fromtimestamp((hidx + 1) * 3600 - 1, UTC)


def absence_findings(
    store: Store, systems: Systems, rules: dict, now: datetime, activity: HostActivity | None = None
) -> list[d.Finding]:
    expected = {s.id for s in systems.systems if s.expected}
    rows = activity.host_rows(now) if activity is not None else host_rows(store)
    return d.absence(rows, now, rules, expected)


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


def _bucket_iso(hidx: int) -> str:
    return iso(datetime.fromtimestamp(hidx * 3600, UTC))


def novelty_rows(store: Store, now: datetime, window_hours: int = 24) -> list[d.SignatureRow]:
    """The signatures active in the window, with what the novelty rule needs, computed from the hourly
    counts alone so the answer is the same whether this runs live or replays an earlier moment
    (nothing here reads a column that only holds today's value, like signature.prev_last_seen).

    For a signature active in the window: its first hour in the window; the last hour it was active
    before that (the start of any quiet spell); how many days it was seen on before the spell; and how
    many hours its busiest host logged anything meanwhile."""
    now_h = hour_index(now)
    lo = now_h - window_hours + 1
    since = iso(now - timedelta(hours=window_hours))
    with store.read() as db:
        first_in: dict[int, int] = {}
        last_in: dict[int, int] = {}
        worst: dict[int, str] = {}
        busy: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        query = (
            f"SELECT sig_id, host, severity, SUM(n) AS n, MIN({HIDX}) AS lo, MAX({HIDX}) AS hi FROM hourly "
            f"WHERE {HIDX} BETWEEN ? AND ? GROUP BY sig_id, host, severity"
        )
        for r in db.execute(query, (lo, now_h)):
            sid = r["sig_id"]
            busy[sid][r["host"]] += r["n"]
            first_in[sid] = min(first_in.get(sid, r["lo"]), r["lo"])
            last_in[sid] = max(last_in.get(sid, r["hi"]), r["hi"])
            cur = worst.get(sid, "debug")
            if d.SEVERITY_RANK.get(r["severity"], 7) < d.SEVERITY_RANK[cur]:
                cur = r["severity"]
            worst[sid] = cur
        if not first_in:
            return []
        meta = {}
        ids = sorted(first_in)
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for r in db.execute(
                f"SELECT sig_id, app, template, first_seen, ignored FROM signature WHERE sig_id IN ({marks})",
                chunk,
            ):
                meta[r["sig_id"]] = r
        systems = {r["host"]: r["system_id"] for r in db.execute("SELECT host, system_id FROM host")}
        rows = []
        for sid in ids:
            m = meta.get(sid)
            if m is None or m["ignored"]:
                continue
            hosts = tuple(h for h, _ in sorted(busy[sid].items(), key=lambda kv: -kv[1])[:3])
            prev_end = returned_at = host_hours = prior_days = None
            if m["first_seen"] < since:  # not new: was it away for a while and back inside the window?
                first_day = datetime.fromtimestamp(first_in[sid] * 3600, UTC).strftime("%Y-%m-%d")
                before = db.execute(
                    f"SELECT MAX({HIDX}) FROM hourly WHERE sig_id = ? AND day <= ? AND {HIDX} < ?",
                    (sid, first_day, first_in[sid]),
                ).fetchone()[0]
                if before is not None:
                    prev_end = _bucket_iso(before + 1)  # the end of the last hour it was active
                    returned_at = _bucket_iso(first_in[sid])
                    prior_days = db.execute(
                        f"SELECT COUNT(DISTINCT day) FROM hourly WHERE sig_id = ? AND {HIDX} <= ?",
                        (sid, before),
                    ).fetchone()[0]
                    if hosts:  # did its host keep logging (anything) while this signature was away?
                        host_hours = db.execute(
                            f"SELECT COUNT(DISTINCT {HIDX}) FROM hourly "
                            f"WHERE host = ? AND {HIDX} > ? AND {HIDX} < ?",
                            (hosts[0], before, first_in[sid]),
                        ).fetchone()[0]
            rows.append(
                d.SignatureRow(
                    sid,
                    m["app"],
                    m["template"],
                    m["first_seen"],
                    _bucket_iso(last_in[sid]),
                    prev_end,
                    False,
                    worst.get(sid, "info"),
                    hosts,
                    tuple(dict.fromkeys(s for s in (systems.get(h) for h in hosts) if s)),
                    returned_at,
                    host_hours,
                    prior_days,
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
        # the windows in which each host logged anything at all: the baseline only counts those, so a
        # host that was down (or an outage of the collector) does not make everything "usually zero"
        active: dict[str, set[int]] = defaultdict(set)
        host_query = (
            f"SELECT host, ({now_h} - {HIDX}) / 24 AS k FROM hourly WHERE {HIDX} BETWEEN ? AND ? "
            "GROUP BY host, k"
        )
        for r in db.execute(host_query, (now_h - 24 * (days + 1) + 1, now_h)):
            active[r["host"]].add(r["k"])
        systems = {r["host"]: r["system_id"] for r in db.execute("SELECT host, system_id FROM host")}
        busy = {key: w for key, w in windows.items() if w.get(0, 0) >= min_count}
        labels = _labels(db, (sig for _, sig in busy))
    rows = []
    for (host, sig), w in busy.items():
        history = tuple(w.get(k, 0) for k in sorted(active[host]) if 1 <= k <= days)
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
        spans: dict[tuple[str, int], tuple[int, int]] = {}  # (distinct days, events) in the history
        wanted = {(r["host"], r["sig_id"]) for r in today}
        bounds = (now_h - 24 * (days + 1) + 1, lo0 - 1)
        for r in db.execute(
            f"SELECT host, sig_id, hour FROM hourly WHERE {HIDX} BETWEEN ? AND ? GROUP BY host, sig_id, hour",
            bounds,
        ):
            if (r["host"], r["sig_id"]) in wanted:
                history[(r["host"], r["sig_id"])].add(r["hour"])
        for r in db.execute(
            f"SELECT host, sig_id, COUNT(DISTINCT day) AS days, SUM(n) AS n FROM hourly "
            f"WHERE {HIDX} BETWEEN ? AND ? GROUP BY host, sig_id",
            bounds,
        ):
            if (r["host"], r["sig_id"]) in wanted:
                spans[(r["host"], r["sig_id"])] = (r["days"], r["n"])
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
            *spans.get((r["host"], r["sig_id"]), (0, 0)),
        )  # fmt: skip
        for r in today
    ]


def metric_rows(
    store: Store, now: datetime, recent_days: int, baseline_days: int, names: dict[str, str] | None = None
) -> list[d.MetricRow]:
    """The recent and the earlier samples of every collector metric, as of `now` (a replay sees only what
    existed then). Only `collector.*` series are read; the product's own self-monitoring is not judged."""
    names = names or {}
    split = iso(now - timedelta(days=recent_days))
    start = iso(now - timedelta(days=recent_days + baseline_days))
    with store.read() as db:
        found = db.execute(
            "SELECT metric_id, ts, value FROM metric WHERE metric_id LIKE 'collector.%' "
            "AND ts > ? AND ts <= ? AND value IS NOT NULL",
            (start, iso(now)),
        ).fetchall()
    recent: dict[str, list[float]] = defaultdict(list)
    baseline: dict[str, list[float]] = defaultdict(list)
    for row in found:
        key = row["metric_id"][len("collector.") :]
        (recent if row["ts"] > split else baseline)[key].append(row["value"])
    return [
        d.MetricRow(key, names.get(key) or key.replace("_", " "), tuple(recent.get(key, ())), tuple(values))
        for key, values in sorted(baseline.items())
    ]


# ---------------------------------------------------------------- entry points


def run(
    store: Store,
    systems: Systems,
    now: datetime | None = None,
    env=None,
    kinds: tuple[str, ...] = ALL,
    ignore_learning: bool = False,
    activity: HostActivity | None = None,
) -> dict:
    """{"learning": bool, "findings": [Finding...]} for the chosen detector kinds.

    With `activity` the run is "as of" `now` (a replay of an earlier moment): absence reads host
    activity as it was then, and clock skew, which is only measured live, is left out."""
    now = now or datetime.now(UTC)
    t = thresholds(env)
    learning = False if ignore_learning else is_learning(store, t["learning"]["days"], now)
    findings: list[d.Finding] = []
    if "absence" in kinds:
        findings += absence_findings(store, systems, t["absence"], now, activity)
    if "clock_skew" in kinds and activity is None:
        findings += d.clock_skew(host_rows(store), now, t["clock_skew"]["max_minutes"])
    if "novelty" in kinds and not learning:
        nv = t["novelty"]
        findings += d.novelty(
            novelty_rows(store, now),
            now,
            learning,
            nv["quiet_days_before_return"],
            min_host_hours=nv["host_active_hours_in_gap"],
            min_prior_days=nv["min_prior_days"],
        )
    if "deviation" in kinds and not learning:
        dev = t["deviation"]
        rows = series_rows(store, now, dev["window_days"], dev["min_count"])
        findings += d.deviation(
            rows,
            learning,
            dev["min_count"],
            dev["mad_multiple"],
            dev["min_history_days"],
            dev["min_ratio"],
        )
    if "off_hours" in kinds and not learning:
        oh = t["off_hours"]
        findings += d.off_hours(
            hour_rows(store, now, oh["window_days"], oh["min_count"]),
            learning,
            oh["min_count"],
            oh["min_history_days"],
            oh["min_history_events"],
            oh["max_usual_hours"],
        )
    if "metric_drift" in kinds:  # needs its own history (samples), not the learning period
        md = t["metric_drift"]
        findings += d.metric_drift(
            metric_rows(store, now, md["recent_days"], md["baseline_days"], _metric_names(env, systems)),
            md["recent_days"],
            md["baseline_days"],
            md["min_change_pct"],
            md["min_samples"],
        )
    findings += _tag_systems(detect_hw.run(store, now, t, kinds), systems)
    return {"learning": learning, "findings": findings}


def _metric_names(env, systems: Systems) -> dict[str, str]:
    """Readable names for collector metrics (from the registry); empty if the registry cannot be read."""
    from brief import registry

    try:
        return {m["id"]: str(m.get("name") or m["id"]) for m in registry.load(env, systems)}
    except Exception:  # a label is nice to have, never a reason to skip the detector
        return {}


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


def _tag_systems(findings: list[d.Finding], systems: Systems) -> list[d.Finding]:
    """A collector finding names its `server`; when a system with that id exists, the finding belongs to it
    (so it groups and shows under that tile)."""
    known = {s.id for s in systems.systems}
    return [
        replace(f, system=f.detail["server"]) if f.system is None and f.detail.get("server") in known else f
        for f in findings
    ]
