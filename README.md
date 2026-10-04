# Homelab Brief

One container for Unraid that collects your homelab's logs and API data, learns what normal looks like, and turns it into a daily news-style brief, alerts that don't nag, and live status for Home Assistant and any other dashboard.

- **Collects:** syslog straight from every device (on the container's own LAN IP), plus the Unraid, UniFi, SNMP, iDRAC, and Home Assistant (power) APIs.
- **Publishes:** MQTT with Home Assistant discovery, Prometheus `/metrics`, a REST API, static views for tablets and e-readers, and optional Apprise notifications.
- **Administered** from a modern web GUI (HTTPS, login, audit log, versioned config with rollback), with a full CLI and optional key-only SSH.
- **AI-agnostic:** optional AI through any OpenAI-compatible model server (local on a GPU box or hosted), always with a no-AI fallback, plus a read-only MCP server so Home Assistant's AI or any assistant can query it.
- **Efficient:** low memory, batched writes, smooth on old tablets.

Start with `docs/START-HERE.md`. Instructions for coding agents are in `AGENTS.md`, the plan is `docs/project-plan.md`, and the build backlog is `docs/TASKS.md`.

This repository is private. Do not commit secrets, real hostnames, IPs, MACs, or real log lines.
