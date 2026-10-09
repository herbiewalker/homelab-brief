-- Proposals (docs/guide/10-agents.md): a change an agent or a propose-scoped MCP client suggests. It is only a
-- record until a person approves it on the AI page; approval goes through the normal config editor.
CREATE TABLE proposal (
  proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created     TEXT NOT NULL,
  source      TEXT NOT NULL,          -- agent:<name> | mcp:<client> | cli:<user>
  run_id      INTEGER,                -- the agent run that made it
  file        TEXT NOT NULL,          -- a config file name
  base_digest TEXT NOT NULL,          -- sha256 of the file's text when the proposal was made
  new_text    TEXT NOT NULL,
  reason      TEXT NOT NULL,
  evidence    TEXT NOT NULL,          -- cited tool results, and replay numbers where there are any
  status      TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | stale | invalid | expired
  decided     TEXT,
  decided_by  TEXT,
  note        TEXT
);
CREATE INDEX proposal_status ON proposal (status, created);
