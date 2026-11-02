#!/usr/bin/env bash
# Smoke-check a running harness through its HTTPS front door: serving process, pages, static files,
# security headers and cookies, API, database TLS, Blob storage and media seeking.
# Usage: ./smoke.sh   (after `docker compose up -d`; exits non-zero on the first failed check)

set -euo pipefail

cd "$(dirname "$0")"
set -a
source .env
set +a

BASE_URL=${HARNESS_BASE_URL:-https://localhost:${HARNESS_HTTPS_PORT:-8443}}
CA_CERT=$(pwd)/.certs/ca.crt
COOKIES=$(mktemp)
trap 'rm -f "$COOKIES"' EXIT

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1" >&2; exit 1; }

# the front door's certificate comes from the harness CA
curl() { command curl --cacert "$CA_CERT" "$@"; }
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
# links are absolute once the public address is configured
asset=$(grep -oE '(src|href)="[^"]*/(static|react-app)/[^"]+\.(js|css)"' <<<"$login_page" | head -1 | cut -d'"' -f2 || true)
[[ $asset == /* ]] && asset=$BASE_URL$asset
[[ -n $asset && $(status "$asset") == 200 ]] && pass "static asset $asset" || fail "static asset ${asset:-none found}"

csrf=$(curl -s -c "$COOKIES" "$BASE_URL/user/login/" >/dev/null; awk '$6 == "csrftoken" {print $7}' "$COOKIES")
login=$(curl -s -o /dev/null -w '%{http_code}' -b "$COOKIES" -c "$COOKIES" -H "Referer: $BASE_URL/user/login/" \
  --data-urlencode "csrfmiddlewaretoken=$csrf" --data-urlencode "email=$LABEL_STUDIO_USERNAME" \
  --data-urlencode "password=$LABEL_STUDIO_PASSWORD" "$BASE_URL/user/login/")
grep -q sessionid "$COOKIES" && pass "admin signs in over HTTPS (HTTP $login)" \
  || fail "admin signs in over HTTPS (HTTP $login)"

for cookie in sessionid csrftoken; do
  [[ $(awk -v name="$cookie" '$6 == name {print $4}' "$COOKIES") == TRUE ]] && pass "$cookie cookie is HTTPS-only" \
    || fail "$cookie cookie is HTTPS-only"
done

headers=$(curl -s -D - -o /dev/null "$BASE_URL/user/login/")
grep -qi '^strict-transport-security: max-age=31536000' <<<"$headers" && pass "HSTS header" || fail "HSTS header"
grep -qi '^x-frame-options: SAMEORIGIN' <<<"$headers" && pass "frame protection header" \
  || fail "frame protection header"
# the front door overwrites the HTTPS note, so a client claiming plain HTTP is still treated as HTTPS
spoofed=$(curl -s -D - -o /dev/null -H 'X-Forwarded-Proto: http' "$BASE_URL/user/login/")
grep -qi '^strict-transport-security' <<<"$spoofed" && pass "front door sets the HTTPS note itself" \
  || fail "front door sets the HTTPS note itself"
[[ $(status -H 'Host: unknown.example' "$BASE_URL/health") == 400 ]] && pass "unknown host name is refused" \
  || fail "unknown host name is refused"

# the browser is told not to report: no analytics script on the login page, analytics and Sentry off once signed in
signed_in_page=$(api "$BASE_URL/projects/")
! grep -q googletagmanager <<<"$login_page" && grep -q 'collect_analytics: false' <<<"$signed_in_page" \
  && grep -q 'sentry_dsn: null' <<<"$signed_in_page" && pass "pages tell the browser not to report" \
  || fail "pages tell the browser not to report"

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
# the same image with its own trust store must refuse the harness database, proving the certificate is checked
untrusted=$(docker compose run --rm --no-deps -T -e PGPASSWORD="$APP_DB_PASSWORD" --entrypoint python3 blob-init -c '
import psycopg
try:
    psycopg.connect(host="db", dbname="labelstudio", user="labelstudio", sslmode="verify-full",
                    sslrootcert="/etc/ssl/cert.pem").close()
    print("connected")
except psycopg.OperationalError as error:
    print(error)' 2>&1)
[[ $untrusted == *"certificate verify failed"* ]] && pass "database certificate from an untrusted authority is refused" \
  || fail "database certificate from an untrusted authority is refused ($untrusted)"
docker compose exec -T app sh -c 'cd /label-studio/label_studio && python3 manage.py migrate --check' >/dev/null 2>&1 \
  && pass "database schema matches the image" || fail "database schema matches the image"

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
# Blob tasks arrive as JSON task files referring to their media file, as in real use; the storage
# connection asks for pre-signed links, the risky default, and the app must still stream through itself
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
container = client.get_container_client('source')
container.upload_blob('smoke/media/fictional.wav', bytes(80044), overwrite=True)
task = '{"data": {"audio": "azure-blob://source/smoke/media/fictional.wav"}}'
container.upload_blob('smoke/tasks/fictional.json', task, overwrite=True)
EOF
storage_id=$(api -H 'Content-Type: application/json' -d "{
  \"project\": $project_id, \"container\": \"source\", \"prefix\": \"smoke/tasks/\",
  \"use_blob_urls\": false, \"presign\": true,
  \"account_name\": \"$AZURE_BLOB_ACCOUNT_NAME\", \"account_key\": \"$AZURE_BLOB_ACCOUNT_KEY\"}" \
  "$BASE_URL/api/storages/azure" | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])')
api -o /dev/null -X POST "$BASE_URL/api/storages/azure/$storage_id/sync"

media=$(api "$BASE_URL/api/tasks?project=$project_id" \
  | python3 -c 'import json, sys; print("\n".join(t["data"]["audio"] for t in json.load(sys.stdin)["tasks"]))')
for kind in /data/upload/ /resolve/; do
  url=$(grep -F "$kind" <<<"$media" | head -1)
  [[ -n $url ]] || fail "media task for $kind"
  [[ $url == /* ]] && url=$BASE_URL$url
  range=$(api -o /dev/null -w '%{http_code}' -H 'Range: bytes=1000-1999' "$url")
  [[ $range == 206 ]] && pass "media seeks via $kind" || fail "media seeks via $kind (HTTP $range)"
  anonymous=$(status "$url")
  [[ $anonymous == 401 ]] && pass "media refuses anonymous via $kind" \
    || fail "media refuses anonymous via $kind (HTTP $anonymous)"
done

# every name the app looked up since it started must belong to the harness (see egress/sitecustomize.py)
outside=$(docker compose exec -T app sh -c 'sort -u /tmp/outbound-lookups.log' \
  | grep -vxF -e db -e "$AZURE_BLOB_ACCOUNT_NAME.blob.core.windows.net" || true)
[[ -z $outside ]] && pass "app contacted nothing outside the harness" \
  || fail "app contacted nothing outside the harness: $(tr '\n' ' ' <<<"$outside")"

echo "All smoke checks passed against $BASE_URL"
