-- Baseline schema (docs/guide/02-baseline.md section 2b, docs/guide/03-brief.md for alerts).
-- All times are UTC text in the form 2026-09-26T13:22:33Z, so they sort as strings.

-- Small key/value store for the program's own state (miner_generation and similar).
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- One row per message template. sig_id is ours, never Drain3's: Drain3 ids restart when its state
-- file is lost, so the pair (miner_generation, cluster_id) maps to a stable sig_id instead.
CREATE TABLE signature (
  sig_id           INTEGER PRIMARY KEY AUTOINCREMENT,
  miner_generation INTEGER NOT NULL,
  cluster_id       INTEGER NOT NULL,
  app              TEXT,
  template         TEXT,
  first_seen       TEXT NOT NULL,
  last_seen        TEXT NOT NULL,
  prev_last_seen   TEXT,              -- last_seen from before the most recent quiet gap
  ignored          INTEGER NOT NULL DEFAULT 0,
  UNIQUE (miner_generation, cluster_id)
);
CREATE INDEX signature_last_seen ON signature (last_seen);

CREATE TABLE host (
  host             TEXT PRIMARY KEY,  -- the sender's IP
  system_id        TEXT,
  first_seen       TEXT NOT NULL,
  last_seen        TEXT NOT NULL,
  expected_gap_min INTEGER            -- learned typical gap between lines
);

-- Event counts by event hour (UTC), flushed once per cycle.
CREATE TABLE hourly (
  day      TEXT    NOT NULL,          -- YYYY-MM-DD
  hour     INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
  host     TEXT    NOT NULL,
  sig_id   INTEGER NOT NULL REFERENCES signature (sig_id),
  severity TEXT    NOT NULL,          -- emerg, alert, crit, err, warning, notice, info, debug
  n        INTEGER NOT NULL CHECK (n >= 0),
  PRIMARY KEY (day, hour, host, sig_id, severity)
) WITHOUT ROWID;
CREATE INDEX hourly_sig ON hourly (sig_id, day);
CREATE INDEX hourly_host ON hourly (host, day);

-- The last few raw lines per signature, for the brief and the admin GUI.
CREATE TABLE sample (
  sig_id INTEGER NOT NULL REFERENCES signature (sig_id),
  ts     TEXT    NOT NULL,
  host   TEXT,
  msg    TEXT
);
CREATE INDEX sample_sig ON sample (sig_id, ts);

-- Numeric series (collectors, self-monitoring).
CREATE TABLE metric (
  ts        TEXT NOT NULL,
  metric_id TEXT NOT NULL,
  value     REAL
);
CREATE INDEX metric_series ON metric (metric_id, ts);

-- Every admin change.
CREATE TABLE audit_log (
  ts     TEXT NOT NULL,
  user   TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT
);
CREATE INDEX audit_ts ON audit_log (ts);

-- Notification lifecycle (dedupe, re-notify, escalate, resolve) and acknowledgements.
CREATE TABLE alert (
  finding_id   TEXT PRIMARY KEY,
  kind         TEXT,
  rule         TEXT,
  level        TEXT,
  state        TEXT NOT NULL DEFAULT 'open',   -- open | acked | snoozed | resolved
  first_sent   TEXT,
  last_sent    TEXT,
  last_value   TEXT,
  snooze_until TEXT
);
CREATE INDEX alert_state ON alert (state);

CREATE TABLE feedback (
  ts         TEXT NOT NULL,
  finding_id TEXT NOT NULL,
  action     TEXT NOT NULL
);
CREATE INDEX feedback_finding ON feedback (finding_id, ts);
