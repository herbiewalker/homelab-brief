# Installing and upgrading Homelab Brief on Unraid

You run these steps yourself, one at a time. Claude Code prepares the image, template, and files; it does not deploy. Verify each pass with the checklist in `docs/guide/05-operations-and-verification.md`.

Files in this folder:

| File | Use |
|---|---|
| `unraid-template.xml` | The Unraid template (own IP on `br0` by default). Primary install path. |
| `docker-compose.own-ip.yml` | Same thing for Compose Manager, mode 1: own IP on `br0` (recommended). |
| `docker-compose.host.yml` | Mode 2: host network, ports moved away from Unraid's own. |
| `docker-compose.bridge.yml` | Mode 3: Docker bridge with published ports (can hide sender addresses; read the warning inside). |

## Before you start

- A release tag exists in the registry (see "Get the image" below). Always use an exact version such as `0.1.0`; the project publishes no `latest`.
- Pick a network mode (own IP on `br0` is recommended; host and bridge also work, see the Network page table in `docs/guide/08-admin-and-access.md`). For own IP, pick a free LAN IP on the host server's network. The default is `br0`, which Unraid already provides. A dedicated network (for example named `syslog-net`) is an option later: it needs its own subnet, gateway, and parent interface in the compose file, and "host access to custom networks" applies to it too.
- In own-IP mode, decide on **host access to custom networks** (Settings, Docker, advanced view). It must be enabled for the host server itself to send syslog to the container. Host and bridge modes do not need it.
- A broker is reachable if you want MQTT: the temporary Mosquitto container (`mosquitto/` in the repo has its config and ACL) or Home Assistant's Mosquitto add-on. Create its `brief` user first (see `docs/guide/04-publish.md`, section 4a). MQTT is optional; leave `MQTT_HOST` empty to run without it.
- **Exclude `homelab-brief` (and Mosquitto) from automatic Docker updates** (for example in the CA Auto Update plugin's per-container list). An upgrade is a deliberate step with a backup, see "Upgrade" below.

## Get the image

From the registry (the release workflow pushes `ghcr.io/<owner>/homelab-brief:<version>` when a version tag is created; the package is private, so log in once with a personal access token that can read packages):

```bash
docker login ghcr.io -u <github-user>
docker pull ghcr.io/<owner>/homelab-brief:<version>
```

In the template, replace `<owner>` and `<pin>` in the Repository field (`ghcr.io/<owner>/homelab-brief:<version>`). Compose Manager uses the same `docker login` credentials.

Without a registry (or before the first release), build on a machine with Docker and copy it over; then use the local name `homelab-brief:<version>` as the repository:

```bash
docker build --target runtime -t homelab-brief:<version> .
docker save homelab-brief:<version> | ssh root@<host> docker load
```

## Pass 1: install, collect, and baseline

1. **Folders.** (`cache` is Unraid's default pool name; use yours if it differs.) Create `/mnt/user/appdata/homelab-brief/config/brief`, `/mnt/cache/appdata/homelab-brief/state`, `/mnt/cache/syslog-archive`, and the share folder `<share>/brief`.
2. **Config.** Put only your own files in `config/brief/`: the real `systems.yaml` (made from your audit; from `brief/systems.example.yaml`), and `network.yaml` if you want an allowlist of sender subnets or non-default ports. The image ships every other default, so do not copy the whole `brief/` folder (a copy would stay behind when you upgrade).
3. **Install.** Add the container from the template (or the compose reference) in the chosen network mode, with the volumes above. Set these variables (in the template, or in the `env_file` in appdata, mode 600): `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASSWORD` only when you have a broker. Add `APPRISE_URLS` (comma-separated Apprise URLs) only if you turn Apprise on in `notify.yaml`; prove it with `docker exec <container> brief notify test`. **Leave `MQTT_HOST` empty for the first pass**: the container runs normally without MQTT, and you add the four values later and restart. Keep the read-only `/import` mount for step 7.
4. **First start.** Check `docker logs homelab-brief`: you should see `worker_started`, `web_starting` for both ports, a `tls` line, and a `network` line. The `network` line shows the mode and address the container detected, the address to point devices at (`send_syslog_to`), and any warnings (the same block is in `/status.json`). With DHCP the address can change after a restart, so reserve it in your router or pin it in the template once it works. Then open `http://<container-ip>/healthz` (expect `"status": "ok"`). Open `https://<container-ip>/admin` and follow the setup wizard (the one-time setup link is in the log), or run `docker exec -it homelab-brief brief init` for the same steps in a terminal. The admin password can also be set with `docker exec -it homelab-brief brief admin set-password`.
5. **Test the path.** From each Unraid server, send one test line and look for it in the log within a minute (`ingest_cycle` with `events: 1`, or the count in `/status.json`):

   ```bash
   logger -n <container-ip> -P 514 "homelab-brief test line"
   ```

   If the host server itself cannot reach the container, enable host access to custom networks (see above).
6. **Repoint sources.** Point every UniFi device (and Protect, if separate), and each Unraid server's remote syslog, at `<container-ip>` port 514. Most devices send to one target only, so the old server's files stop growing as each one moves. Open `http://<container-ip>/status.json`: `events_24h` should climb, and `hosts_silent` stays 0 while every system keeps sending (until typical gaps are learned in Task 8, a quiet host is reported only after 24 hours).
7. **Backfill.** Once every source is repointed, the old `syslog-<ip>.log` files are final. Import them with `docker exec -it homelab-brief brief import --from /import`. It prints progress, can take a while for a large archive, and refuses to run a second time (that would count every line twice). The worker reads the imported events in the background, bucketed by their own event time. When `ingest_cycle` lines stop appearing in the log, remove the `/import` mount.
8. **Check the sensors (skip while MQTT is unconfigured).** In Home Assistant, or with `mosquitto_sub -u ha -P <password> -t 'homelab/#' -t 'homeassistant/#' -v`: three retained sensors (`events_24h`, `new_signatures_24h`, `hosts_silent`) and `homelab/brief/availability` showing `online`. Stopping the container flips availability to `offline`. The sensors refresh every hour.
9. **Leave it learning.** `/status.json` shows `learning.active: true` for the first live week, and live baselines grow from the first real line (an import does not start the learning period).

`/status.json` is also written hourly to `<share>/brief/latest/status.json`. If `run_ok` is `false`, its headline says what is wrong, and the MQTT sensors stop updating until it is fixed (they go unavailable after about two hours instead of showing a calm network).

## Pass 2: full product

10. **Outputs.** Confirm MQTT sensors in HA (or with `mosquitto_sub`), the status board on the oldest tablet, `/metrics`, and `/api/v1/status`.
11. **Watchdog.** Add the hourly stale-file User Script from the operations guide.
12. **Optional SSH.** Set `SSH_ENABLED=true` and add your public key to `config/ssh/authorized_keys` (plain key lines only), then restart. In host mode set `advertised_address` (the host IP) and a port other than 22. Check with `docker exec <container> brief ssh show`.
13. **Retire the old receiver.** After a week of clean collection, turn off the host server's built-in syslog server. The old files stay as they are.


## Upgrade

An upgrade replaces the image and keeps `config`, `state`, `archive`, and `out` as they are. Do it when you can watch it, not from auto-update.

1. **Read the changelog** (`CHANGELOG.md`) for the versions between yours and the new one. A change that needs your attention is listed there.
2. **Back up first.** In the admin GUI use Backups, "Download backup" (config, a snapshot of the database, signature state), or run `docker exec homelab-brief brief backup create --out /out/backup.tar.gz`. Keep the file somewhere safe; it is as sensitive as the admin login.
3. **Change the version** in the template (or the compose file) to the new exact tag, then apply and let the container restart. Nothing else changes.
4. **What happens at start.** The database applies its numbered migrations (a copy is kept as `state/baseline.db.pre-v<N>` first), and your config files are brought to the new schema if a release changed one (a copy of the config folder is saved in `state/config-backups/` first). A broken or newer file is never rewritten; the config guard keeps the last good copy and raises a finding.
5. **Verify.** `docker logs homelab-brief` shows `version_changed` and no `config_migration_failed`; `http://<container-ip>/healthz` says `"status": "ok"`; `/healthz` and the status page at `http://<container-ip>/` show the new version, and the 24-hour event count carries on from before.

**Roll back.** Set the previous tag and restart. This is safe when the new version made no database migration (the log line `version_changed` and the absence of a `baseline.db.pre-v<N>` file newer than the upgrade tell you). If it did, a newer database is refused by the older program on purpose: stop the container, restore `state/baseline.db` from `baseline.db.pre-v<N>` (and `config/brief` from `state/config-backups/` if a config file was migrated), then start the older tag.

The project proves the upgrade path in the release workflow: the new image is started on the state of the previous release and must keep its events, audit log, config, and admin login.

## Backup and restore

- **Config only** (everything you edited): Backups page, "Restore", or `brief backup restore FILE` (each file is checked, versioned, and audited like a normal edit).
- **Database and history**: stop the container, replace `state/baseline.db` (and `state/drain3.bin`) from the backup file, start it. The raw log archive is not in the backup; it is the source of truth on its own.
- **Whole appdata**: a plain copy of `config` and `state` while the container is stopped is also a valid backup.
- Test a restore once, on a spare container, before you need it.

## Phase 2 additions (any order)

### Unraid collector (Task 19)

1. **Keys.** On each Unraid server create an API key with read-only access (Settings, Management Access, API Keys) and add it to the container as a template variable (Add another Variable), for example `UNRAID_SERVER_1_API_KEY`. The names in `collectors.yaml` (`key_var`) must match.
2. **Settings.** In `config/brief/collectors.yaml` (or the config editor) set `unraid.enabled: true` and list your servers: an id (lowercase letters, digits, hyphens; it names the sensors), the URL `https://<address>/graphql`, the `key_var`, and `verify_tls: false` for a self-signed certificate. A server whose id matches a system id in `systems.yaml` shows its findings under that system's tile.
3. **Try it first.** `docker exec homelab-brief brief collector test unraid` asks every server for every section and says which answered, which fields this Unraid version did not know (those are dropped, not fatal), and which sections it does not have (for example VMs). Run this before relying on it: the queries follow the Unraid API's documented shape but were built without a live server, so this is the first real check.
4. **Run it.** The worker runs enabled collectors hourly; `brief collector run unraid` runs it now. `brief collector list` shows how the last run went, and `collector_unraid_ok` plus `unraid_<id>_reachable` appear as sensors.
5. **SMART attributes (optional).** The API gives each disk's overall SMART verdict and temperature, not the attribute table. For reallocated, pending, and CRC trends set `smart_source: smartctl_files`, install `deploy/unraid-smart-export.sh` as an hourly User Script on each server (set `DEST` and `SERVER_ID` in it), and mount that share read-only into the container at `smart_files_path`.
6. **What it finds.** `smart_failing`, `smart_trend`, `crc_rising`, `disk_temp`, `parity_slow` (a check far slower than the server's earlier ones or under 40 MB/s), `unraid_warning` (unread warning and alert notifications), and `version_behind`; thresholds are in `playbook.yaml`, wording and buckets in its `rules`.

### UniFi collector (Task 20)

1. **Key.** In the UniFi Network application create an API key (Settings, Control Plane, Integrations) and add two template variables to the container: `UNIFI_URL` (the console address, for example `https://<console-ip>`) and `UNIFI_API_KEY`.
2. **Settings.** In `collectors.yaml` set `unifi.enabled: true`. A console on a LAN address usually has a self-signed certificate: add `verify_tls: false`. Use `site:` to read one site, and `api_path: /integration` for a self-hosted Network application (a UniFi OS console is the default).
3. **Try it first.** `docker exec homelab-brief brief collector test unifi` says whether the console answered and how many devices and pending updates it found. The client follows the documented Integration API but was built without a live console, so this is the first real check; send me the message if it fails.
4. **Without a key.** Firmware is read from the syslog headers of switches, access points, and PDUs anyway, so inventory and `firmware_pending` have it as soon as those devices log to the container. The collector reports the missing key as a failed run, but the header firmware is still stored.
5. **What it finds.** `firmware_pending` (an update is waiting; after 30 days it becomes `version_behind`) and `new_infrastructure` (a gateway, switch, access point, PDU, or server that appeared after the first discovery pass; the first pass is treated as your starting inventory). Uplinks feed the network map.

### Clients, traffic, and DNS (Task 21)

This is the most personal data the product can hold, so it is off until you turn it on and every device is limited by its profile.

1. **Profiles first.** Copy `brief/devices.example.yaml` to `config/brief/devices.yaml` and list the devices you care about by MAC with a `profile`: `full` (presence timeline, DNS detail, odd-hours check, traffic, new-device), `security_only` (that the device exists and when, and its traffic volume; **the default for any device you do not list**), or `ignore` (nothing at all is stored). Agree the profiles with the people whose devices they are. Quiet hours there are UTC.
2. **Switch it on** in `collectors.yaml` under `unifi`: `clients: true` (who is connected, hourly), `traffic: true` (hourly byte counters from the older `stat/sta` endpoint; whether your console accepts the API key there is what `brief collector test unifi` and the `unifi_traffic_ok` sensor tell you), and `client_logs: true` (gateway DNS and access point join and leave lines from syslog).
3. **What the logs add.** The gateway logs the blocked or categorised DNS queries, not every query, so DNS data is a view of filtering (which domains and categories were blocked for which device), not total DNS volume. For a device that is not `full`, its DNS and join lines are taken out of the generic log counts and samples, so they are not kept anywhere; for an `ignore` device they are counted only as "suppressed".
4. **What it finds.** `new_device` (a device that appeared after the first pass and is not in `devices.yaml`), `traffic_spike` (the last 24 hours against that device's own usual day, never against other devices), and `odd_hours` (a `full` device online during its own quiet hours). Thresholds are in `playbook.yaml`.
5. **Retention.** Hourly rows roll up to daily rows after `rollup_after_days` (90) in `retention.yaml`. Nothing here ever leaves the container: the AI layer never receives device-level data.

- In Home Assistant, enable `mqtt_statestream` with base topic `homelab/ha` for the power entities you want, then list them in `collectors.yaml` (or the GUI).

## Rollback

Stop the container and repoint the devices back at the host server's built-in syslog server (most devices send to only one target, so there is no automatic second copy). That is why the old server stays configured, idle, for the first week. Remove `appdata/homelab-brief` only when you are sure you no longer want the history.
