-- MCP client tokens (docs/guide/09-ai-integration.md): one per outside AI client, stored only as a hash.
-- scopes is a comma-separated list; locality is lan or cloud (a cloud client never gets device, DNS, or
-- raw-log scopes, and what it is sent is scanned for private names).
CREATE TABLE mcp_token (
  token_id  TEXT PRIMARY KEY,
  name      TEXT NOT NULL,
  hash      TEXT NOT NULL,
  scopes    TEXT NOT NULL,
  locality  TEXT NOT NULL DEFAULT 'lan',
  created   TEXT NOT NULL,
  created_by TEXT,
  last_used TEXT,
  revoked   TEXT
);
CREATE UNIQUE INDEX mcp_token_name ON mcp_token (name) WHERE revoked IS NULL;
