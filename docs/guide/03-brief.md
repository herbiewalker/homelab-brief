# Step 3: Brief

Goal: once a day (and lightly every hour), turn the baseline into ranked findings, write a small set of files that Claude, the web page, and Home Assistant all read, and render the morning brief. The pipeline must work with no LLM at all; the prose layer is an add-on.

## 3a. Detectors

The current values live in `brief/playbook.yaml` (thresholds) and `brief/priorities.yaml` (routing); the table explains what each detector does. Computation stays in Python and SQL, so the daily run is a short job. Hosts roll up to systems (dashboard tiles) through `brief/systems.yaml`.

| Detector | Rule | Starting value |
| --- | --- | --- |
| Novelty | Signature first seen in the last 24h, or seen again after at least 7 quiet days; not ignored | Warning and above become stories; info-level are counted only |
| Deviation | Today's count for a host and signature against the median of its daily totals over the previous 14 days | Flag when count is at least 20 and above median plus 4 times the median absolute deviation (MAD floored at 1) |
| Off-hours | A signature appearing in an hour of day where it has not appeared in the previous 14 days | Count of at least 3 |
| Absence | Host `last_seen` older than its normal gap | Greater of 3 times `expected_gap_min` and 30 minutes, capped at 24h |

Absence runs hourly so it can push in time; the other detectors run in the daily full run. Metric drift moves to Phase 2, because there are no collector metrics before then.

Novelty query (new or returning signatures):

```sql
SELECT s.sig_id, s.app, s.template, s.first_seen, s.prev_last_seen
FROM signature s
WHERE s.ignored = 0
  AND (s.first_seen >= :since
       OR (s.last_seen >= :since AND s.prev_last_seen < :quiet_cutoff));
```

Because Step 2 masks IPs and MACs, a new device or a new outbound destination does not appear as a novel signature. Dedicated extractors for DHCP leases, MACs, and firewall destinations come after the baselines mature, and they feed the same findings list.

## 3b. Ignore list

Save as `brief/ignore.yaml` (the repo has a template whose hosts and patterns match nothing). Replace them after looking at real lines from your samples.

```yaml
schema: 1
ignore:
  - name: example-port-flaps
    host: "<switch-system-id>"
    match: "(?i)link (up|down)"
    note: A device that sleeps and wakes flaps its switch port; this is normal
  - name: example-camera-low-memory
    host: "<camera-hostname>"
    match: "(?i)low memory"
    note: Known camera-local bug; reboot the camera if audio detection pauses
```

A rule applies to events from `host` (a sender IP, a hostname from the log header, or a system id from `systems.yaml`, case-insensitive; omit it to match any host) whose `<app>: <message>` text matches `match` (a regular expression). The first matching rule wins. An ignored event is not mined into a signature or counted in `hourly`, but it still refreshes the host's last-seen time, so a quiet source is not mistaken for a silent one.

Ignored events are still counted in a separate `suppressed` counter per rule, so a suppressed source that suddenly changes volume is visible in the pipeline section of the brief.

## 3c. Output files

Everything below is written to `/out` (`\\<host>\<share>\brief`, registered in the Filesystem MCP) and copied where needed to `/www`.

| File | Purpose | Size rule |
| --- | --- | --- |
| `latest/status.json` | Machine-readable state for HA and dashboards | Small, schema versioned |
| `latest/digest.md` | Ranked findings for Claude | 150 lines or fewer |
| `YYYY-MM-DD/digest.md` | Dated copy | Same |
| `YYYY-MM-DD/hosts/<host>.md` | Per-host detail Claude can drill into | 150 lines or fewer each |
| `state.json` | Developing issues carried between days | Resolved after 3 quiet days |
| `export/<host>-<sig>.csv` | History export for "what's normal?" questions | Written by `brief export` |
| `weekly/YYYY-Www.md` | Weekly trend report: trends, recurring issues, what changed | Written by `brief weekly`; 150 lines or fewer |
| `www/brief.html`, `www/status.png` | Rendered brief and tablet status board | Static files |

Example `status.json` (schema 1, sample values):

```json
{
  "schema": 1,
  "generated_at": "2026-10-03T09:45:02Z",
  "run_ok": true,
  "learning": { "active": false, "day": 8, "of": 7 },
  "headline": "Quiet night; one new signature on the UDM Pro",
  "systems": [
    { "id": "udm", "name": "UDM Pro", "state": "yellow", "summary": "1 new signature" },
    { "id": "server-1", "name": "the host server", "state": "green", "summary": "no findings" }
  ],
  "stories": [
    { "id": "novelty-udm-1042", "rank": 1, "system": "udm", "kind": "novelty",
      "title": "New socket error signature", "detail": "hosts/udm.md" }
  ],
  "developing": [ { "id": "drift-udm-mem", "days": 3, "trend": "same" } ],
  "quiet": [ "14 hosts checked, 11 clean" ],
  "pipeline": { "ingest_lag_s": 42, "events_24h": 183000, "parse_failed_24h": 12, "db_mb": 48 }
}
```

Store times in UTC in files and convert to local time only when rendering. Always write `run_ok: false` and a "no data today" headline when the run fails, never an empty "all quiet".

How the files are made (`brief digest` runs the daily job by hand; the worker runs it once a day at `daily_run_utc` in `brief/schedule.yaml`, and not until the install has received its first event, so an empty new install is not reported as a failed night):

- **Stories, not findings.** Detectors report one finding per signature. The digest merges findings of one kind on one host into one story once there are `grouping.min_members` of them (`priorities.yaml`), so twelve signatures jumping on one gateway read as one line with the biggest three named. A story's rank is its kind's rank plus `severity_boost` for its worst severity, so a new error outranks a routine swing in volume. Info-level findings are counted in the "quiet" lines, not listed.
- **System state:** red when any story is routed `push_now`, yellow for any other story, green for none, `unknown` when the run failed.
- **`state.json`** carries issues from day to day. An issue open on more than one day is "developing" with a trend (growing, shrinking, same). It is dropped after 3 daily runs without it. A failed run leaves it untouched, so a bad night never resolves a real problem, and running the same date twice does not count the day twice.
- **Failure path.** If the run fails, a detector crashes, a pipeline check fails, or no events arrived in 24 hours, the digest is "No data today: <reason>", `run_ok` is false, systems are `unknown`, and nothing is described as quiet. The hourly `status.json` carries the last daily run's stories, and turns into the same failure if that run is older than 36 hours or failed.
- **Host pages** (`YYYY-MM-DD/hosts/<host>.md`) exist for every host with findings: last heard from, events in 24 hours, the findings with their numbers, the busiest signatures, and sample lines for the flagged ones. Every Markdown file is clipped to 150 lines.
- **`brief export --host H --sig ID [--days 30]`** (or `--top N`) writes `export/<host>-<sig>.csv`: one row per day with events, hours active, busiest hour, and worst severity, zeros included. **`brief weekly [--week 2026-W40]`** writes `weekly/YYYY-Www.md`: volume by system against the week before, biggest movers, new signatures, signatures that went quiet, recurring issues (open on 3 or more days, found by replaying the week), and hosts that went silent.

## 3d. Writing the brief

The template brief (Jinja2 over `status.json`, rule-based phrasing) always exists and needs nothing else. An AI rewrite is optional and model-agnostic: `brief/ai.yaml` defines providers (any OpenAI-compatible server, local on the GPU server or hosted, or the native Anthropic API) and a fallback chain per task that always ends in the template. Prompts are plain Markdown in `brief/prompts/`. See `09-ai-integration.md`.

Build template-only first. The AI layer reads the same digest, so adding it later is a configuration change, not a rewrite. The pipeline never waits on AI: on any error, timeout, budget limit, or failed validation, the next provider in the chain is tried and the template is the last resort.

## 3e. Schedule and rendering

- **Hourly light run:** runs the absence detector, updates system states and pipeline health in `status.json`, and publishes MQTT. No rendering.
- **Daily full run** (configurable, for example 05:45): runs detectors, writes digests, renders `brief.html` and `status.png`, updates `state.json`.
- **HTML:** one static file with inline CSS and no JavaScript, so it loads fast on old tablets and e-readers.
- **PNG:** drawn with Pillow using a bundled monospace font, with the output size set in config because tablet resolutions differ. This is far lighter than running a headless browser.
- **PDF/EPUB for the e-reader:** added later, once the e-reader model is known.
- **Weekly run:** writes the weekly trend report.
- **Config reload:** every run reloads and validates the config files, keeps the last good version on error, and raises a `config_error` finding naming the file and problem.

## 3f. Replay and tuning

`brief replay --from <date> --to <date> --dry-run` runs the detectors and routing over stored history with the current config and prints the findings and the pushes that would have been sent, without writing anything. Use it after every change to `playbook.yaml` or `priorities.yaml` to see the effect in minutes instead of waiting days. A golden-day test (a synthetic month of logs through the real pipeline, replayed, against committed snapshots of the replay, the digest, and the weekly report in `brief/tests/golden/`) guards against accidental changes in output; if it fails, read the diff, and if the change is intended regenerate the snapshots with `GOLDEN_UPDATE=1` and commit them with the change.

A replay is as of each past moment, so it reproduces what a live run said, with three limits: absence is as precise as the hour, clock skew (measured live only) is left out, and today's system mapping, ignore rules, and learned host gaps are used. The alert lifecycle below is not applied to a replay yet, so "would push" means "new this run and routed push_now".

## 3g. Alert lifecycle

Every push goes through the lifecycle in `brief/priorities.yaml`, so a lasting problem does not nag:

- **Routing:** rule id, then finding kind, then `default`. `urgent` findings bypass quiet hours and the daily budget.
- **Dedupe:** each finding has a stable id (kind, system or device, signature or attribute). An unchanged open finding pushes at most once per `renotify_hours`.
- **Escalate:** a finding that gets worse (higher bucket, larger count, new host) pushes early.
- **Resolve:** when a pushed finding clears for `resolve_after_quiet_runs` runs, a short "resolved" message is sent.
- **Budget:** non-urgent pushes beyond `daily_push_budget` fold into the next daily headline.
- **Feedback:** Home Assistant notification buttons publish `ack`, `snooze_24h`, or `snooze_7d` to `homelab/brief/feedback/<finding_id>`. Acknowledged or snoozed findings drop to the digest until they get worse.

The `alert` table (`finding_id`, `kind`, `rule`, `level`, `state`, `first_sent`, `last_sent`, `last_value`, `snooze_until`; `state` is open, acked, snoozed, or resolved) and the `feedback` table (`ts`, `finding_id`, `action`) are defined in `brief/src/brief/migrations/0001_baseline.sql`.

Alert payloads carry the lifecycle state (`new`, `repeat`, `escalated`, `resolved`) so Home Assistant can format them differently.

**Done when:** a full run writes all files, a deliberately broken run (stop the ingest loop) produces "no data today" instead of "all quiet", and a known-noisy source stays out of the stories.

**As built (Task 11):** `brief/src/brief/lifecycle.py` is the engine and `alerts.py` runs it on the database; the numbers are in `priorities.yaml` under `lifecycle` and `quiet_hours` (quiet hours are UTC until a display timezone exists).

- An alert is one story (a grouped story is one alert, with a stable id like `group-deviation-<host>`). Only stories routed `push_now` or `daily_headline` are tracked; `digest_only` ones never are.
- **new / repeat / escalated / resolved** as described above. "Worse" means the level rose or the size grew by `escalate_factor` (1.5) since the last push, at most once per `escalate_min_minutes` (60). Findings whose size only grows with time (absence) do not escalate on growth, because a longer silence is the same problem; they repeat once per `renotify_hours` instead. Resolution is per kind: a run that did not look at a kind (the hourly run only checks absence) neither confirms nor clears its alerts.
- **Quiet hours** hold a non-urgent push until they end (it goes out as "new"); **the budget** counts non-urgent pushes per UTC day from the `push` table, and the overflow is listed in the next daily headline. Resolved messages wait for the morning too and do not use the budget.
- **Ack and snooze** (`ack`, `snooze_24h`, `snooze_7d`, from Home Assistant buttons or `brief ack` / `brief snooze`) stop pushes until the story gets worse, and a snooze also ends by itself. Every press is recorded in `feedback` and the audit log.
- **The daily headline** (`homelab/brief/headline`, retained) is the digest headline plus the stories that were folded or routed to `daily_headline` ("<headline> Also: a; b (+2 more)"). A failed run sends "No data today: <reason>" and raises a `pipeline_error` alert.
- Decisions are saved before anything is sent, so a crash cannot send a push twice; a channel that is down just leaves `delivered` at 0 and nothing is retried.
