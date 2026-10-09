-- Agent evaluation (docs/guide/10-agents.md): one row per agent per fixture run. The fixtures run in a scratch
-- database, never against the real data; what is kept here is the score and which checks failed, not any output.
CREATE TABLE agent_eval (
  eval_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  agent         TEXT NOT NULL,
  started       TEXT NOT NULL,
  label         TEXT NOT NULL,           -- baseline | candidate | manual
  fingerprint   TEXT NOT NULL,           -- the prompt, model, tools, limits, and chain that were tested
  model         TEXT,
  cases         INTEGER NOT NULL,
  passed        INTEGER NOT NULL,
  score         REAL NOT NULL,           -- 0 to 100
  citation_rate REAL NOT NULL,           -- statements kept / statements written
  avg_steps     REAL NOT NULL,
  avg_ms        REAL NOT NULL,
  detail        TEXT NOT NULL,           -- JSON: per case {id, passed, failed checks, kept, dropped, steps, outcome}
  flagged       INTEGER NOT NULL DEFAULT 0,   -- a candidate that scored lower than the baseline
  baseline_score REAL,
  note          TEXT
);
CREATE INDEX agent_eval_agent ON agent_eval (agent, started);
