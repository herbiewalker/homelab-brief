# Contributing

Thanks for looking. This is a small project with one maintainer, so a short issue before a large change saves everyone time.

## The one hard rule

This repository holds the **generic product only**. Never commit real hostnames, IP addresses, MAC addresses, share paths, names of people or devices, API keys, or real log lines. Use placeholders (`server-1`, `<host>`) and the documentation ranges (`198.51.100.x` addresses, `00:00:5E:00:53:xx` MACs). Anything specific to one network belongs in your own private instance folder (`docs/INSTANCE.md`). A commit guard (`scripts/private_name_guard.py`) and CI check this; if you share logs in an issue, run them through `brief support-bundle` first.

## Setting up

See `docs/START-HERE.md`. In short: clone, run `scripts/setup-dev-tools.sh` for the commit checks, and run the tests inside the container:

```bash
docker compose --profile test run --rm --build test pytest -q
```

To see every page on synthetic data, use `scripts/ui_preview.py`.

## Making a change

- One task at a time: tests first or alongside, then the change. The detectors, ignore rules, signature stability, and the "no data today" failure path must stay covered.
- Read `AGENTS.md`: it lists the rules a change must respect (no AI in the critical path, efficiency, admin security, config conventions). It is written for coding agents, and it is just as good for people.
- Ask before adding a dependency, and say why; the container has a 768 MB memory limit.
- Update `CHANGELOG.md` for anything a user would notice, and `docs/` if behaviour or settings changed.
- Pull requests run the same checks as releases: lint, secrets scan, the private-name guard, the test suite, and the end-to-end scripts against the built image.

## Licence

By contributing you agree that your work is licensed under the project's licence, AGPL-3.0 (see `LICENSE`).
