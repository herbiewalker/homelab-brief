# Weekly report 2026-W38

Mon 2026-09-14 to Sun 2026-09-20 (UTC), against the week before.

## Volume by system

| System | This week | Last week | Change |
| --- | ---: | ---: | ---: |
| Gateway | 1,386 | 889 | +56% |
| Storage server | 185 | 175 | +6% |
| Camera | 95 | 168 | -43% |

1,666 counted events this week; 0 parse failures.

## Biggest movers

- firewall: DROP IN=eth0 SRC=<IP> DST=<IP> LEN=<NUM> (198.51.100.1): 1,200 vs 700 (+71%)
- streamer: frame ok <NUM> (198.51.100.3): 95 vs 168 (-43%)
- kernel: blk_update_request: I/O error, dev sdb, sector <NUM> (198.51.100.2): 5 vs 0 (new)
- cron: job backup started (198.51.100.2): 11 vs 7 (+57%)
- sshd: Accepted publickey for admin from <IP> port <NUM> (198.51.100.1): 18 vs 21 (-14%)
- rsyslogd: action 'omfwd' resumed after <NUM> seconds (198.51.100.2): 1 vs 0 (new)

## New this week

- kernel: blk_update_request: I/O error, dev sdb, sector <NUM>: 5 events

## Went quiet (active on 3 or more days last week, nothing this week)

- nothing

## Recurring issues (open on 3 or more days)

- none

## Hosts that went silent

- 198.51.100.3 has been silent for 3 h (from Fri 02:00 UTC)

Per-host detail pages are in each day's folder, under hosts/.
