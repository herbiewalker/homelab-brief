# Step 2: Baseline

Goal: received lines become normalized events, events become stable signatures, and signatures become hourly counts with first-seen and last-seen history. Everything here is idle-friendly: Vector streams lines as they arrive, and the worker wakes about once a minute to ingest new events.

## 2a. Vector: receive, archive, normalize

The repo file `vector/vector.yaml` is authoritative; this sketch shows the shape. Confirm field names against the docs for the pinned Vector version.

The authoritative files are `vector/vector.yaml` (receive, gate, archive, events), `vector/parse.vrl` (the parser, shared with `vector/backfill.yaml`), and the unit tests in `vector/tests/`. They ship in the image at `/app/vector/`. When the container starts, `python -m brief.vectorcfg` renders the listen port and the sender allowlist from `brief/network.yaml` into `/run/vector/vector.yaml`.

Flow: the UDP and TCP sources go to a **gate** (it flags or drops senders outside `allowed_senders`), then to the **archive** (the raw line, one file per sender per UTC day) and to the **parser** (a normalized event), which writes **events** as NDJSON, one file per day.

### Event fields

| Field | Meaning |
| --- | --- |
| `source_ip` | The sender's address (live) or the address in the file name (backfill). This is the host identity |
| `timestamp` | When Vector received the line (UTC). For operations only; never used for bucketing |
| `format` | `bsd` or `rfc5424` |
| `ts` | The header time exactly as sent ("Sep 26 13:22:33"), with no year or timezone; Python normalizes it |
| `epoch_ms` | Precise UTC time in milliseconds when the line carries one (CEF `timestamp=`, coredns JSON, a UniFi OS ISO prefix) |
| `pri`, `facility`, `severity` | From the priority field when present. `severity` is the syslog level 0-7 |
| `severity_inferred` | `false` when the severity came from the wire, `true` when it came from keywords or CEF |
| `header_host`, `header_host_dup` | The host named in the header; `header_host_dup` is true when a gateway repeated it |
| `device_mac`, `model`, `firmware` | From the `<mac>,<model>-<firmware>:` token that switches, APs, and the PDU add |
| `app`, `pid`, `msg` | Program tag, process id (a string), and the message |
| `cef` | The parsed CEF object (Protect, Network, OS); `app` is the product name |
| `dns` | The parsed coredns JSON object |
| `parse_failed`, `raw` | Set when a line could not be parsed; the raw text is kept and nothing is dropped |
| `unknown_sender` | Set to true by the gate for a sender outside `allowed_senders` (policy `flag`) |
| `imported` | True for events from the backfill import |

Optional fields are absent when they do not apply, never null.

Notes:

- **Host identity** is the sender's IP; `systems.yaml` maps IPs and header names to systems.
- **Sender allowlist:** `allowed_senders` (addresses or subnets) and `unknown_senders` (`flag` or `drop`) in `brief/network.yaml`. An empty list allows everyone. `flag` keeps the line and marks it; `drop` discards it before anything is written. A bad entry stops the container at start with a message, instead of silently weakening the list.
- **Severity:** lines received over the network usually carry the priority field, so severity comes from it. Lines without it (and the backfill, whose files dropped it) use the keyword fallback (crit, error, fail, warn) with `severity_inferred: true`. For CEF lines without a priority the CEF severity is mapped: 9 and up is `err`, 7-8 `warning`, 4-6 `notice`, below that `info`.
- **CEF:** the header can hold spaces and pipes (Protect) or an ISO timestamp (UniFi OS), so the CEF branch starts at `CEF:` and uses its `timestamp=` field (epoch milliseconds, UTC).
- **Event time:** `ts` keeps the header text; classic headers have no year or timezone, so Python normalizes them (2d) using `source_tz` from `systems.yaml`. Counts are bucketed by event time, never by receive time.
- **Firmware:** the `<model>-<firmware>` token becomes fields; Phase 2 inventory uses it as a fallback source.
- Keep normalized event files for about 7 days. The archive is the source of truth.

## 2b. Baseline database

SQLite in WAL mode at `/state/baseline.db` (pool path, not `/mnt/user`). No server, almost no memory, and exporting history is a plain query.

The schema is the numbered SQL files in `brief/src/brief/migrations/` (`0001_baseline.sql` is the starting point), applied by `brief.store`. Phase 2 tables arrive as later migrations.

| Table | Holds |
| --- | --- |
| `signature` | One row per message template: our `sig_id`, the Drain3 identity `(miner_generation, cluster_id)`, `app`, `template`, `first_seen`, `last_seen`, `prev_last_seen`, `ignored` |
| `host` | One row per sender: `system_id`, `first_seen`, `last_seen`, `expected_gap_min` |
| `hourly` | Event counts by event hour: `(day, hour, host, sig_id, severity) -> n` |
| `sample` | The newest few raw lines per signature |
| `metric` | Numeric series from collectors and self-monitoring |
| `audit_log` | Every admin change |
| `alert`, `feedback` | The notification lifecycle and acknowledgements (see `03-brief.md`) |
| `meta` | The program's own state, such as `miner_generation` |

How the store behaves:

- **Migrations:** numbered files, each applied in one transaction (`PRAGMA user_version` records the level). Before upgrading an existing database a copy is saved as `baseline.db.pre-v<N>`, and a database written by a newer program is refused instead of touched.
- **Writes:** one writer at a time. `Store.write()` serializes threads and opens one transaction per cycle, so a whole ingest cycle commits together or not at all (no per-line writes). The worker and the web server each hold their own `Store` and SQLite arbitrates between them (5 second busy timeout). Readers use read-only connections, which WAL lets run alongside the writer.
- **Times** are UTC text such as `2026-09-26T13:22:33Z`, so they sort as strings.
- **`sig_id` is ours,** not Drain3's. `(miner_generation, cluster_id)` maps to a `sig_id` that is never reused. When `drain3.bin` is lost, `bump_miner_generation()` starts a new generation, so the new miner's cluster ids cannot collide with history.
- **`first_seen` and `last_seen` follow event time:** `first_seen` only moves earlier (a backfill), `last_seen` only later.
- **`prev_last_seen`** keeps the `last_seen` from before the most recent quiet gap (24 hours by default), which is what "returned after N quiet days" needs. It is not touched while activity continues.
- **`hourly` counts accumulate** on the same key, so a flush adds to what is there.

## 2c. Signatures with Drain3

Each message is mined as `<app>: <message>` so templates stay app-specific. The code is `brief/src/brief/signatures.py`; `brief/drain3.ini` holds the masking rules (IPs, MACs, numbers), and masking is what turns thousands of near-duplicate lines into one signature.

- Drain3 saves its state to `/state/drain3.bin` on every new cluster or template change, so the miner is always at least as new as the database.
- Drain3's cluster id maps to our own `sig_id` through `(miner_generation, cluster_id)` (see 2b). If `drain3.bin` is lost or unreadable at start, the pipeline starts a new `miner_generation` and records it in the audit log, so new clusters cannot collide with history.
- An exact-match cache in front of the miner makes a storm of identical lines cheap, and very long lines are trimmed before mining.
- Update `signature.template` when the miner changes a template.

## 2d. Ingest loop

The code is `brief/src/brief/ingest.py`, run by the worker (`brief/src/brief/worker.py`) every 60 seconds (`BRIEF_INGEST_INTERVAL`), and straight away again while there is a backlog.

1. List the event files each cycle (a new day and the backfill appear on their own) and read each from its stored offset. Offsets live in the `ingest_offset` table and commit in the same transaction as the counts they produced, so a crash can neither double count nor lose a line, and re-running a cycle over the same files counts nothing twice. Only complete lines are consumed; a half-written last line waits. A file that shrank is read again from the start. A byte budget per cycle (32 MiB) spreads a large backlog over cycles.
2. Normalize each event's time to UTC (`brief/src/brief/timeparse.py`): a precise `epoch_ms` when the line has one; otherwise the header time read in the host's `source_tz` with the year inferred (a time more than a day ahead of the receive time belongs to the previous year); otherwise the receive time, which is counted as a fallback.
3. Map the host to its system (`brief/src/brief/systems.py`). Unknown hosts roll up to `default_system` with `host.known = 0` and appear on the admin GUI's Sources page.
4. Run each event through the ignore list (`brief/src/brief/ignore.py`), then the miner. An ignored event is counted per rule in `suppressed`; an event whose signature is marked ignored is counted as `signature:<id>`. Neither goes into `hourly`, but both refresh the host's last-seen time.
5. Accumulate counts in memory by event hour and flush in one transaction per cycle: signatures (first, last, and previous last seen, fed one UTC day at a time, oldest first), `hourly`, `suppressed`, `host`, a few `sample` rows, the offsets, and a few `ingest.*` metrics (events, backlog bytes, cycle seconds, lag).
6. Config is reloaded every cycle. A broken `systems.yaml` or `ignore.yaml` keeps the last good version and logs a `config_error`. Default config files ship in the image (`/app/defaults/brief`); a file in `/config/brief` overrides its default.

## 2e. Learning period and backfill

- **Learning period:** for the first 7 days of live data, collect baselines but suppress novelty and deviation findings. The brief shows "learning, day N of 7". The clock starts at the first live event (`meta.live_since`), not at a backfill, and the length is `thresholds.learning.days` in `playbook.yaml`; with the backfill imported it can be shortened (`brief/src/brief/learning.py`).
- **Backfill:** see `01-collect.md`, section 1c. Imported lines land in their own hours because counts use event time.
- **Early deploy:** deploy as soon as ingest works locally (Task 7), so real history accumulates while the rest is built.

**Done when:** `hourly` fills each hour, `signature` shows a plausible number of templates (tens to a few hundred, not tens of thousands), and a source you silence on purpose shows a stale `last_seen`.
