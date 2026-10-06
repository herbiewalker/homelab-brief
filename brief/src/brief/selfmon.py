"""The product watching itself: memory, disk space, how fast the archive grows, and per-source rates.

Sampled once an hour by the worker into the `metric` table (no extra timers, no per-line work). From
those samples it projects how many days are left before the archive's disk fills, and raises a
`storage_low` finding while there is still time to act. Nothing here writes outside the database.
"""

import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from brief import detect
from brief.detectors import Finding
from brief.store import Store, iso, parse_time

CGROUP = Path("/sys/fs/cgroup")
MB = 1_000_000
GB = 1_000_000_000
KIND = "storage_low"
SYSTEM = "brief"  # findings about the product itself belong to this pseudo-system (a tile of its own)
HISTORY_DAYS = 14  # growth is measured over at most this long
MIN_HISTORY_DAYS = 2  # and never from less than this: a single day's burst is not a trend


def dir_bytes(path: Path | str) -> int:
    """Total size of the files under `path` (0 when it does not exist). Symlinks are not followed."""
    total, stack = 0, [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _number(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return None if text == "max" else int(text)
    except (OSError, ValueError):
        return None


def _stat_value(path: Path, key: str) -> int:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(" ")
            if name == key:
                return int(value)
    except (OSError, ValueError):
        pass
    return 0


def memory_mb(
    root: Path = CGROUP, proc: Path = Path("/proc/self/status")
) -> tuple[float | None, float | None]:
    """(working set in MB, limit in MB) of the whole container: what its memory limit is judged against.
    The working set leaves out the page cache the kernel can drop, like `docker stats` does. Without
    cgroup files it falls back to this process's resident size, with no limit."""
    used = _number(root / "memory.current")
    limit = _number(root / "memory.max")
    if used is not None:
        used -= _stat_value(root / "memory.stat", "inactive_file")
    else:
        used = _number(root / "memory" / "memory.usage_in_bytes")
        limit = _number(root / "memory" / "memory.limit_in_bytes")
        if used is not None:
            used -= _stat_value(root / "memory" / "memory.stat", "total_inactive_file")
            if limit is not None and limit > 1 << 50:  # cgroup v1 says "no limit" with a huge number
                limit = None
    if used is None:
        try:
            for line in proc.read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    used = int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None, None
    if used is None:
        return None, None
    return round(max(used, 0) / MB, 1), round(limit / MB, 1) if limit else None


def disk(path: Path | str) -> tuple[int, int] | None:
    """(free bytes, total bytes) of the disk holding `path`, or None when it is not there."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return usage.free, usage.total


def record(store: Store, now: datetime, archive_dir: Path | str, state_dir: Path | str) -> dict:
    """Sample archive size and free space (and memory) into the metric table, and return the sample."""
    sample: dict[str, float] = {}
    if Path(archive_dir).exists():
        sample["storage.archive_bytes"] = dir_bytes(archive_dir)
        found = disk(archive_dir)
        if found:
            sample["storage.free_bytes"], sample["storage.total_bytes"] = found
    used, _ = memory_mb()
    if used is not None:
        sample["process.memory_mb"] = used
    if sample:
        with store.write() as tx:
            tx.add_metrics([(now, key, value) for key, value in sample.items()])
    return sample


def _latest(db, metric_id: str) -> float | None:
    row = db.execute("SELECT value FROM metric WHERE metric_id = ? ORDER BY ts DESC LIMIT 1", (metric_id,))
    row = row.fetchone()
    return row["value"] if row else None


def growth_per_day(store: Store, now: datetime, history_days: int = HISTORY_DAYS) -> float | None:
    """Bytes the archive grows per day, from the oldest sample in the window to the newest, or None until
    there are at least MIN_HISTORY_DAYS of samples."""
    cutoff = iso(now - timedelta(days=history_days))
    with store.read() as db:
        rows = db.execute(
            "SELECT ts, value FROM metric WHERE metric_id = 'storage.archive_bytes' AND ts >= ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
    if len(rows) < 2:
        return None
    span = (parse_time(rows[-1]["ts"]) - parse_time(rows[0]["ts"])).total_seconds() / 86400
    if span < MIN_HISTORY_DAYS:
        return None
    return (rows[-1]["value"] - rows[0]["value"]) / span


def numbers(store: Store, now: datetime) -> dict:
    """The self numbers for status.json and the sensors; a number is None while it cannot be known."""
    with store.read() as db:
        archive = _latest(db, "storage.archive_bytes")
        free = _latest(db, "storage.free_bytes")
        total = _latest(db, "storage.total_bytes")
        hour = db.execute(
            "SELECT COALESCE(SUM(n), 0) FROM hourly WHERE day || printf('%02d', hour) >= ?",
            (f"{now - timedelta(hours=1):%Y-%m-%d}{(now - timedelta(hours=1)).hour:02d}",),
        ).fetchone()[0]
    used, limit = memory_mb()
    growth = growth_per_day(store, now)
    days = round(free / growth, 1) if free is not None and growth and growth > 0 else None
    return {
        "memory_mb": used,
        "memory_limit_mb": limit,
        "archive_mb": round(archive / MB, 1) if archive is not None else None,
        "archive_free_gb": round(free / GB, 1) if free is not None else None,
        "archive_free_pct": round(100 * free / total, 1) if free is not None and total else None,
        "storage_days_left": days,
        "events_last_hour": int(hour),
    }


def storage_findings(nums: dict, env=None) -> list[Finding]:
    """A storage_low finding while the archive disk is nearly full or projected to fill soon."""
    rules = detect.thresholds(env)["storage_low"]
    pct, days, free = nums.get("archive_free_pct"), nums.get("storage_days_left"), nums.get("archive_free_gb")
    reasons = []
    if pct is not None and pct < rules["min_free_pct"]:
        reasons.append(f"only {pct:g}% of the archive disk is free ({free:g} GB)")
    if days is not None and days < rules["min_days_until_full"]:
        reasons.append(f"at the current rate the archive disk is full in about {days:g} days")
    if not reasons:
        return []
    value = float(days) if days is not None else float(pct)
    return [
        Finding(
            id="storage_low-archive",
            system=SYSTEM,
            kind=KIND,
            title="Archive disk space is running low: " + "; ".join(reasons),
            value=value,
            detail={"free_pct": pct, "free_gb": free, "days_left": days},
            escalates=False,
        )
    ]


def source_rates(store: Store, now: datetime) -> dict[str, int]:
    """Events received per sender in the last hour (counted plus suppressed)."""
    low = now - timedelta(hours=1)
    key = f"{low:%Y-%m-%d}{low.hour:02d}"
    out: dict[str, int] = {}
    with store.read() as db:
        for table in ("hourly", "suppressed"):
            for r in db.execute(
                f"SELECT host, SUM(n) AS n FROM {table} WHERE day || printf('%02d', hour) >= ? GROUP BY host",
                (key,),
            ):
                out[r["host"]] = out.get(r["host"], 0) + int(r["n"])
    return out
