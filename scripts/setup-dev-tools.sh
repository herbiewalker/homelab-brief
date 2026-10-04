#!/bin/sh
# One-time developer setup, run inside WSL or Linux from the repo root:
#   sh scripts/setup-dev-tools.sh
# Creates ~/.venvs/homelab-brief with ruff, pre-commit, and pytest, and installs the commit hook.
set -e
cd "$(git rev-parse --show-toplevel)"
python3 -m venv "$HOME/.venvs/homelab-brief"
"$HOME/.venvs/homelab-brief/bin/pip" install --quiet -r requirements-tools.txt
"$HOME/.venvs/homelab-brief/bin/pre-commit" install-hooks
cp .githooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
echo "Dev tools ready and commit hook installed."
