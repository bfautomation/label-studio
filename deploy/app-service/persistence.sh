#!/usr/bin/env bash
# Prove that work and sign-ins survive the app container being replaced, as App Service does on every
# restart and deployment. Creates a project with an uploaded file and an annotation, replaces the app container,
# then checks everything with the sign-in from before the replacement. Restarts the app; run it on its own.
# Usage: ./persistence.sh   (after `docker compose up -d`; exits non-zero on the first failed check)

set -euo pipefail

cd "$(dirname "$0")"
set -a
source .env
set +a

BASE_URL=${HARNESS_BASE_URL:-https://localhost:${HARNESS_HTTPS_PORT:-8443}}
CA_CERT=$(pwd)/.certs/ca.crt
COOKIES=$(mktemp)
AUDIO=$(mktemp).wav
DOWNLOAD=$(mktemp)
project_id=
cleanup() {
  [[ -n $project_id ]] && api -o /dev/null -X DELETE "$BASE_URL/api/projects/$project_id"
  rm -f "$COOKIES" "$AUDIO" "$DOWNLOAD"
}
trap cleanup EXIT

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1" >&2; exit 1; }

curl() { command curl --cacert "$CA_CERT" "$@"; }
status() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
api() { curl -s -b "$COOKIES" -H "X-CSRFToken: $(awk '$6 == "csrftoken" {print $7}' "$COOKIES")" -H "Referer: $BASE_URL/" "$@"; }
json() { python3 -c "import json, sys; data = json.load(sys.stdin); print($1)"; }
wait_healthy() {
  for _ in $(seq 90); do
    [[ $(status "$BASE_URL/health") == 200 ]] && return
    sleep 2
  done
  fail "app healthy"
}

wait_healthy

# --- create work before the replacement
csrf=$(curl -s -c "$COOKIES" "$BASE_URL/user/login/" >/dev/null; awk '$6 == "csrftoken" {print $7}' "$COOKIES")
curl -s -o /dev/null -b "$COOKIES" -c "$COOKIES" -H "Referer: $BASE_URL/user/login/" \
  --data-urlencode "csrfmiddlewaretoken=$csrf" --data-urlencode "email=$LABEL_STUDIO_USERNAME" \
  --data-urlencode "password=$LABEL_STUDIO_PASSWORD" "$BASE_URL/user/login/"
grep -q sessionid "$COOKIES" || fail "admin signs in"

label_config='<View><Audio name=\"audio\" value=\"$audio\"/><Choices name=\"choice\" toName=\"audio\"><Choice value=\"fictional\"/></Choices></View>'
project_id=$(api -H 'Content-Type: application/json' \
  -d "{\"title\": \"harness persistence\", \"label_config\": \"$label_config\"}" "$BASE_URL/api/projects" | json 'data["id"]')

python3 -c 'import sys, wave
w = wave.open(sys.argv[1], "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
w.writeframes(bytes(range(256)) * 300); w.close()' "$AUDIO"
api -o /dev/null -F "file=@$AUDIO" "$BASE_URL/api/projects/$project_id/import"

task=$(api "$BASE_URL/api/tasks?project=$project_id" | json 'data["tasks"][0]["id"], data["tasks"][0]["data"]["audio"]')
task_id=${task%% *}
upload_url=${task#* }
upload_path=/home/label-studio/media/${upload_url#*/data/}
api -o /dev/null -H 'Content-Type: application/json' -d '{"result": [{"from_name": "choice", "to_name": "audio",
  "type": "choices", "value": {"choices": ["fictional"]}}]}' "$BASE_URL/api/tasks/$task_id/annotations"
echo "Created project $project_id with an uploaded file and an annotation"

# --- replace the app container: same image and persistent storage, nothing else kept
old_container=$(docker compose ps -q app)
docker compose up -d --force-recreate --no-deps app >/dev/null 2>&1
wait_healthy
[[ $(docker compose ps -q app) != "$old_container" ]] && pass "app container replaced" || fail "app container replaced"

# --- check with the sign-in from before the replacement
[[ $(api -o /dev/null -w '%{http_code}' "$BASE_URL/api/projects/$project_id") == 200 ]] \
  && pass "still signed in, project still there" || fail "still signed in, project still there"
annotations=$(api "$BASE_URL/api/tasks/$task_id/annotations" | json 'len(data)')
[[ $annotations == 1 ]] && pass "task and annotation still there" || fail "task and annotation still there ($annotations)"

api -o "$DOWNLOAD" "$BASE_URL$upload_url"
cmp -s "$AUDIO" "$DOWNLOAD" && pass "uploaded file unchanged" || fail "uploaded file unchanged"

api "$BASE_URL/api/projects/$project_id/export?exportType=JSON" | grep -q '"fictional"' \
  && pass "export includes the annotation" || fail "export includes the annotation"

# the signing key comes from settings, so the app must not have generated and saved one of its own
docker compose exec -T app test ! -e /home/label-studio/.env \
  && pass "no signing key file generated" || fail "no signing key file generated"

# --- deleting the project must remove its uploaded file from persistent storage
api -o /dev/null -X DELETE "$BASE_URL/api/projects/$project_id"
project_id=
docker compose exec -T app test ! -e "$upload_path" \
  && pass "deleting the project removes its uploaded file" || fail "deleting the project removes its uploaded file"
# a download export must not keep a copy of the data in persistent storage
leftovers=$(docker compose exec -T app find /home/label-studio/export -type f)
[[ -z $leftovers ]] && pass "exports leave no copy of the data behind" || fail "exports leave no copy of the data behind"

echo "All persistence checks passed against $BASE_URL"
