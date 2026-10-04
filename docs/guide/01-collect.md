# Step 1: Collect

Goal: every device sends syslog straight to the container, the container keeps a compressed daily raw archive, and any existing history is imported once. The container no longer depends on another server's syslog service, which is what failed on 2026-09-26.

## 1a. Receiver

Vector inside the container listens on UDP and TCP 514 on the container's own IP, using raw `socket` sources so the full wire format (including the priority field) is kept. The sender's IP becomes the host, matching `brief/systems.yaml`.

Syslog arriving over the network carries the priority field that the old file archive dropped, so most lines get a real severity instead of a keyword guess.

## 1b. Raw archive

Vector writes each received line to `/archive/<YYYY-MM-DD>/<source-ip>.log`. After each day ends, the worker compresses the previous day's files and deletes days past the retention window in `brief/retention.yaml` (or the retention section of the admin GUI). The archive is the source of truth: any day can be replayed.

Keep the archive on the pool if you want array disks to stay spun down; nothing in the archive is written to the array unless you choose that path.

## 1c. Backfill import

The old files at `\\<host>\<share>\syslog` (one ever-growing `syslog-<ip>.log` per source) are imported once:

1. Mount the old folder read-only into the container (for example at `/import`).
2. Run `brief import --from /import` (a one-off Vector config reads the files and feeds the same archive and event stream).
3. Because counts are bucketed by event time, imported lines land in their own days and hours, and the baselines start with weeks of history instead of none.
4. Remove the mount afterwards. The old files are never modified.

## 1d. Repoint every source

After the container is healthy, change each device's remote syslog target to the container's IP, port 514:

- **UniFi Network** (gateways, switches, APs, PDU): the remote syslog setting in the UniFi Network application (its location varies by version). Use UDP or TCP 514 to the container IP.
- **UniFi Protect** (the NVR's CEF events): its own syslog or SIEM setting, if separate in your version.
- **Unraid servers** (every one): Settings, then Syslog Server, then remote syslog server set to the container IP. The host server reaching its own container needs "host access to custom networks" enabled.
- **Future devices** (Home Assistant, others): same target.

Then confirm in the admin GUI's Sources page that all of your sources report a recent line. Keep the host server's old syslog server configured but idle for a week, so you can repoint devices back quickly if needed (most devices send to only one target), then turn it off.

## 1e. What the audit found (format notes)

Recorded in `docs/DECISIONS.md` (sanitized) and `docs/private/step1-audit.md`:

- Gateways repeat the hostname; switches, APs, and the PDU add `<mac>,<model>-<firmware>`; the NVR sends CEF with epoch-millisecond timestamps; gateway coredns lines are JSON with full timestamps.
- Headers have no year; device clocks are US Eastern.

**Done when:** every expected source shows a line from the last hour on the Sources page, the archive holds one file per source per day, yesterday's files are compressed, and the backfill appears as history in the baselines.
