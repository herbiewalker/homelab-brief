#!/bin/sh
# Run the commit checks (ruff, gitleaks, private-name guard) on staged files. Linux or WSL.
tool="$HOME/.venvs/homelab-brief/bin/pre-commit"
[ -x "$tool" ] || { echo "pre-commit is missing: run scripts/setup-dev-tools.sh" >&2; exit 1; }
exec "$tool" run "$@"
