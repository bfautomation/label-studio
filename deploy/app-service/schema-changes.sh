#!/usr/bin/env bash
# Compare the migration names in two images: the deployed one and a candidate. Django records each applied migration by
# name and runs only names it has not recorded, so names the candidate adds are migrations start-up may apply. Reads
# each image's migrations from the image itself, so it needs Docker but no database access: it compares the images, not
# what the target database has applied. A migration edited under an existing name is not detected, and start-up does
# not run it either.
# Usage: ./schema-changes.sh DEPLOYED_IMAGE CANDIDATE_IMAGE
# Exit codes: 0 the images have the same migration names, 1 the names differ (listed), 2 the comparison failed.

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
  count=$(wc -l <<<"$candidate" | tr -d ' ')
  echo "Unchanged migration-name inventory: both images have the same $count migration names."
  exit 0
fi

if [[ -n $added ]]; then
  echo "Migration names the candidate adds:"
  sed 's/^/  /' <<<"$added"
fi
if [[ -n $removed ]]; then
  echo "Migration names the candidate no longer has:"
  sed 's/^/  /' <<<"$removed"
fi
if [[ -n $added ]]; then
  echo "Start-up may apply these: back up the database before deploying."
else
  echo "The candidate adds no migration names, so this comparison gives start-up nothing new to apply."
fi
exit 1
