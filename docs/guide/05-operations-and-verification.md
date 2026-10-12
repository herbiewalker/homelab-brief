# Operations, verification, and troubleshooting

## Efficiency settings

- Keep the database, Drain3 state, event files, and raw archive on the pool so array disks can spin down. After a day of running, check on the Main tab that they actually do; if not, a file activity plugin will show which process keeps writing.
- Vector streams lines as they arrive and the worker flushes once per cycle, so there are no per-line writes to the database.
- The Unraid template sets a memory limit for the container and `--log-opt max-size=10m --log-opt max-file=2` under Extra Parameters so container logs cannot grow without bound.
- Only the heartbeat metrics run on a short interval. Everything else is hourly or daily.
- Exclude `homelab-brief` (and the temporary Mosquitto container) from CA Docker Auto Update, which runs nightly on these servers. Upgrades happen deliberately by changing the pinned tag.

## Retention

| Data | Keep | Why |
| --- | --- | --- |
| Raw archive (compressed daily files) | 60 days to start | Replay source; the decision is still open (30, 60, or 90); set in `brief/retention.yaml` or the admin GUI |
| Normalized event files | About 7 days | Rebuildable from raw logs |
| `hourly`, `signature`, `host` tables | Indefinitely | A few MB per year; this is the long-term baseline |
| `sample` rows | Last 5 per signature | Quoting in the brief |
| History exports | 30 days | Regenerated on request |

## Backups and code

- Back up the appdata folder (`config`, `state`) and the archive. `brief backup create` (or the Backups page) produces a single file. Losing `drain3.bin` makes every message look new.
- Keep your real `systems.yaml`, `devices.yaml`, and `topology.yaml` in appdata and in your own private instance folder, never in the public repository. Never commit secrets.

## Security

- Mosquitto requires a login and uses the ACL above; port 1883 stays LAN-only.
- Secrets are Unraid template variables (or a mode-600 env file in appdata), never inside a Filesystem MCP folder.
- The admin GUI requires a login over HTTPS, keeps an audit log, and has no default password. See `08-admin-and-access.md`.
- The container is LAN-only and not routed through any Cloudflare tunnel or public reverse proxy. Remote administration goes through a VPN.
- The optional SSH server is off by default and key-only when enabled.
- Logs and digests contain device names, IPs, and MACs, and `sample` lines can contain raw messages. If you later send the digest to an external API, send findings and counts, not raw sample lines, or redact first.

## Self-monitoring

The container reports its own health as registry metrics (ingest lag, events per day, receive rate per source, parse failures, database size, memory) and through Docker's health check, which Unraid shows on the Docker page. Two independent checks catch a dead pipeline: HA's sensor expiry, and an Unraid-side watchdog that does not depend on HA or the container. Add this User Script on the host server, scheduled hourly:

```bash
#!/bin/bash
# Warn if the status file has not been refreshed in 3 hours.
F=/mnt/user/<share>/brief/latest/status.json
AGE=999999
[ -f "$F" ] && AGE=$(( $(date +%s) - $(stat -c %Y "$F") ))
if [ "$AGE" -gt 10800 ]; then
  /usr/local/emhttp/webGui/scripts/notify -e "Homelab brief" -s "Brief is stale" \
    -d "status.json is older than 3 hours" -i "warning"
fi
```

## Updates

Before changing the pinned image tag, read its release notes and run `brief config validate` with the new image against your config (the GUI does this on the upgrade page). Then swap the tag and watch the first hour on the Overview page. Keep the previous tag noted so you can roll back; config migrations keep a backup of the old config.

## Verification checklist

Work through it in order. Each step's gate must pass before the next one depends on it.

**Step 1: Collect**

- [ ] Every expected source shows a recent line on the admin GUI's Sources page.
- [ ] The archive holds one file per source per day, and yesterday's files are compressed.
- [ ] The backfill import appears as history.
- [ ] The Filesystem MCP can list `\\<host>\<share>\brief`.

**Step 2: Baseline**

- [ ] `vector validate` passes and event files appear under `/events` within a minute.
- [ ] The parse-failure rate is low, and the failures are a known format such as CEF.
- [ ] `hourly` rows grow each hour, and the signature count is in the tens to hundreds.
- [ ] A host you silence on purpose shows a stale `last_seen`.

**Step 3: Brief**

- [ ] A full run writes `status.json`, both digests, the host files, `brief.html`, and `status.png`.
- [ ] Stopping the ingest loop produces "no data today", not "all quiet".
- [ ] Each ignore rule suppresses its known-noisy source and shows up in the `suppressed` counters.
- [ ] The learning banner shows for the first 7 days and then clears.
- [ ] `brief replay` over the last week shows the findings and pushes you would expect.
- [ ] An unchanged open finding pushes once, not every hour; tapping "acknowledge" in Home Assistant stops repeats.

**Step 4: Publish**

- [ ] `mosquitto_sub` as the `ha` user shows retained sensor states.
- [ ] Stopping `brief` flips `availability` to `offline`.
- [ ] `status.png` loads on the oldest tablet.
- [ ] The stale-file watchdog sends an Unraid notification when you pause the pipeline for over 3 hours.

**Soak test:** run for 7 days with the learning banner on, tune the ignore list daily, then review which findings you would actually have wanted.

## Troubleshooting

| Symptom | Likely cause | Check |
| --- | --- | --- |
| No event files appear | Mount path or `include` glob does not match the real file names | `docker logs vector`, then `vector validate` |
| Nearly everything has `parse_failed` | Lines are not plain syslog, or the header differs (CEF, custom format) | Compare the Step 1 samples with the VRL |
| Thousands of signatures | Unique tokens (UUIDs, hashes, session IDs) are not masked | Add masks to `drain3.ini`, lower `sim_th`, restart with a fresh `drain3.bin` |
| `database is locked` errors | Database sits on `/mnt/user`, or two writers | Move it to the pool path; keep a single writer |
| Sensors missing in HA | Discovery not retained, wrong prefix, or the ACL blocks `homeassistant/#` | `mosquitto_sub -t 'homeassistant/#' -v` |
| Sensors stuck unavailable | `expire_after` shorter than the publish interval, or `availability` is `offline` | Compare interval and expiry; check the availability topic |
| Claude cannot read the digest | New folder not registered, or the Filesystem MCP dropped after idle | `list_allowed_directories`; if it fails, fully restart Claude Desktop |
| Array disks never spin down | Live logs still land on the array, or another process is writing | Share settings; a file activity plugin |
| Pipeline behaves differently after a week | An auto-updater changed an image tag | Pin tags and exclude these containers from CA Auto Update |
| Scripts fail on Unraid with `\r` or "bad interpreter" errors | Windows line endings | `.gitattributes` forces LF; re-checkout the file |
| All of yesterday's events land in one hour | Bucketing by read time, or a timestamp that failed to parse | Check the share of events flagged with fallback time; check `source_tz` in `systems.yaml` |
| The same alert arrives every hour | Alert lifecycle not applied, or the finding id is not stable | Check the `alert` table and how the finding id is built |
| A config edit seems ignored | The edit failed validation and the last good config is in use | Look for a `config_error` finding in the brief |
| One source never arrives | Device still points at the old receiver, or a firewall blocks UDP/TCP 514 to the container IP | Sources page; the device's remote syslog setting; send a test with `logger -n <container-ip> -P 514 test` |
| The host server's own logs never arrive | Host access to custom networks is disabled, so the host cannot reach its own container's IP | Docker settings on the host server |
| Admin GUI shows a certificate warning | Self-signed certificate | Expected on first use; install your own certificate or trust this one |
| Locked out of the admin GUI | Forgotten password | `docker exec -it homelab-brief brief admin set-password` |
