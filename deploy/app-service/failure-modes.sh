#!/usr/bin/env bash
# Check how the app behaves when its platform fails it. These checks replace the app container with broken settings,
# so they run in a harness of their own on exactly the image given, which is removed afterwards whatever happens.
# Usage: ./failure-modes.sh IMAGE   (exits non-zero if the image is missing or on the first failed check)

set -euo pipefail

cd "$(dirname "$0")"
source disposable-harness.sh

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 IMAGE" >&2
  exit 2
fi
start_disposable_harness "$1" failure-modes

BASE_URL=https://localhost:$HARNESS_HTTPS_PORT
CA_CERT=$(pwd)/.certs/ca.crt
VISITOR=$(mktemp)
TIMEOUT_PROBE=label-studio-timeout-probe-$$
trap 'rm -f "$VISITOR"; docker rm -f "$TIMEOUT_PROBE" >/dev/null 2>&1; remove_disposable_harness' EXIT

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $*" >&2; exit 1; }

curl() { command curl --cacert "$CA_CERT" "$@"; }
status() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
healthy() {
  for _ in $(seq 90); do
    [[ $(status "$BASE_URL/health") == 200 ]] && return 0
    sleep 2
  done
  return 1
}

healthy || fail "harness healthy"

# App Service passes a Key Vault reference's own text when it cannot resolve it. The container must refuse to start
# rather than create an admin with that text as the password or sign sessions with it, and start normally once the
# reference resolves
reference='@Microsoft.KeyVault(SecretUri=https://example.vault.azure.net/secrets/unresolved)'
first_admin=first-admin-$(date +%s)@example.com
first_admin_password=$(openssl rand -hex 12)
replace_app() { env "$@" docker compose up -d --force-recreate --no-deps app >/dev/null 2>&1; }
refuses_to_start() {
  local state=
  for _ in $(seq 30); do
    state=$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$(docker compose ps -aq app)")
    [[ $state == exited* ]] && break
    sleep 2
  done
  [[ $state == 'exited 1' ]] && docker compose logs app 2>&1 | grep -q "unresolved Key Vault reference: .*$1"
}
user_count() {
  docker compose exec -T db psql -U labelstudio -d labelstudio -tAc "SELECT count(*) FROM htx_user WHERE email = '$1'"
}

replace_app LABEL_STUDIO_USERNAME="$first_admin" LABEL_STUDIO_PASSWORD="$reference"
refuses_to_start LABEL_STUDIO_PASSWORD && [[ $(user_count "$first_admin") == 0 ]] \
  && pass "unresolved first-admin password: refuses to start, no admin created" \
  || fail "unresolved first-admin password: refuses to start, no admin created"
replace_app SECRET_KEY="$reference"
refuses_to_start SECRET_KEY && pass "unresolved signing key: refuses to start" \
  || fail "unresolved signing key: refuses to start"

replace_app LABEL_STUDIO_USERNAME="$first_admin" LABEL_STUDIO_PASSWORD="$first_admin_password"
healthy || fail "starts once the reference resolves"
csrf=$(curl -s -c "$VISITOR" "$BASE_URL/user/login/" >/dev/null; awk '$6 == "csrftoken" {print $7}' "$VISITOR")
curl -s -o /dev/null -b "$VISITOR" -c "$VISITOR" -H "Referer: $BASE_URL/user/login/" \
  --data-urlencode "csrfmiddlewaretoken=$csrf" --data-urlencode "email=$first_admin" \
  --data-urlencode "password=$first_admin_password" "$BASE_URL/user/login/"
grep -q sessionid "$VISITOR" && pass "once resolved, the first admin signs in with the intended password" \
  || fail "once resolved, the first admin signs in with the intended password"

# Requests that fail must be logged without their query string, which can carry secrets such as invite tokens.
# Each check also proves the failure was logged, so a silent log cannot pass
marker=not-a-real-token-$(openssl rand -hex 6)

# the app cannot be reached: the front door answers 502 and logs the request
docker compose stop app >/dev/null 2>&1
# past the 5 seconds the front door keeps the app's address, so it finds no app rather than waiting on the old address
sleep 6
unreachable=$(status "$BASE_URL/user/signup/?token=$marker")
frontdoor_logs=$(docker compose logs frontdoor 2>&1)
grep -qF -- "$marker" <<<"$frontdoor_logs" && marker_logged=yes || marker_logged=no
[[ $unreachable == 502 && $marker_logged == no ]] && grep -qF '"GET /user/signup/ HTTP/1.1" 502' <<<"$frontdoor_logs" \
  && pass "app unreachable: front door logs the failure without its query string" \
  || fail "app unreachable: front door logs the failure without its query string" \
    "(HTTP $unreachable, query string logged: $marker_logged)"

# a request outlives the worker's time limit: uWSGI kills it and reports it. Runs the image's own uwsgi.ini with only
# the app swapped for one that sleeps, and a limit of 2 seconds
docker run -d --name "$TIMEOUT_PROBE" --network none -e UWSGI_WORKER_HARAKIRI=2 -e UWSGI_PROCESSES=1 \
  --entrypoint sh "$HARNESS_IMAGE" -c 'mkdir -p /tmp/probe &&
    printf "%s\n" "import time" "def application(environ, start_response):" "    time.sleep(10)" \
      >/tmp/probe/sleeps.py &&
    exec uwsgi --ini /label-studio/deploy/uwsgi.ini --chdir /tmp/probe --module sleeps:application' >/dev/null
timed_out() {
  docker exec "$TIMEOUT_PROBE" python3 -c "import urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:8000$1', timeout=30)
except Exception:
    pass"
}
for _ in $(seq 30); do
  docker logs "$TIMEOUT_PROBE" 2>&1 | grep -q 'WSGI app 0' && break
  sleep 1
done
timed_out "/with-query?token=$marker"
timed_out /no-query
for _ in $(seq 10); do
  timeout_logs=$(docker logs "$TIMEOUT_PROBE" 2>&1)
  grep -qF 'GET /no-query' <<<"$timeout_logs" && break
  sleep 1
done
grep -qF -- "$marker" <<<"$timeout_logs" && marker_logged=yes || marker_logged=no
[[ $(grep -c 'HARAKIRI ON WORKER' <<<"$timeout_logs") -ge 2 && $marker_logged == no ]] \
  && grep -qF 'GET /no-query' <<<"$timeout_logs" \
  && pass "request timed out: uWSGI reports it without its query string" \
  || fail "request timed out: uWSGI reports it without its query string (query string logged: $marker_logged)"

echo "All failure checks passed on $HARNESS_IMAGE"
