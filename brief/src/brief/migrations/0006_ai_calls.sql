-- The AI call log (Task 12): one row per attempt, never the content. The daily call and cloud-token
-- budgets are counted from here, and the AI page shows why a chain fell through to the template.

CREATE TABLE ai_call (
  ts         TEXT    NOT NULL,
  task       TEXT    NOT NULL,        -- morning_brief | weekly_summary | explain_finding | ...
  provider   TEXT    NOT NULL,
  model      TEXT,
  locality   TEXT,                    -- lan | cloud
  latency_ms INTEGER,
  tokens_in  INTEGER,
  tokens_out INTEGER,
  outcome    TEXT    NOT NULL,        -- ok | error | timeout | invalid | budget | skipped
  detail     TEXT                     -- a short reason, never model output
);
CREATE INDEX ai_call_ts ON ai_call (ts);
