# Decisions

Update this file as decisions are made. Do not decide an open item silently; ask.

## Settled

| Decision | Choice | Why |
| --- | --- | --- |
| Host | A dedicated, always-on Unraid server (the host server) | Stable, can hold the syslog archive, and keeps monitoring off a server doing heavy GPU work |
| Split | The container does all analysis; Home Assistant displays and owns power/energy data | Keeps HA light and rebuildable; HA is better at energy |
| Packaging | One image and one container (`homelab-brief`) with Vector, worker, and web server under a process supervisor; one appdata folder; Unraid template | Single repeatable install that is easy to administer, back up, and upgrade |
| Repo versus instance | This repository is the generic product: no hostnames, IPs, share paths, people, or hardware from any one household. Everything about one install lives in a separate instance folder (`docs/INSTANCE.md`, `instance-template/`), normally its own private repository | The repo can go public or be handed to anyone; a second household starts from the template |
| Configuration source | The container reads its configuration from `/config` and template variables only. Real `systems.yaml`, `devices.yaml`, and `topology.yaml` are created in the instance (by hand or by the setup wizard), never shipped in the image or the repo | Nothing personal is baked into a build |
| First-run setup | A setup wizard and `brief init` discover senders, servers, and gear and write the config files (Task 16a) | A new user never hand-edits YAML to get a working install |
| Test samples | A small trimmed set of sanitized lines (`samples/sanitized/`) is public; the full sanitized set and golden days stay in the instance | Enough for parser tests and CI without publishing a household's device mix |
| Supervisor | s6-overlay v3 | Tiny (no Python process), proper PID 1 with clean shutdown, ISC license, per-service restart and ordering; supervisord would add a Python process and memory |
| Base image and Vector | python:3.13.16-slim-bookworm; Vector as the static musl release binary, checksum-verified | Static binary avoids libc and OpenSSL mismatches and keeps the image small; services run as uid 99 / gid 100 (Unraid nobody:users), binding low ports through Docker's default unprivileged-port sysctl |
| Syslog | Devices send directly to the container (own IP on br0 recommended; host and bridge supported), UDP/TCP 514 or the configured port; the container owns the raw archive | Removes the dependency on a separate rsyslog server that can fail silently; keeps the priority field |
| Vector config | The Vector config, parser (`vector/parse.vrl`), and tests ship in the image at `/app/vector`; `/config` holds only the user's `brief/*.yaml`. At start `python -m brief.vectorcfg` renders the listen port (`SYSLOG_PORT`, else `ports.syslog`) and the sender allowlist into `/run/vector/vector.yaml` and refuses invalid values | The parser is product code that must match the image version; a stale copy in appdata would outlive upgrades. Vector 0.58 does not interpolate variables, and an allowlist is a list |
| Sender gate | Senders outside `allowed_senders` are flagged (`unknown_sender: true`, kept) or dropped before the archive and parser, per `unknown_senders` | Syslog over UDP can be spoofed by anything on the LAN. The `unknown_sender` finding itself comes with the detectors |
| Event time | `ts` keeps the header text; `epoch_ms` carries precise UTC when CEF, coredns JSON, or a UniFi OS ISO prefix provides it; Vector's own `timestamp` is receive time and is never used for bucketing | Headers have no year or timezone; Python normalizes them per system |
| Severity | The priority field when present; otherwise keywords, or the CEF severity (9+ err, 7-8 warning, 4-6 notice, else info), always with `severity_inferred: true` | The wire value is the truth; the fallback is clearly marked |
| Archive compression | After each UTC day, `python -m brief.archive compress` gzips files beside the originals, verifies by reading them back, then removes the original; dry run unless `--apply` | Vector's own gzip would leave unreadable files after a crash, and destructive scripts default to a dry run |
| Network settings | Docker sets the mode in the template; the GUI's Network page (`brief/network.yaml`) detects it, sets the advertised address and listen ports, warns about lost sender IPs, port clashes, and missing host access, and shows the template settings per mode. The container never changes its own network | A wrong setting can't lock you out of the GUI, and every mode stays supported |
| Broker | Outside the container: temporary Mosquitto now, HA's Mosquitto add-on later | Standard home for the broker |
| Outputs | MQTT with HA discovery, Prometheus `/metrics`, versioned REST `/api/v1`, static views, optional Apprise | Industry-standard interfaces for HA and any other dashboard |
| Admin GUI | FastAPI + Jinja2 + htmx + Alpine.js, vendored, no build step; HTTPS with login, CSRF, audit log, versioned config with rollback | Modern GUI with low overhead and strong safety for constant tweaking |
| Web apps | Two apps from one codebase: HTTP holds only the read-only routes (`/admin` is redirected, never served), HTTPS adds `/admin`; ports from `network.yaml` with environment overrides | The admin area cannot be reached or tricked into answering over plain HTTP, because it is not there |
| Admin access control | One router-level guard gives default-deny: a session for every `/admin` route except sign-in and setup, and a CSRF token (plus an Origin check on scheme and host) for every write | A new admin route is safe without anyone remembering to opt in |
| Accounts and sessions | One admin account (the table allows more later); Argon2id, 12 characters minimum; server-side sessions with only a token hash stored, `__Host-` Secure HttpOnly SameSite=Strict cookies, 30 minute idle and 12 hour absolute limits, ended by sign-out and by any password change | A copy of the database cannot be used to sign in, and a stolen cookie has a short life |
| Login throttling | Per address and per username, 5 free failures in 15 minutes then a lock of 30 seconds doubling to 15 minutes, stored in the database; unknown users answer like wrong passwords | Slows guessing without a permanent lockout and survives restarts |
| First-run setup | `brief admin set-password` through docker exec, or a one-time token in the container log (hash stored, 24 hour expiry, replaced each start while no admin exists, works once) | No default credentials, and either path works from the Unraid console |
| TLS | A self-signed certificate (EC P-256, 2 years) generated on first start, regenerated when its names change or it nears expiry; `cert.pem` and `key.pem` in `/config/tls` are used instead and never overwritten; no HSTS | Works out of the box, and a changing self-signed certificate must not lock people out |
| Health | `/healthz` covers the database, the worker (a recent `ingest.cycle_seconds` metric), and the receiver, and Docker's health check uses it | A crashed worker or Vector no longer leaves the container looking healthy |
| Command line | `brief` CLI for every GUI action; default access via Unraid SSH plus `docker exec`; optional in-container SSH, key-only, off by default | Command line when needed without widening the attack surface by default |
| Power | Home Assistant collects power and energy; the container reads chosen entities via MQTT statestream; iDRAC optional | HA's Energy dashboard, Zigbee, and NUT; no HA token in the container |
| Releases | Versioned images built by GitHub Actions to a private registry; never `latest`; excluded from CA auto-update | Repeatable, deliberate upgrades |
| Tier | Tier 1 first (Vector + Python + SQLite + Mosquitto) | Low power and resources; heavier tiers fed later from the same stream |
| Database | SQLite, WAL, on the pool path (not `/mnt/user`) | No server, tiny footprint, avoids FUSE locking problems |
| Schema and migrations | Numbered SQL files in `brief/src/brief/migrations/` are the schema; each runs in one transaction and `PRAGMA user_version` records the level; a copy `baseline.db.pre-v<N>` is made before upgrading an existing database; a database from a newer program is refused | Upgrades are repeatable and reversible, and a downgrade cannot corrupt data |
| Writes | One writer at a time per process (`Store.write()`, one transaction per cycle); the worker and web server each open their own store and SQLite arbitrates; readers use read-only connections; times are UTC text | A whole cycle commits or none of it does, and reads never block on the writer |
| `prev_last_seen` | The `last_seen` from before the most recent quiet gap (24 hours by default), unchanged while activity continues | Gives "returned after N quiet days" without a separate table; finer detail comes from `hourly` |
| Ingest offsets | Stored in the database (`ingest_offset`), committed in the same transaction as the counts, not in `offsets.json` | A crash can neither count a line twice nor lose one; replay safety needs no extra mechanism |
| Default config in the image | Default `brief/*.yaml` and `drain3.ini` ship in the image at `/app/defaults/brief`; a file in `/config/brief` overrides its default; each cycle reloads and validates, keeping the last good version on error | A new install runs with no files to write, and a bad edit cannot stop ingest |
| Ignore rules | A rule has a name, an optional host (IP, header hostname, or system id), a regex over `<app>: <message>`, and a note; first match wins; ignored events are counted per rule in `suppressed`, not mined or counted in `hourly`, but still refresh the host's last-seen time | Known noise is filtered before detection, a change in its volume stays visible, and a quiet source is not mistaken for a silent one |
| Event time | Precise `epoch_ms`, else the header time in the host's `source_tz` with the year inferred (more than a day ahead of receive time means last year), else receive time, flagged as a fallback | Counts are bucketed by when an event happened, so backfills and late lines land in the right hour |
| Unknown hosts | Hosts not in `systems.yaml` roll up to `default_system` with `host.known = 0` | The admin GUI can show them for naming |
| Learning period | Counts from the first live event (`meta.live_since`), not a backfill; length is `thresholds.learning.days` in `playbook.yaml` (7) | Baselines need real live days; a backfill shortens the wait but does not start the clock |
| Lost miner state | If `drain3.bin` is missing or unreadable while signatures exist, start a new `miner_generation` and write an audit-log entry | Drain3 would otherwise reuse cluster ids that already map to other templates |
| Signatures | Drain3 with masking for IPs, MACs, numbers | Turns near-duplicate lines into stable signatures |
| Brief writer | Template first; AI rewrite optional through `ai.yaml` chains that end in the template | Pipeline must never depend on AI |
| AI providers | OpenAI-compatible Chat Completions for any model (local or hosted); optional native Anthropic adapter; nothing vendor-specific outside the provider layer | Works with Ollama, llama.cpp, vLLM, LiteLLM, and hosted services; no lock-in |
| Local AI | Ollama container on the GPU server (NVIDIA runtime) serving both this product and Home Assistant; LAN-only via firewall rule or key gateway; short keep-alive | One GPU for everything; power-aware |
| AI clients | Read-only MCP server (Streamable HTTP and stdio), per-client scoped tokens, audited | One standard for Home Assistant, Claude, and other assistants |
| AI privacy | Providers marked `cloud` never receive device-level detail, raw logs, DNS data, or `devices.yaml` content | Household privacy |
| Agent instructions | `AGENTS.md` is the source of truth; `CLAUDE.md` only points to it; prompts are plain Markdown | Tool-neutral context for any coding agent or model |
| AI switches | AI off by default; master switch plus per-feature and per-agent switches; locked guardrails enforced in code and shown separately from adjustable ones on the AI page | Full control without being able to disable the safety rules by accident |
| Product agents (Phase 3) | Investigator, triage, tuner, ask the lab; read-only shared tool registry with MCP; small in-house loop over OpenAI-compatible tool calling; proposals need approval; citations required; background only | Real investigative value without autonomy risk; fits an 8 GB local model |
| Coding workflow | One task at a time, tests, a review pass against `AGENTS.md` before each merge, pre-commit lint, secrets scan, and a private-name guard; the same checks in CI | Keeps privacy and security rules enforced as the code grows |
| Extra detectors | `clock_skew`, `unknown_sender` (sender allowlist), `storage_low` | Bad clocks, spoofed senders, and full disks silently break everything else |
| HA link | MQTT discovery and feedback topics; HA statestream for data coming back; no HA token in the container | Standard, token-free integration |
| Dashboards | Flat, retro, cheap to render for old tablets | Efficiency requirement |
| Phase 2 design | Pluggable collectors, all behavior in config files, order of pieces flexible | Priorities will change as notifications and dashboards come online |
| Who decides | Detectors and the playbook decide what is a finding; a model, if enabled, only rewrites | Repeatable, testable, cheap; model errors cannot create or hide alerts |
| Actions | Backend suggests, Home Assistant taps trigger; backend never executes | Safety |
| Privacy | Per-device data stays local; `devices.yaml` and activity data never in git or external calls | Household privacy |
| Tool choice | Prefer open source unless it adds a heavy service or much more build time | Owner preference with an efficiency guardrail |
| Disk health | Unraid API per server first; per-server `smartctl --json` files as a fallback; Scrutiny optional | Unprivileged container, covers all three servers, lightest footprint |
| Time handling | Bucket by event time in UTC; rolling analysis windows | Backfills land correctly; no day-boundary or DST bugs |
| Signature ids | Our own `sig_id` mapped from `(miner_generation, cluster_id)` | Losing `drain3.bin` cannot corrupt history |
| Alerts | Lifecycle (dedupe, re-notify, escalate, resolved, budget) plus HA feedback topic | Useful notifications instead of nagging |
| Systems | `brief/systems.yaml` maps hosts to systems (tiles, sensors, map grouping) | One definition shared by dashboards, detectors, and the map |
| Config safety | Structured playbook conditions, no `eval`; reload every run with last-good fallback | Safe to tweak constantly |
| Build order | Thin slice and early deploy of ingestion (Task 7); replay mode (Task 9) for tuning | Baselines need real days; tuning should take minutes |
| Docs | Values live only in config files; docs link to them | Prevents drift |
| Network map | Cytoscape.js interactive page (also in the admin GUI), Python-drawn static SVG, optional draw.io export; layout saved in the browser, exportable, and "Save as default" from the admin GUI | Draggable holistic view; only logged-in admins write; works on old tablets via SVG |
| Host identity | The sender's IP (live) or the file name's IP (backfill) is the host; header hostnames are aliases | Headers are inconsistent (duplicated on gateways, spaces and pipes on the NVR) |
| Severity | Priority field from the wire for live lines; keyword fallback for the backfill and lines without it; CEF severity for Protect | The old file archive dropped the priority field; direct receive keeps it |
| Timestamps | Infer the year; `source_tz` UTC; prefer CEF epoch-ms and JSON timestamps when present | Audit: headers have no year or timezone; gateway JSON shows -04:00 |
| Samples | `scripts/pull-samples.ps1` copies the last N lines per file, read-only | Claude Code on the Windows machine can read the share directly |

## Open

| Item | Options | Notes |
| --- | --- | --- |
| Raw log retention | 30, 60, or 90 days | Plan assumes 60 to start |
| Cache pool name | Default `cache`, changeable in the template like any Unraid container | Paths use /mnt/cache; settled for the first deploy |
| Where live logs are written | Array share vs pool-only live share | Decide after the Step 1 audit |
| Mosquitto long-term home | Unraid, or HA's broker once Home Assistant is set up | Switch is a `MQTT_HOST` change |
| E-reader model | Kindle, Kobo, or e-paper display | Decides the e-reader output route |
| Dashboard retro style | Green or amber phosphor terminal, 80s arcade, airport split-flap | Picked when dashboard work starts |
| Brief prose layer | Which providers and chain per task in `ai.yaml` | Start template-only; add the GPU server first |
| Local model server and first model | Ollama on an 8 GB GPU (RTX 3060 Ti class): Qwen3.5 9B at Q4 (Apache-2.0) primary, Phi-4-mini (MIT) lighter fallback; context 8192 with flash attention and q8_0 context cache. Verified 2026-10-03: both load 100% on GPU (5.5 GB and 3.2 GB); thinking off works | Final pick after the AI page's preview with real digests (Task 24) |
| Model server access control | UniFi firewall rule (allow only this container and HA) or a key gateway (LiteLLM or reverse proxy) | Ollama has no built-in authentication |
| Tier 2 or 3 | Loki/Grafana, Wazuh/Graylog | Revisit after about a month of baselines |
| Daily run time | For example 05:45 local | Needs the display timezone |
| UniFi data access | API key, local account, or an existing library | Check what the installed UniFi Network version exposes (Task 20) |
| DNS activity source | UDM coredns log lines, AdGuard Home, Pi-hole, or none | Audit: the gateway's coredns JSON lines already carry client IP, MAC, and domain, which may make a separate DNS filter unnecessary (Task 21) |
| SMART thresholds and buckets | Starting values in `playbook.yaml` | Tune per drive model after real data |
| Device profiles | Which profile each person's devices get | Agree with each household member |
| Home Assistant AI | Which HA AI integration and agent to use, which sensors to expose to Assist, whether to connect HA's MCP client | Verify integration names in the installed HA version (Task 26) |
| Container IP | Network mode (own IP recommended, host, or bridge) and, for own IP, a free fixed LAN IP | Needed before Task 7 |
| Host access to custom networks | Enable on the host server (own-IP mode only) | Required for the host server to reach its own container in own-IP mode |
| TLS certificate | Self-signed (default), your own, or a reverse proxy in front | Decide at deploy |
| Image registry | GitHub Container Registry (private) or build on the host server | Decide in Task 17 |
| Transitive Python pins | `brief/requirements.txt` pins direct dependencies only | Generate a full lock with `pip freeze` in the built image once Docker is available |
| Local tooling | Docker and Python are not installed on the dev machine | Task 0 build, healthcheck, and pytest acceptance cannot run until Docker Desktop (or another engine) is installed |
| Log storm handling | Archive every line and rely on compression, or throttle repeated identical lines per host and signature in Vector before the archive (keep the first N per minute, count the rest as suppressed) | A single server can produce millions of near-identical lines in a day (see "What real logs look like"); decide in Task 3 with the sanitized samples, and add a `log_storm` finding either way |
| SMART data source | Unraid API (preferred) or per-server `smartctl --json` files | Decide in Task 19 |

## Open-source picks and licenses

Expected licenses come from general knowledge. Verify each at pin time and fill in the date.

| Component | Expected license | Verified on |
| --- | --- | --- |
| Vector | MPL-2.0 | 2026-10-03 (GitHub) |
| Drain3 | MIT | 2026-10-03 (PyPI) |
| SQLite | Public domain | |
| Mosquitto | EPL-2.0 / EDL-1.0 | |
| nginx | BSD-2-Clause | |
| paho-mqtt | EPL-2.0 / EDL-1.0 | 2026-10-03 (PyPI: EPL-2.0 OR BSD-3-Clause) |
| PyYAML | MIT | 2026-10-03 (PyPI) |
| Jinja2 | BSD-3-Clause | |
| Pillow | MIT-CMU (HPND) | 2026-10-03 (PyPI) |
| Home Assistant | Apache-2.0 | |
| smartmontools | GPL-2.0-or-later | |
| Scrutiny (optional) | MIT | |
| UniFi client library (for example aiounifi) | MIT | |
| AdGuard Home / Pi-hole (optional) | GPL-3.0 / EUPL-1.2 | |
| Ollama / llama.cpp (optional) | MIT | |
| Pandoc (optional) | GPL-2.0-or-later | |
| Cytoscape.js (network map) | MIT | 2026-10-03 (npm) |
| draw.io / diagrams.net file format (optional export) | Apache-2.0 | |
| FastAPI | MIT | 2026-10-03 (PyPI) |
| Uvicorn | BSD-3-Clause | 2026-10-03 (PyPI) |
| htmx | BSD (verify which variant for the pinned version) | 2026-10-03 (npm reports 0BSD for 2.0.11) |
| Alpine.js | MIT | 2026-10-03 (npm) |
| argon2-cffi | MIT | 2026-10-04 (PyPI) |
| python-multipart | Apache-2.0 | 2026-10-04 (PyPI) |
| cryptography | Apache-2.0 OR BSD-3-Clause | 2026-10-04 (PyPI) |
| httpx2 (dev) | BSD-3-Clause | 2026-10-04 (PyPI) |
| s6-overlay (chosen over supervisord) | ISC | 2026-10-03 (COPYING in v3.2.3.2) |
| Apprise (optional) | BSD-2-Clause | |
| SNMP library, for example pysnmp (optional) | BSD-2-Clause | |
| Redfish client library (optional) | BSD-3-Clause | |
| ruff | MIT | 2026-10-03 (PyPI/GitHub) |
| pre-commit | MIT | 2026-10-03 (GitHub) |
| gitleaks | MIT | 2026-10-03 (GitHub) |
| OpenSSH server (optional) | BSD-style | |
| OpenAI Python SDK or plain httpx (provider client) | Apache-2.0 / BSD-3-Clause | |
| MCP Python SDK | MIT | |
| LiteLLM (optional gateway) | MIT (core; check enterprise parts) | |
| vLLM (optional) | Apache-2.0 | |
| Local model weights | Varies by model; record each | |

Not open source and not replaceable here: Unraid, UniFi hardware and firmware, Fire OS on the tablets. Hosted AI services (Claude or any other) are optional and interchangeable through the provider layer; the local path (Ollama or llama.cpp with an open-weight model) needs none of them.

## Version pins (Task 0, checked 2026-10-03)

| Component | Version | Date checked |
| --- | --- | --- |
| Vector | 0.58.0 (static musl x86_64 release binary, sha256-checked in the Dockerfile) | 2026-10-03 |
| Mosquitto | eclipse-mosquitto:2.1.2-alpine (dev broker only) | 2026-10-03 |
| nginx | not used (dropped; the web server is Uvicorn) | 2026-10-03 |
| Python base image | python:3.13.16-slim-bookworm | 2026-10-03 |
| drain3 | 0.9.11 | 2026-10-03 |
| paho-mqtt | 2.1.0 | 2026-10-03 |
| PyYAML | 6.0.3 | 2026-10-03 |
| Jinja2 | 3.1.6 | 2026-10-03 |
| Pillow | 12.3.0 | 2026-10-03 |
| FastAPI | 0.142.2 | 2026-10-03 |
| Uvicorn | 0.54.0 | 2026-10-03 |
| s6-overlay | 3.2.3.2 (x86_64, sha256-checked in the Dockerfile) | 2026-10-03 |
| pytest (dev) | 9.1.1 | 2026-10-03 |
| httpx2 (dev, for FastAPI's TestClient; replaces httpx, which Starlette now deprecates) | 2.13.1 | 2026-10-03 |
| argon2-cffi | 25.1.0 (Task 6) | 2026-10-03 |
| python-multipart | 0.0.32 (Task 6; form parsing; Apache-2.0) | 2026-10-04 |
| cryptography | 50.0.2 (Task 6; generates the self-signed certificate; Apache-2.0 OR BSD-3-Clause) | 2026-10-04 |
| htmx | 2.0.11 (to vendor in Task 15) | 2026-10-03 |
| Alpine.js | 3.17.4 (to vendor in Task 15) | 2026-10-03 |
| Cytoscape.js | 3.34.3 (to vendor in Task 25) | 2026-10-03 |
| ruff / ruff-pre-commit | 0.16.10 | 2026-10-03 |
| pre-commit | 4.6.2 | 2026-10-03 |
| gitleaks | v8.30.1 (pre-commit hook and pinned CI image) | 2026-10-03 |
| GitHub Actions | actions/checkout v7.0.1, actions/setup-python v7.0.0 | 2026-10-03 |

## What real logs look like (from a representative audit)

Details of any one install belong in its instance folder, not here.

- **Receiver and layout:** rsyslog (Unraid's built-in syslog server) writes one file per source IP, `syslog-<ip>.log`, appended forever with no rotation.
- **Typical sources:** UniFi gateway, Protect NVR, access points, switches, a PDU, and Unraid servers.
- **Formats:** classic BSD syslog with no priority field and no year or timezone in the header. Gateways repeat the hostname; switches, APs, and the PDU add `<mac>,<model>-<firmware>`; the NVR sends CEF with epoch-millisecond timestamps; gateway coredns lines are JSON with full timestamps.
- **Bonus data already in logs:** per-client DNS (gateway coredns), client presence (AP join/leave), gateway memory (earlyoom), device temperatures (fanctrl), firmware versions (switch, AP, PDU headers), inbound firewall hits.
- **Volume is bursty, not steady:** in one audit a single day was 99% of the largest source's lines (one repeated nginx error, then rsyslogd failing to write when Unraid's small in-memory log filesystem filled). Design consequences: signatures collapse such a storm to a handful, the pipeline must survive hundreds of lines per second for hours, and deviation, `storage_low`, and a possible `log_storm` finding matter more than the raw rate.

## Session log

- Scaffold created in chat; review changes applied (time handling, signature ids, alert lifecycle and feedback, systems.yaml, replay mode, early deploy, structured playbook conditions, config reload, per-server SMART scripts, network map, task renumbering).
- Re-architected as one container with its own IP: direct syslog receive and archive, standard outputs (MQTT, `/metrics`, `/api/v1`, Apprise), admin GUI with HTTPS login and audit log, CLI plus optional key-only SSH, HA owns power (statestream back). Tasks renumbered: Phase 1 is 0-17, Phase 2 is 18-25.
- Made AI vendor-neutral: `ai.yaml` providers over the OpenAI-compatible API (optional Anthropic adapter), per-task chains ending in the template, plain-Markdown prompts in `brief/prompts/`, a local Ollama server for both this product and HA, a read-only MCP server with scoped tokens (Task 26), and `AGENTS.md` as the instruction file with `CLAUDE.md` pointing to it.
- An 8 GB GPU sets the local model size: Qwen3.5 9B at Q4 suggested, context capped at 8192. Added Phase 3 agents (Tasks 27-30), AI master and feature switches with locked and adjustable guardrails, a review pass and leak guards for coding sessions, and the `clock_skew`, `unknown_sender`, and `storage_low` detectors.
- Task 0 verified in Docker Engine on WSL2, driven from Windows with `wsl`; `scripts/dev-init.ps1` falls back to WSL when there is no Windows `docker`. Vector 0.58 turns off `${VAR}` interpolation by default and enabling it needs a flag named "dangerously", so the `init-dirs` step renders the syslog port into `/run/vector/vector.yaml` (validated as a number) and Vector runs with an empty environment. The container showed healthy while Vector was crash-looping on its config, so `/healthz` needs to cover the ingest path (Tasks 6 and 14).
- Leak guards added: ruff, gitleaks, and a stdlib private-name guard run through pre-commit in WSL and in CI; `.githooks/pre-commit` is copied to `.git/hooks` by `scripts/setup-dev-tools.sh` rather than set through git config. The guard treats only RFC 1918 and link-local as private and exempts the documentation ranges.
- Sample sanitizer built (`scripts/sanitize_samples.py`): stable placeholders in the documentation ranges (RFC 5737, 3849, 7042), structure kept, secrets redacted and listed for review, and a self-check that writes nothing if a replaced value survives. Run against real samples it found gaps that invented fixtures missed (colon-terminated MACs, iptables `MAC=` fields, an adoption `authkey`, a random SSH username, pipe-bearing device names, port-forward rule names).
- Repo and instance separated: all personal data moved to an instance folder (its own private repository for the maintainer); the guard, sanitizer, trimmer, and pull script read it from `BRIEF_INSTANCE_DIR`; the repo history was rewritten to remove earlier mentions; samples split into a small public set and a full private set; Task 16a (setup wizard and discovery) added.
- Task 3 built and verified end to end: parser with 26 Vector unit tests, sender gate, archive, backfill, port and allowlist rendering, archive compression, replay and report scripts, and a five-scenario end-to-end check that CI runs. Gotchas worth keeping: in VRL `to_int(null)` is 0 rather than an error (it silently overwrote a good time until a test caught it); VRL's type inference rejects `to_string(...) ?? x` in some places and requires it in others, so keep the message in a local string variable; Vector will not create its `data_dir`; and Docker's port proxy accepts connections before the container is listening, so wait on Vector's own log lines. The Vector config moved into the image (it used to be expected under `/config/vector`).
- Task 4 built: the SQLite store with migrations, a single-writer helper, signature id mapping with miner generations, and the baseline, alert, feedback, and audit tables, with 31 tests. Decided within the task: `prev_last_seen` semantics (quiet gap, above), CHECK constraints on `hourly` (hour 0-23, counts not negative), and a `meta` table for the miner generation. A batch that spans long gaps (a backfill) should call `touch_signature` once per hour bucket, because the gap is judged from the first timestamp given.
- Task 5 built and verified end to end: event-time parsing, systems and ignore loaders, the Drain3 wrapper, the offset-tracked ingest cycle with one transaction per cycle, the worker loop with last-good config reload, and an `ingest` scenario in the end-to-end check that runs the whole path (samples, Vector, event files, worker, database). On the full private sample set (2,800 lines) Drain3 produced 278 signatures, and the most numerous templates are the expected telemetry and DHCP lines. Also changed: the quiet gap for `prev_last_seen` is 24 hours (one hour let a later short gap overwrite a "returned after N days" signal), and household-specific wording was removed from `ignore.yaml`, the brief guide, and the project plan.
- Task 6 built: the web apps, the guarded admin area with login, logout, setup, and change-password, throttling, TLS, the CLI command, and the health model, with an end-to-end check over real TLS. Fixed a latent Task 4 bug it exposed (two processes starting on a fresh database raced on the migration; each migration is now re-checked under the write lock and statements run inside our own transaction). FastAPI keeps an included router lazy, so tests walk the router object (`app.state.admin_router`) instead of `app.routes`. The Origin check must compare the scheme as well as the host; a test caught that it did not. Not done: the admin GUI pages (Task 15), TOTP and single sign-on (later). The HTTPS port a browser should use when the template maps a different one is `HTTPS_PUBLIC_PORT`.
- Task 7 built (the deploy itself is the user's step): `status.py` (status.json schema 1 from the database), `publish.py` (MQTT discovery, retained states, availability with a last will, driven by `registry.yaml` and limited to the metrics `status.py` can compute), an hourly worker job that writes `/out/latest/status.json` and publishes, `GET /status.json` on both web apps, `brief import` (runs Vector with `backfill.yaml`, stops it when the output stops growing, refuses a second run), and a rewritten Pass 1 in `deploy/README.md` with `scripts/e2e_mqtt.py` (real broker, ACL, clean stop and crash). Decided within the task: (1) states are published every hour, not only on change, because `expire_after` is about twice the interval and an unchanged value would otherwise expire a healthy sensor (the guide's "only when it changes" conflicts with its own `expire_after`; the guide should say "every run"); (2) a failed run publishes no values, so sensors expire instead of reporting calm; (3) `hosts_silent` is a thin absence check (3 x learned gap, clamped by `thresholds.absence` in `playbook.yaml`; 24 hours until a gap is learned; only hosts of `expected: true` systems), and Task 8 replaces it with the real detector and learns the gaps; (4) three sensors, not two, because `new_signatures_24h` came free from the same query; (5) deploy order is repoint sources first, then import, so the old files are final and nothing falls in a gap (event-time bucketing makes the order safe either way); (6) the deploy README no longer tells people to copy the whole `brief/` folder into `/config`, since a copy shadows the image defaults and goes stale on upgrade. Gotchas: the Mosquitto password file must be owned by the `mosquitto` user or the broker exits ("Unable to open pwfile"); Vector's file source never exits at end of file, so the import detects completion by output size going quiet.
- Task 7 follow-up (network): `netinfo.py` detects the mode (own_ip, host, bridge) and LAN address with no packets sent, logs a `network` line at start, adds a `network` block to status.json, and in own-IP mode fills the certificate's address from the detected one when `advertised_address` is empty. `network.yaml` `mode` overrides detection; a mismatch or a bridge/host-mode trap is a warning, not an error. Default stays zero-config (DHCP-style address from the Docker network). Multi-network containers (admin on a bridge, syslog on br0) were considered and deferred: Unraid templates allow one network, two networks risk reply-route problems, and it would need an `advertised_address` split (syslog vs web). Single network for the first deploy.
- First-deploy choices (settled with the user): network `br0` by default with "host access to custom networks" on (the old syslog server stays on as the fallback until a week of clean collection, then goes off); a dedicated network such as `syslog-net` is a later option; pool path defaults to `cache`; MQTT stays unconfigured (`MQTT_HOST` empty) until Home Assistant and a broker exist, the connection values are added later; the container address is to be fixed (outside the Docker pool), chosen by the user.
- Import hardened after the first real-data run on the staging server (a 346 MB slice of real history): the backfill Vector was killed by the 768 MB container limit at about 305 MB of output, and the worker had already counted part of the half-written file, with no way to retry. Measured: the backfill Vector holds a flat ~225 MB of anonymous memory (it does not leak; docker stats also shows the page cache of the files it writes, which grows with output). Fix: the import writes `import.ndjson.partial` (the worker ignores it), is verified against the source line count (0.1% tolerance for blank or oversize lines), and only then renamed to `import.ndjson`; a failed run removes its leftovers and can be re-run; a killed Vector gets a memory hint; the sinks use small buffers. Separately noticed: the worker ingests only about 500 events per second (75k events took 42 to 159 s per cycle under memory pressure), so a multi-million-line backlog takes a long time. This feeds the open log-storm decision and probably wants a profile of Drain3 and the flush before the real multi-GB archive is imported.
- Task 8 built (detectors, `brief detect`, gap learning, clock-skew capture, migration 0004); see TASKS for the details. Two things found on the way, both fixed. (1) Ingest speed: a profile of 153k real imported events showed 88% of the time going into Drain3 rewriting its whole state file (a jsonpickle dump of the template tree) on every new cluster or template change, 2,435 times for 1,620 signatures, and the cost grows with the tree. The miner now saves once per cycle, just before the database commit (`Signatures.save()`), which keeps the invariant that matters (the miner on disk is never older than the database; one that lagged would hand a used cluster id to a different template). Measured on the same events: 2,300 to 45,000 events per second. This mostly answers the log-storm concern for volume; the open question is now only how a storm should be summarized in the brief, not whether the pipeline can keep up. (2) A latent startup race: on a brand-new database, several processes opening it at the same instant (the worker and both web servers) could get "database is locked" from the switch to WAL mode, which ignores the busy timeout; it showed up as a flaky test once a fourth migration changed the timing. `_wal()` now retries for a few seconds (40 of 40 runs clean).
- Health fix found on the staging server: a worker working through a large backlog takes minutes per cycle and records its heartbeat metric only when a cycle ends, so the 180 s "stale" limit marked a busy, healthy worker unhealthy (Docker showed `unhealthy` while cycles of 171 to 304 s were running). The allowance now adds twice the last cycle's length (`ingest.cycle_seconds` is measured before its flush). A worker that stops is still caught, just later when the last cycle was long.
- Detector tuning on the real staging history (14 hosts, 6 weeks, evaluated as of the end of the data): the first rules gave 555 findings, nearly all noise; after four changes they give 89, and what is left is worth reading. (1) `returned` needs the signature to have been regular (`novelty.min_prior_days`, 5 days seen before it went quiet) and its host to have kept logging other things meanwhile (`host_active_hours_in_gap`, 3), because a sparse source's signatures "return" constantly and a collector outage makes every signature return at once; novelty 408 to 33. (2) Deviation baselines count only the 24-hour windows in which the host logged at all, so an outage no longer makes everything "usually zero", and a flag needs at least `min_ratio` (3) times the median as well as median + 4 x MAD (a chatty signature's MAD is wide but 2x swings are routine); 72 to 52. (3) Off-hours only judges signatures with a real pattern: at least `min_history_days` (3) and `min_history_events` (20) of history, concentrated in at most `max_usual_hours` (8) hours of the day; 75 to 4. All thresholds are in `playbook.yaml`. Still open for Task 10: the digest should group findings by host and time (twelve deviations on one gateway are one story, not twelve), and a rule for "a source's whole signature set changed at once" (a firmware update) so it is reported once.
- Task 9 built: `brief replay` and the golden day. Replay forced the detector inputs to be as of any moment: novelty is now computed from the hourly counts only, and absence from per-host hourly activity (`detect.HostActivity`), so the columns that only hold the latest value (`signature.prev_last_seen`, `host.last_seen`) are not read. Building the golden data found a threshold mistake: `off_hours.min_history_events` of 20 dismissed a nightly job (14 events in a 14-day window), so it is 10 now. Replay on the real staging history for six days gives 20 to 60 new deviations a day, almost all UniFi telemetry, which confirms Task 10 must group findings by host. On a database with only imported history the learning period never ends (no live data yet), so replay of it needs `--ignore-learning`.
