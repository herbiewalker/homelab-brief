# Deploying to the host server

You run these steps yourself, one at a time. Claude Code prepares the image, template, and files; it does not deploy. Verify each pass with the checklist in `docs/guide/05-operations-and-verification.md`.

## Before you start

- Local tests pass and the dev stack runs against sanitized samples.
- The image is built and pushed to the private registry with a versioned tag (Task 17; until then, build on the host server from the repo).
- Pick a network mode (own IP on `br0` is recommended; host and bridge also work, see the Network page table in `docs/guide/08-admin-and-access.md`). For own IP, pick a free LAN IP on the host server's network.
- In own-IP mode, decide on **host access to custom networks** (Settings, Docker, advanced view). It must be enabled for the host server itself to send syslog to the container. Host and bridge modes do not need it.
- A broker is reachable: the temporary Mosquitto container now, HA's Mosquitto add-on later.
- Exclude `homelab-brief` and Mosquitto from CA Docker Auto Update (it runs nightly).

## Pass 1: collect and baseline (after Task 7)

1. **Folders.** Create `/mnt/user/appdata/homelab-brief/config`, `/mnt/<pool>/appdata/homelab-brief/state`, `/mnt/<pool>/syslog-archive`, and the share folder `<share>/brief`.
2. **Config.** Copy `brief/` and `vector/` from the repo into `config/`. Create the real `config/brief/systems.yaml` from the audit (it exists locally, gitignored). Later also `devices.yaml` and `topology.yaml`.
3. **Install.** Add the container from the template (or the compose reference) in the chosen network mode, with the volumes above and the template variables (MQTT, display timezone). Leave the read-only `/import` mount in place for now.
4. **Admin login.** Run `docker exec -it homelab-brief brief admin set-password`, then open `https://<advertised-address>/admin` (in host mode, the HTTPS port the template sets) and accept the self-signed certificate.
5. **Backfill.** Run `docker exec -it homelab-brief brief import --from /import`, check the Sources page shows history for all of your sources, then remove the `/import` mount.
6. **Network check.** Open the admin GUI's Network page: confirm the detected mode and advertised address, run the syslog test from each Unraid server, and fix any warning it shows.
7. **Repoint sources.** Point every UniFi device (and Protect, if separate), and each Unraid server's remote syslog, at the address and port the Network page shows. Confirm every source shows a line from the last few minutes.
8. **Leave it learning.** The brief shows the learning banner until enough live data exists.

## Pass 2: full product (after Task 17)

9. **Upgrade** to the release tag. The container migrates config and keeps a backup of the old one.
10. **Outputs.** Confirm MQTT sensors in HA (or with `mosquitto_sub`), the status board on the oldest tablet, `/metrics`, and `/api/v1/status`.
11. **Watchdog.** Add the hourly stale-file User Script from the operations guide.
12. **Optional SSH.** Set `SSH_ENABLED=true` and add your public key to `config/ssh/authorized_keys`.
13. **Retire the old receiver.** After a week of clean collection, turn off the host server's built-in syslog server. The old files stay as they are.

## Phase 2 additions (any order)

- Create read-only API keys on each Unraid server and in UniFi Network, add them as template variables, enable the collectors on the Collectors page, and use "Test connection".
- In Home Assistant, enable `mqtt_statestream` with base topic `homelab/ha` for the power entities you want, then list them in `collectors.yaml` (or the GUI).

## Rollback

Stop the container and repoint the devices back at the host server's built-in syslog server (most devices send to only one target, so there is no automatic second copy). That is why the old server stays configured, idle, for the first week. Remove `appdata/homelab-brief` only when you are sure you no longer want the history.
