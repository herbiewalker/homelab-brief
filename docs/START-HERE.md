# Start here

Two ways in, depending on what you want:

- **Just run it:** read the [README](../README.md), then [`deploy/README.md`](../deploy/README.md). You do not need anything below.
- **Work on it** (fix a bug, add a collector, change the console): the rest of this page sets up a development session. The building is done in small tested tasks, either by you or with a coding agent; Claude Code is the worked example below, but any agent that reads `AGENTS.md` works the same way. Agents do not share memory between sessions, so the files in `docs/` are their memory.

## One-time setup

1. **Read the spec.** The plan is `docs/project-plan.md`; the technical guide is `docs/unraid-backend-guide.md` plus the parts in `docs/guide/`; the rules for contributors and agents are `AGENTS.md` (`CLAUDE.md` only points to it); the backlog is `docs/TASKS.md`; settled and open choices are `docs/DECISIONS.md`.
2. **Get the code.** Fork or clone the repository. Install the commit checks with `scripts/setup-dev-tools.sh` (Linux or WSL): they run ruff, a secrets scan, and the private-name guard before every commit.
3. **Check local tools.** Docker (Docker Desktop, or Docker in WSL). Tests run inside the container, so a local Python install is optional: `docker compose --profile test run --rm --build test pytest -q`.
4. **Create your local secrets file.** Copy `secrets.env.example` to `secrets.env` and fill in throwaway MQTT passwords (`MQTT_PASSWORD`, `MQTT_HA_PASSWORD`). It is gitignored. `scripts/dev-init.ps1` creates the dev folders and Mosquitto users from it. On Unraid, the same values become template variables.
5. **Create your instance folder.** Everything specific to your network (real `systems.yaml`, `devices.yaml`, `topology.yaml`, audit notes, raw and full sanitized logs, private word lists) lives outside this repository, normally in its own private repository. Copy `instance-template/` to a sibling folder named `homelab-brief-instance` (or set `BRIEF_INSTANCE_DIR`) and read `docs/INSTANCE.md`. Never put that material in this repository: the commit guard reads your instance's word lists to stop you.
6. **Look at the interface without any data.** `scripts/ui_preview.py` serves every page on synthetic data (see its header for the one-line command).

## Starting a session

From this folder, start your coding agent. For Claude Code:

```powershell
cd <path to your clone of this repository>
claude
```

First message (works for any agent):

```
Read AGENTS.md, docs/START-HERE.md, docs/TASKS.md, docs/DECISIONS.md, docs/project-plan.md,
docs/unraid-backend-guide.md and every file in docs/guide/.
Summarize the project in five lines and tell me which task in TASKS.md is next.
Then wait for me to say go. Work one task at a time, run tests, and commit after each task.
Do not touch any real server. Local Docker only.
```

After each task, before merging (fresh context or a second agent session):

```
Review the last task's diff against the hard rules in AGENTS.md (privacy, secrets, admin security,
AI guardrails, efficiency, tests). List every problem with file and line, fix them, and re-run
the tests and pre-commit checks.
```

At the end of every session:

```
Update the status in docs/TASKS.md, record decisions and surprises in docs/DECISIONS.md,
and commit. Then list what I should bring back to the chat side.
```

## Getting sample logs (Tasks 2 and 3 depend on it)

Samples let the agent build and test against real formats. Everything stays in your instance folder until it is sanitized (see `docs/INSTANCE.md`). To get samples:

1. Make sure the syslog share is reachable read-only from the machine you work on; if collection has stopped, history is still usable, because the samples come from the files already there.
2. Run `.\scripts\pull-samples.ps1 -Share <path to your syslog folder>` from the repo root. It copies the last 200 lines of each source into the instance's `samples/raw/` and only reads from the share.
3. Run `python3 scripts/sanitize_samples.py` (in WSL or Linux), then read the output. `python3 scripts/trim_samples.py` copies a small public set into this repository's `samples/sanitized/`; only that set is committed.

## What stays in chat

- Reviewing real daily digests and asking "what's normal for this host?"
- Tuning the ignore list against real lines
- HA dashboards, the retro style choice, and e-reader output
- Open decisions in `docs/DECISIONS.md`

## Deployment (later, by you)

The agent builds and tests locally, including the admin GUI at `https://localhost:8443/admin` and syslog on port 5514. When a deploy pass in `deploy/README.md` is ready, you run it yourself against the host server, one step at a time, and verify with the guide's checklist before moving on.
