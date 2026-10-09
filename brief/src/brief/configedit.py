"""Editing the config files from the admin GUI: validate, diff, save with history, roll back.

The rules that make an edit safe:

- A file is validated with the same validator the config guard uses every cycle (configguard.VALIDATORS),
  so what the editor accepts is exactly what the pipeline will accept. An invalid edit is never written.
- Every save keeps the version it replaces under /state/config-history/<file>/, so any change can be undone
  with one click. A rollback is itself a save: it is validated again and keeps what it replaces.
- Every save, rollback, and reset writes a row to the audit log.
- Two people editing at once cannot overwrite each other: a save names the text it started from, and is
  refused if the file has changed since.
- Only the files in configguard.VALIDATORS can be touched, and a file name or version never becomes a path
  without being checked against a fixed list or pattern first.
"""

import difflib
import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from brief import configguard
from brief.config import default_file, user_file
from brief.store import Store

EDITABLE = tuple(configguard.VALIDATORS)
MAX_BYTES = 256 * 1024
HISTORY = "config-history"
VERSION = re.compile(r"^\d{8}T\d{6}Z(?:-\d{1,3})?$")
MAX_DIFF_LINES = 400
KEEP_VERSIONS = 50

DESCRIPTIONS = {
    "systems.yaml": "Which senders belong to which system (one tile and one sensor each).",
    "ignore.yaml": "Messages dropped before they are counted.",
    "priorities.yaml": "Ranking, routes, quiet hours, and the alert lifecycle.",
    "playbook.yaml": "Detector thresholds, the learning period, and the advice rules.",
    "schedule.yaml": "When the daily and weekly jobs run.",
    "registry.yaml": "The sensors and metrics that are published.",
    "notify.yaml": "Notification channels outside Home Assistant.",
    "views.yaml": "The brief page, tablet board, and status picture.",
    "ai.yaml": "AI providers, tasks, and guardrails (off by default).",
    "network.yaml": "Ports and the advertised address. A change here applies after a restart.",
    "retention.yaml": "How long samples and history are kept.",
    "devices.yaml": "Your devices by MAC address: names, owners, how closely to watch (private).",
    "topology.yaml": "Manual nodes and links for the network map, and overrides (private).",
    "collectors.yaml": "Which data collectors are on, how often they run, and where their keys come from.",
    "map.yaml": "Network map settings.",
    "agents.yaml": "AI agents (off by default; need ai.yaml enabled too).",
}
_lock = threading.Lock()


class EditError(Exception):
    """An edit that was refused; the message says why and is safe to show. `status` is the HTTP code."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass
class Version:
    stamp: str
    user: str
    action: str
    size: int


def checked_name(name: str) -> str:
    if name not in EDITABLE:
        raise EditError("That is not a config file you can edit.", 404)
    return name


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(text: str) -> str:
    """Browsers send CRLF line ends from a textarea; the files use LF and end with one newline."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text if text.endswith("\n") or not text else text + "\n"


# ---------------------------------------------------------------- reading


def current(name: str, env: Mapping[str, str] | None = None) -> tuple[str, bool]:
    """(text, customised): the user's file, or the shipped default when there is no user file."""
    checked_name(name)
    user = user_file(name, env)
    try:
        if user.exists():
            return user.read_text(encoding="utf-8"), True
        return starting_text(name, env), False
    except (OSError, UnicodeDecodeError) as exc:
        raise EditError(f"{name} cannot be read ({type(exc).__name__}).") from None


def starting_text(name: str, env: Mapping[str, str] | None = None) -> str:
    """What a file looks like before you have your own: the shipped default, or for a file that has none
    (systems.yaml describes one install, so only an example ships) the example, or an empty document."""
    shipped = default_file(name, env)
    for path in (shipped, shipped.with_name(shipped.stem + ".example" + shipped.suffix)):
        if path.exists():
            return path.read_text(encoding="utf-8")
    return "schema: 1\n"


def validate_text(name: str, text: str) -> str | None:
    """None when the text is a valid `name`, else a one-line reason."""
    checked_name(name)
    if "\x00" in text:
        return "The text contains a NUL character."
    if len(text.encode("utf-8")) > MAX_BYTES:
        return f"The file is larger than {MAX_BYTES // 1024} KB."
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / name
        path.write_text(text, encoding="utf-8")
        try:
            configguard.VALIDATORS[name](path)
        except Exception as exc:  # any failure to load means it would not load for the pipeline either
            return " ".join(str(exc).split())[:400] or type(exc).__name__
    return None


# ---------------------------------------------------------------- diff


def diff_lines(old: str, new: str) -> list[dict]:
    """A unified diff as [{kind: add|del|ctx|gap, text}], at most MAX_DIFF_LINES long."""
    out: list[dict] = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=3):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            out.append({"kind": "gap", "text": line})
        elif line.startswith("+"):
            out.append({"kind": "add", "text": line[1:]})
        elif line.startswith("-"):
            out.append({"kind": "del", "text": line[1:]})
        else:
            out.append({"kind": "ctx", "text": line[1:]})
        if len(out) >= MAX_DIFF_LINES:
            out.append({"kind": "gap", "text": "... the rest of the diff is not shown"})
            break
    return out


def changed_lines(old: str, new: str) -> tuple[int, int]:
    added = removed = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


# ---------------------------------------------------------------- history


def _folder(state_dir: Path | str, name: str) -> Path:
    return Path(state_dir) / HISTORY / checked_name(name)


def _stamp(folder: Path, now: datetime) -> str:
    base = now.strftime("%Y%m%dT%H%M%SZ")
    stamp, n = base, 0
    while (folder / f"{stamp}.yaml").exists():  # two saves in one second
        n += 1
        stamp = f"{base}-{n}"
    return stamp


def versions(state_dir: Path | str, name: str) -> list[Version]:
    """The saved previous versions of `name`, newest first."""
    folder = _folder(state_dir, name)
    out = []
    for path in sorted(folder.glob("*.yaml"), reverse=True) if folder.exists() else []:
        if not VERSION.match(path.stem):
            continue
        try:
            meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        out.append(
            Version(path.stem, str(meta.get("user", "")), str(meta.get("action", "")), path.stat().st_size)
        )
    return sorted(out, key=lambda v: v.stamp, reverse=True)


def version_text(state_dir: Path | str, name: str, stamp: str) -> str:
    if not VERSION.match(stamp):
        raise EditError("That is not a saved version.", 404)
    path = _folder(state_dir, name) / f"{stamp}.yaml"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        raise EditError("That version no longer exists.", 404) from None


def _keep(state_dir: Path | str, name: str, text: str, user: str, action: str, now: datetime) -> str:
    folder = _folder(state_dir, name)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = _stamp(folder, now)
    (folder / f"{stamp}.yaml").write_text(text, encoding="utf-8")
    (folder / f"{stamp}.json").write_text(json.dumps({"user": user, "action": action}), encoding="utf-8")
    for old in versions(state_dir, name)[KEEP_VERSIONS:]:  # a long-lived install does not grow without end
        for suffix in (".yaml", ".json"):
            (folder / f"{old.stamp}{suffix}").unlink(missing_ok=True)
    return stamp


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- changes


def save(
    store: Store,
    name: str,
    text: str,
    user: str,
    base: str,
    state_dir: Path | str,
    env: Mapping[str, str] | None = None,
    action: str = "config.save",
    now: datetime | None = None,
) -> str:
    """Write `text` as the user's `name`. Returns a message. Raises EditError when the edit is refused:
    the text is not valid, the file changed since `base` (a digest of the text the edit started from), or
    nothing changed."""
    now = now or datetime.now(UTC)
    text = normalize(text)
    if (
        not text.strip()
    ):  # an empty file is "valid" (everything defaults), so an accident would pass: refuse it
        raise EditError(
            "The file would be empty. To go back to the shipped default use 'Use the shipped default' "
            "(or `brief config reset`)."
        )
    problem = validate_text(name, text)
    if problem:
        raise EditError(problem)
    with _lock:
        old, customised = current(name, env)
        if digest(old) != base:
            raise EditError(
                "The file changed while you were editing it. Reload it and make the change again.", 409
            )
        if text == old:
            raise EditError("Nothing changed.")
        kept = _keep(state_dir, name, old, user, "replaced by " + action.removeprefix("config."), now)
        _write(user_file(name, env), text)
        added, removed = changed_lines(old, text)
        store.audit(user, action, f"{name}: +{added} -{removed} lines; previous version {kept}")
    configguard.ConfigGuard(state_dir, env).check()  # applies at once here, and clears a config error
    return (
        f"Saved {name}: {added} line(s) added, {removed} removed. The pipeline picks it up on its next cycle."
    )


def rollback(
    store: Store,
    name: str,
    stamp: str,
    user: str,
    base: str,
    state_dir: Path | str,
    env: Mapping[str, str] | None = None,
) -> str:
    """Restore a saved version (validated again, and what it replaces is kept)."""
    text = version_text(state_dir, name, stamp)
    return save(store, name, text, user, base, state_dir, env, action="config.rollback").replace(
        "Saved", "Restored version " + stamp + " of", 1
    )


def reset(
    store: Store,
    name: str,
    user: str,
    state_dir: Path | str,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    """Remove the user's file so the shipped default applies again (the old text is kept as a version)."""
    now = now or datetime.now(UTC)
    with _lock:
        old, customised = current(name, env)
        if not customised:
            raise EditError("This file already uses the shipped default.")
        kept = _keep(state_dir, name, old, user, "replaced by reset", now)
        user_file(name, env).unlink(missing_ok=True)
        store.audit(user, "config.reset", f"{name}: back to the shipped default; previous version {kept}")
    configguard.ConfigGuard(state_dir, env).check()
    return f"{name} now uses the shipped default."
