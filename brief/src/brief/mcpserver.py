"""The read-only MCP server (docs/guide/09-ai-integration.md): tools and resources for outside AI clients.

A small JSON-RPC 2.0 handler for the parts of the Model Context Protocol a client needs to list and call
read-only tools and read resources (`initialize`, `ping`, `tools/list`, `tools/call`, `resources/list`,
`resources/read`). It has no dependency; `web.py` serves it over Streamable HTTP at `POST /mcp` behind a bearer
token, and `brief mcp` runs the same handler over stdio. Nothing here changes anything.

Locked rules, enforced here and not by any setting:
  - every tool needs a scope the token holds; the default scope has no per-device activity, DNS data, or
    raw log lines (those need the `devices`, `dns`, and `logs` scopes), and a `cloud` token cannot hold them;
  - findings about one household device (aiguard.DEVICE_KINDS) are left out unless the token has `devices`;
  - what a `cloud` client receives is scanned for addresses and private names first, and withheld if any;
  - every tool call and resource read is written to the audit log as `mcp:<client>` with its outcome only (never
    the arguments or the result).
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from brief import __version__, aitasks, api, logsearch, mapview
from brief.aiguard import DEVICE_KINDS, privacy_leak, private_terms
from brief.jsonlog import log
from brief.mcptokens import Client
from brief.settings import Settings
from brief.store import Store

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_HISTORY_DAYS = 31
MAX_SEARCH_DAYS = 3
MAX_SEARCH_LINES = 100
MAX_FINDINGS = 50
MAX_RESULT_CHARS = 60_000
METRIC_ID = r"^[A-Za-z0-9_.:/@-]{1,120}$"

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = (
    -32700,
    -32600,
    -32601,
    -32602,
    -32603,
)


class ToolError(Exception):
    """A failure the client should see as a tool result with isError (bad arguments, nothing found)."""


@dataclass
class Context:
    store: Store
    settings: Settings
    client: Client
    status: Callable[[], tuple[dict, Any, list]]  # (status document, systems, registry metrics) right now


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
                "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}}  # fmt: skip


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


# ---------------------------------------------------------------- resources

DIGEST_URI = "brief://digest/latest"
WEEKLY_URI = "brief://report/weekly"
SYSTEMS_URI = "brief://systems"
RESOURCES = (
    (DIGEST_URI, "Latest daily digest", "text/markdown", "findings", "The ranked daily digest."),
    (WEEKLY_URI, "Latest weekly report", "text/markdown", "findings", "The most recent weekly report."),
    (SYSTEMS_URI, "Systems", "application/json", "systems", "Every system with its state."),
)  # fmt: skip
WITHHELD = (
    "Withheld: this includes findings about individual household devices, which need the devices scope. "
    "Use list_findings for the rest."
)


def _device_level_today(ctx: Context) -> bool:
    doc = ctx.status()[0]
    return any(
        isinstance(i, dict) and i.get("kind") in DEVICE_KINDS
        for key in ("stories", "developing", "quiet")
        for i in doc.get(key) or []
    )


def read_resource(ctx: Context, uri: str) -> tuple[str, str]:
    """(media type, text) of a resource the client may read."""
    if uri == DIGEST_URI:
        if _device_level_today(ctx) and not ctx.client.allows("devices"):
            return "text/markdown", WITHHELD
        return "text/markdown", _read(ctx.settings.out_dir / "latest" / "digest.md")
    if uri == WEEKLY_URI:
        files = (
            sorted((ctx.settings.out_dir / "weekly").glob("*.md"))
            if (ctx.settings.out_dir / "weekly").is_dir()
            else []
        )
        if not files:
            return "text/markdown", "No weekly report has been written yet."
        text = _read(files[-1])
        if not ctx.client.allows("devices"):  # the system-level tables only
            text = aitasks.cloud_weekly_text(text.splitlines()) or WITHHELD
        return "text/markdown", text
    if uri == SYSTEMS_URI:
        doc, systems, _ = ctx.status()
        return "application/json", json.dumps(api.systems_doc(doc, systems), indent=1)
    raise KeyError(uri)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "Not generated yet."


# ---------------------------------------------------------------- the protocol


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _ok(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _tool_result(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _guard_cloud(ctx: Context, text: str) -> str | None:
    """Why this text may not go to a cloud client, or None."""
    if ctx.client.locality != "cloud":
        return None
    leak = privacy_leak(text, private_terms(ctx.store))
    return f"the result contains {leak}" if leak else None


def _audit(ctx: Context, what: str, outcome: str) -> None:
    ctx.store.audit(f"mcp:{ctx.client.name}", "mcp.call", f"{what} {outcome}")


def call_tool(ctx: Context, name: str, args: Mapping) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        raise KeyError(name)
    if not ctx.client.allows(tool.scope):
        _audit(ctx, name, "denied")
        return _tool_result(f"This token does not have the {tool.scope} scope.", True)
    unknown = set(args) - set(tool.properties)
    if unknown:
        _audit(ctx, name, "bad arguments")
        return _tool_result(f"Unknown argument: {sorted(unknown)[0]}", True)
    try:
        text = json.dumps(tool.run(ctx, args), indent=1, default=str)
    except ToolError as exc:
        _audit(ctx, name, "error")
        return _tool_result(str(exc), True)
    except Exception as exc:  # a broken tool must not take the server down or leak a stack trace
        log("mcp_tool_failed", tool=name, error=type(exc).__name__)
        _audit(ctx, name, "failed")
        return _tool_result("The tool failed.", True)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n... (cut: ask for less)"
    why = _guard_cloud(ctx, text)
    if why:
        _audit(ctx, name, "withheld")
        return _tool_result(f"Withheld from a cloud client: {why}.", True)
    _audit(ctx, name, "ok")
    return _tool_result(text)


def handle(message: Any, ctx: Context) -> dict | None:
    """One JSON-RPC message in, one response out (None for a notification, which gets none)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
    method, msg_id = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if not isinstance(method, str):
        return None  # a response to something we never sent
    if "id" not in message:
        return None  # a notification (initialized, cancelled): nothing to answer
    if not isinstance(params, dict):
        return _error(msg_id, INVALID_PARAMS, "params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _ok(
            msg_id,
            {
                "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                "serverInfo": {"name": "homelab-brief", "version": __version__},
                "instructions": "Read-only access to a homelab's daily brief, findings, history, systems, and map.",
            },
        )
    if method == "ping":
        return _ok(msg_id, {})
    if method == "tools/list":
        return _ok(msg_id, {"tools": [t.listing() for t in TOOLS.values() if ctx.client.allows(t.scope)]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return _error(msg_id, INVALID_PARAMS, "name and arguments are required")
        try:
            return _ok(msg_id, call_tool(ctx, name, args))
        except KeyError:
            return _error(msg_id, INVALID_PARAMS, f"unknown tool: {name[:60]}")
    if method == "resources/list":
        allowed = [
            {"uri": u, "name": n, "mimeType": m, "description": d}
            for u, n, m, scope, d in RESOURCES
            if ctx.client.allows(scope)
        ]
        return _ok(msg_id, {"resources": allowed})
    if method == "resources/read":
        uri = params.get("uri")
        spec = next((r for r in RESOURCES if r[0] == uri), None)
        if spec is None:
            return _error(msg_id, INVALID_PARAMS, "unknown resource")
        if not ctx.client.allows(spec[3]):
            _audit(ctx, str(uri), "denied")
            return _error(msg_id, INVALID_PARAMS, f"this token does not have the {spec[3]} scope")
        media, text = read_resource(ctx, spec[0])
        why = _guard_cloud(ctx, text)
        if why:
            _audit(ctx, str(uri), "withheld")
            return _error(msg_id, INVALID_PARAMS, f"withheld from a cloud client: {why}")
        _audit(ctx, str(uri), "ok")
        return _ok(msg_id, {"contents": [{"uri": spec[0], "mimeType": media, "text": text}]})
    return _error(msg_id, METHOD_NOT_FOUND, "method not found")


def handle_text(raw: str | bytes, ctx: Context) -> dict | None:
    """Parse a request body (HTTP) or line (stdio) and handle it. Batches are not supported (the current
    protocol revision dropped them)."""
    try:
        message = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return _error(None, PARSE_ERROR, "invalid JSON")
    return handle(message, ctx)


def default_scopes(env: Mapping[str, str] | None = None) -> list[str]:
    """The scope a new token starts with (ai.yaml `mcp.default_scope`), never including a sensitive one."""
    from brief.config import ConfigError, config_file, load_mapping
    from brief.mcptokens import SCOPES, SENSITIVE

    try:
        data = load_mapping(config_file("ai.yaml", env))
        wanted = (data.get("mcp") or {}).get("default_scope") or []
    except (ConfigError, OSError):
        wanted = []
    return [s for s in wanted if s in SCOPES and s not in SENSITIVE] or ["status", "findings", "systems"]
