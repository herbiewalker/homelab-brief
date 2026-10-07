"""Search the raw syslog archive by sender, days, and text, with hard limits.

The archive is /archive/<YYYY-MM-DD>/<sender>.log (finished days are gzip-compressed to .log.gz, see
archive.py). This is read-only. A search is bounded three ways so it can never hang the web server or
read the whole archive: a few days at most, a cap on bytes read, and a time limit; when a limit stops
it, the result says so. The sender comes from the user, so it must match a strict pattern before it can
become part of a file name, and the path is checked to stay inside the archive.
"""

import gzip
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from brief import archive

MAX_DAYS = 14
MAX_LINES = 200
MAX_LINE_CHARS = 500
MAX_TEXT = 200
MAX_BYTES = 300 * 1_000_000  # uncompressed bytes read per search
MAX_SECONDS = 15.0
HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.:_-")


class SearchError(ValueError):
    """A search that was refused; the message is safe to show."""


@dataclass
class Result:
    lines: list[dict] = field(default_factory=list)  # newest first: {day, host, text}
    files: int = 0
    bytes_read: int = 0
    stopped: str | None = None  # why the search ended early, if it did


def check_host(host: str) -> str:
    host = host.strip()
    if host and (len(host) > 80 or not set(host) <= HOST_CHARS or host.startswith(".")):
        raise SearchError("A sender is an IP address or a host name (letters, digits, dots, colons, dashes).")
    return host


def check_range(start: date, end: date) -> None:
    if end < start:
        raise SearchError("The end day is before the start day.")
    if (end - start).days + 1 > MAX_DAYS:
        raise SearchError(f"Search at most {MAX_DAYS} days at a time.")


def _files(archive_dir: Path, day: date, host: str) -> list[Path]:
    folder = archive_dir / day.isoformat()
    if not folder.is_dir():
        return []
    names = [f"{host}.log", f"{host}.log.gz"] if host else sorted(p.name for p in folder.iterdir())
    out = []
    for name in names:
        path = folder / name
        if not (name.endswith(".log") or name.endswith(".log.gz")) or not path.is_file():
            continue
        if archive_dir.resolve() not in path.resolve().parents:  # a link out of the archive is not followed
            continue
        out.append(path)
    return out


def _lines(path: Path):
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rb") as handle:
        yield from handle


def search(
    archive_dir: Path | str,
    host: str,
    start: date,
    end: date,
    text: str,
    limit: int = MAX_LINES,
    max_bytes: int = MAX_BYTES,
    max_seconds: float = MAX_SECONDS,
    clock=time.monotonic,
) -> Result:
    """Lines from `host` (or every sender when empty) between the two days (inclusive) that contain `text`
    (case-insensitive, plain text, not a pattern). Newest day first; at most `limit` lines."""
    archive_dir = Path(archive_dir)
    host = check_host(host)
    check_range(start, end)
    text = text.strip()
    if len(text) > MAX_TEXT:
        raise SearchError(f"Search for at most {MAX_TEXT} characters.")
    if not host and not text:
        raise SearchError("Give a sender or some text to look for.")
    needle = text.lower().encode("utf-8")
    result = Result()
    began = clock()
    day = end
    while day >= start and len(result.lines) < limit:
        for path in _files(archive_dir, day, host):
            sender = path.name.removesuffix(".gz").removesuffix(".log")
            kept: deque[bytes] = deque(maxlen=limit - len(result.lines))  # the latest matches of this file
            result.files += 1
            try:
                for raw in _lines(path):
                    result.bytes_read += len(raw)
                    if needle in raw.lower():
                        kept.append(raw)
                    if result.bytes_read >= max_bytes:
                        result.stopped = "the byte limit"
                        break
                    if clock() - began > max_seconds:
                        result.stopped = "the time limit"
                        break
            except (OSError, EOFError):
                continue  # a file still being compressed or cut short: skip it, keep the rest
            for raw in reversed(kept):
                result.lines.append(
                    {
                        "day": day.isoformat(),
                        "host": sender,
                        "text": raw.decode("utf-8", "replace").rstrip("\r\n")[:MAX_LINE_CHARS],
                    }
                )
            if result.stopped or len(result.lines) >= limit:
                break
        if result.stopped:
            break
        day -= timedelta(days=1)
    if len(result.lines) >= limit and not result.stopped:
        result.stopped = f"{limit} lines"
    return result


def days_available(archive_dir: Path | str) -> tuple[date, date] | None:
    """The first and last day in the archive, or None when it is empty (for the form's hint)."""
    days = []
    folder = Path(archive_dir)
    if folder.is_dir():
        for p in folder.iterdir():
            if p.is_dir() and archive.DAY_DIR.match(p.name):
                try:
                    days.append(date.fromisoformat(p.name))
                except ValueError:
                    continue
    return (min(days), max(days)) if days else None
