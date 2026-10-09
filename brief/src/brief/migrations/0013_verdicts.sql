-- What the triage agent decided about a signature (docs/guide/10-agents.md). A verdict is advice for a
-- person; it never hides, ignores, or routes anything by itself.
CREATE TABLE agent_verdict (
  sig_id  INTEGER NOT NULL REFERENCES signature (sig_id),
  verdict TEXT NOT NULL,          -- noise | watch | concern | unsure
  reason  TEXT NOT NULL,
  run_id  INTEGER NOT NULL,
  created TEXT NOT NULL,
  PRIMARY KEY (sig_id, run_id)
);
