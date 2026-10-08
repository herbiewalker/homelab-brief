"""Bring the user's config files up to this version's schema on upgrade.

Every config file carries `schema: N`. When a release changes what a file means, it raises that file's
number in CURRENT and adds one step in STEPS that turns schema N text into schema N+1 text. At start
(before anything reads the files) `python -m brief.confmigrate` finds user files with an older number,
saves a copy of the whole config folder to `/state/config-backups/`, runs the steps in order, checks the
result with the same validators the pipeline uses, and writes the file atomically. A file whose result
does not validate is left exactly as it was (the config guard then reports it and keeps its last good
copy). A file with a NEWER number is never touched: that is a downgrade, and the program says so.

No step exists yet: every shipped file is at schema 1. The machinery is here so the first schema change
is a step plus a test, not a new mechanism. The database has its own numbered migrations (store.py).
"""

import os
import re
import sys
import tarfile
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from brief import __version__
from brief.config import user_file
from brief.configguard import VALIDATORS
from brief.jsonlog import log

KEEP_BACKUPS = 10
VERSION_FILE = "app-version"
BACKUP_DIR = "config-backups"

# The schema number every editable file has in this version.
CURRENT: dict[str, int] = {name: 1 for name in VALIDATORS}

# (file name, from schema) -> function turning that file's text into the next schema's text.
STEPS: dict[tuple[str, int], Callable[[str], str]] = {}

_SCHEMA = re.compile(r"^schema:\s*(\d+)\s*(#.*)?$", re.MULTILINE)


@dataclass
class Report:
    migrated: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    too_new: list[str] = field(default_factory=list)
    backup: Path | None = None
    previous_version: str | None = None


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


def _schema_of(text: str) -> int | None:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("schema", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _set_schema(text: str, number: int) -> str:
    if _SCHEMA.search(text):
        return _SCHEMA.sub(f"schema: {number}", text, count=1)
    return f"schema: {number}\n{text}"


def _backup(files: Mapping[str, Path], state: Path, now: datetime) -> Path:
    folder = state / BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"pre-{__version__}-{now:%Y%m%d-%H%M%S}.tar.gz"
    with tarfile.open(dest, "w:gz") as tar:
        for name, path in files.items():
            tar.add(path, arcname=f"config/{name}", recursive=False)
    for old in sorted(folder.glob("pre-*.tar.gz"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return dest


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _stamp(state: Path, report: Report) -> None:
    path = state / VERSION_FILE
    try:
        report.previous_version = path.read_text(encoding="utf-8").strip() or None
    except OSError:
        report.previous_version = None
    prev = report.previous_version
    if prev and _version_tuple(prev) > _version_tuple(__version__):
        log(
            "downgrade_detected",
            was=prev,
            now=__version__,
            note="config and database written by a newer version are not changed",
        )
        return
    if prev != __version__:
        log("version_changed", was=prev or "none", now=__version__)
        path.write_text(__version__ + "\n", encoding="utf-8")


def run(state_dir: Path | str, env: Mapping[str, str] | None = None, now: datetime | None = None) -> Report:
    """Migrate what needs it. A problem with the user's files is reported, never raised."""
    env = os.environ if env is None else env
    state = Path(state_dir)
    now = now or datetime.now(UTC)
    report = Report()
    pending: dict[str, tuple[Path, str, int]] = {}
    present: dict[str, Path] = {}
    for name, target in CURRENT.items():
        path = user_file(name, env)
        if not path.is_file():
            continue
        present[name] = path
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found = _schema_of(text)
        if found is None:
            continue  # broken YAML or odd schema value: the config guard reports it
        if found > target:
            report.too_new.append(name)
            log("config_schema_newer", file=name, found=found, supported=target)
        elif found < target:
            pending[name] = (path, text, found)
    try:
        if pending:
            report.backup = _backup(present, state, now)
        for name, (path, text, found) in pending.items():
            current = found
            try:
                while current < CURRENT[name]:
                    step = STEPS.get((name, current))
                    if step is None:
                        raise ValueError(f"no migration from schema {current}")
                    text = _set_schema(step(text), current + 1)
                    current += 1
                with tempfile.TemporaryDirectory() as tmp:
                    probe = Path(tmp) / name
                    probe.write_text(text, encoding="utf-8")
                    VALIDATORS[name](probe)
                _write_atomic(path, text)
            except Exception as exc:  # leave the file as it was; the guard keeps the last good copy
                report.failed[name] = str(exc)
                log("config_migration_failed", file=name, error=str(exc), from_schema=found)
            else:
                report.migrated.append(name)
                log("config_migrated", file=name, from_schema=found, to_schema=CURRENT[name])
        _stamp(state, report)
    except OSError as exc:  # read-only config or state: carry on, nothing was changed that matters
        log("config_migration_skipped", error=str(exc))
    return report


def main() -> int:
    run(os.environ.get("STATE_DIR", "/state"))
    return 0  # an upgrade problem is logged, never a reason to keep the container from starting


if __name__ == "__main__":
    sys.exit(main())
