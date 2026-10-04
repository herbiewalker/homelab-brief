"""Admin authentication: Argon2 passwords, server-side sessions, login throttling, setup token.

There are no default credentials. The first admin is created either with `brief admin set-password`
or through /admin/setup with a one-time token printed to the container log. Every sign-in, failed
sign-in, sign-out, and password change is written to the audit log.
"""

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from brief.store import Store, iso, parse_time

PASSWORD_MIN = 12
PASSWORD_MAX = 256
IDLE_TIMEOUT = timedelta(minutes=30)
ABSOLUTE_TIMEOUT = timedelta(hours=12)
TOUCH_AFTER = timedelta(seconds=60)  # write last_seen at most this often
FAIL_WINDOW = timedelta(minutes=15)
FAIL_FREE = 5  # failures allowed per key inside the window before locking
LOCK_BASE_SECONDS = 30
LOCK_MAX_SECONDS = 900
SETUP_TOKEN_LIFETIME = timedelta(hours=24)
GENERIC_FAILURE = "Invalid username or password."
COMMON_PASSWORDS = {
    "passwordpassword", "password1234", "123456789012", "1234567890123", "qwertyuiopas", "administrator",
    "adminadminadmin", "iloveyou1234", "letmeinletmein", "welcome12345", "changemechangeme", "homelabbrief",
}  # fmt: skip


class AuthError(Exception):
    pass


class BadCredentials(AuthError):
    pass


class Throttled(AuthError):
    def __init__(self, retry_after: int):
        super().__init__(f"too many failed attempts; try again in {retry_after} seconds")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Session:
    user_id: int
    username: str
    csrf: str
    created: str
    expires: str


def password_problem(password: str, username: str = "") -> str | None:
    """Why a password is not acceptable, or None. Length matters most; no composition rules."""
    if len(password) < PASSWORD_MIN:
        return f"The password must be at least {PASSWORD_MIN} characters."
    if len(password) > PASSWORD_MAX:
        return f"The password must be at most {PASSWORD_MAX} characters."
    lowered = password.lower()
    if username and lowered == username.lower():
        return "The password must not be the username."
    if len(set(password)) < 4 or lowered in COMMON_PASSWORDS:
        return "That password is too easy to guess."
    return None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Auth:
    def __init__(
        self,
        store: Store,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        hasher: PasswordHasher | None = None,
    ):
        self.store = store
        self.clock = clock
        self.hasher = hasher or PasswordHasher()
        # Checked for unknown usernames so a missing account takes as long as a wrong password.
        self._dummy_hash = self.hasher.hash(secrets.token_urlsafe(16))

    # ------------------------------------------------------------------ accounts

    def has_admin(self) -> bool:
        with self.store.read() as db:
            return db.execute("SELECT 1 FROM admin_user LIMIT 1").fetchone() is not None

    def _check_new_password(self, password: str, username: str) -> None:
        problem = password_problem(password, username)
        if problem:
            raise AuthError(problem)

    def create_admin(self, username: str, password: str, source: str = "setup") -> int:
        username = username.strip()
        if not username or len(username) > 64:
            raise AuthError("The username must be 1 to 64 characters.")
        self._check_new_password(password, username)
        now = iso(self.clock())
        with self.store.write() as tx:
            if tx.db.execute("SELECT 1 FROM admin_user LIMIT 1").fetchone():
                raise AuthError("An admin account already exists.")
            cur = tx.db.execute(
                "INSERT INTO admin_user (username, password_hash, created, password_changed) "
                "VALUES (?, ?, ?, ?)",
                (username, self.hasher.hash(password), now, now),
            )
            tx.audit(username, "admin.created", f"source={source}", now)
            return cur.lastrowid

    def set_password(self, username: str, password: str, source: str = "web") -> None:
        """Change an existing account's password and end all of its sessions."""
        self._check_new_password(password, username)
        now = iso(self.clock())
        with self.store.write() as tx:
            row = tx.db.execute(
                "SELECT id, username FROM admin_user WHERE username = ?", (username,)
            ).fetchone()
            if row is None:
                raise AuthError("No such account.")
            tx.db.execute(
                "UPDATE admin_user SET password_hash = ?, password_changed = ? WHERE id = ?",
                (self.hasher.hash(password), now, row["id"]),
            )
            tx.db.execute("DELETE FROM session WHERE user_id = ?", (row["id"],))
            tx.audit(row["username"], "password.changed", f"source={source}", now)

    # ------------------------------------------------------------------ throttling

    def _retry_after(self, db, scopes: list[str], now: datetime) -> int:
        """Seconds until a sign-in may be tried again, from the failures of these scopes (0 if free)."""
        wait = 0
        since = iso(now - FAIL_WINDOW)
        for scope in scopes:
            rows = db.execute(
                "SELECT ts FROM login_failure WHERE scope = ? AND ts >= ? ORDER BY ts", (scope, since)
            ).fetchall()
            if len(rows) >= FAIL_FREE:
                lock = min(LOCK_MAX_SECONDS, LOCK_BASE_SECONDS * 2 ** (len(rows) - FAIL_FREE))
                unlock = parse_time(rows[-1]["ts"]) + timedelta(seconds=lock)
                wait = max(wait, int((unlock - now).total_seconds()) + 1)
        return max(0, wait)

    def throttle_remaining(self, scopes: list[str]) -> int:
        with self.store.read() as db:
            return self._retry_after(db, scopes, self.clock())

    def _record_failure(self, scopes: list[str], now: datetime) -> None:
        stamp = iso(now)
        with self.store.write() as tx:
            tx.db.executemany(
                "INSERT INTO login_failure (scope, ts) VALUES (?, ?)", [(s, stamp) for s in scopes]
            )
            tx.db.execute("DELETE FROM login_failure WHERE ts < ?", (iso(now - 2 * FAIL_WINDOW),))

    @staticmethod
    def scopes(ip: str, username: str) -> list[str]:
        return [f"ip:{ip}", f"user:{username.strip().lower()[:64]}"]

    # ------------------------------------------------------------------ sign in and sessions

    def login(self, username: str, password: str, ip: str) -> tuple[str, Session]:
        """Verify a password and open a session. Returns (cookie token, session)."""
        now = self.clock()
        scopes = self.scopes(ip, username)
        wait = self.throttle_remaining(scopes)
        if wait:
            self.store.audit(username[:64] or "-", "login.throttled", f"ip={ip}")
            raise Throttled(wait)
        with self.store.read() as db:
            row = db.execute(
                "SELECT id, username, password_hash FROM admin_user WHERE username = ?", (username.strip(),)
            ).fetchone()
        stored = row["password_hash"] if row else self._dummy_hash
        try:
            ok = self.hasher.verify(stored, password) and row is not None
        except (VerificationError, InvalidHashError):
            ok = False
        if not ok:
            self._record_failure(scopes, now)
            self.store.audit(username[:64] or "-", "login.failed", f"ip={ip}")
            raise BadCredentials(GENERIC_FAILURE)

        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        stamp = iso(now)
        with self.store.write() as tx:
            if self.hasher.check_needs_rehash(stored):
                tx.db.execute(
                    "UPDATE admin_user SET password_hash = ? WHERE id = ?",
                    (self.hasher.hash(password), row["id"]),
                )
            tx.db.execute("DELETE FROM login_failure WHERE scope IN (?, ?)", scopes)
            tx.db.execute("DELETE FROM session WHERE expires < ?", (stamp,))
            tx.db.execute(
                "INSERT INTO session (token_hash, user_id, csrf, created, last_seen, expires, ip) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_hash_token(token), row["id"], csrf, stamp, stamp, iso(now + ABSOLUTE_TIMEOUT), ip),
            )
            tx.db.execute("UPDATE admin_user SET last_login = ? WHERE id = ?", (stamp, row["id"]))
            tx.audit(row["username"], "login.ok", f"ip={ip}", stamp)
        return token, Session(row["id"], row["username"], csrf, stamp, iso(now + ABSOLUTE_TIMEOUT))

    def session(self, token: str | None) -> Session | None:
        """The live session for a cookie token, or None. Idle and absolute timeouts apply."""
        if not token:
            return None
        now = self.clock()
        with self.store.read() as db:
            row = db.execute(
                "SELECT s.user_id, u.username, s.csrf, s.created, s.last_seen, s.expires FROM session s "
                "JOIN admin_user u ON u.id = s.user_id WHERE s.token_hash = ?",
                (_hash_token(token),),
            ).fetchone()
        if row is None:
            return None
        if parse_time(row["expires"]) <= now or parse_time(row["last_seen"]) + IDLE_TIMEOUT <= now:
            self._end(token)
            return None
        if now - parse_time(row["last_seen"]) >= TOUCH_AFTER:
            with self.store.write() as tx:
                tx.db.execute(
                    "UPDATE session SET last_seen = ? WHERE token_hash = ?", (iso(now), _hash_token(token))
                )
        return Session(row["user_id"], row["username"], row["csrf"], row["created"], row["expires"])

    def _end(self, token: str) -> None:
        with self.store.write() as tx:
            tx.db.execute("DELETE FROM session WHERE token_hash = ?", (_hash_token(token),))

    def logout(self, token: str, session: Session | None = None) -> None:
        self._end(token)
        if session:
            self.store.audit(session.username, "logout")

    # ------------------------------------------------------------------ first-run setup token

    def issue_setup_token(self) -> str | None:
        """A fresh one-time token for creating the first admin, or None if one already exists."""
        if self.has_admin():
            return None
        token = secrets.token_urlsafe(24)
        expires = iso(self.clock() + SETUP_TOKEN_LIFETIME)
        with self.store.write() as tx:
            tx.set_meta("setup_token_hash", _hash_token(token))
            tx.set_meta("setup_token_expires", expires)
        return token

    def setup_admin(self, token: str, username: str, password: str, ip: str) -> int:
        """Create the first admin with the one-time token. The token works once."""
        scopes = [f"ip:{ip}", "setup"]
        wait = self.throttle_remaining(scopes)
        if wait:
            raise Throttled(wait)
        with self.store.read() as db:
            stored = db.execute("SELECT value FROM meta WHERE key = 'setup_token_hash'").fetchone()
            expires = db.execute("SELECT value FROM meta WHERE key = 'setup_token_expires'").fetchone()
        valid = (
            stored is not None
            and expires is not None
            and hmac.compare_digest(stored["value"], _hash_token(token or ""))
            and parse_time(expires["value"]) > self.clock()
        )
        if not valid:
            self._record_failure(scopes, self.clock())
            self.store.audit("-", "setup.failed", f"ip={ip}")
            raise BadCredentials("That setup token is not valid. Restart the container to get a new one.")
        admin_id = self.create_admin(username, password, source="setup token")
        with self.store.write() as tx:
            tx.db.execute("DELETE FROM meta WHERE key IN ('setup_token_hash', 'setup_token_expires')")
        return admin_id

    def change_password(self, username: str, current: str, new: str, ip: str) -> None:
        """Change a password after checking the current one (throttled like a sign-in)."""
        scopes = self.scopes(ip, username)
        wait = self.throttle_remaining(scopes)
        if wait:
            raise Throttled(wait)
        with self.store.read() as db:
            row = db.execute(
                "SELECT password_hash FROM admin_user WHERE username = ?", (username,)
            ).fetchone()
        try:
            ok = row is not None and self.hasher.verify(row["password_hash"], current)
        except (VerificationError, InvalidHashError):
            ok = False
        if not ok:
            self._record_failure(scopes, self.clock())
            self.store.audit(username[:64], "password.change_failed", f"ip={ip}")
            raise BadCredentials("The current password is not correct.")
        self.set_password(username, new, source="web")
