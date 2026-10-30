#!/usr/bin/env bash
# Report whether deploying a candidate image over the deployed one changes the database structure.
# Reads each image's migrations from the image itself, so it needs Docker but no database access.
# Usage: ./schema-changes.sh DEPLOYED_IMAGE CANDIDATE_IMAGE
# Exit codes: 0 no schema change, 1 schema change (back up the database first), 2 the comparison failed.

set -uo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 DEPLOYED_IMAGE CANDIDATE_IMAGE" >&2
  exit 2
fi

LIST_MIGRATIONS='
import django
django.setup()
from django.db.migrations.loader import MigrationLoader
for app, name in MigrationLoader(None, ignore_no_migrations=True).disk_migrations:
    print(f"MIGRATION {app}.{name}")
'

list_migrations() {
  local output
  # no network: listing must not depend on, or reach, anything outside the image
  output=$(docker run --rm --network none -w /label-studio/label_studio --entrypoint python3 "$1" \
    -c "$LIST_MIGRATIONS" 2>/dev/null) || return 1
  output=$(sed -n 's/^MIGRATION //p' <<<"$output" | sort)
  [[ -n $output ]] || return 1
  printf '%s\n' "$output"
}

deployed=$(list_migrations "$1") || { echo "Could not list migrations in $1" >&2; exit 2; }
candidate=$(list_migrations "$2") || { echo "Could not list migrations in $2" >&2; exit 2; }

added=$(comm -13 <(printf '%s\n' "$deployed") <(printf '%s\n' "$candidate"))
removed=$(comm -23 <(printf '%s\n' "$deployed") <(printf '%s\n' "$candidate"))

if [[ -z $added && -z $removed ]]; then
  echo "No schema change: both images have the same $(wc -l <<<"$candidate" | tr -d ' ') migrations."
  exit 0
fi

if [[ -n $added ]]; then
  echo "Migrations the candidate adds:"
  sed 's/^/  /' <<<"$added"
fi
if [[ -n $removed ]]; then
  echo "Migrations the candidate no longer has:"
  sed 's/^/  /' <<<"$removed"
fi
echo "Schema change: back up the database before deploying."
exit 1
