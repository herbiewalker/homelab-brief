# Homelab brief for 2026-09-20

Generated 2026-09-20T06:00:00Z (UTC). Run ok.

**5 things need a look; first: 198.51.100.3 has been silent for 2 days**

## Pipeline
- 798 events in 24 hours; 1 new signature
- 0 parse failures; ingest lag 0 s
- 1 silent host

## Systems

| System | State | Summary |
| --- | --- | --- |
| Gateway | yellow | 1 deviation |
| Storage server | yellow | 2 novelty, 1 off_hours |
| Camera | RED | 1 absence |

## Stories (5, most important first)

1. [push_now, rank 80] 198.51.100.3 has been silent for 2 days (cam)
   - silent minutes: 3351
   - last seen: 2026-09-17T22:09:00Z
   - detail: hosts/198.51.100.3.md
2. [digest_only, rank 55] New: kernel: blk_update_request: I/O error, dev sdb, sector <NUM> (nas)
   - state: new
   - worst severity: err
   - detail: hosts/198.51.100.2.md
3. [digest_only, rank 50] firewall: DROP IN=eth0 SRC=<IP> DST=<IP> LEN=<NUM>: 700 in 24h, usually about 100 (gw)
   - today: 700
   - median: 100
   - detail: hosts/198.51.100.1.md
4. [digest_only, rank 45] Returned after a quiet spell: rsyslogd: action 'omfwd' resumed after <NUM> seconds (nas)
   - state: returned
   - worst severity: warning
   - detail: hosts/198.51.100.2.md
5. [digest_only, rank 40] cron: job backup started at 14:00 UTC, an unusual hour (nas)
   - hour utc: 14
   - detail: hosts/198.51.100.2.md

## Quiet

- 3 hosts checked, 0 clean
