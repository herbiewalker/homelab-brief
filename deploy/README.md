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

- Create read-only API keys on each Unraid server and in UniFi Network, add them as template variables, enable the collectors on the Collectors page, and use "Test connection".
- In Home Assistant, enable `mqtt_statestream` with base topic `homelab/ha` for the power entities you want, then list them in `collectors.yaml` (or the GUI).

## Rollback

Stop the container and repoint the devices back at the host server's built-in syslog server (most devices send to only one target, so there is no automatic second copy). That is why the old server stays configured, idle, for the first week. Remove `appdata/homelab-brief` only when you are sure you no longer want the history.
