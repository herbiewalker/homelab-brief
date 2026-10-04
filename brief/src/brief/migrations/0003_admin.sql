-- Admin accounts, sessions, and login throttling (Task 6).

-- One admin account is created today; the table is shaped for several users later.
CREATE TABLE admin_user (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  username         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash    TEXT NOT NULL,         -- Argon2id, never the password
  created          TEXT NOT NULL,
  password_changed TEXT NOT NULL,
  last_login       TEXT
);

-- Server-side sessions. The cookie holds a random token; only its SHA-256 is stored here, so a
-- copy of the database cannot be used to sign in.
CREATE TABLE session (
  token_hash TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES admin_user (id) ON DELETE CASCADE,
  csrf       TEXT NOT NULL,               -- the CSRF token bound to this session
  created    TEXT NOT NULL,
  last_seen  TEXT NOT NULL,
  expires    TEXT NOT NULL,               -- absolute lifetime
  ip         TEXT
);
CREATE INDEX session_user ON session (user_id);

-- Failed sign-ins, kept in the database so a restart does not reset the throttle.
CREATE TABLE login_failure (
  scope TEXT NOT NULL,                    -- ip:<address> or user:<name, lower case>
  ts    TEXT NOT NULL
);
CREATE INDEX login_failure_scope ON login_failure (scope, ts);
