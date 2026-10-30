#!/bin/sh
# Label Studio connects with its own login that owns its database, separate from the admin login.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" \
  -v app_password="$APP_DB_PASSWORD" <<'EOSQL'
CREATE ROLE labelstudio LOGIN PASSWORD :'app_password';
CREATE DATABASE labelstudio OWNER labelstudio;
REVOKE ALL ON DATABASE labelstudio FROM PUBLIC;
EOSQL
