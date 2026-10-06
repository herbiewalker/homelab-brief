# Step 4: Publish

Goal: results reach Home Assistant and the tablets without HA knowing anything about the pipeline internals. The backend publishes MQTT topics and static files; HA automations react to the topics. In this design the backend needs no HA token. Add one only if you later want it to call HA services directly.

## 4a. Mosquitto

Save as `mosquitto/mosquitto.conf` (the repo has it):

```
persistence true
persistence_location /mosquitto/data/
listener 1883
allow_anonymous false
password_file /mosquitto/config/passwd
acl_file /mosquitto/config/acl
```

Save as `mosquitto/acl` (the publisher writes; Home Assistant reads everything, writes feedback from notification buttons, and publishes chosen entity states under `homelab/ha/`):

```
user brief
topic readwrite homelab/#
topic readwrite homeassistant/#

user ha
topic read homelab/#
topic read homeassistant/#
topic write homelab/brief/feedback/#
topic write homelab/ha/#
```

Create the users once the container is running, then restart it:

```bash
docker exec -it mosquitto mosquitto_passwd -c /mosquitto/config/passwd brief
docker exec -it mosquitto mosquitto_passwd /mosquitto/config/passwd ha
```

Keep port 1883 LAN-only. This Mosquitto container is temporary: when Home Assistant is set up, move to HA's Mosquitto add-on by changing the `MQTT_HOST` template variable. Discovery messages are retained, so the publisher simply republishes on connect.

## 4b. Topics

| Topic | Retained | Content |
| --- | --- | --- |
| `homelab/brief/availability` | yes | `online` or `offline`; the publisher registers `offline` as its last-will message and also publishes it on a clean shutdown, so a stopped or dead container flips every sensor to unavailable |
| `homelab/brief/<metric_id>/state` | yes | Latest value, published only when it changes |
| `homelab/brief/headline` | yes | Today's one-line headline for the phone push |
| `homelab/brief/alert` | no | JSON for pushes: finding id, level, lifecycle state (`new`, `repeat`, `escalated`, `resolved`), title, body, detail link, suggested actions |
| `homelab/brief/feedback/<finding_id>` | no | Written by Home Assistant when you tap a button: `ack`, `snooze_24h`, or `snooze_7d` |
| `homelab/ha/<domain>/<entity>/state` | per HA setting | Home Assistant's `mqtt_statestream` output for the entities you choose (power, energy, UPS); read by the container (Phase 2, Task 23) |
| `homeassistant/sensor/homelab_brief/<metric_id>/config` | yes | MQTT discovery payload |

## 4c. Metric registry

Save as `brief/registry.yaml` (the repo has a starting version). Each entry becomes a Home Assistant sensor automatically, so adding a metric means one collector (or a derived query) plus one entry here.

```yaml
metrics:
  - id: events_24h
    name: Log events, last 24h
    group: pipeline
    kind: number
    unit: events
    interval: hourly
  - id: new_signatures_24h
    name: New signatures, last 24h
    group: findings
    kind: number
    interval: hourly
  - id: hosts_silent
    name: Silent hosts
    group: findings
    kind: number
    interval: hourly
    thresholds: { warn: 1, crit: 3, direction: high }
  - id: system_udm
    name: UDM Pro status
    group: systems
    kind: state            # green, yellow, red
    interval: hourly
  - id: ingest_lag_s
    name: Ingest lag
    group: pipeline
    kind: number
    unit: s
    interval: hourly
    thresholds: { warn: 300, crit: 1800, direction: high }
```

Start with metrics derived from the database, which need no new collectors. Add collectors one at a time later (Unraid array state and disk temperatures, UniFi device stats). Each collector is a small function that returns `{metric_id: value}` and is listed in the registry by name.

Discovery payload the publisher generates per entry (shown for a number metric):

```json
{
  "name": "Silent hosts",
  "unique_id": "homelab_brief_hosts_silent",
  "state_topic": "homelab/brief/hosts_silent/state",
  "availability_topic": "homelab/brief/availability",
  "state_class": "measurement",
  "expire_after": 7200,
  "device": { "identifiers": ["homelab_brief"], "name": "Homelab Brief" }
}
```

Set `expire_after` to about twice the metric's interval so a stalled pipeline turns the sensor to unavailable on its own. Slow metrics get slow intervals; only the heartbeat metrics run frequently. Registry entries carry a schema version so dashboards survive changes.

## 4d. Web server and other standard outputs

The container's own web server (FastAPI on Uvicorn) replaces the separate nginx container. On the container's IP:

- `http://<container-ip>/` serves the read-only views: `brief.html`, `status.png`, `status.json`, and the static map. Tablets and HA webpage or picture cards fetch these.
- `http://<container-ip>/api/v1/...` serves read-only JSON: `status`, `systems`, `findings`, `metrics`, `alerts` (the map arrives with the map task). No write endpoints.
- `http://<container-ip>/metrics` serves Prometheus text for any scraper: the same sensors as MQTT, plus `homelab_brief_system_status{system}` (0 green, 1 yellow, 2 red) and `homelab_brief_run_ok`.
- `https://<container-ip>/admin` is the admin GUI (login required). See `08-admin-and-access.md`.
- Optional Apprise notifications (ntfy, Gotify, email, and others) are configured in `brief/notify.yaml`, off by default.

Rules:

- Keep it on the LAN. If the host server sits behind a Cloudflare tunnel or any public reverse proxy, make sure no route points at the container. For remote access use a VPN.
- The files describe your network, including device names and IPs. Treat them as private.
- Status files are served with `Cache-Control: no-cache` so a tablet never shows yesterday's board without noticing.

## 4e. Hand-off to the HA guide

The HA side consumes exactly these: the MQTT topics above, `status.json`, `status.png`, `brief.html`, and the network map files (`map.html`, `map.svg`). The phone push is an HA automation triggered by a change to `homelab/brief/headline`, alerts trigger on `homelab/brief/alert`, and notification buttons publish to `homelab/brief/feedback/<finding_id>`.

**Done when:** `mosquitto_sub -u ha -P <password> -t 'homelab/#' -v` shows retained sensor states, stopping the `homelab-brief` container flips `availability` to `offline`, `http://<container-ip>/status.png` loads on a tablet, and `/metrics` and `/api/v1/status` return the same run.
