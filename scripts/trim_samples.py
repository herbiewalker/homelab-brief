#!/usr/bin/env python3
"""Build the small public sample set for this repository from the instance's full sanitized set.

The full sanitized logs stay in the instance folder. The repository only gets a few lines per
message shape per source, enough for parser tests and CI. A line is dropped if it holds a word
from the instance's guard/trim-exclude.txt (for example service names you would rather not
publish), a redacted value, or anything the commit guard objects to.

Usage (from the repo root, in WSL or Linux):
  python3 scripts/trim_samples.py [--per-shape 2] [--max-lines 30]
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import instance  # noqa: E402
import private_name_guard as guard  # noqa: E402

DEFAULT_OUT = guard.ROOT / "samples" / "sanitized"


def shape(line: str) -> str:
    """The line with numbers and long hex runs collapsed, so repeats of one message share a shape."""
    return re.sub(r"[0-9a-fA-F]{8,}|\d+", "#", line[16:])


def load_exclude(path: Path | None = None) -> list[re.Pattern]:
    path = path or instance.guard_file("trim-exclude.txt")
    if not path.exists():
        return []
    words = [w.strip() for w in path.read_text(encoding="utf-8").splitlines()]
    return [
        re.compile(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", re.IGNORECASE)
        for w in words
        if w and not w.startswith("#")
    ]


def trim(lines, exclude, private, allow, per_shape=2, max_lines=30):
    seen: Counter = Counter()
    kept: list[str] = []
    for line in lines:
        if len(kept) >= max_lines:
            break
        if "[REDACTED]" in line or any(p.search(line) for p in exclude):
            continue
        if guard.scan_line(line, private, allow):
            continue
        key = shape(line)
        if seen[key] >= per_shape:
            continue
        seen[key] += 1
        kept.append(line)
    return kept


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", type=Path, default=instance.sanitized_dir())
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--per-shape", type=int, default=2)
    ap.add_argument("--max-lines", type=int, default=30)
    args = ap.parse_args(argv)

    files = sorted(args.src.glob("syslog-*.log"))
    if not files:
        print(f"no syslog-*.log in {args.src}; run scripts/sanitize_samples.py first", file=sys.stderr)
        return 1
    exclude = load_exclude()
    private = guard._compile(guard.load_private_tokens())
    allow = guard.load_allow()
    args.out.mkdir(parents=True, exist_ok=True)
    for old in args.out.glob("syslog-*.log"):
        old.unlink()
    total = 0
    for f in files:
        kept = trim(
            f.read_text(encoding="utf-8").splitlines(),
            exclude,
            private,
            allow,
            args.per_shape,
            args.max_lines,
        )
        if kept:
            (args.out / f.name).write_text("\n".join(kept) + "\n", encoding="utf-8", newline="\n")
            total += len(kept)
        print(f"{f.name:32} {len(kept):3} lines")
    print(f"wrote {total} lines to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
