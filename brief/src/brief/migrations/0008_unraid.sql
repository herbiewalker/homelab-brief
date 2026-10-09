-- Unraid collector (Task 19).

-- One row per parity check a server has run (its history is read from the API every run; a check is
-- identified by its start date, so reading it again changes nothing).
CREATE TABLE parity_check (
  server      TEXT NOT NULL,
  date        TEXT NOT NULL,
  duration_s  INTEGER,
  speed_bps   INTEGER,
  status      TEXT,
  errors      INTEGER,
  PRIMARY KEY (server, date)
);

-- The notifications a server currently shows as unread (replaced every run, so an archived one disappears).
CREATE TABLE server_notice (
  server      TEXT NOT NULL,
  notice_id   TEXT NOT NULL,
  ts          TEXT,
  importance  TEXT,
  subject     TEXT,
  description TEXT,
  PRIMARY KEY (server, notice_id)
);

-- When an available version was first seen, so version_behind can say how long it has been pending.
ALTER TABLE inventory ADD COLUMN available_since TEXT;
