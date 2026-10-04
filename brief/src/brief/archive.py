"""Compress finished days of the raw syslog archive.

Vector writes /archive/<YYYY-MM-DD>/<source-ip>.log (days are UTC). Once a day is over, each file
is gzip-compressed next to itself and the original removed, but only after the compressed copy has
been read back and matches. Files still being written (modified recently) are skipped, other
folders such as import/ are left alone, and nothing changes unless --apply is given.

  python -m brief.archive compress [--archive /archive] [--apply] [--min-age 600]
"""

import argparse
import gzip
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

DAY_DIR = re.compile(r"^\d{4}-\d\d-\d\d$")
CHUNK = 1 << 20


@dataclass
class Report:
    compressed: int = 0
    finished: int = 0  # a verified .gz already existed; only the original was removed
    skipped_young: int = 0
    errors: list[str] = field(default_factory=list)
    bytes_before: int = 0
    bytes_after: int = 0
    lines: list[str] = field(default_factory=list)


def finished_days(archive: Path, today: date) -> list[Path]:
    if not archive.is_dir():
        return []
    days = []
    for p in sorted(archive.iterdir()):
        if p.is_dir() and DAY_DIR.match(p.name):
            try:
                if date.fromisoformat(p.name) < today:
                    days.append(p)
            except ValueError:
                continue
    return days


def _gz_size(path: Path) -> int:
    total = 0
    with gzip.open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            total += len(chunk)
    return total


def _compress(src: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    with src.open("rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, CHUNK)
    if _gz_size(tmp) != src.stat().st_size:
        tmp.unlink()
        raise OSError("compressed copy did not match the original")
    st = src.stat()
    os.utime(tmp, (st.st_atime, st.st_mtime))
    os.replace(tmp, dest)


def compress(
    archive: Path,
    today: date | None = None,
    apply: bool = False,
    min_age_secs: int = 600,
    now: float | None = None,
) -> Report:
    today = today or datetime.now(UTC).date()
    now = time.time() if now is None else now
    report = Report()
    verb = "compress" if apply else "would compress"
    for day in finished_days(archive, today):
        for src in sorted(day.glob("*.log")):
            gz = src.with_name(src.name + ".gz")
            try:
                size = src.stat().st_size
                if now - src.stat().st_mtime < min_age_secs:
                    report.skipped_young += 1
                    report.lines.append(f"skip (still fresh): {src}")
                    continue
                if gz.exists():
                    if _gz_size(gz) != size:
                        report.errors.append(f"{src}: a .gz exists but does not match; left both")
                        continue
                    report.finished += 1
                    report.lines.append(
                        f"{'remove original of' if apply else 'would remove original of'} {gz}"
                    )
                    if apply:
                        src.unlink()
                    continue
                report.lines.append(f"{verb}: {src} ({size} bytes)")
                report.bytes_before += size
                if apply:
                    _compress(src, gz)
                    report.bytes_after += gz.stat().st_size
                    src.unlink()
                report.compressed += 1
            except OSError as exc:
                report.errors.append(f"{src}: {exc}")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="brief.archive", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compress", help="compress finished days (dry run unless --apply)")
    c.add_argument("--archive", type=Path, default=Path("/archive"))
    c.add_argument("--apply", action="store_true", help="actually compress and remove the originals")
    c.add_argument("--min-age", type=int, default=600, help="skip files modified within this many seconds")
    args = ap.parse_args(argv)

    report = compress(args.archive, apply=args.apply, min_age_secs=args.min_age)
    print("\n".join(report.lines))
    mode = "applied" if args.apply else "dry run, nothing changed (use --apply)"
    saved = f", {report.bytes_before} -> {report.bytes_after} bytes" if args.apply else ""
    print(
        f"archive compress ({mode}): {report.compressed} compressed, {report.finished} finished, "
        f"{report.skipped_young} fresh skipped, {len(report.errors)} errors{saved}"
    )
    for err in report.errors:
        print(f"error: {err}", file=sys.stderr)
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
