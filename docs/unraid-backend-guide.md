# Unraid Backend: Technical Guide

Index, scope, and layout. The overall plan (outputs, tiers, dashboards, phases) is in `docs/project-plan.md`.

| Part | File |
| --- | --- |
| Step 1: Collect (syslog receiver, archive, backfill) | `docs/guide/01-collect.md` |
| Step 2: Baseline | `docs/guide/02-baseline.md` |
| Step 3: Brief | `docs/guide/03-brief.md` |
| Step 4: Publish | `docs/guide/04-publish.md` |
| Operations, verification, troubleshooting | `docs/guide/05-operations-and-verification.md` |
| Phase 2: more data sources | `docs/guide/06-phase2-device-intelligence.md` |
| Network map | `docs/guide/07-network-map.md` |
| Admin GUI, API, and command-line access | `docs/guide/08-admin-and-access.md` |
| AI integration (any model, the GPU server GPU, Home Assistant AI, MCP) | `docs/guide/09-ai-integration.md` |
| Phase 3: agents | `docs/guide/10-agents.md` |

**One home per fact.** Code and config blocks in this guide are illustrative. The files in the repo (`vector/`, `brief/*.yaml`, `mosquitto/`, `deploy/`) are authoritative for values such as thresholds, routes, and schedules. If the guide and a file disagree, the file wins and the guide gets fixed.

## Scope

The product is **one container**, `homelab-brief`, on the host server. It:

- receives syslog from every device directly on its own LAN IP and keeps the raw archive,
- polls APIs for data logs do not carry (Unraid, UniFi, optional SNMP and iDRAC) and reads Home Assistant's power data over MQTT,
- runs the baselines, detectors, digest, alerts, and network map,
- serves the admin GUI, read-only tablet views, a REST API, `/metrics`, and a read-only MCP server for AI clients,
- optionally uses any AI model (local on the GPU server's GPU or hosted) through the OpenAI-compatible API, always with a no-AI fallback,
- publishes to Home Assistant over MQTT with discovery.

The MQTT broker is outside the container: a temporary Mosquitto container during the build, then HA's Mosquitto add-on. Power and energy are owned by Home Assistant and read back by the container.

**Why a dedicated host server.** Pick a stable, always-on Unraid server that can hold the syslog archive, and keep monitoring off a server that runs heavy or experimental workloads.

## Inside the container

| Process | Role |
| --- | --- |
| Vector | Syslog receiver (UDP and TCP 514), raw archive writer, parser to normalized events |
| Worker | Ingest, signatures, baselines, collectors, detectors, digest, alerts, MQTT publisher, scheduler |
| Web server (FastAPI on Uvicorn) | Admin GUI (HTTPS), read-only views (HTTP), REST API, `/metrics`, `/healthz` |
| SSH server (optional, off by default) | Key-only command-line access |

A process supervisor (s6-overlay or supervisord, chosen in Task 0) runs them and restarts any that exit. Docker's health check calls `/healthz`, so Unraid shows healthy or unhealthy.

## Network

Docker decides the network mode from the Unraid template; the admin GUI's Network page (and `brief/network.yaml`) detects it, sets the advertised address and listen ports, warns about problems, and shows the template settings for switching modes. See `08-admin-and-access.md`.

The recommended mode gives the container its own fixed LAN IP on Unraid's custom network (`br0`), so standard ports are free and devices send syslog straight to it. Host and bridge modes also work, with the trade-offs listed on the Network page.

| Port | Protocol | Use |
| --- | --- | --- |
| 514 | UDP and TCP | Syslog from every device and server |
| 80 | HTTP | Read-only views, read-only API, `/metrics` |
| 443 | HTTPS | Admin GUI and all writes |
| 22 | SSH | Optional, key-only, off by default |

On Unraid, a container with its own IP can be unreachable from its own host unless **host access to custom networks** is enabled in Docker settings. The host server sending its own syslog to the container depends on it, so check this during deployment.

## Storage (one appdata folder)

| Path in the container | Host path | Contents |
| --- | --- | --- |
| `/config` | `/mnt/user/appdata/homelab-brief/config` | `brief/*.yaml` (yours), TLS certificate, SSH keys, config history. The Vector config and parser ship in the image (`/app/vector`) and are not edited here |
| `/state` | `/mnt/<pool>/appdata/homelab-brief/state` | SQLite database, Drain3 state, offsets (pool path, not `/mnt/user`) |
| `/archive` | `/mnt/<pool>/syslog-archive` (or a share of your choice) | Raw syslog, one file per host per day, compressed after the day ends |
| `/out` | `/mnt/user/<share>/brief` | Digests and exports for Claude (its own Filesystem MCP entry) |
| `/www` | inside `/state` or `/config` | Rendered views served by the web server |

Backup is the appdata folder plus the archive. Secrets come from the Unraid template's variables, never from files in a Filesystem MCP folder.

## Prerequisites

- [ ] A free LAN IP for the container on the host server's network, and a decision on "host access to custom networks".
- [ ] The cache pool name (replaces `<pool>`).
- [ ] An MQTT broker reachable from the container (temporary Mosquitto now; HA's add-on later).
- [ ] Read-only API keys for each Unraid server and the UniFi controller when Phase 2 starts.
- [ ] Register `\\<host>\<share>\brief` in the Filesystem MCP as its own subfolder entry.
