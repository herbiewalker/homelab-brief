"""Try an ignore rule against recent log lines before saving it (Task 34).

Read-only. The sample is the end of the newest normalized event files in the state folder. A rule is tested
the way the worker applies it (the host matches a sender IP, a header host name, or a system id; the
pattern is searched in "<app>: <message>"). The answer is how many sampled lines it would have ignored, with
examples.
"""

import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path

from brief.ignore import MAX_PATTERN

SAMPLE_FILES = 4
SAMPLE_BYTES = 6_000_000
MAX_EVENTS = 20000
MAX_LINE = 400
EXAMPLES = 5
MAX_SECONDS = 3.0


def regex_problem(text: str) -> str | None:
    """None when `text` is a usable pattern for a rule, else a short reason."""
    if len(text) > MAX_PATTERN:
        return f"The pattern is longer than {MAX_PATTERN} characters."
    try:
        re.compile(text)
    except re.error as exc:
        return f"Not a valid pattern: {exc}."
    return None


def recent_events(state_dir: Path | str) -> list[dict]:
    """Up to MAX_EVENTS recent events (newest files first), each {source_ip, header_host,
    app, msg}. Empty when nothing has been received yet."""
    folder = Path(state_dir) / "events"
    if not folder.exists():
        return []
    events: list[dict] = []
    files = sorted(folder.glob("*.ndjson"), key=lambda p: p.stat().st_mtime, reverse=True)[:SAMPLE_FILES]
    for path in files:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(handle.tell() - SAMPLE_BYTES, 0))
                tail = handle.read().splitlines()
        except OSError:
            continue
        for line in reversed(tail):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, Mapping):
                events.append(
                    {
                        "source_ip": str(event.get("source_ip") or ""),
                        "header_host": str(event.get("header_host") or ""),
                        "app": str(event.get("app") or ""),
                        "msg": str(event.get("msg") or "")[:MAX_LINE],
                    }
                )
            if len(events) >= MAX_EVENTS:
                return events
    return events


def test_rule(events: list[dict], pattern: str, host: str, systems=None) -> dict:
    """{"checked": n, "matched": n, "examples": [text], "error": str | None, "stopped": bool}."""
    problem = regex_problem(pattern) if pattern else "Type a pattern to test."
    if problem:
        return {"checked": 0, "matched": 0, "examples": [], "error": problem, "stopped": False}
    rx = re.compile(pattern)
    want = host.strip().lower()
    matched, examples, checked, started = 0, [], 0, time.monotonic()
    for event in events:
        if time.monotonic() - started > MAX_SECONDS:
            return {
                "checked": checked,
                "matched": matched,
                "examples": examples,
                "error": None,
                "stopped": True,
            }
        if want:
            names = {event["source_ip"].lower(), event["header_host"].lower()}
            if systems is not None:
                try:
                    names.add(
                        systems.resolve(event["source_ip"], event["header_host"] or None).system_id.lower()
                    )
                except Exception:  # a name that cannot be resolved is just not that host
                    pass
            if want not in names:
                continue
        checked += 1
        text = f"{event['app']}: {event['msg']}" if event["app"] else event["msg"]
        if rx.search(text):
            matched += 1
            if len(examples) < EXAMPLES:
                examples.append(text[:MAX_LINE])
    return {"checked": checked, "matched": matched, "examples": examples, "error": None, "stopped": False}
