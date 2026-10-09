# Changelog

Versions follow [semantic versioning](https://semver.org/). Until 1.0.0 the product is in staging: a minor
step (0.x.0) may change config files, and the upgrade step migrates them (see `docs/DECISIONS.md`); a
patch step (0.0.x) is a build of the next backlog task or a fix. The release workflow refuses to publish a
tag that has no entry here. Images are tagged with the exact version; there is no `latest`.

## 0.0.34

Task 24: the AI provider layer, completed (still off by default).

- Three tasks through one chain runner: the morning brief, **explain a finding**, and a **weekly summary** (written next to the weekly report as `<week>-summary.md`). Each has its own switch and fallback chain ending in the template.
- Privacy: a cloud provider receives system-level facts only; a finding about one household device (a new device, a traffic spike, odd hours) has no cloud input at all; and whatever is built for a cloud call is scanned before sending, blocking any IP address, MAC address, or private name (a sender, a `devices.yaml` label or owner, a client hostname). Replies naming a host, device, MAC, or finding that was not in the input are rejected.
- Provider tools: list models (`/v1/models`), test connection with latency and a tool-calling check, and a side-by-side preview of the template brief and a provider's version. All are real calls, logged without content and counted against the budget.
- **AI page** in the admin GUI (master switch, features, providers, chains, locked and adjustable guardrails, prompt editor with history and rollback, preview, explain, call log) and `brief ai ...` for every action; edits go through the config editor, so they are validated, versioned, and audited. `ai.yaml` is now validated (chains must name real providers and end in the template).
- `deploy/ollama.md`: running a local model on a GPU server.
- `scripts/e2e_ai.py` drives the CLI and the HTTPS page in the image against a fake model server.

## 0.0.33

Task 23: power and energy from Home Assistant (off by default; no Home Assistant token).

- `ha_statestream` collector: reads the entities you list from Home Assistant's `mqtt_statestream` topics over the same MQTT broker, hourly, into `power_reading`; per-entity and per-system power sensors and energy for the last 24 hours. `scale` handles kW and Wh.
- The daily brief gains a power line and the weekly report a "Power and energy" table (this week against last, average draw), only when power data exists.
- `power_drift` finding: a system's average draw over the last day well above its own usual (an increase of 25% and 10 W by default).
- `scripts/e2e_mqtt.py` now proves the path through a real broker and the real access list.

## 0.0.32

Task 22: SNMP and iDRAC (both optional and off by default; read-only).

- SNMP collector (`snmp`): SNMPv3 preferred, v2c on a trusted LAN. Profiles `switch` (uptime, ports up, interface errors since the last poll), `ups` (RFC 1628 UPS-MIB) and `custom` (your own OIDs with a scale, for a PDU). Credentials are written to a private temporary file for each run, never on a command line. Targets are independent.
- iDRAC collector (`idrac`): power draw, hottest sensor, inlet temperature, slowest fan, sensors in a warning or critical state, hardware event log size, over IPMI (the only protocol an iDRAC 7 offers) or Redfish.
- The image now includes the `snmp` and `ipmitool` Debian packages: the image grows by about 89 MB (563 to 652 MB), nearly all from net-snmp's Perl dependencies.
- `scripts/e2e_devices.py` runs the real tools against a fake SNMPv2c agent and a fake Redfish API.

## 0.0.31

Task 21: clients, traffic, and DNS activity (off by default; every device is limited by its `devices.yaml` profile).

- Presence and hourly traffic per device from the UniFi API (`clients`, `traffic`), and gateway DNS and access point join and leave lines from syslog (`client_logs`).
- Profiles are enforced where data would be stored: `ignore` keeps nothing (its log lines are only counted as suppressed), `security_only` (the default for an unlisted device) keeps that the device exists and its traffic volume, `full` also keeps DNS detail and hourly presence. Client log lines of non-`full` devices stay out of the signature counts and samples.
- Detectors `new_device`, `traffic_spike` (judged against the device's own usual day), and `odd_hours`, with playbook advice; hourly rows roll up to daily rows after 90 days. Migration none (tables from 0007).
- `scripts/e2e_collector.py` now sends DNS and join lines through Vector in the real image and checks what each profile kept.

## 0.0.30

Task 20: the UniFi collector (off by default; needs a read-only API key from the Network application).

- Reads devices, models, firmware, update availability, and uplinks from the UniFi Network Integration API into inventory and the network map tables; sensors for device, offline, and update counts.
- Firmware is also read from the `<mac>,<model>-<firmware>` token in switch, access point, and PDU syslog headers (new `device_header` table, written once per ingest cycle, migration 0009), so inventory has it when the API is unreachable or no key is set.
- Detectors: `firmware_pending` (an update waiting; after 30 days it becomes `version_behind`) and `new_infrastructure` (a gateway, switch, access point, PDU, camera, or server first seen after discovery settled), with playbook advice.
- The Unraid and UniFi collectors now share their HTTP and topology code; `detectors_disks` and `detect_disks` are now `detectors_hw` and `detect_hw`.
- `scripts/e2e_collector.py` also runs the UniFi path (fake API, plus firmware arriving in a syslog header).

## 0.0.29

Task 19: the Unraid collector (off by default; needs a read-only API key per server).

- Reads each server's GraphQL API: array state, disks with temperature and SMART verdict, parity check history, containers and VMs, unread warnings, OS and plugin versions. Servers are independent (an unreachable one reports `unraid_<id>_reachable` 0 and the others still update); a field or section a server does not have is dropped, not fatal.
- Optional `smart_source: smartctl_files` reads reallocated, pending and CRC attributes from files a User Script writes (`deploy/unraid-smart-export.sh`), because the API does not report the attribute table.
- Detectors: `smart_failing`, `smart_trend`, `crc_rising`, `disk_temp`, `parity_slow`, `unraid_warning`, `version_behind`, with playbook advice and buckets.
- Rows for the network map (server, containers, VMs), inventory, and per-server sensors. Migration 0008.
- `brief collector test unraid` reports per server and per section what answered. New end-to-end check `scripts/e2e_collector.py` runs the whole path against a fake Unraid API.

## 0.0.28

Task 18: the Phase 2 framework. No collector ships yet (Tasks 19 to 23 add them), and every one is off.

- Collector contract and framework: `collectors.yaml` is validated (a credential written into it is refused, keys come from template variables), enabled collectors run when due from the hourly job, each isolated with its own timeout; a failing one reports `collector_<name>_ok` 0 with a safe reason and keeps its last good numbers. Enabling or disabling one adds or removes its sensors, `/metrics` lines and API values with no other change. `brief collector list|test|run`.
- Migration 0007: the Phase 2 tables (disks, SMART readings, inventory, clients, DNS, power, topology).
- Playbook engine: `playbook.yaml` rules turn findings into advice with structured conditions (no code, regexes with nested repetition refused); the advice shows in the brief and a rule id can be routed in `priorities.yaml`.
- `metric_drift` detector: any collector metric whose recent mean moves far from its own earlier mean.
- `collectors.yaml`, `map.yaml` and `agents.yaml` are now validated, guarded with a last-good fallback, and editable in the config editor.

## 0.0.27

Task 17: packaging and release.

- Release workflow: a tag runs every check, then builds and pushes the image to the container registry under the exact version tag (never `latest`), and refuses a tag that differs from the program version or has no entry here.
- Unraid template (`deploy/unraid-template.xml`) and compose references for all three network modes: own IP on `br0` (default), host, bridge with port mappings.
- Config files are brought up to the current schema on upgrade (`brief.confmigrate`): a copy of the config folder is saved to `/state/config-backups/` first, a downgrade is noticed and never touches files.
- `deploy/README.md` rewritten as the install and upgrade guide.

## 0.0.26

Task 16a: setup wizard (`/admin/wizard`) and `brief init`, support bundle (`brief support-bundle`, structure and counts only, self-checked for private values), log scrubbing, `devices.yaml` and `topology.yaml` in the config editor.

## 0.0.25

Task 16: the full `brief` command (status, sources, config list/show/validate/history/diff/set/rollback/reset, systems, signatures, logs search, audit, network, backup) and the optional key-only SSH server (off by default).

## 0.0.22

Task 15d: replay and tuning page, signatures page with ignore, log search, systems form, backup download and config restore, report downloads. Task 15 complete.

## 0.0.21

Task 15d part 1: replay and tuning, signatures with ignore, log search.

## 0.0.20

Task 15c: Network page (mode and address detection, warnings for lost sender addresses, live syslog test, repointing status, template settings).

## 0.0.19

Task 15b: config editor with validation, diff, history, rollback, reset, and audit.

## 0.0.18

Task 15a: admin GUI shell (overview, sources, findings and alerts, audit log). Fixed browser sign-in being refused under `Referrer-Policy: no-referrer`.

## 0.0.16

Task 14: weekly schedule, config guard (a broken config file falls back to its last good copy and raises a finding), self-monitoring, storage guard.

## 0.0.15

Task 13 part 2: optional Apprise notifications (off by default) and `brief notify test`.

## 0.0.14

Task 13 part 1: registry-driven MQTT sensors, Prometheus `/metrics`, versioned `/api/v1`.

## 0.0.13

Readable story titles in the brief; the headline cuts at a word.

## 0.0.12

Status board uses DejaVu Sans Mono from the image, with the built-in font as fallback.

## 0.0.11

Task 12: static views (`brief.html`, `board.html`, `status.png`) and the optional AI provider layer (off by default; the template always works without it).

## 0.0.1 to 0.0.10

Tasks 0 to 11, built and tagged during the first staging runs: image and CI, syslog receiver and archive (Vector), SQLite store with numbered migrations, ingest and signatures (Drain3), web apps with admin login and TLS, `status.json` and MQTT sensors, `brief import`, detectors (novelty, deviation, off-hours, absence, clock skew), `brief replay` and the golden day, the daily digest with grouped stories, and the alert lifecycle with Home Assistant feedback. Individual tags in this range were rewritten when the repository history was cleaned and are not listed.
