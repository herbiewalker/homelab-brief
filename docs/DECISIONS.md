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
| Network settings | Docker sets the mode in the template; the GUI's Network page (`brief/network.yaml`) detects it, sets the advertised address and listen ports, warns about lost sender IPs, port clashes, and missing host access, and shows the template settings per mode. The container never changes its own network | A wrong setting can't lock you out of the GUI, and every mode stays supported |
| Broker | Outside the container: temporary Mosquitto now, HA's Mosquitto add-on later | Standard home for the broker |
| Outputs | MQTT with HA discovery, Prometheus `/metrics`, versioned REST `/api/v1`, static views, optional Apprise | Industry-standard interfaces for HA and any other dashboard |
| Admin GUI | FastAPI + Jinja2 + htmx + Alpine.js, vendored, no build step; HTTPS with login, CSRF, audit log, versioned config with rollback | Modern GUI with low overhead and strong safety for constant tweaking |
| Command line | `brief` CLI for every GUI action; default access via Unraid SSH plus `docker exec`; optional in-container SSH, key-only, off by default | Command line when needed without widening the attack surface by default |
| Power | Home Assistant collects power and energy; the container reads chosen entities via MQTT statestream; iDRAC optional | HA's Energy dashboard, Zigbee, and NUT; no HA token in the container |
| Releases | Versioned images built by GitHub Actions to a private registry; never `latest`; excluded from CA auto-update | Repeatable, deliberate upgrades |
| Tier | Tier 1 first (Vector + Python + SQLite + Mosquitto) | Low power and resources; heavier tiers fed later from the same stream |
| Database | SQLite, WAL, on the pool path (not `/mnt/user`) | No server, tiny footprint, avoids FUSE locking problems |
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
| Cache pool name | Per install | Replaces `<pool>` in paths |
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
| argon2-cffi | MIT | |
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
| httpx (dev, for FastAPI's TestClient) | 0.28.1 | 2026-10-03 |
| argon2-cffi | 25.1.0 (not yet in requirements; added in Task 6) | 2026-10-03 |
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
