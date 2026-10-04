# Homelab Brief: instructions for coding agents

These instructions are tool-neutral (the `AGENTS.md` convention), for any coding agent or person. `CLAUDE.md` only points here.

## What this project is

One container (`homelab-brief`) on an Unraid host server, with its own LAN IP, that turns network and homelab logs and API data into:

- a daily news-style morning brief,
- background trend baselines (SIEM-like) for "is this normal?" questions,
- alerts, MQTT sensors, a REST API, Prometheus metrics, and static views that Home Assistant (HA) and other dashboards display on phones, repurposed tablets, and an e-reader,
- a modern admin GUI (and CLI/optional SSH) to run and tune all of it.

Data flow: devices send syslog to the container (Vector receives, archives, parses) and collectors poll standard APIs -> worker (Drain3 signatures, SQLite baselines, detectors, alerts) -> MQTT with HA discovery, `/api/v1`, `/metrics`, `/mcp` for AI clients, static views, digests for people and AI assistants.

The container does all analysis and storage. Home Assistant displays results and owns power and energy data, which it sends back over MQTT statestream. The MQTT broker is outside the container.

## Source of truth

1. `docs/unraid-backend-guide.md` (index, scope, layout) and the parts in `docs/guide/` are the technical spec (configs, schemas, steps, verification). `docs/project-plan.md` is the overall plan (outputs, tiers, dashboards, HA link, phases).
2. `docs/TASKS.md` is the build backlog. Phase 1 runs in order; Phase 2 starts with Task 15, then any order.
3. `docs/DECISIONS.md` records open and settled decisions. Update it; do not decide open items silently.

If the code and the guide disagree, stop and ask which is right, then update the guide.

## Hard rules

- **This repository is the generic product, with nothing personal in it, ever.** It must be safe to publish or hand to someone else. Never commit passwords, API keys, hostnames, IPs, MAC addresses, share paths, names of people, rooms, devices, or services, or real log lines, and do not describe any one household's setup (servers, hardware, family, location, timezone). Docs and examples use placeholders (`<host>`, `<share>`, `server-1`). Everything specific to one install lives in its instance folder (`docs/INSTANCE.md`, `scripts/instance.py`), normally a separate private repository, and the container reads it only through `/config` and template variables. There is no allowed exception for deployment values. Only the small trimmed public sample set (`scripts/trim_samples.py`) is committed. The commit guard and CI enforce this; add to `scripts/guard-allow.txt` only values that are generic and public, and put install-specific exceptions in the instance's `guard/allow.txt`.
- **The syslog share is read-only.** The user's live raw archive is read from (for samples and analysis) but never written to, moved, renamed, compressed, or deleted from the dev machine. The share path comes from the user (`-Share` or `BRIEF_SYSLOG_SHARE`), never from this repository.
- **No LLM dependency, no vendor lock-in.** The pipeline must run and publish with no network access and no AI. AI is reached only through `brief/ai.yaml`: OpenAI-compatible Chat Completions for any provider (local or hosted), with the native Anthropic API as one optional adapter. Never hard-code a vendor, model, or SDK outside the provider layer. Every AI task has a fallback chain ending in the template, runs with a timeout and budget, and its output is validated. AI never decides findings, severity, or routing and never takes actions. `cloud` providers never receive device-level detail, raw log lines, DNS data, or `devices.yaml` content. Prompts live in `brief/prompts/` as plain Markdown with no vendor-specific syntax. See `docs/guide/09-ai-integration.md`.
- **AI clients reach the product through MCP only.** The MCP server is read-only, token-authenticated with scopes, and audited.
- **AI is off by default and fully switchable.** `ai.yaml` `enabled: false` means no model calls, no agents, and no MCP server. Each feature and each agent has its own switch. Locked guardrails (never waits on AI, never decides or acts, cloud privacy, read-only tools, proposals need approval, citations required, everything logged) are enforced in code and have no setting. See `docs/guide/10-agents.md`.
- **Fail loudly.** If a run fails, write `run_ok: false` and a "no data today" headline. Never publish "all quiet" when data is missing.
- **Efficiency is a requirement.** No per-line database writes (batch once per cycle). SQLite lives on the pool path, never `/mnt/user`. Hourly or daily timers except heartbeats. No headless browsers; render PNG with Pillow. Tablet and e-reader views are static HTML with no JavaScript. The admin GUI and network map use vendored, pinned htmx, Alpine.js, and Cytoscape.js with no external requests and no build step.
- **Output size.** Every generated markdown file stays at or under 150 lines, because AI assistants (through the Filesystem connector, MCP, or a small local model's context) read them with limited room, and some reads over SMB stop around 150-180 lines.
- **Times in files are UTC.** Convert to local time only when rendering.
- **Event time, not read time.** Bucket counts by when an event happened (normalized to UTC, missing year inferred), never by when it was read, so backfills and late lines land in the right hour. Analysis windows are rolling ("last 24 hours"), which avoids day-boundary and daylight-saving bugs.
- **No code in config.** Config conditions are structured (field, operator, value). Never `eval` anything from a config file.
- **Config reloads safely.** Reload config every run, validate it, keep the last good version on error, and raise a `config_error` finding.
- **Notifications must not nag.** Every push goes through the alert lifecycle: dedupe, re-notify interval, escalate only when worse, resolved message, daily budget, and acknowledgements from Home Assistant.
- **One home per fact.** Values (thresholds, routes, schedules) live in the config files under `brief/`. Docs explain and link to them instead of copying values. If a doc and a config file disagree, the config file wins; fix the doc.
- **Pin versions.** Pin exact image tags and Python dependency versions. Never use `latest`. Look up the current stable version when pinning and record it in `docs/DECISIONS.md`.
- **Destructive scripts default to dry run.** Any script that deletes or compresses files prints what it would do unless passed `--apply`.
- **Never touch production.** Develop and test locally in Docker against sanitized samples. Deployment to Unraid is a separate step run by the user from `deploy/`.
- **Admin security is not optional.** `/admin` and every write require login over HTTPS; no default credentials; Argon2 password hashes; Secure/HttpOnly/SameSite=Strict sessions; CSRF on every write; login rate limiting; an audit-log row for every change. Read-only views, `/api/v1` reads, and `/metrics` never change anything. The optional SSH server is off by default, key-only, no root login. Secrets come from template variables, never from committed files. See `docs/guide/08-admin-and-access.md`.
- **Every GUI action has a CLI equivalent** (`brief ...`), usable through `docker exec`.
- **Tests are required** for the detectors, the ignore rules, signature stability, and the failure path ("no data today"). Use pytest.
- **Ask before adding a dependency**, and say why in the commit message. The whole container (Vector, worker, web server) has a 768 MB memory limit; keep the worker well under 512 MB.
- **Phase 2 rules** (see `docs/guide/06-phase2-device-intelligence.md`): behavior is configured in `brief/*.yaml`, not hard-coded; collectors are pluggable and start disabled; detectors and the playbook decide findings, and a model only rewrites existing ones; the backend suggests actions but never executes them; `brief/devices.yaml`, client data, and DNS data never go in git, samples, or any call that leaves the LAN.
- **Prefer open source** unless it adds a heavy service or much more build time for the same result. Record each pick and its license in `docs/DECISIONS.md` and verify the license when pinning.

## Layout

```
CLAUDE.md                  pointer to this file for Claude Code
AGENTS.md                  this file
docs/                      guide index, guide parts (guide/), plan, tasks, decisions, session directions
vector/                    Vector config
brief/                     Python service (src/, tests/, config yaml, requirements)
mosquitto/                 broker config and ACL (no password file in git)
samples/                   the small trimmed set of sanitized log samples (the full set stays in your instance)
deploy/                    Unraid template, compose reference, deploy steps
scripts/                   helpers: dev-init, pull-samples, audit-syslog, sanitize and trim samples, the commit guard
instance-template/         skeleton of your private instance folder (docs/INSTANCE.md)
docker-compose.yml         local dev stack (the app image plus a dev Mosquitto)
Dockerfile                 the single homelab-brief image (Task 0)
secrets.env.example        template; the real secrets.env is gitignored
```

## Module plan for `brief/src`

`ingest` (read complete NDJSON lines by offset), `importer` (one-off backfill), `timeparse` (event time, year and timezone), `systems` (host to system mapping), `signatures` (Drain3 plus our own signature ids), `store` (SQLite and migrations), `ignore` (rules and suppressed counters), `detectors`, `alerts` (routing, lifecycle, feedback), `digest` (files for people and AI clients, and status.json), `render` (HTML and PNG), `ai` (provider layer: OpenAI-compatible client, optional Anthropic adapter, chains, validation, budgets, call log), `mcp` (read-only MCP server, tokens and scopes), `registry` and `collectors/` (metrics and data sources), `publish` (MQTT discovery, state, availability, feedback and statestream subscriptions), `outputs` (`/metrics`, `/api/v1`, Apprise), `map` (topology merge, map.json, SVG), `config` (load, validate, reload, versioned history, rollback), `web` (FastAPI app: read-only views, API, admin GUI), `auth` (accounts, sessions, CSRF, audit log), `scheduler` (hourly, daily, weekly runs), `cli` (every admin action), `main`.

Keep modules small and testable. Detectors are pure functions over database rows.

## Working style

- Do one task from `docs/TASKS.md` at a time, run the tests, then commit with a clear message.
- **Review pass before each commit to main:** re-read the diff against the hard rules above (privacy, secrets, security, AI guardrails, efficiency) in a fresh context or a second agent session, and fix anything found. The pre-commit checks (lint, secrets scan, private-name guard) must pass.
- At the end of each session, update the status in `docs/TASKS.md` and add any decisions or surprises to `docs/DECISIONS.md`. That file is the memory between sessions.
- When a real log format or network detail is needed and is not in `samples/sanitized/`, ask the user for it instead of guessing.
