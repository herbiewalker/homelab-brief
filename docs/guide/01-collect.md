# Step 1: Collect

Goal: every device sends syslog straight to the container, the container keeps a compressed daily raw archive, and any existing history is imported once. The container no longer depends on another server's syslog service.

## 1a. Receiver

Vector inside the container listens on UDP and TCP 514 on the container's own IP, using raw `socket` sources so the full wire format (including the priority field) is kept. The sender's IP becomes the host, matching `brief/systems.yaml`.

Syslog arriving over the network carries the priority field that the old file archive dropped, so most lines get a real severity instead of a keyword guess.

**Sender allowlist.** Syslog over UDP can be spoofed by anything on the LAN, so set `allowed_senders` (addresses or subnets) and `unknown_senders` (`flag` or `drop`) in `brief/network.yaml`. Flagged lines are kept with `unknown_sender: true`; dropped ones are discarded before anything is written. An empty list allows everyone.

## 1b. Raw archive

Vector writes each received line to `/archive/<YYYY-MM-DD>/<source-ip>.log`. After each UTC day ends, `python -m brief.archive compress --apply` gzips that day's files (it is a dry run without `--apply`, skips files still being written, and removes an original only after reading the compressed copy back). The scheduler runs it daily (Task 14), and deleting days past the retention window in `brief/retention.yaml` follows. The archive is the source of truth: any day can be replayed.

Keep the archive on the pool if you want array disks to stay spun down; nothing in the archive is written to the array unless you choose that path.

## 1c. Backfill import

The old files at `\\<host>\<share>\syslog` (one ever-growing `syslog-<ip>.log` per source) are imported once:

1. Mount the old folder read-only into the container (for example at `/import`).
2. Run `brief import --from /import` (a one-off Vector config reads the files and feeds the same archive and event stream).
3. Because counts are bucketed by event time, imported lines land in their own days and hours, and the baselines start with weeks of history instead of none.
4. Remove the mount afterwards. The old files are never modified.

`brief import` runs a one-off Vector config over the files. Events go to `/state/events/import.ndjson.partial`, which the worker ignores, and are renamed to `import.ndjson` (every event has `imported: true` and an inferred severity) only once the line count checks out against the source, so a killed or cut-off import counts nothing and can simply be run again. The raw lines go to `/archive/import/`. Give the container about 1 GB of memory for a large import (the import Vector uses about 225 MB on top of the running services), then lower it again.

## 1d. Repoint every source

After the container is healthy, change each device's remote syslog target to the container's IP, port 514:

- **UniFi Network** (gateways, switches, APs, PDU): the remote syslog setting in the UniFi Network application (its location varies by version). Use UDP or TCP 514 to the container IP.
- **UniFi Protect** (the NVR's CEF events): its own syslog or SIEM setting, if separate in your version.
- **Unraid servers** (every one): Settings, then Syslog Server, then remote syslog server set to the container IP. The host server reaching its own container needs "host access to custom networks" enabled.
- **Future devices** (Home Assistant, others): same target.

Then confirm in the admin GUI's Sources page that all of your sources report a recent line. Keep the host server's old syslog server configured but idle for a week, so you can repoint devices back quickly if needed (most devices send to only one target), then turn it off.

## 1e. Format notes

What real UniFi and Unraid logs look like is recorded in `docs/DECISIONS.md` ("What real logs look like"), and the parser that handles them is `vector/parse.vrl` (fields in `docs/guide/02-baseline.md`). Headers have no year or timezone; set `source_tz` per system in `systems.yaml`.

**Done when:** every expected source shows a line from the last hour on the Sources page, the archive holds one file per source per day, yesterday's files are compressed, and the backfill appears as history in the baselines.
