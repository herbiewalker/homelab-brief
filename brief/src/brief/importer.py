"""`brief import --from /import`: the one-off backfill of old syslog-<ip>.log files.

It runs Vector with vector/backfill.yaml (same parser as the live receiver; the sender comes from the
file name and severity is inferred) and stops it once the output has stopped growing, because
Vector's file source never exits on its own. The source folder is only ever read. The worker picks
up /state/events/import.ndjson like any other event file, bucketing by event time, so history lands
in its own hours and does not start the learning period.

Running it twice would count every line twice, so it refuses when an earlier import left its output
behind. To redo an import, start from a fresh /state.
"""

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

VECTOR_CONFIG = Path("/app/vector/backfill.yaml")
DEFAULT_SOURCE = "/import"
GLOB = "syslog-*.log"
CHUNK = 1 << 20


class ImportFailed(Exception):
    pass


def count_lines(paths) -> int:
    total = 0
    for path in paths:
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK):
                total += chunk.count(b"\n")
    return total


def output_size(state: Path, archive: Path) -> int:
    total = 0
    events = state / "events" / "import.ndjson"
    if events.exists():
        total += events.stat().st_size
    folder = archive / "import"
    if folder.is_dir():
        total += sum(p.stat().st_size for p in folder.glob("*.log"))
    return total


def run_import(
    source: Path,
    state: Path,
    archive: Path,
    vector: str = "vector",
    config: Path = VECTOR_CONFIG,
    idle_seconds: float = 20.0,
    poll_seconds: float = 2.0,
    say: Callable[[str], None] = print,
    spawn=subprocess.Popen,
) -> int:
    files = sorted(source.glob(GLOB)) if source.is_dir() else []
    if not files:
        raise ImportFailed(f"No {GLOB} files in {source}. Mount the old syslog folder there (read-only).")
    previous = state / "events" / "import.ndjson"
    if previous.exists():
        raise ImportFailed(
            f"{previous} exists, so an import already ran, and importing again would count every "
            "line twice. To redo it, start again from an empty state folder."
        )
    (state / "events").mkdir(parents=True, exist_ok=True)
    (state / "vector-import").mkdir(parents=True, exist_ok=True)

    text = config.read_text(encoding="utf-8")
    run_config = config
    if Path(source).as_posix() != DEFAULT_SOURCE:
        # the shipped config reads /import; point a temporary copy at another folder
        text = text.replace(f"{DEFAULT_SOURCE}/{GLOB}", f"{Path(source).as_posix()}/{GLOB}")
        fd, name = tempfile.mkstemp(suffix=".yaml", dir=state / "vector-import")
        os.close(fd)
        run_config = Path(name)
        run_config.write_text(text, encoding="utf-8")

    total = count_lines(files)
    say(f"Importing {total:,} lines from {len(files)} files in {source} (read-only).")
    proc = spawn([vector, "--config", str(run_config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        last, still_since, started = -1, time.monotonic(), time.monotonic()
        while True:
            time.sleep(poll_seconds)
            if proc.poll() is not None:
                raise ImportFailed(f"Vector stopped early (exit code {proc.returncode}).")
            size = output_size(state, archive)
            if size != last:
                last, still_since = size, time.monotonic()
                say(f"  written so far: {size / 1e6:,.1f} MB")
            elif size > 0 and time.monotonic() - still_since >= idle_seconds:
                break
            elif size == 0 and time.monotonic() - started >= max(idle_seconds * 3, 60):
                raise ImportFailed("Vector wrote nothing. Check that the files are readable.")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        if run_config != config:
            run_config.unlink(missing_ok=True)

    imported = count_lines((state / "events").glob("import.ndjson"))
    say(f"Done: {imported:,} of {total:,} lines written to the event stream.")
    if imported < total:
        say("  Fewer than the source: blank lines are skipped. A large gap means the import was cut off; "
            "check the source files and the container log.")  # fmt: skip
    say("The worker now reads them in the background; watch the container log or /status.json.")
    return 0


def main(source: str, state: Path, archive: Path) -> int:
    try:
        return run_import(Path(source), state, archive)
    except ImportFailed as exc:
        print(str(exc), file=sys.stderr)
        return 1
