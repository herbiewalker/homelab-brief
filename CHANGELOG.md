# Changelog

Versions follow [semantic versioning](https://semver.org/). Until 1.0.0 the product is in staging: a minor
step (0.x.0) may change config files, and the upgrade step migrates them (see `docs/DECISIONS.md`); a
patch step (0.0.x) is a build of the next backlog task or a fix. The release workflow refuses to publish a
tag that has no entry here. Images are tagged with the exact version; there is no `latest`.

## 0.0.28

Task 18: the Phase 2 framework. No collector ships yet (Tasks 19 to 23 add them), and every one is off.

- Collector contract and framework: `collectors.yaml` is validated (a credential written into it is refused, keys come from template variables), enabled collectors run when due from the hourly job, each isolated with its own timeout; a failing one reports `collector_<name>_ok` 0 with a safe reason and keeps its last good numbers. Enabling or disabling one adds or removes its sensors, `/metrics` lines and API values with no other change. `brief collector list|test|run`.
- Migration 0007: the Phase 2 tables (disks, SMART readings, inventory, clients, DNS, power, topology).
- Playbook engine: `playbook.yaml` rules turn findings into advice with structured conditions (no code, regexes with nested repetition refused); the advice shows in the brief and a rule id can be routed in `priorities.yaml`.
- `metric_drift` detector: any collector metric whose recent mean moves far from its own earlier mean.
- `collectors.yaml`, `map.yaml` and `agents.yaml` are now validated, guarded with a last-good fallback, and editable in the config editor.

## 0.0.27

Task 17: packaging and release.

- Release workflow: a tag runs every check, then builds and pushes the image to the container registry under the exact version tag (never `latest`), and refuses a tag that differs from the program version or has no entry here.
- Unraid template (`deploy/unraid-template.xml`) and compose references for all three network modes: own IP on `br0` (default), host, bridge with port mappings.
- Config files are brought up to the current schema on upgrade (`brief.confmigrate`): a copy of the config folder is saved to `/state/config-backups/` first, a downgrade is noticed and never touches files.
- `deploy/README.md` rewritten as the install and upgrade guide.

## 0.0.26

Task 16a: setup wizard (`/admin/wizard`) and `brief init`, support bundle (`brief support-bundle`, structure and counts only, self-checked for private values), log scrubbing, `devices.yaml` and `topology.yaml` in the config editor.

## 0.0.25

Task 16: the full `brief` command (status, sources, config list/show/validate/history/diff/set/rollback/reset, systems, signatures, logs search, audit, network, backup) and the optional key-only SSH server (off by default).

## 0.0.22

Task 15d: replay and tuning page, signatures page with ignore, log search, systems form, backup download and config restore, report downloads. Task 15 complete.

## 0.0.21

Task 15d part 1: replay and tuning, signatures with ignore, log search.

## 0.0.20

Task 15c: Network page (mode and address detection, warnings for lost sender addresses, live syslog test, repointing status, template settings).

## 0.0.19

Task 15b: config editor with validation, diff, history, rollback, reset, and audit.

## 0.0.18

Task 15a: admin GUI shell (overview, sources, findings and alerts, audit log). Fixed browser sign-in being refused under `Referrer-Policy: no-referrer`.

## 0.0.16

Task 14: weekly schedule, config guard (a broken config file falls back to its last good copy and raises a finding), self-monitoring, storage guard.

## 0.0.15

Task 13 part 2: optional Apprise notifications (off by default) and `brief notify test`.

## 0.0.14

Task 13 part 1: registry-driven MQTT sensors, Prometheus `/metrics`, versioned `/api/v1`.

## 0.0.13

Readable story titles in the brief; the headline cuts at a word.

## 0.0.12

Status board uses DejaVu Sans Mono from the image, with the built-in font as fallback.

## 0.0.11

Task 12: static views (`brief.html`, `board.html`, `status.png`) and the optional AI provider layer (off by default; the template always works without it).

## 0.0.1 to 0.0.10

Tasks 0 to 11, built and tagged during the first staging runs: image and CI, syslog receiver and archive (Vector), SQLite store with numbered migrations, ingest and signatures (Drain3), web apps with admin login and TLS, `status.json` and MQTT sensors, `brief import`, detectors (novelty, deviation, off-hours, absence, clock skew), `brief replay` and the golden day, the daily digest with grouped stories, and the alert lifecycle with Home Assistant feedback. Individual tags in this range were rewritten when the repository history was cleaned and are not listed.
