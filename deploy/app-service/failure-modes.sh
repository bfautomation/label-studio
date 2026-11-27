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
trap 'rm -f "$VISITOR"; remove_disposable_harness' EXIT

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1" >&2; exit 1; }

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

echo "All failure checks passed on $HARNESS_IMAGE"
