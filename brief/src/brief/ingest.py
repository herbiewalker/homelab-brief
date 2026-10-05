"""The ingest cycle: event files -> signatures, hourly counts, hosts, and samples.

Each cycle reads the NDJSON events Vector wrote, from where the last cycle stopped, and flushes
everything in ONE database transaction, offsets included. That is what makes replays safe: the
offset and the counts it produced commit together, so a crash can neither count a line twice nor
lose one. Only complete lines are consumed; a half-written last line waits for the next cycle.
New files (a new day, the backfill) are picked up by listing the folder each cycle.

Per event: normalize its time to UTC (event time, never read time), map the host to a system,
apply the ignore rules, mine a signature, and add to the in-memory counts.
"""

import json
import statistics
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from brief.ignore import IgnoreRules
from brief.signatures import Signatures
from brief.store import Store, iso
from brief.systems import Systems
from brief.timeparse import event_time, parse_received

SEVERITY_NAMES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")
MAX_SAMPLE_CHARS = 500
MAX_SKEW_SAMPLES = 200  # per host per cycle; the median of these is stored


@dataclass
class CycleResult:
    events: int = 0
    counted: int = 0
    suppressed: int = 0
    malformed: int = 0
    parse_failed: int = 0
    time_fallback: int = 0
    unknown_hosts: int = 0
    new_signatures: int = 0
    files: int = 0
    bytes_read: int = 0
    backlog_bytes: int = 0
    lag_seconds: float | None = None
    truncated: list[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class _Cluster:
    app: str | None
    template: str
    days: dict[str, list[str]] = field(default_factory=dict)  # day -> [first, last] (ISO UTC)
    samples: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class _Acc:
    hourly: Counter = field(default_factory=Counter)
    suppressed: Counter = field(default_factory=Counter)
    clusters: dict[int, _Cluster] = field(default_factory=dict)
    hosts: dict[str, list] = field(default_factory=dict)  # host -> [first, last, system_id, known]
    live_since: str | None = None
    latest_event: datetime | None = None
    skew: dict[str, list[float]] = field(default_factory=dict)  # host -> event minus receive, seconds


class Pipeline:
    def __init__(
        self,
        store: Store,
        events_dir: Path,
        signatures: Signatures,
        systems: Systems,
        ignore: IgnoreRules,
        samples_per_signature: int = 5,
        max_bytes_per_cycle: int = 32 * 1024 * 1024,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.store = store
        self.events_dir = Path(events_dir)
        self.signatures = signatures
        self.systems = systems
        self.ignore = ignore
        self.samples_per_signature = samples_per_signature
        self.max_bytes = max_bytes_per_cycle
        self.clock = clock
        with store.write() as tx:
            self.generation = tx.miner_generation()
            # If the miner's state file is gone but signatures exist, Drain3 would hand out cluster
            # ids that were already used, so start a new generation instead.
            if not signatures.state_existed and tx.signature_count(self.generation) > 0:
                self.generation = tx.bump_miner_generation()
                tx.audit(
                    "system", "miner.generation", f"miner state was lost; new generation {self.generation}"
                )

    def set_config(self, systems: Systems, ignore: IgnoreRules) -> None:
        self.systems, self.ignore = systems, ignore

    # ------------------------------------------------------------------ the cycle

    def run_cycle(self) -> CycleResult:
        started = time.monotonic()
        result = CycleResult()
        acc = _Acc()
        with self.store.read() as db:
            offsets = {r["path"]: r["offset"] for r in db.execute("SELECT path, offset FROM ingest_offset")}
            ignored = {
                (r["miner_generation"], r["cluster_id"]): r["sig_id"]
                for r in db.execute(
                    "SELECT miner_generation, cluster_id, sig_id FROM signature WHERE ignored = 1"
                )
            }
            before = db.execute("SELECT COUNT(*) FROM signature").fetchone()[0]

        files = sorted(self.events_dir.glob("*.ndjson")) if self.events_dir.is_dir() else []
        new_offsets: dict[str, int] = {}
        budget = self.max_bytes
        for path in files:
            result.files += 1
            size = path.stat().st_size
            offset = offsets.get(path.name, 0)
            if size < offset:  # the file was replaced or truncated: start it over
                result.truncated.append(path.name)
                offset = 0
            if size > offset and budget > 0:
                consumed = self._read(path, offset, budget, acc, result, ignored)
                budget -= consumed
                offset += consumed
                result.bytes_read += consumed
            if offset != offsets.get(path.name):
                new_offsets[path.name] = offset
            result.backlog_bytes += max(0, size - offset)

        now = self.clock()
        if acc.latest_event is not None:
            result.lag_seconds = max(0.0, (now - acc.latest_event).total_seconds())
        result.seconds = time.monotonic() - started
        self._flush(acc, result, new_offsets, {p.name for p in files}, offsets, now, before)
        return result

    def _read(
        self, path: Path, offset: int, budget: int, acc: _Acc, result: CycleResult, ignored: dict
    ) -> int:
        consumed = 0
        with path.open("rb") as f:
            f.seek(offset)
            while consumed < budget:
                line = f.readline()
                if not line.endswith(b"\n"):  # nothing, or a half-written line: wait for the rest
                    break
                consumed += len(line)
                if line.strip():
                    self._handle(line, acc, result, ignored)
        return consumed

    # ------------------------------------------------------------------ one event

    def _handle(self, raw: bytes, acc: _Acc, result: CycleResult, ignored: dict) -> None:
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("not an object")
        except ValueError:
            result.malformed += 1
            return
        result.events += 1
        host = str(event.get("source_ip") or "unknown")
        header_host = event.get("header_host")
        resolved = self.systems.resolve(host, header_host if isinstance(header_host, str) else None)
        timed = event_time(event, resolved.source_tz, self.clock())
        when = timed.when
        result.time_fallback += timed.fallback
        if event.get("parse_failed"):
            result.parse_failed += 1

        stamp = iso(when)
        day, hour = stamp[:10], when.hour
        entry = acc.hosts.get(host)
        if entry is None:
            acc.hosts[host] = [stamp, stamp, resolved.system_id, resolved.known]
        else:
            entry[0], entry[1] = min(entry[0], stamp), max(entry[1], stamp)
        if acc.latest_event is None or when > acc.latest_event:
            acc.latest_event = when
        if not event.get("imported"):
            received = parse_received(event.get("timestamp")) or self.clock()
            if acc.live_since is None or iso(received) < acc.live_since:
                acc.live_since = iso(received)
            # a clock-skew sample: only live events whose time came from the device, not from us
            if not timed.fallback:
                samples = acc.skew.setdefault(host, [])
                if len(samples) < MAX_SKEW_SAMPLES:
                    samples.append((when - received).total_seconds())

        app = event.get("app") if isinstance(event.get("app"), str) else ""
        if event.get("parse_failed") and not app:
            app = "(unparsed)"
        msg = str(event.get("msg") or "")

        rule = self.ignore.match(
            source_ip=host,
            header_host=header_host if isinstance(header_host, str) else None,
            system_id=resolved.system_id,
            app=app,
            msg=msg,
        )
        if rule is not None:
            acc.suppressed[(day, hour, host, rule.name)] += 1
            result.suppressed += 1
            return

        cluster = self.signatures.classify(app, msg)
        sig_id = ignored.get((self.generation, cluster.cluster_id))
        if sig_id is not None:
            acc.suppressed[(day, hour, host, f"signature:{sig_id}")] += 1
            result.suppressed += 1
            return

        severity = event.get("severity")
        name = SEVERITY_NAMES[severity] if isinstance(severity, int) and 0 <= severity <= 7 else "info"
        acc.hourly[(day, hour, host, cluster.cluster_id, name)] += 1
        result.counted += 1
        c = acc.clusters.get(cluster.cluster_id)
        if c is None:
            c = acc.clusters[cluster.cluster_id] = _Cluster(app or None, cluster.template)
        c.template = cluster.template
        span = c.days.get(day)
        if span is None:
            c.days[day] = [stamp, stamp]
        else:
            span[0], span[1] = min(span[0], stamp), max(span[1], stamp)
        c.samples.append((stamp, host, msg[:MAX_SAMPLE_CHARS]))
        if len(c.samples) > self.samples_per_signature * 4:  # keep memory bounded within a cycle
            c.samples = sorted(c.samples)[-self.samples_per_signature :]

    # ------------------------------------------------------------------ the one transaction

    def _flush(self, acc, result, new_offsets, present, old_offsets, now, signatures_before) -> None:
        # the miner state goes to disk BEFORE the commit, so it is never older than the database
        self.signatures.save()
        with self.store.write() as tx:
            sig_ids: dict[int, int] = {}
            for cluster_id, c in acc.clusters.items():
                first_day = min(c.days)
                sid = tx.signature_for(self.generation, cluster_id, c.app, c.template, c.days[first_day][0])
                sig_ids[cluster_id] = sid
                for day in sorted(c.days):  # oldest first, so a backfill sets prev_last_seen correctly
                    first, last = c.days[day]
                    tx.touch_signature(sid, first, last)
                tx.add_samples(
                    sid, sorted(c.samples)[-self.samples_per_signature :], keep=self.samples_per_signature
                )
            tx.add_hourly(
                (day, hour, host, sig_ids[cluster_id], severity, n)
                for (day, hour, host, cluster_id, severity), n in acc.hourly.items()
            )
            tx.add_suppressed(
                (day, hour, host, rule, n) for (day, hour, host, rule), n in acc.suppressed.items()
            )
            for host, (first, last, system_id, known) in acc.hosts.items():
                tx.touch_host(host, first, last, system_id=system_id, known=known)
                if not known:
                    result.unknown_hosts += 1
            for host, samples in acc.skew.items():
                tx.set_clock_skew(host, statistics.median(samples), now)
            if acc.live_since is not None:
                tx.note_live_data(acc.live_since)
            tx.set_offsets(new_offsets, now)
            tx.forget_offsets([p for p in old_offsets if p not in present])
            result.new_signatures = max(
                0, tx.db.execute("SELECT COUNT(*) FROM signature").fetchone()[0] - signatures_before
            )
            stamp = iso(now)
            metrics = [
                (stamp, "ingest.events", float(result.events)),
                (stamp, "ingest.backlog_bytes", float(result.backlog_bytes)),
                (stamp, "ingest.cycle_seconds", round(result.seconds, 3)),
            ]
            if result.lag_seconds is not None:
                metrics.append((stamp, "ingest.lag_seconds", round(result.lag_seconds, 1)))
            tx.add_metrics(metrics)
