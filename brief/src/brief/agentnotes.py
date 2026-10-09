"""Where an investigation note shows up: the findings page, the MCP finding tool, and the host's detail page.

The note is an agent run's validated output (every statement cited). It is advice for a person, stored in
`agent_run`; attaching it to a page changes nothing about the finding, its rank, or its route. A host page is
rewritten by every daily run, so `attach` is called again after each one (and after each new note); the block
between the markers is replaced in place, so it never doubles up, and the page stays within the 150-line limit.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from brief.digest import host_file
from brief.store import Store, iso

AGENT = "investigator"
KEEP_DAYS = 14
MAX_NOTE_LINES = 25
MAX_PAGE_LINES = 150
BLOCK = re.compile(r"<!-- agent-note:(?P<id>[^ >]+) -->.*?<!-- /agent-note -->\n?", re.S)


def note_for(store: Store, story_id: str, now: datetime | None = None) -> dict | None:
    """The newest cited note for this story (or finding) id, or None."""
    now = now or datetime.now(UTC)
    with store.read() as db:
        row = db.execute(
            "SELECT run_id, started, provider, output FROM agent_run WHERE agent = ? AND subject = ? "
            "AND outcome = 'ok' AND started >= ? ORDER BY run_id DESC LIMIT 1",
            (AGENT, story_id, iso(now - timedelta(days=KEEP_DAYS))),
        ).fetchone()
    return dict(row) if row else None


def notes_by_story(store: Store, ids: list[str], now: datetime | None = None) -> dict[str, dict]:
    return {i: n for i in ids if (n := note_for(store, i, now))}


def _block(story_id: str, note: Mapping) -> str:
    lines = [ln for ln in str(note["output"]).splitlines() if ln.strip()][:MAX_NOTE_LINES]
    return (
        f"<!-- agent-note:{story_id} -->\n## Investigation note (written by an AI agent, every line cites a tool result)\n"
        + "\n".join(lines)
        + f"\n\n(run {note['run_id']}, {note['started']}; advice only, nothing was changed)\n<!-- /agent-note -->\n"
    )


def attach(store: Store, out_dir: Path, status: Mapping, now: datetime | None = None) -> int:
    """Put each story's note on its host's detail page for the day of the last daily run. Returns how many."""
    stamp = str(status.get("daily_generated_at") or "")
    if not stamp:
        return 0
    day = stamp[:10]
    done = 0
    for story in status.get("stories") or []:
        if not isinstance(story, dict):
            continue
        detail = story.get("detail")
        if not isinstance(story, dict) or not detail or not isinstance(story.get("id"), str):
            continue
        note = note_for(store, story["id"], now)
        path = out_dir / day / detail
        if note is None or not path.is_file() or out_dir.resolve() not in path.resolve().parents:
            continue
        text = path.read_text(encoding="utf-8")
        text = _without(text, story["id"])
        room = MAX_PAGE_LINES - len(text.splitlines())
        block = _block(story["id"], note)
        if room < 6:
            continue  # the page is already full: a note never pushes it over the limit
        if len(block.splitlines()) > room:
            keep = block.splitlines()[: room - 3]
            block = "\n".join([*keep, "(note shortened to fit this page)", "<!-- /agent-note -->"]) + "\n"
        path.write_text(text.rstrip("\n") + "\n\n" + block, encoding="utf-8")
        done += 1
    return done


def page_name(host: str | None) -> str:
    return host_file(host)


def _without(text: str, story_id: str) -> str:
    """The page text with this story's earlier note block removed (other stories' blocks stay)."""
    return BLOCK.sub(lambda m: "" if m.group("id") == story_id else m.group(0), text)
