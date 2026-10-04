#!/bin/sh
# Run the Vector configuration tests inside the built image (Vector is not installed on the host).
#   sh scripts/test-vector.sh [image]      default image: homelab-brief:dev (docker compose build first)
# Checks that the live and backfill configs load, then runs the parse.vrl unit tests.
set -e
cd "$(dirname "$0")/.."
image="${1:-homelab-brief:dev}"
run() {
  docker run --rm --entrypoint /usr/local/bin/vector \
    -v "$PWD/vector:/app/vector:ro" "$image" "$@"
}
echo "== validate live config (unrendered defaults) =="
run validate --no-environment /app/vector/vector.yaml
echo "== validate backfill config =="
run validate --no-environment /app/vector/backfill.yaml
echo "== unit tests =="
run test /app/vector/vector.yaml /app/vector/tests/parse.yaml
