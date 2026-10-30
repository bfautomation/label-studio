#!/usr/bin/env bash
# Smoke-check a running harness: pages, static files, API, database TLS and Blob storage.
# Usage: ./smoke.sh   (after `docker compose up -d`; exits non-zero on the first failed check)

set -euo pipefail

cd "$(dirname "$0")"
set -a
source .env
set +a

BASE_URL=${HARNESS_BASE_URL:-http://127.0.0.1:${HARNESS_HTTP_PORT:-8090}}
COOKIES=$(mktemp)
trap 'rm -f "$COOKIES"' EXIT

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1" >&2; exit 1; }

status() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
# authenticated requests carry the session cookie and, for writes, Django's CSRF token
api() { curl -s -b "$COOKIES" -H "X-CSRFToken: $(awk '$6 == "csrftoken" {print $7}' "$COOKIES")" -H "Referer: $BASE_URL/" "$@"; }

for _ in $(seq 60); do
  [[ $(status "$BASE_URL/health") == 200 ]] && break
  sleep 2
done
[[ $(curl -s "$BASE_URL/health") == *'"UP"'* ]] && pass "health endpoint" || fail "health endpoint"

processes=$(docker compose exec -T app ps -o args)
[[ $processes == *uwsgi* && $processes != *runserver* ]] && pass "served by uWSGI, no dev server" \
  || fail "served by uWSGI, no dev server"

login_page=$(curl -s -w '\n%{http_code}' "$BASE_URL/user/login/")
[[ ${login_page##*$'\n'} == 200 ]] && pass "login page" || fail "login page"

# fetch the first script the login page references, so the check follows whatever the build produced
asset=$(grep -oE '(src|href)="/(static|react-app)/[^"]+\.(js|css)"' <<<"$login_page" | head -1 | cut -d'"' -f2)
[[ -n $asset && $(status "$BASE_URL$asset") == 200 ]] && pass "static asset $asset" || fail "static asset ${asset:-none found}"

csrf=$(curl -s -c "$COOKIES" "$BASE_URL/user/login/" >/dev/null; awk '$6 == "csrftoken" {print $7}' "$COOKIES")
login=$(curl -s -o /dev/null -w '%{http_code}' -b "$COOKIES" -c "$COOKIES" -H "Referer: $BASE_URL/user/login/" \
  --data-urlencode "csrfmiddlewaretoken=$csrf" --data-urlencode "email=$LABEL_STUDIO_USERNAME" \
  --data-urlencode "password=$LABEL_STUDIO_PASSWORD" "$BASE_URL/user/login/")
grep -q sessionid "$COOKIES" && pass "admin signs in (HTTP $login)" || fail "admin signs in (HTTP $login)"

[[ $(api -o /dev/null -w '%{http_code}' "$BASE_URL/api/projects") == 200 ]] && pass "API with session" \
  || fail "API with session"
[[ $(status "$BASE_URL/api/projects") == 401 ]] && pass "API rejects anonymous" || fail "API rejects anonymous"

# the signup page always renders; the server must refuse the submission without an invite token
signup=$(curl -s -o /dev/null -w '%{http_code}' -b "$COOKIES" -H "Referer: $BASE_URL/user/signup/" \
  --data-urlencode "csrfmiddlewaretoken=$csrf" --data-urlencode "email=smoke-$RANDOM@example.com" \
  --data-urlencode "password=Smoke-$RANDOM-Password" "$BASE_URL/user/signup/")
[[ $signup == 403 ]] && pass "signup without invite is refused" || fail "signup without invite is refused (HTTP $signup)"

# every app login is logged with its TLS state; any login without TLS must have been refused
app_logins=$(docker compose logs db | grep 'connection authorized: user=labelstudio' || true)
[[ -n $app_logins ]] && ! grep -qv 'SSL enabled' <<<"$app_logins" && pass "app database logins use TLS" \
  || fail "app database logins use TLS"
plain=$(docker compose exec -T db psql "host=127.0.0.1 sslmode=disable user=labelstudio dbname=labelstudio" \
  -c 'SELECT 1' 2>&1 || true)
[[ $plain == *"no encryption"* ]] && pass "database refuses logins without TLS" \
  || fail "database refuses logins without TLS"

label_config='<View><Audio name=\"audio\" value=\"$audio\"/></View>'
project_id=$(api -H 'Content-Type: application/json' \
  -d "{\"title\": \"harness smoke\", \"label_config\": \"$label_config\"}" "$BASE_URL/api/projects" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])')
trap 'api -o /dev/null -X DELETE "$BASE_URL/api/projects/$project_id"; rm -f "$COOKIES" "$AUDIO"' EXIT
validate=$(api -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d "{
  \"project\": $project_id, \"container\": \"source\",
  \"account_name\": \"$AZURE_BLOB_ACCOUNT_NAME\", \"account_key\": \"$AZURE_BLOB_ACCOUNT_KEY\"}" \
  "$BASE_URL/api/storages/azure/validate")
[[ $validate == 200 ]] && pass "Azure Blob storage validates" || fail "Azure Blob storage validates (HTTP $validate)"

# media must seek: audio playback asks for byte ranges, both for uploads and for files in Blob storage
AUDIO=$(mktemp).wav
python3 -c 'import sys, wave
w = wave.open(sys.argv[1], "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
w.writeframes(bytes(2 * 8000 * 5)); w.close()' "$AUDIO"
api -o /dev/null -F "file=@$AUDIO" "$BASE_URL/api/projects/$project_id/import"

docker compose exec -T app python3 - <<'EOF'
import os
from azure.storage.blob import BlobServiceClient
name = os.environ['AZURE_BLOB_ACCOUNT_NAME']
client = BlobServiceClient(f'https://{name}.blob.core.windows.net',
                           credential={'account_name': name, 'account_key': os.environ['AZURE_BLOB_ACCOUNT_KEY']})
client.get_blob_client('source', 'smoke/fictional.wav').upload_blob(bytes(80044), overwrite=True)
EOF
storage_id=$(api -H 'Content-Type: application/json' -d "{
  \"project\": $project_id, \"container\": \"source\", \"prefix\": \"smoke/\",
  \"use_blob_urls\": true, \"presign\": false,
  \"account_name\": \"$AZURE_BLOB_ACCOUNT_NAME\", \"account_key\": \"$AZURE_BLOB_ACCOUNT_KEY\"}" \
  "$BASE_URL/api/storages/azure" | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])')
api -o /dev/null -X POST "$BASE_URL/api/storages/azure/$storage_id/sync"

media=$(api "$BASE_URL/api/tasks?project=$project_id" \
  | python3 -c 'import json, sys; print("\n".join(t["data"]["audio"] for t in json.load(sys.stdin)["tasks"]))')
for kind in /data/upload/ /resolve/; do
  url=$(grep -F "$kind" <<<"$media" | head -1)
  [[ -n $url ]] || fail "media task for $kind"
  range=$(api -o /dev/null -w '%{http_code}' -H 'Range: bytes=1000-1999' "$BASE_URL$url")
  [[ $range == 206 ]] && pass "media seeks via $kind" || fail "media seeks via $kind (HTTP $range)"
  anonymous=$(status "$BASE_URL$url")
  [[ $anonymous == 401 ]] && pass "media refuses anonymous via $kind" \
    || fail "media refuses anonymous via $kind (HTTP $anonymous)"
done

echo "All smoke checks passed against $BASE_URL"
