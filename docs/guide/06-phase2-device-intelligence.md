# Phase 2: More data sources, smarter brief

Goal: go beyond logs. Pull data the logs do not carry from standard APIs, read power from Home Assistant, and feed everything into the same findings, brief, alerts, metrics, and network map as Phase 1.

Phase 2 starts after Phase 1 (Tasks 0-17) works end to end. Task 18 (the framework) comes first; Tasks 19-26 can go in any order, and the order can change as notifications and dashboards come online. Every collector is switched on, tested, and tuned from `brief/collectors.yaml` or the admin GUI's Collectors page.

## Principles

1. **Everything is configurable.** Collectors in `collectors.yaml`, routing and alert lifecycle in `priorities.yaml`, thresholds and advice in `playbook.yaml`, device identity and profiles in `devices.yaml` (private), systems in `systems.yaml`, AI providers and tasks in `ai.yaml` (prompts in `prompts/`), the map in `map.yaml` and `topology.yaml`, retention in `retention.yaml`, notifications in `notify.yaml`. All editable in the admin GUI with validation and rollback.
2. **Rules decide; AI explains.** Detectors and the playbook decide what is a finding. A model, if enabled, only rewrites existing findings. It never decides alerts and never takes actions.
3. **Actions are explicit.** The container suggests; Home Assistant buttons or the admin GUI trigger anything that changes a system.
4. **Private by default.** Per-device detail stays local.
5. **Standards and least privilege.** Every connection uses a standard interface and a read-only account or key, stored as a template variable.
6. **Open source first,** unless a tool adds a heavy service or much more build time. Picks and licenses live in `docs/DECISIONS.md`.
7. **Light on resources.** Collectors run on slow timers and write small rows to the existing database.

## Data sources

| Collector | Interface | Account | What it adds |
| --- | --- | --- | --- |
| `unraid` (each of your Unraid servers) | Unraid GraphQL API | Read-only API key per server | Array and parity state, parity history (duration, speed), disks and temperatures, SMART data as exposed, containers and VMs, notifications, OS and plugin versions |
| `unifi` | UniFi Network API (verify what your version offers) | Read-only API key or local read-only user | Devices, models, firmware and update availability, uplinks, clients, per-client traffic |
| Logs (no collector needed) | Syslog already received | n/a | Client join/leave with signal strength (APs), per-client DNS (gateway coredns), memory (earlyoom), temperatures (fanctrl), firmware (device headers) |
| `snmp` (optional) | SNMP v3 preferred, v2c on the LAN | Read-only user or community | Interface counters, PDU and UPS readings |
| `idrac` (optional) | IPMI or Redfish, whichever your iDRAC firmware supports | Read-only iDRAC user | Server power draw, temperatures, fans, hardware event log |
| `ha_statestream` | MQTT (Home Assistant `mqtt_statestream`) | MQTT user | Power and energy per device, UPS state, smart plugs, anything HA knows |
| `dns_filter` (optional) | AdGuard Home or Pi-hole API | Read-only token | Only if the gateway's DNS logs are not enough |

**SMART fallback:** if the Unraid API does not expose the SMART attributes needed (reallocated, pending, offline-uncorrectable, CRC errors, NVMe media errors), add a User Script on each server that writes `smartctl --json -a` per disk to a share, and the `unraid` collector reads those files.

## Power: Home Assistant owns it

Home Assistant is the better home for power and energy: it has the Energy dashboard, long-term statistics, a Zigbee radio or dongle for smart plugs, Network UPS Tools (NUT) for the UPS, and possibly per-outlet power for the PDU through its UniFi integration (check your version).

The container reads power back with no HA token: in HA, enable `mqtt_statestream` for the chosen entities with base topic `homelab/ha`, and the `ha_statestream` collector subscribes. The brief then reports things like "Server 2 drew 18% more this week" and the weekly report includes energy per system. iDRAC is the one place the container may read power directly, if HA has no clean way to get it.

## Collector framework

A collector is a small module in `brief/src/collectors/` that fetches data, writes rows, and returns metric values for the registry. Adding one requires no changes to the scheduler, publisher, digest, or GUI.

Contract:

- Declares `name`, `interval`, `requires` (template variables it needs), and a `test()` used by the GUI's "Test connection" button.
- `run(db, config) -> dict[str, float | str]` writes its rows and returns metrics keyed by registry id.
- A failing collector reports an error metric and keeps its last data. It never crashes the run.

New tables (migrations):

```sql
CREATE TABLE disk (disk_id TEXT PRIMARY KEY, server TEXT, model TEXT, serial TEXT, role TEXT, first_seen TEXT);
CREATE TABLE smart_reading (ts TEXT, disk_id TEXT, attr TEXT, value INTEGER, worst INTEGER, raw INTEGER,
                            PRIMARY KEY (ts, disk_id, attr));
CREATE TABLE inventory (item_id TEXT PRIMARY KEY, kind TEXT, name TEXT, model TEXT,
                        current_version TEXT, available_version TEXT, checked_at TEXT);
CREATE TABLE client (mac TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT, hostname TEXT, vendor TEXT, vlan TEXT);
CREATE TABLE client_hourly (day TEXT, hour INTEGER, mac TEXT, bytes_up INTEGER, bytes_down INTEGER,
                            online_min INTEGER, PRIMARY KEY (day, hour, mac));
CREATE TABLE dns_hourly (day TEXT, hour INTEGER, client TEXT, queries INTEGER, blocked INTEGER,
                         top_domains TEXT, PRIMARY KEY (day, hour, client));
CREATE TABLE power_reading (ts TEXT, entity TEXT, system_id TEXT, watts REAL, kwh REAL,
                            PRIMARY KEY (ts, entity));
```

The topology tables are in `07-network-map.md`. Roll `client_hourly`, `dns_hourly`, and `power_reading` up to daily rows after 90 days.

## Detectors

Starting values live in `brief/playbook.yaml`; each device is judged against its own history.

| Detector | Rule |
| --- | --- |
| `smart_trend` | A watched SMART attribute increasing over 14 days (any reallocated increase; any non-zero pending or offline-uncorrectable) |
| `smart_failing` | Drive reports SMART overall status failed |
| `disk_temp` | Sustained high temperature |
| `crc_rising` | UDMA CRC errors increasing (cable or backplane, not the disk) |
| `parity_slow` | A parity check much slower than that server's previous checks or below a floor |
| `unraid_warning` | An Unraid warning or alert notification |
| `firmware_pending` | An update is available for a device |
| `version_behind` | Pending for more than the configured days |
| `new_infrastructure` | A gateway, switch, AP, camera, or server not seen before (also badged on the map) |
| `new_device` | A client MAC not seen before |
| `traffic_spike` | A client's daily bytes far above its own median |
| `odd_hours` | A device online during its profile's quiet hours |
| `power_drift` | A system's average power well above its own baseline |
| `metric_drift` | Any collector metric's 7-day mean far from its prior 21-day mean |

Disk guidance uses buckets, not predicted dates, because SMART is a useful warning but a weak predictor: `watch` (recheck in a week), `plan` (replace within roughly 4-8 weeks), `act_now` (back up now and replace). The finding states the trend and window.

## Playbook, routing, and actions

`brief/playbook.yaml` turns findings into recommendations by plain templating with structured conditions (field, operator, value); nothing in a config file is executed as code. Phase 2 findings use the same routing and alert lifecycle as Phase 1 (`03-brief.md`, section 3g, and `brief/priorities.yaml`). Alerts carry suggested actions; anything that changes a system is a tap in Home Assistant or an action in the admin GUI, recorded in the audit log.

## Profiles and privacy

`brief/devices.yaml` (private) maps MACs to a label, owner, and profile:

- `full`: activity timeline, odd-hours checks, and a DNS category summary.
- `security_only`: new-device, traffic-spike, and security findings only.
- `ignore`: nothing stored.

Unknown devices default to `security_only`. Choose profiles per person and per age, and agree with household members on what is monitored. Traffic is mostly encrypted, so what you see is volume, timing, and destinations, not content. `devices.yaml`, client data, DNS data, and power per device never go in git or in any call that leaves the LAN.

## AI layer

Model-agnostic and optional: any OpenAI-compatible server (Ollama on the GPU server's GPU, llama.cpp, vLLM, a LiteLLM gateway, hosted services) or the native Anthropic API, configured in `brief/ai.yaml` with per-task fallback chains that end in the template. Phase 2 data (devices, DNS, power per device) is the most sensitive, so `cloud` providers never receive it. Home Assistant's AI can use the same model server and reach this product's data through the MCP server. See `09-ai-integration.md`.

## Done when

- Enabling a collector (GUI or config) makes its metrics appear in HA and `/metrics` with no code changes; disabling it removes them cleanly; "Test connection" reports success or a clear error.
- Each server's parity history produces a `parity_slow` finding for a check that crawls along at a few MB/s.
- A fixture disk with rising pending sectors produces an `act_now` finding with playbook text and an alert.
- Power published by HA through statestream appears as metrics, in the brief, and in the weekly report.
- No file in the repo contains real device names, MACs, or serial numbers.
