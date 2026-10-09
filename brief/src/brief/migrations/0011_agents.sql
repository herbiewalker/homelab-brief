-- Agent runs, their tool steps, and the background queue (docs/guide/10-agents.md). Metadata for every
-- step; the one piece of content kept is `output`, the validated note a person is meant to read.
CREATE TABLE agent_run (
  run_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  agent     TEXT NOT NULL,
  trigger   TEXT NOT NULL,        -- on_demand | finding | schedule | queue ...
  subject   TEXT,                 -- a finding id, or the question asked
  started   TEXT NOT NULL,
  finished  TEXT,
  provider  TEXT,
  model     TEXT,
  outcome   TEXT NOT NULL,        -- ok | no_evidence | limit | failed | off | skipped
  steps     INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  tokens_in  INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  kept      INTEGER NOT NULL DEFAULT 0,   -- statements that cited a tool result
  dropped   INTEGER NOT NULL DEFAULT 0,   -- statements removed for having no valid citation
  output    TEXT,
  detail    TEXT                  -- a short reason, never model output
);
CREATE INDEX agent_run_started ON agent_run (agent, started);

CREATE TABLE agent_step (
  run_id  INTEGER NOT NULL REFERENCES agent_run (run_id),
  n       INTEGER NOT NULL,
  result  TEXT,                   -- the id the output cites, such as r2
  tool    TEXT NOT NULL,
  outcome TEXT NOT NULL,          -- ok | error | denied | withheld | failed | unknown tool | over limit
  ms      INTEGER,
  PRIMARY KEY (run_id, n)
);

CREATE TABLE agent_job (
  job_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  agent     TEXT NOT NULL,
  subject   TEXT,
  trigger   TEXT NOT NULL,
  created   TEXT NOT NULL,
  requested_by TEXT,
  status    TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | skipped | failed
  run_id    INTEGER
);
CREATE INDEX agent_job_status ON agent_job (status, created);
