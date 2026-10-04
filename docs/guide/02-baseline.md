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

```sql
CREATE TABLE signature (
  sig_id INTEGER PRIMARY KEY AUTOINCREMENT,      -- our own id, never Drain3's
  miner_generation INTEGER, cluster_id INTEGER,  -- Drain3 identity within one miner state
  app TEXT, template TEXT,
  first_seen TEXT, last_seen TEXT,
  prev_last_seen TEXT,                           -- for "returned after N quiet days"
  ignored INTEGER DEFAULT 0,
  UNIQUE (miner_generation, cluster_id)
);
CREATE TABLE host (
  host TEXT PRIMARY KEY, system_id TEXT,
  first_seen TEXT, last_seen TEXT,
  expected_gap_min INTEGER                       -- learned typical gap between lines
);
CREATE TABLE hourly (
  day TEXT, hour INTEGER, host TEXT, sig_id INTEGER, severity TEXT,
  n INTEGER,
  PRIMARY KEY (day, hour, host, sig_id, severity)
);
CREATE TABLE sample (sig_id INTEGER, ts TEXT, host TEXT, msg TEXT);   -- last few lines per signature
CREATE TABLE metric (ts TEXT, metric_id TEXT, value REAL);           -- numeric series
CREATE TABLE audit_log (ts TEXT, user TEXT, action TEXT, detail TEXT); -- admin changes
CREATE INDEX hourly_sig ON hourly (sig_id, day);
```

The alert, feedback, and Phase 2 tables are defined in `03-brief.md`, `06-phase2-device-intelligence.md`, and `07-network-map.md`.

## 2c. Signatures with Drain3

Prefix each message with its app name so templates stay app-specific, and persist the miner state so signatures survive restarts.

```python
from drain3 import TemplateMiner
from drain3.file_persistence import FilePersistence
from drain3.template_miner_config import TemplateMinerConfig

cfg = TemplateMinerConfig()
cfg.load("/config/drain3.ini")
miner = TemplateMiner(FilePersistence("/state/drain3.bin"), cfg)

result = miner.add_log_message(f"{app}: {msg}")
cluster_id = result["cluster_id"]      # Drain3's id; map to our own sig_id via (miner_generation, cluster_id)
template = result["template_mined"]     # evolves as the miner generalizes
```

`brief/drain3.ini` holds the masking rules (IPs, MACs, numbers). Masking is what turns thousands of near-duplicate lines into one signature. Update `signature.template` when the miner changes a template. If `drain3.bin` is ever lost, increment `miner_generation` so new clusters get new `sig_id` values instead of colliding with history.

## 2d. Ingest loop

1. Every 60 seconds, read new bytes from today's (and yesterday's, near midnight) event file, tracking byte offsets in `/state/offsets.json`. Consume only complete lines.
2. Normalize each event's time to UTC: CEF or JSON timestamps when present; otherwise the header time with the host's `source_tz` and an inferred year (rolled back a year if it lands in the future); receive time as a flagged fallback.
3. Map the host to its system via `systems.yaml`; unknown hosts roll up to `default_system` and appear on the admin GUI's Sources page.
4. Run each event through the ignore list, then the miner.
5. Accumulate counts in memory by event hour and flush to `hourly` in one transaction per cycle, updating `signature.last_seen` (and `prev_last_seen`), `host.last_seen`, and a few `sample` rows.
6. Record ingest lag, events per cycle, and receive rate per source for self-monitoring.

## 2e. Learning period and backfill

- **Learning period:** for the first 7 days of live data, collect baselines but suppress novelty and deviation findings. The brief shows "learning, day N of 7". With the backfill imported, the learning period can be shortened in config.
- **Backfill:** see `01-collect.md`, section 1c. Imported lines land in their own hours because counts use event time.
- **Early deploy:** deploy as soon as ingest works locally (Task 7), so real history accumulates while the rest is built.

**Done when:** `hourly` fills each hour, `signature` shows a plausible number of templates (tens to a few hundred, not tens of thousands), and a source you silence on purpose shows a stale `last_seen`.
