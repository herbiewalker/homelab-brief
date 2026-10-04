"""`brief import --from /import`: the one-off backfill of old syslog-<ip>.log files.

It runs Vector with vector/backfill.yaml (same parser as the live receiver; the sender comes from the
file name and severity is inferred) and stops it once the output has stopped growing, because
Vector's file source never exits on its own. The source folder is only ever read.

Vector writes /state/events/import.ndjson.partial, which the worker ignores. Only when the import is
complete and checked against the source line count is it renamed to import.ndjson, which the worker
then reads like any other event file (bucketing by event time, so history lands in its own hours and
does not start the learning period). A failed or killed import therefore leaves nothing counted, and
the next run starts clean (it removes the leftover partial file and the raw copies under
/archive/import that it wrote). A finished import is never repeated: that would count every line twice.
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
TOLERANCE = 0.001  # fraction of lines that may be missing (blank or oversize lines are skipped)


class ImportFailed(Exception):
    pass


def count_lines(paths) -> int:
    total = 0
    for path in paths:
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK):
                total += chunk.count(b"\n")
    return total


def output_size(partial: Path, archive: Path) -> int:
    total = partial.stat().st_size if partial.exists() else 0
    folder = archive / "import"
    if folder.is_dir():
        total += sum(p.stat().st_size for p in folder.glob("*.log"))
    return total


def clean_partial(partial: Path, archive: Path) -> None:
    """Remove what an unfinished import wrote: the partial event file and the raw copies it made."""
    partial.unlink(missing_ok=True)
    folder = archive / "import"
    if folder.is_dir():
        for p in folder.glob("*.log"):
            p.unlink(missing_ok=True)


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
    final = state / "events" / "import.ndjson"
    partial = state / "events" / "import.ndjson.partial"
    if final.exists():
        raise ImportFailed(
            f"{final} exists, so an import already ran, and importing again would count every "
            "line twice. To redo it, start again from an empty state folder."
        )
    (state / "events").mkdir(parents=True, exist_ok=True)
    (state / "vector-import").mkdir(parents=True, exist_ok=True)
    if partial.exists():
        say("An earlier import did not finish; removing its leftovers and starting again.")
    clean_partial(partial, archive)

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
    ok = False
    try:
        last, still_since, started = -1, time.monotonic(), time.monotonic()
        while True:
            time.sleep(poll_seconds)
            if proc.poll() is not None:
                hint = ""
                if proc.returncode in (-9, 137):
                    hint = (
                        " It was almost certainly killed for lack of memory: raise the container's memory "
                        "limit (for example to 1 GB) for the import, then lower it again."
                    )
                raise ImportFailed(f"Vector stopped early (exit code {proc.returncode}).{hint}")
            size = output_size(partial, archive)
            if size != last:
                last, still_since = size, time.monotonic()
                say(f"  written so far: {size / 1e6:,.1f} MB")
            elif size > 0 and time.monotonic() - still_since >= idle_seconds:
                break
            elif size == 0 and time.monotonic() - started >= max(idle_seconds * 3, 60):
                raise ImportFailed("Vector wrote nothing. Check that the files are readable.")
        proc.terminate()  # a graceful stop lets Vector flush its last buffers
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        imported = count_lines([partial]) if partial.exists() else 0
        say(f"Read back {imported:,} of {total:,} lines.")
        if total - imported > max(TOLERANCE * total, 10):
            raise ImportFailed(
                f"Only {imported:,} of {total:,} lines were written, so the import was cut off. "
                "Nothing was counted. Check the container log, then run it again."
            )
        os.replace(partial, final)
        ok = True
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        if run_config != config:
            run_config.unlink(missing_ok=True)
        if not ok:
            clean_partial(partial, archive)
    say("Done. The worker now reads the imported events in the background (see the container log).")
    return 0


def main(source: str, state: Path, archive: Path) -> int:
    try:
        return run_import(Path(source), state, archive)
    except ImportFailed as exc:
        print(str(exc), file=sys.stderr)
        return 1
