#!/usr/bin/env bash
# Qualify one image for publication: run the harness against exactly that image, without building anything,
# then the smoke, persistence and failure checks. Each harness starts from empty volumes and is always removed
# afterwards (see disposable-harness.sh).
# Usage: ./qualify.sh IMAGE   (exits non-zero if the image is missing or any check fails)

set -euo pipefail

cd "$(dirname "$0")"
source disposable-harness.sh

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 IMAGE" >&2
  exit 2
fi
start_disposable_harness "$1" qualify

./smoke.sh
./persistence.sh
# remove this harness first, so the failure checks' own harness never runs alongside it
remove_disposable_harness
./failure-modes.sh "$HARNESS_IMAGE"
echo "Qualified $HARNESS_IMAGE ($(docker image inspect --format '{{.Id}}' "$HARNESS_IMAGE"))"
