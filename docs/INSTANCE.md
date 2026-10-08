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

## Setting up an empty install

Start the container with an empty `/config`. The product runs on its shipped defaults, so nothing has to be written first.

1. Point your devices at the container (the Setup wizard and `brief init` print the address and port).
2. When the first lines arrive, name your systems: open **Setup wizard** in the admin GUI, or run `docker exec -it <container> brief init`. Both list every sender with the programs it talks about and a suggested kind (a hint, never applied for you). The same name groups senders into one system. The result is a validated `systems.yaml`, saved through the config editor, so it is versioned, audited, and can be rolled back.
3. Fine-tune on the **Systems** page, add `devices.yaml` and `topology.yaml` in the **Config** editor if you want them (they start from the shipped examples, and they stay in your config folder: they hold real MACs and names), and run `brief config validate`.
4. Back up your settings from **Backups**. Keep that file private: it holds the admin password hash.

Discovery through the Unraid and UniFi APIs (servers, devices, topology) arrives with the collectors in Phase 2. Until then the senders themselves are the source.

## Asking for help without leaking your network

`brief support-bundle` (or **Backups, Support bundle** in the GUI) writes a diagnostics file with no names in it. It holds structure and numbers only: the status, the shape of your config files, counts of senders, alerts, audit actions and AI calls. It never holds log lines, message text, signature text, alert titles, or user names. Every IP, MAC, e-mail address, domain, host or device name, and secret becomes an alias such as `<ip-1>` or `<name-2>`, the same everywhere in the bundle. It checks itself before it is written and is not made at all if anything private is left. `brief support-bundle --map aliases.json` also writes the alias map, so a reply that says `<name-2>` can be turned back into a name; keep that file private.

Container logs are scrubbed the same way for the shapes that are easy to catch (IP and MAC addresses, e-mail addresses, credentials in URLs, long secrets), and the log calls leave out host names, alert titles, and headlines. A private word the product was never told about can still appear in free text, so look through anything before you share it.

## Handing the project to someone else

Give them this repository. They copy `instance-template/` and fill in their own. Nothing of yours travels with it, and nothing of theirs ever needs to be committed here.
