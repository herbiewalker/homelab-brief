# Start here: setting up a coding session

This folder was prepared in chat. A coding agent does the building (Claude Code is the worked example below, but any agent that reads `AGENTS.md` works the same way); chat stays for planning, tuning, and reviewing real digests. Coding agents do not share chat memory, so the files in `docs/` are their memory.

## One-time setup

1. **The spec is already in the repo.** The plan is `docs/project-plan.md`; the technical guide is `docs/unraid-backend-guide.md` plus the parts in `docs/guide/`; agent instructions are `AGENTS.md` (`CLAUDE.md` only points to it).
2. **Initialize git and make a private repo.** Check `.gitignore` first, then from this folder in PowerShell:
   ```powershell
   git init
   git add .
   git status        # confirm no secrets.env, samples/raw, .dev, docs/private, or real systems.yaml appear
   git commit -m "Initial scaffold"
   ```
   Create a new **private** repository (not the public scripts repository) and push. With the GitHub CLI: `gh repo create homelab-brief --private --source=. --push`.
3. **Check local tools.** Docker (Docker Desktop or Docker in WSL) on the machine where the agent runs. Tests run inside the container, so a local Python install is optional.
4. **Create your local secrets file.** Copy `secrets.env.example` to `secrets.env` and fill in throwaway MQTT passwords (`MQTT_PASSWORD`, `MQTT_HA_PASSWORD`). It is gitignored. Task 0 runs `scripts/dev-init.ps1` to create the dev folders and Mosquitto users from it. On Unraid, the same values become template variables.
5. **Create your instance folder.** Everything specific to your network (real `systems.yaml`, `devices.yaml`, `topology.yaml`, audit notes, raw and full sanitized logs, private word lists) lives outside this repository, normally in its own private repository. Copy `instance-template/` to a sibling folder named `homelab-brief-instance` (or set `BRIEF_INSTANCE_DIR`) and read `docs/INSTANCE.md`. Never put real names, MACs, or IPs in this repository.

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
