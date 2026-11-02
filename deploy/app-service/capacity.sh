#!/usr/bin/env bash
# Measure how large an import, storage sync or export the app handles within one request, with no worker:
# without Redis these jobs run inside the request, which uWSGI stops after 220 s and App Service after about 230 s.
# Each size uses a throwaway project; a scenario stops at the first size slower than the budget. Prints a table.
#
# Harness:  ./capacity.sh   (after `docker compose up -d`; puts the sync test files into Azurite itself)
# Deployed: HARNESS_BASE_URL=https://<app> LABEL_STUDIO_USERNAME=… LABEL_STUDIO_PASSWORD=… \
#           [CAPACITY_SYNC_PREFIX=<prefix holding test files>] ./capacity.sh
# Optional: CAPACITY_SIZES (default "100 500 1000 2000 5000 10000"), CAPACITY_BUDGET seconds (default 110).

set -euo pipefail

cd "$(dirname "$0")"
BASE_URL=${HARNESS_BASE_URL:-https://localhost:${HARNESS_HTTPS_PORT:-8443}}
SIZES=${CAPACITY_SIZES:-100 500 1000 2000 5000 10000}
BUDGET=${CAPACITY_BUDGET:-110}
HARNESS=
if [[ -z ${HARNESS_BASE_URL:-} ]]; then
  HARNESS=1
  set -a
  source .env
  set +a
fi
CURL_TLS=()
[[ -n $HARNESS ]] && CURL_TLS=(--cacert "$(pwd)/.certs/ca.crt")

WORK=$(mktemp -d)
COOKIES=$WORK/cookies
project_id=
cleanup() {
  [[ -n $project_id ]] && api -o /dev/null -X DELETE "$BASE_URL/api/projects/$project_id"
  rm -rf "$WORK"
}
trap cleanup EXIT

curl() { command curl "${CURL_TLS[@]}" "$@"; }
api() { curl -s -b "$COOKIES" -H "X-CSRFToken: $(awk '$6 == "csrftoken" {print $7}' "$COOKIES")" -H "Referer: $BASE_URL/" "$@"; }
json() { python3 -c "import json, sys; data = json.load(sys.stdin); print($1)"; }
over_budget() { python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) > float(sys.argv[2]) else 1)" "$1" "$BUDGET"; }
row() { printf '%-15s %6s %9ss  %s\n' "$@"; }

new_project() {
  local config='<View><Audio name=\"audio\" value=\"$audio\"/><Choices name=\"choice\" toName=\"audio\"><Choice value=\"fictional\"/></Choices></View>'
  project_id=$(api -H 'Content-Type: application/json' \
    -d "{\"title\": \"harness capacity\", \"label_config\": \"$config\"}" "$BASE_URL/api/projects" | json 'data["id"]')
}
drop_project() {
  api -o /dev/null -X DELETE "$BASE_URL/api/projects/$project_id"
  project_id=
}

curl -s -c "$COOKIES" "$BASE_URL/user/login/" >/dev/null
curl -s -o /dev/null -b "$COOKIES" -c "$COOKIES" -H "Referer: $BASE_URL/user/login/" \
  --data-urlencode "csrfmiddlewaretoken=$(awk '$6 == "csrftoken" {print $7}' "$COOKIES")" \
  --data-urlencode "email=$LABEL_STUDIO_USERNAME" --data-urlencode "password=$LABEL_STUDIO_PASSWORD" "$BASE_URL/user/login/"
[[ $(api -o /dev/null -w '%{http_code}' "$BASE_URL/api/current-user/whoami") == 200 ]] || { echo "Sign-in failed" >&2; exit 1; }

echo "Capacity against $BASE_URL, budget ${BUDGET}s per request"
if [[ -n $HARNESS ]]; then
  # uWSGI's stats server, inside the container only, lists the workers
  workers=$(docker compose exec -T app python3 -c 'import json, urllib.request
print(len(json.load(urllib.request.urlopen("http://127.0.0.1:1717"))["workers"]))')
  echo "uWSGI workers (web concurrency): $workers"
else
  echo "uWSGI workers (web concurrency): UWSGI_PROCESSES app setting, 4 when unset"
fi
printf '%-15s %6s %10s  %s\n' scenario tasks time result

# --- import N tasks, each with one annotation, then export them as JSON
for size in $SIZES; do
  new_project
  python3 -c 'import json, sys
n = int(sys.argv[1])
result = [{"from_name": "choice", "to_name": "audio", "type": "choices", "value": {"choices": ["fictional"]}}]
tasks = [{"data": {"audio": f"fictional/audio-{i:05d}.wav", "text": f"Fictional utterance {i}"},
          "annotations": [{"result": result}]} for i in range(n)]
json.dump(tasks, open(sys.argv[2], "w"))' "$size" "$WORK/tasks.json"
  read -r code seconds < <(api -o "$WORK/import.out" -w '%{http_code} %{time_total}\n' \
    -H 'Content-Type: application/json' --data-binary "@$WORK/tasks.json" "$BASE_URL/api/projects/$project_id/import")
  counts=$(api "$BASE_URL/api/projects/$project_id" | json "f'{data[\"task_number\"]} tasks, {data[\"total_annotations_number\"]} annotations'")
  row import "$size" "$seconds" "HTTP $code, $counts"
  if [[ $code != 201 ]] || over_budget "$seconds"; then drop_project; break; fi

  read -r code seconds bytes < <(api -o /dev/null -w '%{http_code} %{time_total} %{size_download}\n' \
    "$BASE_URL/api/projects/$project_id/export?exportType=JSON")
  row export "$size" "$seconds" "HTTP $code, $((bytes / 1024)) KiB"

  if [[ $size == "${SIZES%% *}" ]]; then
    task_ids=$(api "$BASE_URL/api/tasks?project=$project_id&page_size=50" | json '" ".join(str(t["id"]) for t in data["tasks"])')
    for task_id in $task_ids; do
      api -o /dev/null -w '%{time_total}\n' -H 'Content-Type: application/json' -d '{"result": [{"from_name": "choice",
        "to_name": "audio", "type": "choices", "value": {"choices": ["fictional"]}}]}' \
        "$BASE_URL/api/tasks/$task_id/annotations"
    done | sort -n | awk '{t[NR] = $1} END {printf "%-15s %6s  typical %.3fs, slowest %.3fs\n", "save annotation", NR, t[int((NR + 1) / 2)], t[NR]}'
  fi
  drop_project
  if over_budget "$seconds"; then break; fi
done

# --- sync N files from Blob storage into tasks
put_blobs() {
  docker compose exec -T app python3 - "$1" "$2" <<'EOF'
import os, sys
from concurrent.futures import ThreadPoolExecutor
from azure.storage.blob import BlobServiceClient
prefix, count = sys.argv[1], int(sys.argv[2])
name = os.environ['AZURE_BLOB_ACCOUNT_NAME']
client = BlobServiceClient(f'https://{name}.blob.core.windows.net',
                           credential={'account_name': name, 'account_key': os.environ['AZURE_BLOB_ACCOUNT_KEY']})
container = client.get_container_client('source')
if count:
    with ThreadPoolExecutor(16) as pool:
        list(pool.map(lambda i: container.upload_blob(f'{prefix}audio-{i:05d}.wav', b'RIFF', overwrite=True), range(count)))
else:
    for blob in container.list_blobs(name_starts_with=prefix):
        container.delete_blob(blob.name)
EOF
}
sync_size() {
  # storage created without a key, so the server's AZURE_BLOB_ACCOUNT_KEY setting is used, as on App Service
  local storage_id
  storage_id=$(api -H 'Content-Type: application/json' -d "{\"project\": $project_id, \"container\": \"source\",
    \"prefix\": \"$1\", \"use_blob_urls\": true, \"presign\": false}" "$BASE_URL/api/storages/azure" | json 'data["id"]')
  api -o /dev/null -w '%{http_code} %{time_total}\n' -X POST "$BASE_URL/api/storages/azure/$storage_id/sync"
}

if [[ -n $HARNESS ]]; then
  for size in $SIZES; do
    prefix="capacity/$size/"
    put_blobs "$prefix" "$size"
    new_project
    read -r code seconds < <(sync_size "$prefix")
    tasks=$(api "$BASE_URL/api/projects/$project_id" | json 'data["task_number"]')
    row "storage sync" "$size" "$seconds" "HTTP $code, $tasks tasks"
    drop_project
    put_blobs "$prefix" 0
    if over_budget "$seconds"; then break; fi
  done
elif [[ -n ${CAPACITY_SYNC_PREFIX:-} ]]; then
  new_project
  read -r code seconds < <(sync_size "$CAPACITY_SYNC_PREFIX")
  tasks=$(api "$BASE_URL/api/projects/$project_id" | json 'data["task_number"]')
  row "storage sync" "$tasks" "$seconds" "HTTP $code"
  drop_project
else
  echo "storage sync skipped: set CAPACITY_SYNC_PREFIX to a prefix in source holding test files"
fi
