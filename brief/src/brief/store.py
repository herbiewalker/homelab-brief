"""The SQLite store: schema migrations, one writer at a time, and the operations the pipeline needs.

SQLite in WAL mode lives on the pool path (never /mnt/user). One process holds a Store; the worker
and the web server each open their own, and SQLite arbitrates between them. Writes go through
Store.write(), which serializes threads and starts one IMMEDIATE transaction, so a whole cycle
commits together (no per-line writes). Readers use read-only connections, which WAL lets run
alongside the writer.

Times are UTC text like 2026-09-26T13:22:33Z, so they compare correctly as strings.
"""

import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
QUIET_GAP = timedelta(hours=24)
ALERT_COLUMNS = ("kind", "rule", "level", "state", "first_sent", "last_sent", "last_value", "snooze_until")
ALERT_STATES = ("open", "acked", "snoozed", "resolved")


class StoreError(Exception):
    pass


def iso(ts: datetime | str) -> str:
    """UTC text for a datetime (naive means UTC) or an already formatted string."""
    if isinstance(ts, datetime):
        if ts.tzinfo is not None:
            ts = ts.astimezone(UTC)
        return ts.strftime(TIME_FORMAT)
    if isinstance(ts, str) and TIME_RE.match(ts):
        return ts
    raise StoreError(f"not a UTC time like 2026-09-26T13:22:33Z: {ts!r}")


def parse_time(ts: str) -> datetime:
    return datetime.strptime(iso(ts), TIME_FORMAT).replace(tzinfo=UTC)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found = []
    for path in sorted(directory.glob("*.sql")):
        m = re.match(r"^(\d{4})_(.+)\.sql$", path.name)
        if not m:
            raise StoreError(f"migration file names look like 0001_name.sql, got {path.name}")
        found.append(Migration(int(m.group(1)), m.group(2), path.read_text(encoding="utf-8")))
    if [m.version for m in found] != list(range(1, len(found) + 1)):
        raise StoreError("migration numbers must run 1, 2, 3 with no gaps or repeats")
    return found


def _connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=5)
    else:
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(
    conn: sqlite3.Connection, path: str | Path, migrations: list[Migration], backup: bool = True
) -> int:
    """Apply pending migrations, each in its own transaction. Returns the resulting version."""
    current, latest = _version(conn), len(migrations)
    if current > latest:
        raise StoreError(
            f"the database is at schema {current} but this program only knows {latest}; refusing to touch it"
        )
    pending = [m for m in migrations if m.version > current]
    if not pending:
        return current
    if backup and current > 0 and str(path) != ":memory:":
        dest = Path(f"{path}.pre-v{current}")
        with sqlite3.connect(str(dest)) as target:
            conn.backup(target)
        target.close()
    for m in pending:
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{m.sql}\nPRAGMA user_version = {m.version};\nCOMMIT;")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise StoreError(f"migration {m.version:04d}_{m.name} failed and was rolled back: {exc}") from exc
    return _version(conn)


class Tx:
    """Write operations. Only valid inside `with store.write() as tx:`."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    # --- signatures ---
    def signature_for(
        self, generation: int, cluster_id: int, app: str | None, template: str | None, ts: datetime | str
    ) -> int:
        """Our sig_id for a Drain3 cluster, created on first sight. Never reuses an id."""
        row = self.db.execute(
            "SELECT sig_id, template FROM signature WHERE miner_generation = ? AND cluster_id = ?",
            (generation, cluster_id),
        ).fetchone()
        if row is not None:
            if template is not None and template != row["template"]:
                self.db.execute(
                    "UPDATE signature SET template = ? WHERE sig_id = ?", (template, row["sig_id"])
                )
            return row["sig_id"]
        t = iso(ts)
        cur = self.db.execute(
            "INSERT INTO signature (miner_generation, cluster_id, app, template, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (generation, cluster_id, app, template, t, t),
        )
        return cur.lastrowid

    def touch_signature(
        self, sig_id: int, first_ts: datetime | str, last_ts: datetime | str, quiet_gap: timedelta = QUIET_GAP
    ) -> None:
        """Record that events with this signature occurred between first_ts and last_ts.

        first_seen only moves earlier (a backfill), last_seen only later. When activity resumes
        after at least quiet_gap of silence, prev_last_seen keeps the last_seen from before that
        gap, which is what "returned after N quiet days" needs. Feed one UTC day's range per call:
        a gap of a day or more cannot hide inside a day, so a batch that spans a backfill is fed
        day by day, oldest first.
        """
        row = self.db.execute(
            "SELECT first_seen, last_seen, prev_last_seen FROM signature WHERE sig_id = ?", (sig_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"no signature {sig_id}")
        first, last = iso(first_ts), iso(last_ts)
        prev = row["prev_last_seen"]
        if first > row["last_seen"] and parse_time(first) - parse_time(row["last_seen"]) >= quiet_gap:
            prev = row["last_seen"]
        self.db.execute(
            "UPDATE signature SET first_seen = ?, last_seen = ?, prev_last_seen = ? WHERE sig_id = ?",
            (min(first, row["first_seen"]), max(last, row["last_seen"]), prev, sig_id),
        )

    def set_ignored(self, sig_id: int, ignored: bool) -> None:
        self.db.execute("UPDATE signature SET ignored = ? WHERE sig_id = ?", (int(ignored), sig_id))

    # --- counts, hosts, samples, metrics ---
    def add_hourly(self, rows: Iterable[tuple[str, int, str, int, str, int]]) -> None:
        """Add counts: (day, hour, host, sig_id, severity, n). Counts accumulate on the same key."""
        self.db.executemany(
            "INSERT INTO hourly (day, hour, host, sig_id, severity, n) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (day, hour, host, sig_id, severity) DO UPDATE SET n = n + excluded.n",
            rows,
        )

    def touch_host(
        self,
        host: str,
        first_ts: datetime | str,
        last_ts: datetime | str,
        system_id: str | None = None,
        known: bool = True,
    ) -> None:
        first, last = iso(first_ts), iso(last_ts)
        self.db.execute(
            "INSERT INTO host (host, system_id, first_seen, last_seen, known) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (host) DO UPDATE SET "
            "first_seen = MIN(first_seen, excluded.first_seen), "
            "last_seen = MAX(last_seen, excluded.last_seen), "
            "system_id = COALESCE(excluded.system_id, system_id), "
            "known = excluded.known",
            (host, system_id, first, last, int(known)),
        )

    def set_expected_gap(self, host: str, minutes: int) -> None:
        self.db.execute("UPDATE host SET expected_gap_min = ? WHERE host = ?", (minutes, host))

    def add_samples(
        self, sig_id: int, rows: Iterable[tuple[datetime | str, str | None, str | None]], keep: int = 5
    ):
        """Keep the newest few raw lines per signature: rows are (ts, host, msg)."""
        self.db.executemany(
            "INSERT INTO sample (sig_id, ts, host, msg) VALUES (?, ?, ?, ?)",
            [(sig_id, iso(ts), host, msg) for ts, host, msg in rows],
        )
        self.db.execute(
            "DELETE FROM sample WHERE sig_id = ? AND rowid NOT IN "
            "(SELECT rowid FROM sample WHERE sig_id = ? ORDER BY ts DESC, rowid DESC LIMIT ?)",
            (sig_id, sig_id, keep),
        )

    def add_metrics(self, rows: Iterable[tuple[datetime | str, str, float]]) -> None:
        self.db.executemany(
            "INSERT INTO metric (ts, metric_id, value) VALUES (?, ?, ?)",
            [(iso(ts), metric_id, value) for ts, metric_id, value in rows],
        )

    def add_suppressed(self, rows: Iterable[tuple[str, int, str, str, int]]) -> None:
        """Count events removed by an ignore rule: (day, hour, host, rule, n)."""
        self.db.executemany(
            "INSERT INTO suppressed (day, hour, host, rule, n) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (day, hour, host, rule) DO UPDATE SET n = n + excluded.n",
            rows,
        )

    # --- ingest offsets ---
    def offsets(self) -> dict[str, int]:
        return {r["path"]: r["offset"] for r in self.db.execute("SELECT path, offset FROM ingest_offset")}

    def set_offsets(self, offsets: dict[str, int], ts: datetime | str | None = None) -> None:
        now = iso(ts or datetime.now(UTC))
        self.db.executemany(
            "INSERT INTO ingest_offset (path, offset, updated) VALUES (?, ?, ?) "
            "ON CONFLICT (path) DO UPDATE SET offset = excluded.offset, updated = excluded.updated",
            [(path, offset, now) for path, offset in offsets.items()],
        )

    def forget_offsets(self, paths: Iterable[str]) -> None:
        self.db.executemany("DELETE FROM ingest_offset WHERE path = ?", [(p,) for p in paths])

    # --- admin audit, alerts, feedback ---
    def audit(
        self, user: str, action: str, detail: str | None = None, ts: datetime | str | None = None
    ) -> None:
        self.db.execute(
            "INSERT INTO audit_log (ts, user, action, detail) VALUES (?, ?, ?, ?)",
            (iso(ts or datetime.now(UTC)), user, action, detail),
        )

    def upsert_alert(self, finding_id: str, **fields) -> None:
        """Create or update an alert, changing only the fields given."""
        unknown = set(fields) - set(ALERT_COLUMNS)
        if unknown:
            raise StoreError(f"unknown alert fields: {sorted(unknown)}")
        if "state" in fields and fields["state"] not in ALERT_STATES:
            raise StoreError(f"alert state must be one of {ALERT_STATES}, got {fields['state']!r}")
        columns = ["finding_id", *fields]
        updates = ", ".join(f"{c} = excluded.{c}" for c in fields) or "finding_id = finding_id"
        self.db.execute(
            f"INSERT INTO alert ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "
            f"ON CONFLICT (finding_id) DO UPDATE SET {updates}",
            (finding_id, *fields.values()),
        )

    def add_feedback(self, finding_id: str, action: str, ts: datetime | str | None = None) -> None:
        self.db.execute(
            "INSERT INTO feedback (ts, finding_id, action) VALUES (?, ?, ?)",
            (iso(ts or datetime.now(UTC)), finding_id, action),
        )

    # --- program state ---
    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def note_live_data(self, received: datetime | str) -> None:
        """Remember when live (not imported) data first arrived; the learning period counts from it."""
        self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('live_since', ?)", (iso(received),))

    def signature_count(self, generation: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM signature WHERE miner_generation = ?", (generation,)
        ).fetchone()[0]

    def miner_generation(self) -> int:
        """The current Drain3 miner generation (1 until the miner state is ever lost)."""
        value = self.get_meta("miner_generation")
        if value is None:
            self.set_meta("miner_generation", "1")
            return 1
        return int(value)

    def bump_miner_generation(self) -> int:
        """Start a new generation after the miner state was lost, so cluster ids cannot collide."""
        new = self.miner_generation() + 1
        self.set_meta("miner_generation", str(new))
        return new


class Store:
    def __init__(self, path: str | Path, migrations: list[Migration] | None = None, backup: bool = True):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._writer: int | None = None
        self._conn = _connect(path)
        try:
            self.schema_version = migrate(
                self._conn, path, load_migrations() if migrations is None else migrations, backup
            )
        except Exception:
            self._conn.close()
            raise

    @contextmanager
    def write(self) -> Iterator[Tx]:
        """One transaction for everything inside the block: all of it commits, or none of it."""
        if self._writer == threading.get_ident():
            raise StoreError("write() cannot be nested; pass the Tx you already have")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")  # may time out if another process holds the write lock
            self._writer = threading.get_ident()
            try:
                yield Tx(self._conn)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
            finally:
                self._writer = None

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A read-only connection; it can run while another connection is writing."""
        conn = _connect(self.path, readonly=True)
        try:
            yield conn
        finally:
            conn.close()

    def audit(self, user: str, action: str, detail: str | None = None) -> None:
        with self.write() as tx:
            tx.audit(user, action, detail)

    def check(self) -> bool:
        with self._lock:
            return self._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    def close(self) -> None:
        with self._lock:
            self._conn.close()
