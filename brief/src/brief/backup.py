"""A backup you can download, and restoring your configuration from one.

What a backup holds: your config files (`config/<file>.yaml`, the ones the editor can change), a
consistent snapshot of the database (`state/baseline.db`, made with SQLite's online backup, so it is
safe while the worker runs), the signature miner's state (`state/drain3.bin`), and a manifest. It does not
hold the raw log archive (large, and the source of truth on its own), the TLS key, or the event files.

The database holds the admin password hash and session records, so a backup is as sensitive as the admin
login. Treat the file like a password.

Restoring from this page applies the **config files only**, one at a time through the config editor's
rules (validated, versioned, audited). Putting a database back means stopping the container and replacing
`baseline.db`; that is a documented manual step, not something to do under a running worker.
"""

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from brief import __version__, configedit
from brief.config import user_file
from brief.store import Store

MAX_UPLOAD = 4 * 1024 * 1024
MAX_MEMBER = configedit.MAX_BYTES
MANIFEST = "MANIFEST.json"


class BackupError(ValueError):
    """A backup that cannot be made or read; the message is safe to show."""


def _snapshot(db_path: Path, dest: Path) -> None:
    """A consistent copy of a live SQLite database."""
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def create(state_dir: Path | str, env: Mapping[str, str] | None = None, now: datetime | None = None) -> Path:
    """Write a .tar.gz into a temporary file and return its path (the caller deletes it after sending)."""
    state = Path(state_dir)
    now = now or datetime.now(UTC)
    fd, name = tempfile.mkstemp(prefix="homelab-brief-backup-", suffix=".tar.gz")
    os.close(fd)
    out = Path(name)
    files: dict[str, str] = {}
    try:
        with tempfile.TemporaryDirectory() as work, tarfile.open(out, "w:gz") as tar:

            def add(path: Path, arcname: str) -> None:
                tar.add(path, arcname=arcname, recursive=False, filter=_plain)
                files[arcname] = hashlib.sha256(path.read_bytes()).hexdigest()

            for name_ in configedit.EDITABLE:
                path = user_file(name_, env)
                if path.is_file():
                    add(path, f"config/{name_}")
            db = state / "baseline.db"
            if db.exists():
                snap = Path(work) / "baseline.db"
                _snapshot(db, snap)
                add(snap, "state/baseline.db")
            miner = state / "drain3.bin"
            if miner.is_file():
                add(miner, "state/drain3.bin")
            manifest = json.dumps(
                {
                    "product": "homelab-brief",
                    "version": __version__,
                    "created": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "files": files,
                },
                indent=2,
            ).encode("utf-8")
            info = tarfile.TarInfo(MANIFEST)
            info.size, info.mtime = len(manifest), int(now.timestamp())
            tar.addfile(info, io.BytesIO(manifest))
    except Exception:
        out.unlink(missing_ok=True)
        raise
    return out


def _plain(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """No owner, group, or permission bits from this machine in the archive."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o600
    return info


@dataclass
class Restored:
    applied: list[str]
    unchanged: list[str]
    skipped: list[str]  # members that were not config files (the database, the manifest, anything else)


def read_config_files(data: bytes) -> tuple[dict[str, str], list[str]]:
    """({file name: text}, skipped member names) from a backup's bytes. Only regular files named
    config/<an editable file> are taken; every other member is skipped, never extracted."""
    if len(data) > MAX_UPLOAD:
        raise BackupError(f"The file is larger than {MAX_UPLOAD // (1024 * 1024)} MB.")
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError):
        raise BackupError("That is not a backup file (a .tar.gz made by this page).") from None
    found: dict[str, str] = {}
    skipped: list[str] = []
    with tar:
        for member in tar:
            parts = member.name.split("/")
            if not (
                member.isreg()
                and len(parts) == 2
                and parts[0] == "config"
                and parts[1] in configedit.EDITABLE
                and member.size <= MAX_MEMBER
            ):
                skipped.append(member.name[:80])
                continue
            handle = tar.extractfile(member)
            try:
                found[parts[1]] = handle.read(MAX_MEMBER + 1).decode("utf-8") if handle else ""
            except UnicodeDecodeError:
                raise BackupError(f"{parts[1]} in the backup is not text.") from None
    if not found:
        raise BackupError("That backup holds no config files.")
    return found, skipped


def restore_config(
    store: Store,
    data: bytes,
    user: str,
    state_dir: Path | str,
    env: Mapping[str, str] | None = None,
) -> Restored:
    """Apply the config files in a backup, each through configedit.save. All of them are checked first, so
    one bad file stops the whole restore before anything changes."""
    files, skipped = read_config_files(data)
    for name, text in files.items():
        problem = configedit.validate_text(name, configedit.normalize(text))
        if problem:
            raise BackupError(f"{name} in the backup is not valid: {problem}")
    applied, unchanged = [], []
    for name, text in sorted(files.items()):
        current, _ = configedit.current(name, env)
        try:
            configedit.save(
                store, name, text, user, configedit.digest(current), state_dir, env, action="config.restore"
            )
            applied.append(name)
        except configedit.EditError as exc:
            if "Nothing changed" in str(exc):
                unchanged.append(name)
            else:
                raise BackupError(f"{name}: {exc}") from None
    return Restored(applied, unchanged, skipped)


def reports(out_dir: Path | str) -> dict[str, list[str]]:
    """File names of what the product has written that is worth downloading, newest first."""
    out = Path(out_dir)

    def names(folder: Path, suffix: str, limit: int = 30) -> list[str]:
        if not folder.is_dir():
            return []
        return sorted(
            (p.name for p in folder.iterdir() if p.is_file() and p.name.endswith(suffix)), reverse=True
        )[:limit]

    briefs = (
        sorted((p.name for p in out.iterdir() if p.is_dir() and (p / "digest.md").is_file()), reverse=True)[
            :30
        ]
        if out.is_dir()
        else []
    )
    return {"weekly": names(out / "weekly", ".md"), "export": names(out / "export", ".csv"), "brief": briefs}


def report_path(out_dir: Path | str, kind: str, name: str) -> Path | None:
    """The file for a download link, or None. `kind` is fixed, `name` must match its pattern exactly."""

    out = Path(out_dir)
    if kind == "weekly" and re.fullmatch(r"[A-Za-z0-9._-]{1,80}\.md", name):
        path = out / "weekly" / name
    elif kind == "export" and re.fullmatch(r"[A-Za-z0-9._-]{1,120}\.csv", name):
        path = out / "export" / name
    elif kind == "brief" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
        path = out / name / "digest.md"
    else:
        return None
    return path if path.is_file() and out.resolve() in path.resolve().parents else None


def discard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
