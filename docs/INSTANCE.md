# Your instance folder

This repository is the generic product. It contains nothing about any one household's network, and it never should: you can publish it, fork it, or hand it to a friend with a homelab. Everything that is specific to **your** install lives in an *instance folder* that sits next to it, normally as its own private repository.

## What goes where

| In this repository (generic) | In your instance folder (private) |
| --- | --- |
| The service, the image, the docs, example config (`*.example.yaml`), a small trimmed set of sanitized sample lines | Your real `systems.yaml`, `devices.yaml`, `topology.yaml`, deploy values, audit notes, private word lists, raw logs (never committed anywhere), and the full sanitized sample set |

At run time the container never reads this repository or your instance folder. It reads `/config` (the appdata folder) and template variables. The instance folder is where you keep, version, and back up what you put into `/config`.

## Create it

1. Copy `instance-template/` to a sibling folder named `homelab-brief-instance` (or put it anywhere and set `BRIEF_INSTANCE_DIR`).
2. `git init` it and create a **private** repository for it. Its `.gitignore` already excludes raw logs and secrets.
3. Fill it in as you go. Nothing is required up front: with no instance at all, the tools fall back to `.dev/instance` inside this repository (gitignored).

Lookup order used by every tool in `scripts/`: `BRIEF_INSTANCE_DIR`, then a sibling `homelab-brief-instance`, then `.dev/instance`.

## Layout

```
config/brief/        your real systems.yaml, devices.yaml, topology.yaml (copy from the examples in this repository)
private/             notes: audit results, top-talkers output, decisions specific to you
guard/               private-names.txt, sanitizer-words.txt, allow.txt, trim-exclude.txt
samples/raw/         real log lines pulled from your share (never commit)
samples/sanitized/   the full sanitized set (private; the trimmed public set goes in this repository)
deploy/              your template values and notes
STATUS.md            where you are: your open items, outages, to-dos
```

## How the tools use it

- `scripts/pull-samples.ps1 -Share <path>` writes raw samples to `samples/raw/`.
- `scripts/audit-syslog.ps1` counts lines per program per day on a big file; save the output under `private/`.
- `scripts/sanitize_samples.py` reads `samples/raw/` and writes `samples/sanitized/`, using your word lists in `guard/` so site codes and similar become `SITE`.
- `scripts/trim_samples.py` writes the small public set into this repository, skipping any line that holds a word from `guard/trim-exclude.txt`.
- `scripts/private_name_guard.py` (the commit hook and CI) blocks any commit to **this** repository that contains a hostname, IP, MAC, or word it finds in your instance. Without an instance, it still blocks private IP ranges and real-looking MACs.

## Handing the project to someone else

Give them this repository. They copy `instance-template/` and fill in their own. Nothing of yours travels with it, and nothing of theirs ever needs to be committed here.
