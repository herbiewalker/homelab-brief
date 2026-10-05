-- The alert lifecycle (Task 11): what the engine needs to remember between runs, and a log of
-- every notification it decided to send (also the source of the daily push budget).

ALTER TABLE alert ADD COLUMN title TEXT;
ALTER TABLE alert ADD COLUMN route TEXT;
ALTER TABLE alert ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE alert ADD COLUMN opened TEXT;
ALTER TABLE alert ADD COLUMN pushed INTEGER NOT NULL DEFAULT 0;           -- how many times it was notified
ALTER TABLE alert ADD COLUMN pending_headline INTEGER NOT NULL DEFAULT 0; -- waiting for the daily headline
ALTER TABLE alert ADD COLUMN pending_state TEXT;                          -- new | repeat | escalated
ALTER TABLE alert ADD COLUMN quiet_runs INTEGER NOT NULL DEFAULT 0;       -- consecutive runs without it

CREATE TABLE push (
  ts         TEXT    NOT NULL,
  finding_id TEXT    NOT NULL,
  state      TEXT    NOT NULL,        -- new | repeat | escalated | resolved
  channel    TEXT    NOT NULL,        -- alert | headline
  level      TEXT,
  title      TEXT,
  urgent     INTEGER NOT NULL DEFAULT 0,
  delivered  INTEGER NOT NULL DEFAULT 0   -- 1 once a channel (MQTT) accepted it
);
CREATE INDEX push_ts ON push (ts);
CREATE INDEX push_finding ON push (finding_id, ts);
