"""Bearer tokens for outside AI clients of the MCP server.

A token is `hbm_<id>_<secret>`. Only a SHA-256 of the whole string is stored (the secret is 256 random bits, so
a plain hash is enough and each check is one lookup), and the secret is shown once, when it is made. Each
token has a name, a set of scopes, and a locality. Revoking keeps the row (for the audit trail) and the token
stops working at once. Creating and revoking are audited by the caller (the AI page or `brief mcp token`).
"""

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from brief.store import Store, iso

SCOPES = ("status", "findings", "history", "systems", "map", "logs", "devices", "dns")
SENSITIVE = ("logs", "devices", "dns")  # never part of the default scope; never for a cloud client
LOCALITIES = ("lan", "cloud")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
PREFIX = "hbm_"
TOUCH_EVERY = timedelta(minutes=1)  # last_used is written at most this often per token
MAX_ACTIVE = 50


class TokenError(ValueError):
    pass


@dataclass(frozen=True)
class Client:
    token_id: str
    name: str
    scopes: frozenset[str]
    locality: str = "lan"

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def check_scopes(scopes: list[str], locality: str) -> list[str]:
    if locality not in LOCALITIES:
        raise TokenError(f"locality must be one of {', '.join(LOCALITIES)}")
    clean = sorted(set(scopes))
    unknown = [s for s in clean if s not in SCOPES]
    if unknown:
        raise TokenError(f"unknown scope {unknown[0]!r}; use {', '.join(SCOPES)}")
    if not clean:
        raise TokenError("a token needs at least one scope")
    if locality == "cloud" and any(s in SENSITIVE for s in clean):
        raise TokenError("a cloud client cannot have the logs, devices, or dns scope")
    return clean


def create(
    store: Store, name: str, scopes: list[str], locality: str = "lan", by: str = ""
) -> tuple[str, str]:
    """Returns (token_id, secret). The secret cannot be shown again."""
    name = name.strip()
    if not NAME.match(name):
        raise TokenError("a name is 1 to 40 letters, digits, spaces, dots, dashes, or underscores")
    scopes = check_scopes(scopes, locality)
    token_id = secrets.token_hex(4)
    secret = f"{PREFIX}{token_id}_{secrets.token_urlsafe(32)}"
    with store.write() as tx:
        rows = tx.db.execute("SELECT name FROM mcp_token WHERE revoked IS NULL").fetchall()
        if len(rows) >= MAX_ACTIVE:
            raise TokenError(f"at most {MAX_ACTIVE} active tokens; revoke one first")
        if any(r["name"] == name for r in rows):
            raise TokenError(f"there is already an active token named {name!r}")
        tx.db.execute(
            "INSERT INTO mcp_token (token_id, name, hash, scopes, locality, created, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token_id, name, _hash(secret), ",".join(scopes), locality, iso(datetime.now(UTC)), by),
        )
    return token_id, secret


def verify(store: Store, secret: str, now: datetime | None = None) -> Client | None:
    """The client this secret belongs to, or None (unknown, malformed, or revoked)."""
    if not secret.startswith(PREFIX) or len(secret) > 120:
        return None
    token_id = secret[len(PREFIX) :].split("_", 1)[0]
    with store.read() as db:
        row = db.execute("SELECT * FROM mcp_token WHERE token_id = ?", (token_id,)).fetchone()
    expected = row["hash"] if row else _hash("")
    if not hmac.compare_digest(expected, _hash(secret)) or row is None or row["revoked"]:
        return None
    now = now or datetime.now(UTC)
    last = row["last_used"]
    if not last or datetime.fromisoformat(last.replace("Z", "+00:00")) < now - TOUCH_EVERY:
        with store.write() as tx:
            tx.db.execute("UPDATE mcp_token SET last_used = ? WHERE token_id = ?", (iso(now), token_id))
    return Client(row["token_id"], row["name"], frozenset(row["scopes"].split(",")), row["locality"])


def revoke(store: Store, ident: str) -> str:
    """Revoke by id or name; returns the token's name."""
    with store.write() as tx:
        row = tx.db.execute(
            "SELECT token_id, name FROM mcp_token WHERE revoked IS NULL AND (token_id = ? OR name = ?)",
            (ident, ident),
        ).fetchone()
        if row is None:
            raise TokenError("there is no active token with that id or name")
        tx.db.execute(
            "UPDATE mcp_token SET revoked = ? WHERE token_id = ?", (iso(datetime.now(UTC)), row["token_id"])
        )
    return row["name"]


def listing(store: Store, include_revoked: bool = False) -> list[dict]:
    with store.read() as db:
        rows = db.execute(
            "SELECT token_id, name, scopes, locality, created, created_by, last_used, revoked FROM mcp_token "
            + ("" if include_revoked else "WHERE revoked IS NULL ")
            + "ORDER BY created DESC"
        ).fetchall()
    return [{**dict(r), "scopes": r["scopes"].split(",")} for r in rows]


class Throttle:
    """Failed bearer checks per source address: after `limit` in `window` seconds, refuse for the rest of it."""

    def __init__(self, limit: int = 10, window: float = 300.0, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._fails: dict[str, list[float]] = {}

    def blocked(self, who: str) -> bool:
        now = self.clock()
        recent = [t for t in self._fails.get(who, []) if now - t < self.window]
        self._fails[who] = recent
        return len(recent) >= self.limit

    def failed(self, who: str) -> None:
        if len(self._fails) > 1000:  # a flood of sources must not grow this without bound
            self._fails.clear()
        self._fails.setdefault(who, []).append(self.clock())
