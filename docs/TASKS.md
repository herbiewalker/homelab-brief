# Build backlog

Status: `[ ]` open, `[~]` in progress, `[x]` done. Mark a task done only when its acceptance criteria pass.

The product is **one container** on the host server with its own LAN IP. It receives syslog directly, polls APIs, runs the analysis, serves the web admin GUI and read-only views, and publishes to Home Assistant and other dashboards through standard interfaces. The MQTT broker stays outside (HA's Mosquitto add-on long term).

Phase 1 runs in order. Ingestion is deployed early (Task 7) so real history accumulates while the rest is built. In Phase 2, Task 18 comes first and the rest can go in any order.

# Phase 1: Core product

## Task 0: Scaffold, single image, dev stack
- [x] Look up and pin current stable versions (Python base image, Vector, process supervisor, Python dependencies, vendored web assets). Record versions and licenses in `docs/DECISIONS.md`.
- [x] Multi-stage `Dockerfile` producing one image: Python service, the Vector binary, and a process supervisor (s6-overlay or supervisord; pick one and record why) running Vector, the worker, and the web server.
- [x] Docker `HEALTHCHECK` against the app's `/healthz`.
- [x] Pinned `brief/requirements.txt`, pytest scaffold, `scripts/dev-init.ps1` run once.
- [x] Code quality and leak guards: `ruff` for lint and format, `pre-commit` hooks, `gitleaks` for secrets, and a private-name guard that reads the gitignored `systems.yaml`, `devices.yaml`, and `topology.yaml` and blocks any commit containing those hostnames, IPs, or MACs. The same checks run in GitHub Actions on every push.
- [x] `docker-compose.yml` runs the app plus a dev Mosquitto, with syslog on a high local port and outputs under `.dev/`.
- Accept: `docker compose up` builds and starts with a healthy status; `pytest` runs; `git ls-files --eol` shows LF for shell, YAML, and Python files.
- Status 2026-10-03: **Task 0 done locally.** Acceptance verified in WSL2 Docker Engine: image builds, `docker compose up` reaches healthy, `pytest` passes (3 tests, in the test stage), `git ls-files --eol` shows LF everywhere except the `.ps1` files (CRLF by design). Vector 0.58 listens on 5514 as uid 99; the web app answers `/healthz`. Leak guards: ruff (lint and format, Python only), gitleaks, and a stdlib private-name guard (`scripts/private_name_guard.py`, 9 tests in `scripts/tests/`) run through pre-commit; the hook is proven to block a staged real hostname from Windows Git Bash (it hands off to WSL). `scripts/setup-dev-tools.sh` sets it all up. **CI verified 2026-10-03** (`.github/workflows/checks.yml`, green on `fdb70b9`; the first run caught a gitleaks false positive, now in `.gitleaksignore`). Follow-ups: `/healthz` and the Docker health check only see the web process, so a crashed Vector still shows healthy (fold into Task 6 and Task 14); starlette deprecates `httpx` for its TestClient (switch to what it recommends when pinning).

## Task 1: Audit your syslog share and pull samples
- [x] Audit the existing syslog share read-only: sources, formats, volume, top talkers. Findings that apply to everyone are in `docs/DECISIONS.md` ("What real logs look like"); your own go in your instance folder (`docs/INSTANCE.md`).
- [x] `scripts/pull-samples.ps1 -Share <path>` copies the last N lines per source into the instance's `samples/raw/` (read-only against the share).
- [x] `scripts/audit-syslog.ps1 -Path <file> -Out <file>` counts lines per program per day on the largest file (read-only); record the result in the instance.
- Accept: samples exist in the instance's `samples/raw/` for every source; the top-talkers result is recorded.
- Note: chatty sources cover only an hour or two in their last 200 lines, sparse ones cover weeks, and a busy server's last lines can be boot-time noise, so Tasks 2 and 3 may want `-Lines 1000` or a hand-picked slice.

## Task 2: Sample sanitizer
- [x] Convert the instance's `samples/raw/` to its `samples/sanitized/`, replacing hostnames, IPs (private and public), MACs, camera, device, client, and rule names, site prefixes in hostnames, and domains in DNS lines with stable placeholders, and flag anything that looks like a credential or token.
- [x] Test that no raw value survives in the output of a fixture file.
- [x] `scripts/trim_samples.py` writes a small public set (a few lines per message shape per source) into this repository's `samples/sanitized/`; the full set stays in the instance.
- Accept: sanitized samples exist for every device type and pass the test; the repository holds only the trimmed set and no instance data.
- Status: `scripts/sanitize_samples.py` (stdlib, with a self-check that writes nothing if a replaced value survives), `scripts/trim_samples.py`, and the guard changes they needed are done, with tests in `scripts/tests/`. Real data exposed gaps that invented fixtures missed; all are fixed. Read the output before publishing it: the check cannot find a private name it was never taught.

## Task 3: Syslog receiver, archive, and parsing (Vector)
- [x] Receive syslog over UDP and TCP (raw line `socket` sources, so the wire format including the priority field is kept). The sender's IP is the host.
- [x] Write the raw archive: one file per host per day under `/archive`, compressed after the day ends.
- [x] Parse into normalized events: priority-derived severity when present (keyword fallback otherwise), CEF branch for Protect, duplicated gateway hostnames, `<mac>,<model>-<firmware>` tokens (keep firmware as a field), the NVR header with spaces and pipes, coredns JSON lines, `parse_failed` kept.
- [x] Backfill import: a separate Vector config that reads the old `syslog-<ip>.log` files once (read-only) into the same archive and event stream.
- [x] Sender allowlist in `brief/network.yaml` (subnets or addresses): lines from unknown senders are kept but flagged (`unknown_sender` finding), or dropped if you choose. Syslog over UDP can be spoofed by anything on the LAN.
- Accept: replaying the sanitized samples over UDP and TCP produces archive files and events; a script reports parse-failure rate and real versus inferred severity.
- Status: done and verified end to end on the built image. `vector/vector.yaml`, `vector/parse.vrl`, and `vector/backfill.yaml` ship in the image at `/app/vector/`; 26 Vector unit tests (`vector/tests/parse.yaml`, run with `scripts/test-vector.sh`); `scripts/e2e_vector.py` starts the image and replays the sanitized samples over UDP and TCP in five scenarios (default, allowed, flag, drop, backfill): 374 of 374 lines received, archived, and parsed with 0 failures, 300 real and 74 inferred severities, flagged and dropped senders behaving as set, and the backfill importing every line with an inferred severity. `python -m brief.vectorcfg` renders the port and allowlist from `brief/network.yaml`; `python -m brief.archive compress` compresses finished days (dry run unless `--apply`). `scripts/replay_samples.py` and `scripts/parse_report.py` are the replay and the failure-rate / real-versus-inferred report. CI runs the Vector tests and the end-to-end check. Not done here: scheduling the daily compression and retention pruning (Task 14), `brief import` (Task 16), and the unresolved log-storm decision (DECISIONS). A storm of millions of lines a day will make large NDJSON event files; measure before choosing a throttle.

## Task 4: Store
- [x] SQLite schema from the guide, WAL mode, simple migrations, single-writer helper; our own `sig_id` mapped from `(miner_generation, cluster_id)`; `prev_last_seen`; `alert`, `feedback`, and `audit_log` tables.
- Accept: tests cover create, upsert of hourly counts, first/last-seen updates, and that a reset miner never collides with existing signatures.
- Status: done. `brief/src/brief/store.py` and `brief/src/brief/migrations/0001_baseline.sql`, with 31 tests in `brief/tests/test_store.py` (schema and WAL, atomic migrations with a backup before an upgrade and a refusal of a newer database, our `sig_id` and a reset miner that never collides, first/last-seen and `prev_last_seen`, hourly upserts, host, sample, metric, audit, alert and feedback tables, rollback on error, no nested writes, and no lost updates across threads or across two stores on one file). Phase 2 tables come as later migrations (Task 18).

## Task 5: Ingest, signatures, ignore rules, systems, learning period
- [x] Offset-tracked NDJSON reader that consumes only complete lines, survives restarts, and handles midnight rollover.
- [x] Bucket counts by event time normalized to UTC (`source_tz` from `systems.yaml`, missing year inferred), with fallback to receive time flagged.
- [x] Drain3 wrapper with persistence, masking, template updates, and generation mapping.
- [x] `systems.yaml` loader; unknown hosts roll up to `default_system` and are flagged.
- [x] Ignore rules with `suppressed` counters; learning-period flag; one batched flush per cycle.
- Accept: replaying a sample twice does not double count; partial lines wait; backfilled events land in their own hours; tests for restart, rollover, and unknown hosts.
- Status: done and verified end to end. `ingest.py`, `signatures.py`, `systems.py`, `ignore.py`, `timeparse.py`, `learning.py`, `config.py`, and the worker loop, with migration `0002_ingest.sql`; 88 new tests (replays do not double count, partial lines wait, restarts, midnight rollover, backfilled events land in their own hours, unknown hosts, ignore rules, lost miner state, failed flush rolls back, byte budget, last-good config). The end-to-end check has an `ingest` scenario: 374 sample lines through Vector and the worker into the database. Still ahead: the scheduler's other jobs and self-monitoring metrics (Task 14), and the detectors that read this data (Task 8).

## Task 6: Web app foundation and security
- [x] FastAPI app served by Uvicorn inside the container: read-only views on HTTP (brief, status board, map, `status.json`) and `/admin` on HTTPS only.
- [x] Local admin account: first-run setup through the CLI (`brief admin set-password`) or a one-time setup token printed to the container log; passwords hashed with Argon2; no default credentials.
- [x] Sessions with Secure, HttpOnly, SameSite=Strict cookies; CSRF protection on every write; login rate limiting; an `audit_log` row for every admin change.
- [x] TLS: self-signed certificate generated on first start, replaceable with your own; HTTP to HTTPS redirect for `/admin` only.
- [x] Listen ports come from `brief/network.yaml` (with environment overrides for first start), so the same image runs in own-IP, host, and bridge modes.
- [x] `/healthz` and a minimal status page (version, uptime, ingest rate, last runs, errors). Structured JSON logs to stdout.
- Accept: `/admin` is unreachable without login; a write without a CSRF token is rejected; repeated failed logins are throttled; the read-only views work without login on old tablets.
- Status: done and verified end to end. HTTP and HTTPS apps from one codebase (`web.py`), a default-deny guard on `/admin`, Argon2id accounts, server-side sessions with hardened cookies, CSRF, per-address and per-user login throttling, a one-time setup token and `brief admin set-password`, a self-signed certificate that is replaced by yours, ports from `network.yaml`, JSON logs, and a `/healthz` that covers the database, the worker, and the receiver. Migration `0003_admin.sql`. 94 new tests, and `scripts/e2e_web.py` drives the built image over real TLS (CI runs it). Fixed on the way: two processes starting together on a fresh database could race on the migration (it is now re-checked under the write lock). Only login, sign-out, and change-password exist under `/admin` so far; the GUI pages are Task 15.

## Task 7: Thin slice and early deploy
- [x] Minimal `status.json` (run_ok, events_24h, new_signatures_24h, hosts_silent) and a minimal MQTT publisher for two sensors with availability.
- [x] First deploy pass in `deploy/README.md`: run the container on the host server with its own IP, repoint every device and server's syslog to the container IP, then import the backfill.
- [ ] The user deploys the first pass.
- Accept: the user has deployed the first pass; every source arrives at the container; `hourly` grows on the server; the sensors appear in MQTT.
- Status: everything buildable is done and verified; the deploy is the user's step. `status.py`, `publish.py`, `importer.py` (`brief import`), the hourly job in the worker, `GET /status.json`, and `deploy/README.md` Pass 1 (rewritten for what exists: no GUI pages yet, so it uses logs, `/healthz`, and `/status.json`). `scripts/e2e_mqtt.py` runs a real Mosquitto with the repository ACL against the built image (discovery, retained states, availability online, offline on clean stop and on kill, the `ha` user cannot write sensor topics) and CI runs it. Three sensors ship instead of two (`new_signatures_24h` came free). `hosts_silent` is a thin version of the Task 8 absence detector. Decisions and gotchas are in the DECISIONS session log. Task 3's note that `brief import` waited for Task 16 is now stale: it exists.

## Task 8: Detectors
- [x] Novelty (first seen, or seen again after 7+ quiet days), deviation (daily total against the 14-day median plus MAD), off-hours, absence. Thresholds come from config. Absence runs hourly; the others daily.
- [x] Clock skew: compare each source's event time with receive time; flag a source drifting more than a few minutes (`clock_skew`), because bad device clocks quietly break every time-based detector.
- Accept: each detector has tests for a positive case, a negative case, and learning-period suppression.
- Status: built, unit-tested, tuned on real history (see the DECISIONS tuning entry: 555 findings down to 89), and the miner-save speedup in DECISIONS applies; not yet scheduled (the scheduler is Task 14) and not yet turned into stories or pushes (Tasks 10 to 12). `detectors.py` holds the rules as pure functions, `detect.py` gathers the rows and runs them, and `brief detect [--kind K] [--json] [--ignore-learning] [--now T]` prints the findings read-only. Novelty, deviation, and off-hours are silent during the learning period; absence and clock skew are not (they need no learned baseline; absence falls back to a day until a gap is learned). Absence replaces the thin version in status.json, and the hourly job now learns each host's normal gap (`detect.learn_gaps`, from the hours it was active, needs 24 active hours). Clock skew is the median of event minus receive time per host per cycle, stored on the host row (migration 0004); imported events and time fallbacks are not measured. A signature that returns is reported only on the day it returned (`returned_at`, the first activity after `prev_last_seen`), which the guide's SQL would have repeated daily.

## Task 9: Replay mode and golden-day test
- [x] `brief replay --from --to [--dry-run]` and a golden-day fixture with an expected digest snapshot.
- Accept: replay reproduces live findings; the golden test fails loudly when output changes.
- Status: done. `brief replay --from D --to D [--at HH:MM] [--no-hourly] [--ignore-learning] [--json] [--verbose] [--dry-run]` runs the detectors as of each past moment (daily detectors at `--at`, absence every hour) and prints what was new, what resolved, and which new findings would have been pushed (`routing.py` reads `priorities.yaml`); it never writes. Replay needed every detector input to be as of the replayed moment, so the novelty query now works from the hourly counts only (it no longer reads `signature.prev_last_seen` or `last_seen`) and absence reads `detect.HostActivity` (host activity by hour) instead of `host.last_seen`; a test proves a replay equals what a live run said even after later data arrives. Limits, documented in `replay.py`: absence is as precise as the hour, clock skew cannot be replayed (live measurement), and it uses today's system mapping, ignore rules, and learned host gaps. The golden-day test (`tests/golden_day.py`, `tests/test_golden.py`, snapshot `tests/golden/expected_replay.json`) pushes a synthetic month through the real pipeline and replays three days of it; it fails with a diff and tells you to regenerate with `GOLDEN_UPDATE=1`. Not applied yet (Tasks 11 and 12): dedupe over days, re-notify, escalation, quiet hours, the daily push budget.

## Task 10: Digest, status files, exports, weekly report
- [x] `status.json` (schema 1), digests, host files, `state.json`, the failure path ("no data today"), `brief export`, and `brief weekly`.
- Accept: a test enforces the 150-line limit; a failed run never says "all quiet"; export and weekly have fixture tests.
- Status: done. `grouping.py` (findings to stories), `issues.py` (state.json), `digest.py` (compose, system states, headline, the failure path), `render_md.py` (digest and host pages, 150-line clip), `outputs.py` (the daily run and its files), `export.py`, `weekly.py`, the `brief digest`, `brief export`, and `brief weekly` commands, `brief/schedule.yaml`, and a daily trigger in the worker (replaced by the Task 14 scheduler). `status.json` now carries `systems`, `stories`, `developing`, `quiet`, and `pipeline` (events, parse failures, lag, database size), and the hourly job carries the last daily run's stories forward. Golden snapshots for the replay, the digest, and the weekly report. Tested on the real staging history: 89 findings became 16 stories in about 100 lines. Not done (later): the HTML and PNG renderings (Task 12 renders), AI prose (Task 24), the display-timezone setting.

## Task 11: Routing, alert lifecycle, and feedback
- [x] Routing (rule, then kind, then default; urgent bypasses quiet hours), dedupe, re-notify interval, escalation, resolved message, daily budget, and feedback from `homelab/brief/feedback/<finding_id>`.
- Accept: tests prove an unchanged finding pushes once per interval, a worse one escalates, a cleared one resolves, the budget holds, and an ack stops repeats.
- Status: done. `lifecycle.py` is the pure engine (`step`, no clock, no database, shared by the worker and `brief replay`), `alerts.py` the persistence, sending, and feedback around it, migration `0005_alerts.sql` (alert columns and a `push` log), `routing.py` from Task 9, MQTT `homelab/brief/alert` (not retained), `homelab/brief/headline` (retained), and a subscription to `homelab/brief/feedback/#`. The worker runs it hourly (absence, and whether the pipeline is healthy) and daily (everything, plus the headline), and applies Home Assistant button presses every cycle; a failed run raises a `pipeline_error` alert. `brief alerts`, `brief ack ID`, and `brief snooze ID --hours N` are the command-line equivalents. Replay now runs the same engine in memory, so "would push" shows pushes, repeats, escalations, resolved messages, what quiet hours held, and what the budget folded. 36 lifecycle tests (one or more for each acceptance criterion and the edges around it) plus database, worker, replay, CLI, and a real-broker check of the feedback path. On the real staging history, 558 findings over 8 days become one push and its resolution.

## Task 12: Rendering and brief writer
- Status: done. `render.py` (Jinja2 `brief.html`, `board.html` for tablets with a meta refresh and no JavaScript, and a Pillow `status.png`; sizes and refresh in `brief/views.yaml`), served read-only at `/brief.html`, `/board.html`, `/status.png` with `Cache-Control: no-cache`. `ai.py` is the provider layer: OpenAI-compatible Chat Completions plus an optional native Anthropic adapter (stdlib HTTP, no new dependency), chains from `brief/ai.yaml` that end in `template`, a cloud provider gets system-level facts only (no hosts, addresses, titles, or log text) and only for tasks in `allow_cloud_for`, output validation (a host or address the digest did not mention fails strict mode, drops the paragraph in lenient mode), daily call and cloud-token budgets counted from the new `ai_call` table (migration 0006, metadata only), context fitting (lowest-ranked stories dropped first). A failed run is never sent to a model. The prose step runs last in the daily run, so a slow model delays only `brief.html`, never the files, status, or alerts. 38 new tests.
- Not done (later): the weekly summary and explain-finding tasks (same layer, other callers), the AI admin page (Task 15), PDF/EPUB, display timezone, and checking `disable_thinking` against a live Ollama.

## Task 13: Outputs (standard interfaces)
- [x] MQTT: registry-driven HA discovery (`registry.py`), retained states, availability with last-will, and a state sensor per system from `systems.yaml` (an enum sensor: green, yellow, red, unknown). A changed `systems.yaml` announces the new sensor and withdraws the removed one on the next hourly run, no restart. The hourly status now also carries `parse_failed_24h`, `ingest_lag_s` and `db_mb`, so every registry sensor is live.
- [x] Prometheus text `/metrics` (`metrics.py`): the registry gauges, `homelab_brief_system_status{system}` (0 green, 1 yellow, 2 red, unknown left out), `run_ok`, open alerts, and timestamps. A failed run exports `run_ok 0` and no values.
- [x] Versioned read-only JSON API `/api/v1` (`api.py`): `status`, `systems`, `findings`, `metrics`, `alerts`. GET only, no login; there are no write endpoints (writes stay under `/admin`). Not yet: `/map` (the map arrives with Task 19).
- [x] Optional notifications through Apprise (ntfy, Gotify, email, and others), configured in `notify.yaml`.
- Accept: MQTT topics, `/metrics`, and the API all reflect the same run; Apprise is off by default and sends a test message when enabled.
- Status: done. MQTT, `/metrics` and `/api/v1` are built and tested against one status document (a test and `scripts/e2e_mqtt.py` check they agree). Apprise (`notify.py`, pinned 2.0.1): off by default, `notify.yaml` sets routes (`push_now`, `daily_headline`), resolved messages, and whether the story detail is included; URLs come from `APPRISE_URLS`; `brief notify test` sends a test message. Not done (later): the GUI "send test" button (Task 15), `/map` in the API (Task 19).

## Task 14: Scheduler, config reload, self-monitoring
- [x] Hourly, daily, and weekly runs (`schedule.py`, `schedule.yaml`: `daily_run_utc`, `weekly_day`, `weekly_run_utc`; the weekly job writes the report for the last complete week once a week); config reload every cycle with validation and last-good fallback (`configguard.py`: each watched file is validated, a good one is copied to `/state/config-last-good`, a broken one is replaced for every reader at once by its last good copy or the shipped default, logged once, and shown as a `config_error` finding until fixed); self-metrics sampled hourly (`selfmon.py`: container memory working set against its limit, archive size, free space, events in the last hour, per-sender rate in `/metrics`; ingest lag, parse failures and database size already existed) and exposed as registry sensors.- [x] Storage guard: archive size and free space sampled hourly into the `metric` table, days-until-full projected from the archive's growth (needs at least two days of samples), and a `storage_low` finding (push) when free space is under `min_free_pct` or the projection is under `min_days_until_full` (`playbook.yaml`).- Accept: a simulated stall makes sensors expire and the brief say "no data today"; a broken config keeps the pipeline running and reports the error.- Status: done, with 49 new tests including the stall and the broken-config cases. Findings about the product itself belong to a pseudo-system shown as the "Homelab Brief" tile. Not done (later): the display timezone (needs the Task 15 GUI), per-collector self-metrics (arrive with the collectors), `/metrics` per-sender rates are not MQTT sensors (one sensor per sender would flood Home Assistant).

## Task 15: Admin GUI
- [x] Modern, responsive web GUI (server-rendered pages with htmx and Alpine.js, vendored and pinned, no build step), dark and light themes.
- [x] Pages: overview (health, ingest rate per source, last runs, errors), sources (last seen per host, receive rate), network (see below), collectors (enable or disable, test connection, last success), findings and alerts (acknowledge, snooze, ignore this signature), replay and tuning (run a replay and see the findings and pushes it would produce), config editor, systems/devices/topology editors, log search over the archive (host, time range, text), network map, backups and export.
- [x] Network page (`brief/network.yaml`): detect the Docker network mode (own IP on br0, host, default or user-defined bridge), addresses, and listening ports; edit the advertised address and listen ports (restarting only the affected processes, regenerating the TLS certificate when the address changes); warnings for lost sender IPs, host-mode port clashes, and missing host access; a live syslog test; per-source repointing status; copyable Unraid template settings for each mode. The container never changes its own Docker network.
- [x] Config editor: schema validation before save, a diff view, versioned history with one-click rollback, reload on save, and an audit-log entry per change.
- Accept: every config file can be edited, validated, saved, and rolled back from the GUI; an invalid edit cannot be saved; every change appears in the audit log; in each of the three network modes the Network page reports the mode correctly, the syslog test turns green, and a simulated bridge that hides sender IPs raises the warning.
- Milestones: **15a** done (shell, themes, overview, sources, findings and alerts, audit log); **15b** config editor with validation, diff, history and rollback; **15c** network page and syslog test; **15d** replay and tuning, log search (with "ignore this signature"), backups and export, systems form editor. Collectors (Phase 2), AI (Task 24), devices and topology editors, and the network map come with their own tasks.
- 15a status: done. `admin.py` and `admin_data.py` (thin routes over plain data functions), htmx 2.0.11 and Alpine.js 3.17.4 (CSP build) vendored under `static/vendor` with sha256 in `VENDOR.md`, light and dark themes with a menu that collapses on a phone, an overview that refreshes itself every 30 seconds, and acknowledge or snooze that updates its row in place. Checked in a real browser (sign-in, theme toggle persisted across pages, htmx row swap, CSP clean, audit entries). 27 new tests.
- 15b status: done. `configedit.py` (validate, diff, save, history, rollback, reset) and `admin_config.py` (the pages) for the 11 files the config guard validates: `systems`, `ignore`, `priorities`, `playbook`, `schedule`, `registry`, `notify`, `views`, `ai`, `network`, `retention`. An edit is checked with the same validator the pipeline uses (an invalid one cannot be saved), shown as a diff before saving, kept as a version (the last 50 per file), written atomically, audited (`config.save`, `config.rollback`, `config.reset`), and refused if the file changed since you opened it. A file with no shipped default (`systems.yaml`) starts from its example. Checked in a real browser: review, save, invalid edit refused, history, and the reset confirmation. 55 new tests.
- 15c status: done. `netpage.py` (data), `netconfig.py` (the rules for `network.yaml`, now also applied by the config guard, plus a comment-preserving `patch`), `admin_network.py` and templates. The page shows the detected mode and addresses, which ports answer, warnings (host-mode port clashes, sender IPs lost when one Docker gateway address carries 90% or more of the traffic, a mode that disagrees with detection), a note for each mode, a settings form (mode, advertised address, ports, unknown-sender policy) that saves through the config editor's rules and shows "restart needed" for ports and the address, the live syslog test (a `logger` command with a one-time token; the page polls every 3 seconds and turns green when the line shows up in `/state/events`, naming the address it came from), the status of every expected sender in `systems.yaml`, and the copyable Unraid template settings for each mode. Proved with a real syslog line through the real image (Vector received it and the finder found it), and the page was checked in a real browser (it correctly reported host mode there). 56 new tests, including the three modes and the hidden-sender bridge.
- 15d status: done. **Replay and tuning** (`admin_tools.py`: any 31 days, or 14 with hourly absence checks, run with the current config; shows new findings, what would have been pushed, and the runs that had something to report; nothing sent or stored). **Signatures** (the busiest first, search, "Ignore"/"Stop ignoring" that sets the flag the ingest already honours, audited as `signature.ignore`/`signature.unignore`, and a per-signature history CSV). **Log search** (`logsearch.py`: sender, up to 14 days, plain text, 200 lines, bounded by bytes and time, reads `.log` and `.log.gz`, a POST so the text stays out of URLs, strict sender pattern, links out of the archive not followed). **Systems form** (`sysedit.py`, `admin_manage.py`: rows for each system, unknown senders offered as one-click additions, saved through the config editor so it is validated, versioned and audited; unknown keys per system are kept, hand-written comments are not). **Backups and exports** (`backup.py`: a download with the config files, a consistent database snapshot, the miner state and a manifest, no TLS key; restore of the config files only, all checked first, each through the config editor; downloads of daily briefs, weekly reports and history exports by fixed kind and strict name; writes over 5 MB are refused before they are read). 87 new tests; checked in a real browser (systems form, a real backup download and a multipart restore round trip). Not done: restoring the database from the page (a documented manual step, since it needs the worker stopped), and the devices and topology editors (Phase 2).

## Task 16: Command line and SSH access
- [x] A complete `brief` CLI (status, replay, export, weekly, config validate, config rollback, collector test, admin set-password, backup).
- [x] Default access path documented: SSH to the Unraid host, then `docker exec -it homelab-brief brief ...` (or Unraid's container console).
- [x] Optional built-in SSH server, disabled by default: key-only (`authorized_keys` in appdata), no passwords, no root login, a dedicated `brief` user, listening only on the container IP.
- Accept: every admin GUI action has a CLI equivalent; with SSH enabled, password login is refused and key login lands in a shell where `brief` works.
- Status: done. `cli_tools.py` adds `status`, `sources`, `config` (list, show, validate, history, diff, set, rollback, reset), `systems` (list, add, assign, remove), `signatures` and `signature ignore|unignore`, `logs search`, `audit`, `network show|test`, `backup create|restore`, `collector test` (Phase 2 message), and `ssh show` next to the existing commands; each one that changes something uses the same function as the GUI, so the same checks, history, and audit apply (as `cli:<unix user>`), and a test checks every GUI action has a command. `sshcfg.py` plus an s6 `sshd` service: off unless `SSH_ENABLED`, key-only, `AllowUsers brief`, no root, no forwarding, no sftp, validated copy of `authorized_keys` that root owns, host key kept in `/state/ssh`, listens per network mode (host mode needs an advertised address). Proved on the built image with a real `ssh` client: disabled by default (no process), key login runs `brief`, a password login, a root login, and forwarding are refused, a key line with options keeps the server down, and files written by the command are owned by uid 99. The image grew by about 19 MB (`openssh-server`). 56 new tests. Found on the way: `config set` with an empty stdin saved an empty file (an empty file is "valid"), so `configedit.save` now refuses empty text for the GUI too; a login shell lacks the container's `PYTHONPATH` (the wrapper sets it); the `brief` account must not be locked for sshd to accept a key; `/config/brief` was not created for the `brief` user.

## Task 16a: Instance setup and discovery
- [x] `docs/INSTANCE.md` and `instance-template/` describe and seed an instance folder (normally its own private repository): config, notes, guard word lists, samples, deploy values. The container itself reads only `/config` and template variables, never this repository.
- [x] `brief init` and an admin GUI first-run wizard: list the syslog senders seen (flagging unknown ones), group them into systems, name them, and write a validated `systems.yaml`; optional discovery through the Unraid and UniFi APIs for servers, devices, and topology; write `devices.yaml` and `topology.yaml` with the same validation, audit log, and rollback as the config editor.
- [x] Safe by construction: nothing from an instance appears in logs or shareable output unless the user asks; `brief support-bundle` produces a diagnostics file with names, IPs, and MACs redacted by the sanitizer's rules.
- Accept: starting from only the generic repository and an empty `/config`, a new user reaches a configured install (systems named, collectors enabled) with the wizard alone; a support bundle contains no hostnames, IPs, or MACs.
- Status: done except the collector discovery. `setup.py` (senders with their busiest programs and a kind hint, `apply` through the config editor), `brief init` (interactive, or `--assign HOST=NAME[:LABEL] --yes` for scripts), the **Setup wizard** page (`/admin/wizard`; not `/admin/setup`, which is the first-account page) with an overview banner while senders are unmapped, `redact.py` (`scrub` for logs, `Redactor` with stable aliases), `supportbundle.py` with `brief support-bundle [--out] [--map]` and a download on the Backups page (structure and numbers only; a self-check refuses to write a bundle with anything private left; the alias map is written only on request), log hardening (`jsonlog` scrubs text fields; the alert, headline, daily, feedback, MQTT, CSRF, log-search and SSH log calls no longer carry names), and `devices.yaml` and `topology.yaml` in the config editor with validators (`privateconf.py`), so they get validation, history, rollback, and audit. Proved against the built image: a bundle from a seeded install, scanned for every planted name, IP, MAC, and message text, found none; the wizard was checked in a real browser. 84 new tests. Not done (Phase 2): discovery through the Unraid and UniFi APIs and enabling collectors from the wizard.

## Task 17: Packaging, release, and deployment (built 2026-10-08, version 0.0.27; NOT yet proven: the release workflow has not run on GitHub and no clean install from the template has been done; both are the user's steps)
- [x] GitHub Actions builds the image from the private repo and pushes versioned tags to a private registry; never `latest`. Semantic versioning with a `CHANGELOG.md` entry per release.
- [x] Unraid template (XML) and a compose reference supporting all three network modes (own IP on br0 as the default, host, bridge with port mappings), with appdata paths, variables, and the health check.
- [x] Config schema migrations on upgrade; backup and restore of the appdata folder (CLI and GUI).
- [x] `deploy/README.md`: install, set the IP, enable host access to custom networks if the host server must reach the container, repoint devices, verify.
- Accept: a clean install from the template on the host server reaches a healthy state by following only `deploy/README.md`; an upgrade from the previous tag keeps config and history.

# Phase 2: More data, smarter brief

Spec: `docs/guide/06-phase2-device-intelligence.md`, `docs/guide/07-network-map.md`, `docs/guide/09-ai-integration.md`. Task 18 first; the rest in any order. Everything is switched on and tuned in config (`collectors.yaml` and the other `brief/*.yaml` files) and from the admin GUI.

## Task 18: Collector framework, config loaders, playbook engine (built 2026-10-09, version 0.0.28; no real collector yet: Tasks 19 to 23 add them)
- [x] Collector contract, loader from `collectors.yaml`, failure isolation, connection-test hook for the GUI; migrations for `disk`, `smart_reading`, `inventory`, `client`, `client_hourly`, `dns_hourly`, `topo_node`, `topo_edge`, `power_reading`.
- [x] Loaders and validation for all config files; structured playbook conditions (no `eval`); metric drift detector.
- Accept: enabling or disabling a collector adds or removes its metrics with no other code changes; a failing collector does not break the run.

## Task 19: Unraid collector (all three servers, GraphQL API) (built 2026-10-09, version 0.0.29; NOT yet proven against a live server: the queries follow the API's documented shape, run `brief collector test unraid` with a real read-only key first)
- [x] Read-only API key per server. Array and parity state, parity history (duration and speed), disks and temperatures, SMART data as exposed, containers and VMs (for the map), notifications, OS and plugin versions.
- [x] If the API does not expose the SMART attributes needed, fall back to a per-server User Script writing `smartctl --json` files to a share.
- [x] Detectors: `smart_trend`, `smart_failing`, `disk_temp`, `crc_rising`, `parity_slow`, `unraid_warning`, `version_behind`.
- Accept: fixture responses for each server produce findings, metrics, inventory, and topology rows.

## Task 20: UniFi collector (inventory, firmware, topology) (built 2026-10-09, version 0.0.30; NOT yet proven against a live console: run `brief collector test unifi` with a real read-only key first)
- [x] UniFi Network API with an API key (verify what the installed version offers): devices, models, firmware and update availability, uplinks for the map. Firmware is also read from log headers as a fallback.
- [x] Detectors: `firmware_pending`, `new_infrastructure`.
- Accept: fixture responses produce inventory, topology rows, and findings.

## Task 21: Clients, traffic, and DNS activity (built 2026-10-09, version 0.0.31; the UniFi API parts are NOT yet proven against a live console, the log parts were proven through Vector in the real image)
- [x] Client presence and hourly traffic (UniFi API, plus AP join/leave events from logs); per-client DNS activity from the gateway's coredns log lines (an AdGuard Home or Pi-hole adapter only if wanted).
- [x] Detectors: `new_device`, `traffic_spike`, `odd_hours`; all profile-aware via `devices.yaml`.
- Accept: a spike is judged against the device's own baseline; `security_only` suppresses activity detail; `ignore` stores nothing.

## Task 22: SNMP and iDRAC (optional) (built 2026-10-09, version 0.0.32; proven against the real snmp and ipmitool programs and fake devices, NOT against a real switch, UPS, or iDRAC)
- [x] SNMP polling (v3 preferred, v2c allowed on the LAN) for switches, the PDU, and the UPS where useful.
- [x] iDRAC on a Dell server through IPMI or Redfish (whichever its firmware supports): power draw, temperatures, fans, hardware event log.
- Accept: fixture responses produce metrics; both are off by default.

## Task 23: Home Assistant as a data source (power and energy) (built 2026-10-09, version 0.0.33; proven through a real broker with the real ACL, with invented states)
- [x] Subscribe to HA's MQTT statestream for the entities you choose (power, energy, UPS, smart plugs); no HA token in the container.
- [x] Power metrics per device and system, daily and weekly energy, and a `power_drift` finding.
- Accept: published fixture states become metrics and appear in the brief and the weekly report.

## Task 24: AI provider layer (any model, any host) (built 2026-10-09, version 0.0.34; proven against fake OpenAI-compatible servers over real HTTP and in the image, NOT against a real Ollama or hosted model)
- [x] OpenAI-compatible Chat Completions client (any base URL; `/v1/models` listing) and an optional native Anthropic adapter, configured only through `brief/ai.yaml`.
- [x] Per-task fallback chains ending in the template; context fitting to each provider's size; `disable_thinking` support and stripping of any reasoning block before validation; output validation (every host, device, and finding id must exist in the input); daily call and cloud-token budgets; a call log without content.
- [x] Locality enforcement: `cloud` providers never receive device-level detail, raw lines, DNS data, or `devices.yaml` content.
- [x] Admin GUI AI page laid out as in `docs/guide/09-ai-integration.md`: master switch, feature switches, providers (test connection, list models, latency, tool-calling check), guardrails split into locked and adjustable, prompt editor with history and rollback, a side-by-side preview of template versus provider output, and the call log.
- [x] Deployment notes for an Ollama container on the GPU server (NVIDIA runtime, context cap, short keep-alive, one loaded model, firewall rule or key gateway).
- Accept: with `enabled: false` no model is called anywhere; each feature switch works independently; the same task runs against two different OpenAI-compatible servers by changing only `ai.yaml`; with every provider down the template publishes; a fixture with device detail never reaches a cloud provider; output naming an unknown host falls through.

## Task 25: Network map (built 2026-10-09, version 0.0.35; seeded rows and a real browser, NOT live inventory)
- [x] `map.json` from topology, systems, status, and findings; interactive page (vendored Cytoscape.js) with drag, compound server nodes, layers, detail panel, saved and exportable layouts; static SVG; optional draw.io export. Integrated into the admin GUI and also available read-only.
- Accept: the "Done when" list in `docs/guide/07-network-map.md` passes.

## Task 26: MCP server and Home Assistant AI integration (built 2026-10-09, version 0.0.36; proven with TestClient and stdio, NOT with a real MCP client or HA)
- [x] MCP server over Streamable HTTP at `/mcp` (HTTPS) and stdio through `brief mcp`; read-only tools (`get_status`, `list_findings`, `explain_finding`, `get_history`, `list_systems`, `get_map`, `search_logs`) and resources (latest digest, weekly report, systems).
- [x] Per-client bearer tokens with scopes, created and revoked on the AI page, stored hashed; device detail, DNS data, and raw logs only for explicitly scoped tokens; every call in the audit log.
- [x] Documentation for Home Assistant: pointing HA's AI integration at the same model server, exposing this product's sensors to Assist, and connecting HA's MCP client to `/mcp` (verify integration names against the installed HA version).
- Accept: an MCP client with a default-scope token can list findings and history but cannot reach device detail or raw logs; HA, pointed at the same model server, answers a homelab question from the exposed sensors.

# Phase 3: Agents

Spec: `docs/guide/10-agents.md`. Starts after Tasks 19, 20, 24, and 26, because agents use the same tools, provider layer, and switches. Everything stays off until enabled on the AI page.

## Task 27: Tool registry and agent runtime (built 2026-10-09, version 0.0.37; proven against a scripted model, NOT a real tool-calling model)
- [x] One registry of read-only tools shared by the MCP server and the agent loop (status, findings, a finding, history, signature, log search, inventory, Unraid notifications, systems, map, replay).
- [x] A small agent loop over OpenAI-compatible tool calling: per-run limits (steps, tool calls, tokens, time), a background queue, GPU-idle and quiet-hours rules, citation checking that drops uncited statements, and privacy filtering that withholds device-level tools from cloud-only chains.
- [x] Agents defined in `brief/agents.yaml`; master and feature switches from `ai.yaml` respected.
- Accept: with AI disabled nothing runs; a fixture run stays within every limit; uncited claims are removed; a cloud-only agent never receives device-level tools.

## Task 28: Proposals workflow (built 2026-10-09, version 0.0.38)
- [x] Proposal objects (file, diff, reason, evidence) created only by agents or a `propose`-scoped MCP token; shown on the AI page; approve or reject; approval applies through the normal config path (validation, versioning, rollback, audit log).
- Accept: a proposal changes nothing until approved; approval and rejection are both logged; an invalid proposal cannot be approved.

## Task 29: Investigator, triage, tuner, ask the lab (built 2026-10-09, version 0.0.39; scripted model only)
- [x] The four agents and their prompts, with structured outputs; the investigator's note attaches to the finding in the GUI, the brief's detail page, and MCP.
- [x] Locked safety rules: triage never proposes ignoring disk, authentication, or security events; the tuner never proposes changes that would hide an acknowledged or urgent finding, and every tuner proposal carries replay evidence.
- Accept: each agent produces a valid output on its fixtures; the safety rules are enforced in code, not only in prompts.

## Task 30: Agent evaluation (built 2026-10-09, version 0.0.40; scripted model only)
- [x] Agent fixtures from real cases (slow parity check, hot access point, collection outage, a noisy new signature) with expected outcomes; scores per agent shown on the AI page (citation rate, dropped claims, proposal acceptance, steps, latency).
- [x] Changing an agent's model or prompt runs the fixtures first and flags a lower score before the change takes effect.
- Accept: a deliberately worse prompt is flagged; scores are visible on the AI page.

# Phase 4: Admin interface rebuild

Spec: `docs/guide/11-admin-ui.md`. Started 2026-10-10 after the first real setup attempt: the Unraid collector could not be configured from the GUI (the shipped file has a placeholder address and there was no field for it), and the pages are not pleasant or responsive. Everything configurable in the GUI, with the text editors kept as an option.

## Task 31: Design system and application shell
- [ ] Design tokens (colour, type, spacing, radius, elevation, motion) for light, dark, and system themes; contrast checked.
- [ ] Application shell: grouped side menu (drawer on small screens), top bar with instance status, theme switch, and account menu; breadcrumbs and a page header with one primary action.
- [ ] Components: cards, stat tiles, status chips with icons, buttons, form controls with hints and inline errors, switches, tables (sticky header, stacked on phones), tabs, dialogs, toasts, empty states, loading skeletons, a copy button, diff view. Inline SVG icon sprite.
- [ ] htmx patterns: in-place save with toast and Undo, progress indicators, confirm dialogs, optimistic row actions; no inline script, CSP-safe.
- [ ] Both the admin pages and the public pages (status, brief, map) use the shell and tokens.
- Accept: a style guide page (admin only) shows every component in both themes; checked in a real browser at 360, 768, and 1280 px.

## Task 32: Overview
- [ ] The Overview described in the guide: headline strip, health cards, system tiles, top findings with inline ack and snooze, what changed, setup checklist, live refresh, empty states.
- Accept: a new install shows a useful checklist; an install with a failed run says why and shows the one action that fixes it.

## Task 33: Settings framework and the setup pages
- [ ] Field specs, scalar and row-list writers, save path with digest check, validation, history, audit, toast with Undo, "Edit as text" link on every page, reset-to-default per field.
- [ ] Collectors: a card per collector with its settings, the server list as a row editor (id, address, key variable with set or not-set badge, certificate check), Test connection, Run now, and the switch enabled only when the form is valid.
- [ ] Systems, Network and access, Schedule and retention, Notifications (channels with a test button), converted to the framework.
- Accept: a fresh install configures the Unraid collector end to end with no text editor; the text editor shows the same file afterwards.

## Task 34: Detection and device settings
- [ ] Ignore rules: row editor with a pattern tester against recent lines and the match count.
- [ ] Priorities: kinds table (rank, route, urgent), lifecycle settings, quiet hours with a time zone display, grouping.
- [ ] Playbook thresholds: a control per threshold with units and help; the advice rules viewable and editable as rows.
- [ ] Devices (profile per device, with the privacy explanation), topology and map settings, registry and views.
- Accept: every value in these files has a control; a change shows what it would have done to last week (replay) before it is saved.

## Task 35: Operations pages
- [ ] Findings and alerts, Sources, Signatures, Log search, Replay and tuning, Audit log, Backups and exports rebuilt on the shell: filters, sort, search, pagination, bulk actions where they make sense, saved views.
- Accept: each page is usable at 360 px and by keyboard.

## Task 36: Intelligence pages
- [ ] AI (providers, chains, guardrails, prompts), Agents, Proposals (diff review), Evaluation, MCP clients, and the Network map rebuilt on the shell, every setting a control.
- Accept: the AI page no longer needs the text editor for anything.

## Task 37: Polish and verification
- [ ] A responsive, accessibility, and contrast audit of every page in a real browser at three widths in both themes; screenshots kept; fixes applied.
- [ ] Error and empty states reviewed; keyboard shortcuts and a command palette for jumping between pages; documentation and the deploy guide updated.
- Accept: the "Done when" list in the guide passes.

## Later (not in the first build)
- PDF/EPUB e-reader edition (Pandoc) once the e-reader model is known.
- Network novelty extractors: DHCP leases, MACs, firewall destinations.
- Embeddings and semantic search over findings and history (OpenAI-compatible `/v1/embeddings`), if keyword search proves too limited.
- HA dashboards, the retro theme, and actionable-notification automations (separate guide).
- Single sign-on for the admin GUI (OIDC) and optional TOTP second factor.
- Optional tier 2 or 3 backends fed from the same normalized stream.
