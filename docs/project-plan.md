# Homelab Morning Brief: Project Plan

Kept current in the repo. This file explains the why; the technical build steps are in `docs/unraid-backend-guide.md` and `docs/guide/`, and values such as thresholds, routes, and schedules live in the config files under `brief/` (the config file wins if they disagree).

## Goal

One container on Unraid that collects everything about the homelab, remembers what normal looks like, writes a short news-style brief every morning, raises alerts that don't nag, and feeds Home Assistant and any other dashboard through standard interfaces, administered from a modern web GUI.

## Delivery levels

The brief is written once and rendered at four levels of detail:

| Level | Device | What it shows |
| --- | --- | --- |
| L0 Push | Phone (HA Companion app, or Apprise) | Daily headline, one status line per system, count of new stories; urgent alerts immediately |
| L1 Glance | Repurposed Amazon tablets (HA dashboards or the status board) | Green/yellow/red per system, top stories, developing issues; always on |
| L2 Read | Computer, tablet, e-reader | Full brief as HTML; PDF/EPUB for the e-reader later |
| L3 Dig | Admin GUI and Claude | Findings, replay, log search, history exports, network map |

## Architecture

```
devices ──syslog 514──▶ ┌──────── homelab-brief (one container, own LAN IP) ────────┐
                        │ Vector: receive, archive (daily files), parse             │
APIs ◀──────poll─────── │ collectors: Unraid ×3, UniFi, SNMP, iDRAC                 │
HA ──MQTT statestream─▶ │ worker: signatures, baselines, detectors, digest, alerts  │
                        │ web: admin GUI (HTTPS), read-only views, API, /metrics    │
                        └───────┬──────────────────────────────┬───────────────────┘
                     MQTT + HA discovery                /api/v1, /metrics, files
                                ▼                              ▼
                      Home Assistant (broker)          Grafana, tablets, Claude
```

- **One image, one container, one appdata folder, one Unraid template.** Vector, the worker, and the web server run under a process supervisor; Docker's health check shows status in Unraid.
- **Own LAN IP** on `br0`, so devices send syslog straight to it and standard ports are free. This removes the dependency on the host server's syslog service, which can stop silently.
- **The broker stays outside:** a temporary Mosquitto container now, HA's Mosquitto add-on later.
- **Repeatable:** versioned images from the private repo, config schema migrations, backup and restore of one folder, excluded from nightly auto-updates.

## Data sources

| Source | Interface | Adds |
| --- | --- | --- |
| Every device and server | Syslog (UDP/TCP 514) direct to the container | Events, plus client presence, per-client DNS, memory, temperatures, and firmware already present in the logs |
| Unraid servers | Unraid GraphQL API, read-only keys | Array and parity (including speed), disks and SMART, containers and VMs, notifications, versions |
| UniFi | UniFi Network API, read-only key | Devices, firmware and updates, uplinks, clients, traffic |
| Switches, PDU, UPS (optional) | SNMP v3 | Interface counters, PDU and UPS readings |
| Dell server with iDRAC (optional) | IPMI or Redfish via iDRAC | Power, temperatures, fans, hardware events |
| Home Assistant | MQTT statestream | Power and energy, UPS, smart plugs |

**Power belongs to Home Assistant.** It has the Energy dashboard, long-term statistics, Zigbee plugs, NUT for the UPS, and possibly PDU outlet power. The container reads the chosen entities back over MQTT, so it needs no HA token, and the brief can say "Server 2 drew 18% more this week."

## Outputs (standard interfaces)

- **MQTT with Home Assistant discovery:** sensors, alerts, headline, and feedback buttons (acknowledge, snooze).
- **Prometheus/OpenMetrics `/metrics`:** for Grafana or any scraper.
- **Versioned REST `/api/v1`:** status, findings, systems, map.
- **Static views:** brief, status board PNG, static map, `status.json` for tablets, HA cards, and the e-reader.
- **Digests for Claude** in a Filesystem MCP folder, kept under 150 lines per file.
- **Optional Apprise** notifications (ntfy, Gotify, email, and others).

## Administration

- **Admin GUI** at `https://<container-ip>/admin`: overview, sources, collectors (with "Test connection"), findings and alerts, replay and tuning, a config editor with validation, diff, history, and rollback, editors for systems, devices, and topology, log search, the network map, backups, and an audit log.
- **Security:** login required over HTTPS, no default password, Argon2 hashes, CSRF protection, rate-limited logins, an audit log of every change, LAN only (VPN for remote).
- **Command line:** the `brief` CLI covers every GUI action through `docker exec` from an SSH session on the Unraid host. An optional in-container SSH server (key-only, off by default) gives direct access.

## AI (any model, any host)

AI is optional, vendor-neutral, and **off by default**: a master switch and per-feature switches on the admin GUI's AI page, with locked guardrails (always on) shown separately from adjustable ones. The product reaches models through the OpenAI-compatible API, so the same config works with Ollama on the GPU server's GPU, llama.cpp, vLLM, a LiteLLM gateway, or a hosted service (plus an optional native Anthropic adapter). Each AI task (morning brief, explain a finding, weekly summary) has a fallback chain that ends in the template, so the brief always goes out.

- **Rules decide; AI explains.** AI never decides findings, severity, or routing, and never takes actions.
- **the GPU server as the local GPU:** an Ollama container on the GPU server serves both this product and Home Assistant's AI, restricted to the LAN by a firewall rule or a key gateway, with a short keep-alive to save power.
- **Home Assistant's AI:** HA's Assist can use the same model server, answer from this product's sensors, and call this product's read-only MCP server for deeper questions.
- **Any AI client:** the MCP server (token scopes, audited) works with Home Assistant, Claude, and other MCP-capable assistants.
- **Privacy:** providers marked `cloud` never receive device-level detail, raw logs, or DNS data.
- **Tool-neutral context:** coding instructions in `AGENTS.md`, prompts as plain Markdown, and the digest as a small context pack any model can read.

See `docs/guide/09-ai-integration.md`.

## Agents (Phase 3)

Optional agents that look things up with read-only tools: an **investigator** that attaches a cited note to important findings, **triage** for new log patterns, a weekly **tuner** that proposes noise-cutting changes backed by replay evidence, and **ask the lab**, a chat box in the admin GUI. They never change anything: suggested changes become proposals you approve, every claim must cite a tool result, runs are bounded and run in the background, and cloud models never see device-level data. See `docs/guide/10-agents.md`.

## What the brief contains

Headline, top stories (ranked, with a one-line "so what" and a suggested fix), developing issues (worse, same, resolved), a status line per system, and one line listing what was checked and clean. "No data today" replaces "all quiet" whenever data is missing.

## Detectors and baselines

Counts per host and signature are bucketed by event time, so "is this normal?" is answered against history. Raw logs stay in the archive (retention configurable); aggregates are tiny and kept indefinitely.

| Detector | What it flags |
| --- | --- |
| Novelty | A signature never seen before, or seen again after 7+ quiet days |
| Deviation | A day's count far above the median of the previous 14 days |
| Off-hours | A signature at an hour where it has not appeared in 14 days |
| Absence | A source that normally logs and goes quiet (runs hourly) |
| Hardware and inventory (Phase 2) | SMART trends, slow parity checks, disk temperatures, firmware pending, new infrastructure |
| Devices (Phase 2) | New devices, traffic spikes against each device's own history, odd-hours activity |
| Power and metrics (Phase 2) | Power drift, metric drift |

**Ignore list:** known noise is filtered before detection (Flex switch gaming-device flaps, doorbell low memory, UCG-Max face-sync failures) and still counted, so a change in volume stays visible. "Ignore this signature" in the GUI adds rules.

## Notifications that don't nag

Routing by rule, kind, then default; urgent findings bypass quiet hours; dedupe; a re-notify interval; early escalation only when worse; a "resolved" message; a daily push budget; and acknowledge or snooze from the HA notification or the admin GUI.

## Dashboards

Flat and retro (style picked later), cheap to render so the old Amazon tablets run smoothly: monospace type, solid colors, few cards per view, pre-rendered sparklines, a static status-board image for the oldest tablets, screens dimmed overnight.

## Efficiency

Heavy work happens once, results stay small, everything else idles: the database, state, and archive on the pool so array disks can sleep; batched writes; hourly and daily timers; no headless browsers; a 768 MB container limit; tier 1 only until a heavier backend is clearly needed.

## Build phases

| Phase | Tasks | Milestone |
| --- | --- | --- |
| Scaffold and formats | 0-5 | Single image runs locally; real formats parse; baselines fill |
| Web foundation and early deploy | 6-7 | Container on the host server with its own IP; backfill imported; all sources repointed; history accumulating |
| Findings and brief | 8-12 | Detectors, replay, digests, alert lifecycle, rendered brief |
| Outputs, admin, release | 13-17 | MQTT, `/metrics`, API, Apprise; admin GUI; CLI and SSH; versioned releases and template |
| Phase 2 | 18-26 | Unraid, UniFi, SNMP, iDRAC, HA power, AI provider layer, network map, MCP server and HA AI (any order after 18) |
| Phase 3 | 27-30 | Tool registry and agent runtime, proposals, the four agents, agent evaluation |

## Network map

A draggable diagram of servers, network equipment, and services with live status colors, in the admin GUI and as a read-only page, plus a static SVG for old tablets and an optional draw.io export. Data comes from UniFi uplinks, Unraid containers and VMs, and a manual `topology.yaml`; it works with the manual file alone. Clients are grouped per VLAN and `ignore` devices never appear. See `docs/guide/07-network-map.md`.

## Tier upgrade path

The archive and the normalized event stream are the same for every tier, so a heavier backend is added by pointing another Vector sink at it.

| Tier | Adds | Approximate cost | Add when |
| --- | --- | --- | --- |
| 1 Baseline | SQLite baselines, detectors, brief, MQTT, API, `/metrics` | Negligible | Now |
| 2 Search | Loki and Grafana | About 1-2 GB RAM | You want full-text search beyond the GUI's log search and long graphs |
| 3 SIEM | Wazuh (open source) | 4+ GB RAM, several cores | You want correlation and detection for its own sake |

## Open questions

- [ ] Container IP and whether to enable host access to custom networks on the host server.
- [ ] Raw archive retention (30, 60, or 90 days).
- [ ] Cache pool name.
- [ ] Mosquitto: when to move to HA's add-on.
- [ ] E-reader model.
- [ ] Which Amazon tablet models you have, and whether each runs a kiosk browser without flashing.
- [ ] Retro dashboard style (picked when dashboard work starts).
- [ ] Device profiles per household member.
- [ ] Local model host and server (Ollama on the GPU server recommended), the first model, and how to restrict access (firewall rule or key gateway).
