-- Ingest bookkeeping (Task 5).

-- How far each event file has been read. Stored here, not in a side file, so the offset commits in
-- the same transaction as the counts it produced: a crash can never count a line twice or lose one.
CREATE TABLE ingest_offset (
  path    TEXT PRIMARY KEY,           -- file name inside the events folder
  offset  INTEGER NOT NULL CHECK (offset >= 0),
  updated TEXT NOT NULL
);

-- Events removed by an ignore rule, counted per rule so a change in their volume stays visible.
CREATE TABLE suppressed (
  day   TEXT    NOT NULL,
  hour  INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
  host  TEXT    NOT NULL,
  rule  TEXT    NOT NULL,             -- the ignore rule's name, or signature:<sig_id>
  n     INTEGER NOT NULL CHECK (n >= 0),
  PRIMARY KEY (day, hour, host, rule)
) WITHOUT ROWID;

-- 1 when the host is listed in systems.yaml, 0 when it rolled up to the default system.
ALTER TABLE host ADD COLUMN known INTEGER NOT NULL DEFAULT 1;
