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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from brief import __version__, aitasks, api
from brief.agenttools import (  # noqa: F401  (re-exported: the tool set is the same one agents use)
    TOOLS,
    Context,
    ToolError,
    audit,
    call_tool,
    guard_cloud,
)
from brief.aiguard import DEVICE_KINDS

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
            audit(ctx, str(uri), "denied")
            return _error(msg_id, INVALID_PARAMS, f"this token does not have the {spec[3]} scope")
        media, text = read_resource(ctx, spec[0])
        why = guard_cloud(ctx, text)
        if why:
            audit(ctx, str(uri), "withheld")
            return _error(msg_id, INVALID_PARAMS, f"withheld from a cloud client: {why}")
        audit(ctx, str(uri), "ok")
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
