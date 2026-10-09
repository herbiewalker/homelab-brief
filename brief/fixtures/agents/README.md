# Agent fixtures

Past cases with known good outcomes. Each file is one case that an agent is replayed against, in a scratch
database that is built from the file, so a run never touches the real data. They are generic and sanitized
(documentation addresses only). Add your own real cases as `/config/brief/fixtures/agents/<name>.yaml`
(see `docs/guide/10-agents.md`, Evaluation); a file with the same `id` replaces the shipped one.

Keys: `id`, `agent`, `subject`, `title`; `status` (the status document the tools read); `systems`; `findings`
(what `get_finding` returns); `signatures` (numbered 1, 2, 3 in order, with `app`, `message`, `count`);
`inventory`; `notices`; `metrics`; and `expect`, the checks the output must pass (`outcome_any`, `tools_any`,
`tools_all`, `mention`, `mention_any`, `forbid`, `min_kept`, `verdicts`, `proposals`).
