#!/usr/bin/env bash
# Generate the local-only secrets and test certificates for the harness.
# Everything written here is throwaway and gitignored; rerunning keeps existing files.

set -euo pipefail

cd "$(dirname "$0")"

ACCOUNT_NAME=stlabelstudiolocal
CERTS_DIR=.certs

mkdir -p "$CERTS_DIR"

if [[ ! -f "$CERTS_DIR/ca.crt" ]]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=Label Studio harness test CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "$CERTS_DIR/ca.key" -out "$CERTS_DIR/ca.crt" 2>/dev/null
fi

issue_cert() {
  local name=$1 dns=$2
  [[ -f "$CERTS_DIR/$name.crt" ]] && return
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$dns" \
    -keyout "$CERTS_DIR/$name.key" -out "$CERTS_DIR/$name.csr" 2>/dev/null
  openssl x509 -req -in "$CERTS_DIR/$name.csr" -CA "$CERTS_DIR/ca.crt" -CAkey "$CERTS_DIR/ca.key" \
    -CAcreateserial -days 30 -extfile <(cat <<EOF
subjectAltName=DNS:$dns
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid
EOF
    ) \
    -out "$CERTS_DIR/$name.crt" 2>/dev/null
  rm "$CERTS_DIR/$name.csr"
}

issue_cert db db
issue_cert blob "$ACCOUNT_NAME.blob.core.windows.net"
issue_cert frontdoor localhost
# containers read the keys as other users, and the files never leave this machine
chmod 644 "$CERTS_DIR"/*

if [[ ! -f .env ]]; then
  random() { openssl rand -base64 "$1" | tr -d '\n'; }
  cat > .env <<EOF
POSTGRES_PASSWORD=$(random 24 | tr -d '/+=')
APP_DB_PASSWORD=$(random 24 | tr -d '/+=')
SECRET_KEY=$(random 48)
AZURE_BLOB_ACCOUNT_NAME=$ACCOUNT_NAME
AZURE_BLOB_ACCOUNT_KEY=$(random 64)
LABEL_STUDIO_USERNAME=admin@example.com
LABEL_STUDIO_PASSWORD=$(random 18 | tr -d '/+=')
EOF
  chmod 600 .env
fi

echo "Harness secrets and certificates are ready in $(pwd)"
