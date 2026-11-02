#!/usr/bin/env bash
# Qualify one image for publication: run the harness against exactly that image, without building anything,
# then the smoke and persistence checks. Starts from empty volumes and always removes the harness afterwards.
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
trap 'docker compose down -v --remove-orphans >/dev/null 2>&1' EXIT
docker compose down -v --remove-orphans >/dev/null 2>&1
# never build or pull: the checks must run against the image given, and only that image
docker compose up -d --no-build --pull never

./smoke.sh
./persistence.sh
echo "Qualified $HARNESS_IMAGE ($(docker image inspect --format '{{.Id}}' "$HARNESS_IMAGE"))"
