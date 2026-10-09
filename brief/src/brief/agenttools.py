"""The read-only tool registry shared by the MCP server and the agent loop (docs/guide/10-agents.md).

One definition of every tool, two front doors: outside AI clients reach them through `mcpserver`, and the
product's own agents through `agentrun`. Neither can do more than the other, because both call
`call_tool` with a `Context` whose client carries the scopes it may use. Nothing here changes anything.

Locked rules, enforced here and not by any setting:
  - every tool needs a scope the client holds; the default scope has no per-device activity, DNS data, or
    raw log lines (those need the `devices`, `dns`, and `logs` scopes), and a `cloud` client cannot hold them;
  - findings about one household device (aiguard.DEVICE_KINDS) are left out unless the client has `devices`;
  - what a `cloud` client receives is scanned for addresses and private names first, and withheld if any;
  - every call is written to the audit log as `<source>:<client>` (`mcp:` or `agent:`) with its outcome only,
    never the arguments or the result.
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from brief import aitasks, api, logsearch, mapview
from brief.aiguard import DEVICE_KINDS, privacy_leak, private_terms
from brief.jsonlog import log
from brief.mcptokens import Client
from brief.settings import Settings
from brief.store import Store

MAX_HISTORY_DAYS = 31
MAX_SEARCH_DAYS = 3
MAX_SEARCH_LINES = 100
MAX_FINDINGS = 50
MAX_RESULT_CHARS = 60_000
METRIC_ID = r"^[A-Za-z0-9_.:/@-]{1,120}$"


class ToolError(Exception):
    """A failure the client should see as a tool result with isError (bad arguments, nothing found)."""


@dataclass
class Context:
    store: Store
    settings: Settings
    client: Client
    status: Callable[[], tuple[dict, Any, list]]  # (status document, systems, registry metrics) right now
    source: str = "mcp"  # who is calling: "mcp" or "agent" (the audit log prefix)
    run_id: int | None = None  # the agent run making these calls
    evidence: dict | None = None  # result id -> tool, for the results of this run that succeeded


# ---------------------------------------------------------------- argument helpers


def _int(args: Mapping, key: str, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolError(f"{key} must be a whole number from {low} to {high}")
    return value


def _text(args: Mapping, key: str, limit: int, required: bool = False) -> str:
    value = args.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise ToolError(f"{key} must be text of at most {limit} characters")
    if required and not value.strip():
        raise ToolError(f"{key} is required")
    return value.strip()


def _without_device_findings(items: list[dict], ctx: Context) -> list[dict]:
    if ctx.client.allows("devices"):
        return items
    return [i for i in items if not (isinstance(i, dict) and i.get("kind") in DEVICE_KINDS)]


# ---------------------------------------------------------------- tools


def get_status(ctx: Context, args: Mapping) -> dict:
    doc = dict(ctx.status()[0])
    for key in ("stories", "developing", "quiet"):
        if isinstance(doc.get(key), list):
            doc[key] = _without_device_findings(doc[key], ctx)
    return doc


def list_findings(ctx: Context, args: Mapping) -> dict:
    limit = _int(args, "limit", 20, 1, MAX_FINDINGS)
    doc = api.findings_doc(ctx.status()[0])
    for key in ("stories", "developing", "quiet"):
        doc[key] = _without_device_findings(list(doc.get(key) or []), ctx)
    doc["stories"] = doc["stories"][:limit]
    from brief import agentnotes

    for s in doc["stories"]:
        if isinstance(s, dict) and isinstance(s.get("id"), str) and agentnotes.note_for(ctx.store, s["id"]):
            s["has_investigation"] = True  # read it with get_finding
    return doc


def explain_finding(ctx: Context, args: Mapping) -> dict:
    finding_id = _text(args, "finding_id", 300, required=True)
    _, systems, _ = ctx.status()
    finding = aitasks.find_finding(ctx.store, systems, finding_id, datetime.now(UTC))
    # one message for "no such finding" and "not allowed to see it", so ids cannot be probed
    if finding is None or (finding.kind in DEVICE_KINDS and not ctx.client.allows("devices")):
        raise ToolError("there is no such finding")
    explained = aitasks.explain_finding(ctx.store, finding)
    return {
        "id": finding.id,
        "title": finding.title,
        "kind": finding.kind,
        "system": finding.system,
        "advice": finding.detail.get("advice"),
        "explanation": explained.text if explained else None,
        "explained_by": explained.model if explained else None,
    }


def get_history(ctx: Context, args: Mapping) -> dict:
    kind = args.get("kind")
    if kind not in ("signature", "metric"):
        raise ToolError("kind must be signature or metric")
    days = _int(args, "days", 7, 1, MAX_HISTORY_DAYS)
    since = (datetime.now(UTC) - timedelta(days=days)).date()
    if kind == "signature":
        sig_id = _int(args, "id", -1, 0, 2**31)
        with ctx.store.read() as db:
            sig = db.execute(
                "SELECT sig_id, app, template, first_seen, last_seen FROM signature WHERE sig_id = ?",
                (sig_id,),
            ).fetchone()
            if sig is None:
                raise ToolError("there is no such signature")
            rows = db.execute(
                "SELECT day, SUM(n) AS events FROM hourly WHERE sig_id = ? AND day >= ? GROUP BY day ORDER BY day",
                (sig_id, since.isoformat()),
            ).fetchall()
        out = {"kind": "signature", "id": sig_id, "app": sig["app"], "first_seen": sig["first_seen"],
               "last_seen": sig["last_seen"], "daily": [dict(r) for r in rows]}  # fmt: skip
        if ctx.client.allows("logs"):  # a template is generalised log text
            out["template"] = sig["template"]
        return out
    metric_id = _text(args, "id", 120, required=True)
    if not re.match(METRIC_ID, metric_id):
        raise ToolError("that is not a metric id")
    with ctx.store.read() as db:
        rows = db.execute(
            "SELECT substr(ts, 1, 10) AS day, ROUND(AVG(value), 3) AS avg, MIN(value) AS min, MAX(value) AS max "
            "FROM metric WHERE metric_id = ? AND ts >= ? GROUP BY day ORDER BY day",
            (metric_id, since.isoformat()),
        ).fetchall()
    if not rows:
        raise ToolError("there is no such metric, or no data in that range")
    return {"kind": "metric", "id": metric_id, "daily": [dict(r) for r in rows]}


def list_systems(ctx: Context, args: Mapping) -> dict:
    doc, systems, _ = ctx.status()
    return api.systems_doc(doc, systems)


def get_map(ctx: Context, args: Mapping) -> dict:
    doc, systems, _ = ctx.status()
    found = mapview.current_map(ctx.store, ctx.settings.state_dir, doc, systems)
    if found is None:
        raise ToolError("the network map is switched off")
    return {k: v for k, v in found.items() if k not in ("auto", "layout")}


def search_logs(ctx: Context, args: Mapping) -> dict:
    text = _text(args, "text", logsearch.MAX_TEXT, required=True)
    host = _text(args, "host", 80)
    days = _int(args, "days", 1, 1, MAX_SEARCH_DAYS)
    limit = _int(args, "limit", 50, 1, MAX_SEARCH_LINES)
    today = datetime.now(UTC).date()
    try:
        found = logsearch.search(
            ctx.settings.archive_dir, host, today - timedelta(days=days - 1), today, text, limit
        )
    except logsearch.SearchError as exc:
        raise ToolError(str(exc)) from None
    return {"lines": found.lines, "files_read": found.files, "stopped": found.stopped or None}


@dataclass(frozen=True)
class Tool:
    name: str
    scope: str
    description: str
    properties: dict
    run: Callable[[Context, Mapping], Any]
    required: tuple[str, ...] = ()

    def listing(self) -> dict:
        schema = {"type": "object", "properties": self.properties, "additionalProperties": False}
        if self.required:
            schema["required"] = list(self.required)
        return {"name": self.name, "description": self.description, "inputSchema": schema,
                "annotations": {"readOnlyHint": self.scope != "propose", "destructiveHint": False, "openWorldHint": False}}  # fmt: skip


TOOLS = {
    t.name: t
    for t in (
        Tool(
            "get_status",
            "status",
            "Health of the pipeline, the last daily run, each system's state, and the headline.",
            {},
            get_status,
        ),  # fmt: skip
        Tool(
            "list_findings",
            "findings",
            "Ranked stories, developing issues, and quiet items from the last daily run.",
            {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_FINDINGS,
                    "description": "most stories to return",
                }
            },
            list_findings,
        ),  # fmt: skip
        Tool(
            "explain_finding",
            "findings",
            "Plain-language explanation of one finding, with the playbook's next step.",
            {"finding_id": {"type": "string", "description": "an id from list_findings"}},
            explain_finding,
            ("finding_id",),
        ),  # fmt: skip
        Tool(
            "get_history",
            "history",
            "Daily counts for a message signature, or daily min/avg/max of a metric.",
            {
                "kind": {"type": "string", "enum": ["signature", "metric"]},
                "id": {"description": "a signature id (number) or a metric id (text)"},
                "days": {"type": "integer", "minimum": 1, "maximum": MAX_HISTORY_DAYS},
            },
            get_history,
            ("kind", "id"),
        ),  # fmt: skip
        Tool(
            "list_systems", "systems", "Every system with its state and one-line summary.", {}, list_systems
        ),
        Tool(
            "get_map",
            "map",
            "The network map: nodes, links, status colours, and badges (clients only as counts per VLAN).",
            {},
            get_map,
        ),  # fmt: skip
        Tool(
            "search_logs",
            "logs",
            "Search the raw syslog archive (bounded; needs the logs scope).",
            {
                "text": {"type": "string"},
                "host": {"type": "string", "description": "sender, optional"},
                "days": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_DAYS},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_LINES},
            },
            search_logs,
            ("text",),
        ),  # fmt: skip
    )
}


def tool_result(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def guard_cloud(ctx: Context, text: str) -> str | None:
    """Why this text may not go to a cloud client, or None."""
    if ctx.client.locality != "cloud":
        return None
    leak = privacy_leak(text, private_terms(ctx.store))
    return f"the result contains {leak}" if leak else None


def audit(ctx: Context, what: str, outcome: str) -> None:
    ctx.store.audit(f"{ctx.source}:{ctx.client.name}", f"{ctx.source}.call", f"{what} {outcome}")


def call_tool(ctx: Context, name: str, args: Mapping) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        raise KeyError(name)
    if not ctx.client.allows(tool.scope):
        audit(ctx, name, "denied")
        return tool_result(f"This token does not have the {tool.scope} scope.", True)
    unknown = set(args) - set(tool.properties)
    if unknown:
        audit(ctx, name, "bad arguments")
        return tool_result(f"Unknown argument: {sorted(unknown)[0]}", True)
    try:
        text = json.dumps(tool.run(ctx, args), indent=1, default=str)
    except ToolError as exc:
        audit(ctx, name, "error")
        return tool_result(str(exc), True)
    except Exception as exc:  # a broken tool must not take the server down or leak a stack trace
        log("mcp_tool_failed", tool=name, error=type(exc).__name__)
        audit(ctx, name, "failed")
        return tool_result("The tool failed.", True)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n... (cut: ask for less)"
    why = guard_cloud(ctx, text)
    if why:
        audit(ctx, name, "withheld")
        return tool_result(f"Withheld from a cloud client: {why}.", True)
    audit(ctx, name, "ok")
    return tool_result(text)


# ---------------------------------------------------------------- more tools (agents' evidence)


def get_finding(ctx: Context, args: Mapping) -> dict:
    finding_id = _text(args, "finding_id", 300, required=True)
    _, systems, _ = ctx.status()
    finding = aitasks.find_finding(ctx.store, systems, finding_id, datetime.now(UTC))
    if finding is None and finding_id.startswith("group-"):
        # a grouped story is not one finding: all there is to read is the investigation, if any
        from brief import agentnotes

        device_level = any(finding_id.startswith(f"group-{k}-") for k in DEVICE_KINDS)
        note = (
            None
            if device_level and not ctx.client.allows("devices")
            else agentnotes.note_for(ctx.store, finding_id)
        )
        if note:
            return {
                "id": finding_id,
                "grouped": True,
                "investigation": {"run": note["run_id"], "when": note["started"], "note": note["output"]},
            }
    if finding is None or (finding.kind in DEVICE_KINDS and not ctx.client.allows("devices")):
        raise ToolError("there is no such finding")
    detail = {k: v for k, v in finding.detail.items() if v is not None}
    out = {"id": finding.id, "kind": finding.kind, "title": finding.title, "system": finding.system,
           "value": finding.value, "sig_id": finding.sig_id, "detail": detail}  # fmt: skip
    if finding.host and ctx.client.allows("logs"):
        out["host"] = finding.host
    if ctx.client.allows("logs"):
        out["recent_lines"] = aitasks._samples(ctx.store, finding)
    from brief import agentnotes

    note = agentnotes.note_for(ctx.store, finding.id)
    if note:
        out["investigation"] = {"run": note["run_id"], "when": note["started"], "note": note["output"]}
    return out


def get_signature(ctx: Context, args: Mapping) -> dict:
    sig_id = _int(args, "id", -1, 0, 2**31)
    with ctx.store.read() as db:
        sig = db.execute(
            "SELECT sig_id, app, template, first_seen, last_seen, ignored FROM signature WHERE sig_id = ?",
            (sig_id,),
        ).fetchone()
        if sig is None:
            raise ToolError("there is no such signature")
        since = (datetime.now(UTC) - timedelta(days=7)).date().isoformat()
        daily = db.execute(
            "SELECT day, SUM(n) AS events FROM hourly WHERE sig_id = ? AND day >= ? GROUP BY day ORDER BY day",
            (sig_id, since),
        ).fetchall()
        hosts = db.execute(
            "SELECT COUNT(DISTINCT host) FROM hourly WHERE sig_id = ? AND day >= ?", (sig_id, since)
        ).fetchone()[0]
        samples = (
            [
                dict(r)
                for r in db.execute(
                    "SELECT ts, host, msg FROM sample WHERE sig_id = ? ORDER BY ts DESC LIMIT 3", (sig_id,)
                )
            ]
            if ctx.client.allows("logs")
            else []
        )
    out = {"id": sig["sig_id"], "app": sig["app"], "first_seen": sig["first_seen"], "last_seen": sig["last_seen"],
           "ignored": bool(sig["ignored"]), "senders_7d": hosts, "daily_7d": [dict(r) for r in daily]}  # fmt: skip
    if ctx.client.allows("logs"):
        out["template"], out["recent_lines"] = sig["template"], samples
    return out


def get_inventory(ctx: Context, args: Mapping) -> dict:
    only_pending = args.get("pending_only", False)
    if not isinstance(only_pending, bool):
        raise ToolError("pending_only must be true or false")
    with ctx.store.read() as db:
        rows = db.execute(
            "SELECT item_id, kind, name, model, current_version, available_version, available_since, checked_at "
            "FROM inventory "
            + ("WHERE available_version IS NOT NULL " if only_pending else "")
            + "ORDER BY kind, name LIMIT 200"
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


def get_unraid_notifications(ctx: Context, args: Mapping) -> dict:
    limit = _int(args, "limit", 20, 1, 50)
    with ctx.store.read() as db:
        rows = db.execute(
            "SELECT server, ts, importance, subject, description FROM server_notice ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"notices": [dict(r) for r in rows]}


def run_replay(ctx: Context, args: Mapping) -> dict:
    from brief import replay as rp

    days = _int(args, "days", 3, 1, 7)
    _, systems, _ = ctx.status()
    end = datetime.now(UTC).date()
    rep = rp.replay(ctx.store, systems, end - timedelta(days=days), end, hourly=False, ignore_learning=True)
    return rp.summary(rep)


_MORE = (
    Tool(
        "get_finding",
        "findings",
        "One finding in full: its kind, size, system, and the evidence the detector kept.",
        {"finding_id": {"type": "string", "description": "an id from list_findings"}},
        get_finding,
        ("finding_id",),
    ),  # fmt: skip
    Tool(
        "get_signature",
        "history",
        "One message signature: when it first and last appeared, how often over a week, and how many senders.",
        {"id": {"type": "integer", "minimum": 0}},
        get_signature,
        ("id",),
    ),  # fmt: skip
    Tool(
        "get_inventory",
        "systems",
        "Devices and software with their current and available versions.",
        {"pending_only": {"type": "boolean", "description": "only items with an update waiting"}},
        get_inventory,
    ),  # fmt: skip
    Tool(
        "get_unraid_notifications",
        "logs",
        "Recent notifications raised by the Unraid servers themselves (needs the logs scope).",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        get_unraid_notifications,
    ),  # fmt: skip
    Tool(
        "run_replay",
        "history",
        "Replay the detectors over the last few days (no changes made): what would have been new, pushed, or resolved.",
        {"days": {"type": "integer", "minimum": 1, "maximum": 7}},
        run_replay,
    ),  # fmt: skip
)
TOOLS.update({t.name: t for t in _MORE})


# ---------------------------------------------------------------- proposals (the one tool that writes a record)


def get_config(ctx: Context, args: Mapping) -> dict:
    from brief import configedit, proposals

    name = args.get("file")
    if name not in proposals.PROPOSABLE:
        raise ToolError(f"file must be one of {', '.join(proposals.PROPOSABLE)}")
    text, customised = configedit.current(name)
    return {"file": name, "customised": customised, "text": text}


def propose_change(ctx: Context, args: Mapping) -> dict:
    """Record a proposal for a person to approve. Changes no config file."""
    from brief import proposals

    file = _text(args, "file", 40, required=True)
    new_text = args.get("new_text")
    if not isinstance(new_text, str) or not new_text.strip() or len(new_text) > 200_000:
        raise ToolError("new_text must be the complete new file")
    reason, evidence = (
        _text(args, "reason", proposals.MAX_REASON),
        _text(args, "evidence", proposals.MAX_EVIDENCE),
    )
    if ctx.source == "agent" and ctx.client.name in STRUCTURED_AGENTS:
        raise ToolError(
            "this agent's proposals are made by the product from its structured answer, not with this tool"
        )
    if ctx.source == "agent":
        cited = set(re.findall(r"\[ref:(r\d+)\]", evidence))
        known = ctx.evidence or {}
        if not cited or not cited <= set(known):
            raise ToolError(
                "evidence must cite the tool results it rests on, like [ref:r1], and only real ones"
            )
        evidence += "\n\nResults cited: " + ", ".join(f"{r} ({known[r]})" for r in sorted(cited))
    try:
        pid = proposals.create(
            ctx.store, f"{ctx.source}:{ctx.client.name}", file, new_text, reason, evidence, run_id=ctx.run_id
        )
    except proposals.ProposalError as exc:
        raise ToolError(str(exc)) from None
    return {"proposal": pid, "status": "pending", "note": "Nothing has changed. A person must approve it."}


STRUCTURED_AGENTS = (
    "triage",
    "tuner",
)  # their proposals come from agentout, which adds the replay and the checks
PROPOSABLE_FILES = (
    "ignore.yaml",
    "priorities.yaml",
    "playbook.yaml",
)  # kept equal to proposals.PROPOSABLE (a test checks)
_PROPOSE = (
    Tool(
        "get_config",
        "logs",
        "The current text of a file you may propose a change to (ignore.yaml, priorities.yaml, playbook.yaml).",
        {"file": {"type": "string", "enum": list(PROPOSABLE_FILES)}},
        get_config,
        ("file",),
    ),  # fmt: skip
    Tool(
        "propose_change",
        "propose",
        "Suggest a config change for a person to approve. Records a proposal and changes nothing. Give the complete new file, a reason, and evidence citing tool results.",
        {
            "file": {"type": "string", "enum": list(PROPOSABLE_FILES)},
            "new_text": {"type": "string", "description": "the complete new file"},
            "reason": {"type": "string"},
            "evidence": {"type": "string", "description": "what supports it, citing results like [ref:r1]"},
        },
        propose_change,
        ("file", "new_text", "reason", "evidence"),
    ),  # fmt: skip
)
TOOLS.update({t.name: t for t in _PROPOSE})
