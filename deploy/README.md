# Deploying to the host server

You run these steps yourself, one at a time. Claude Code prepares the image, template, and files; it does not deploy. Verify each pass with the checklist in `docs/guide/05-operations-and-verification.md`.

## Before you start

- Local tests pass and the dev stack runs against sanitized samples.
- The image exists with a versioned tag. Until the private registry exists (Task 17), build it on a machine with Docker and copy it over (see "Get the image" below).
- Pick a network mode (own IP on `br0` is recommended; host and bridge also work, see the Network page table in `docs/guide/08-admin-and-access.md`). For own IP, pick a free LAN IP on the host server's network.
- In own-IP mode, decide on **host access to custom networks** (Settings, Docker, advanced view). It must be enabled for the host server itself to send syslog to the container. Host and bridge modes do not need it.
- A broker is reachable: the temporary Mosquitto container now (`mosquitto/` in the repo has its config and ACL), HA's Mosquitto add-on later. Create its `brief` user first (see `docs/guide/04-publish.md`, section 4a).
- Exclude `homelab-brief` and Mosquitto from CA Docker Auto Update (it runs nightly).

## Get the image

From a machine with Docker, in the repository:

```bash
docker build --target runtime -t homelab-brief:<version> .
docker save homelab-brief:<version> | ssh root@<host> docker load
```

Use the tag in the compose file or template instead of the registry name, and pin it (never `latest`).

## Pass 1: collect and baseline (after Task 7)

1. **Folders.** Create `/mnt/user/appdata/homelab-brief/config/brief`, `/mnt/<pool>/appdata/homelab-brief/state`, `/mnt/<pool>/syslog-archive`, and the share folder `<share>/brief`.
2. **Config.** Put only your own files in `config/brief/`: the real `systems.yaml` (made from your audit; from `brief/systems.example.yaml`), and `network.yaml` if you want an allowlist of sender subnets or non-default ports. The image ships every other default, so do not copy the whole `brief/` folder (a copy would stay behind when you upgrade).
3. **Install.** Add the container from the template (or the compose reference) in the chosen network mode, with the volumes above. Set these variables (in the template, or in the `env_file` in appdata, mode 600): `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASSWORD` (leave `MQTT_HOST` empty to run without MQTT). Keep the read-only `/import` mount for step 7.
4. **First start.** Check `docker logs homelab-brief`: you should see `worker_started`, `web_starting` for both ports, a `tls` line, and a `network` line. The `network` line shows the mode and address the container detected, the address to point devices at (`send_syslog_to`), and any warnings (the same block is in `/status.json`). With DHCP the address can change after a restart, so reserve it in your router or pin it in the template once it works. Then open `http://<container-ip>/healthz` (expect `"status": "ok"`). Set the admin password with `docker exec -it homelab-brief brief admin set-password`.
5. **Test the path.** From each Unraid server, send one test line and look for it in the log within a minute (`ingest_cycle` with `events: 1`, or the count in `/status.json`):

   ```bash
   logger -n <container-ip> -P 514 "homelab-brief test line"
   ```

   If the host server itself cannot reach the container, enable host access to custom networks (see above).
6. **Repoint sources.** Point every UniFi device (and Protect, if separate), and each Unraid server's remote syslog, at `<container-ip>` port 514. Most devices send to one target only, so the old server's files stop growing as each one moves. Open `http://<container-ip>/status.json`: `events_24h` should climb, and `hosts_silent` stays 0 while every system keeps sending (until typical gaps are learned in Task 8, a quiet host is reported only after 24 hours).
7. **Backfill.** Once every source is repointed, the old `syslog-<ip>.log` files are final. Import them with `docker exec -it homelab-brief brief import --from /import`. It prints progress, can take a while for a large archive, and refuses to run a second time (that would count every line twice). The worker reads the imported events in the background, bucketed by their own event time. When `ingest_cycle` lines stop appearing in the log, remove the `/import` mount.
8. **Check the sensors.** In Home Assistant, or with `mosquitto_sub -u ha -P <password> -t 'homelab/#' -t 'homeassistant/#' -v`: three retained sensors (`events_24h`, `new_signatures_24h`, `hosts_silent`) and `homelab/brief/availability` showing `online`. Stopping the container flips availability to `offline`. The sensors refresh every hour.
9. **Leave it learning.** `/status.json` shows `learning.active: true` for the first live week, and live baselines grow from the first real line (an import does not start the learning period).

`/status.json` is also written hourly to `<share>/brief/latest/status.json`. If `run_ok` is `false`, its headline says what is wrong, and the MQTT sensors stop updating until it is fixed (they go unavailable after about two hours instead of showing a calm network).

## Pass 2: full product (after Task 17)

10. **Upgrade** to the release tag. The container migrates config and keeps a backup of the old one.
11. **Outputs.** Confirm MQTT sensors in HA (or with `mosquitto_sub`), the status board on the oldest tablet, `/metrics`, and `/api/v1/status`.
12. **Watchdog.** Add the hourly stale-file User Script from the operations guide.
13. **Optional SSH.** Set `SSH_ENABLED=true` and add your public key to `config/ssh/authorized_keys`.
14. **Retire the old receiver.** After a week of clean collection, turn off the host server's built-in syslog server. The old files stay as they are.

## Phase 2 additions (any order)

- Create read-only API keys on each Unraid server and in UniFi Network, add them as template variables, enable the collectors on the Collectors page, and use "Test connection".
- In Home Assistant, enable `mqtt_statestream` with base topic `homelab/ha` for the power entities you want, then list them in `collectors.yaml` (or the GUI).

## Rollback

Stop the container and repoint the devices back at the host server's built-in syslog server (most devices send to only one target, so there is no automatic second copy). That is why the old server stays configured, idle, for the first week. Remove `appdata/homelab-brief` only when you are sure you no longer want the history.
