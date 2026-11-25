#!/usr/bin/env bash
# Qualify one image for publication: run the harness against exactly that image, without building anything,
# then the smoke and persistence checks. Starts from empty volumes and always removes the harness afterwards.
# Runs as a Compose project and on a port of its own, so an interactive harness on this machine is left untouched.
# Usage: ./qualify.sh IMAGE   (exits non-zero if the image is missing or any check fails)

set -euo pipefail

cd "$(dirname "$0")"

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 IMAGE" >&2
  exit 2
fi
export HARNESS_IMAGE=$1
docker image inspect "$HARNESS_IMAGE" >/dev/null 2>&1 || { echo "Image not found locally: $HARNESS_IMAGE" >&2; exit 1; }

./setup.sh >/dev/null
export COMPOSE_PROJECT_NAME=label-studio-qualify-$$
# the public address includes the port, so it is chosen before start-up
HARNESS_HTTPS_PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
export HARNESS_HTTPS_PORT
trap 'docker compose down -v --remove-orphans >/dev/null 2>&1' EXIT
# every service except app and blob-init, which run the image given
docker compose pull --quiet frontdoor db blob home-init
# never build or pull the image given: the checks must run against it, and only it
docker compose up -d --no-build --pull never

./smoke.sh
./persistence.sh
echo "Qualified $HARNESS_IMAGE ($(docker image inspect --format '{{.Id}}' "$HARNESS_IMAGE"))"
