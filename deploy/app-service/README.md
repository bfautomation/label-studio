# App Service harness

A local copy of the Azure App Service runtime for this image, for testing the image before it is
deployed. It holds no environment-specific values: everything it needs is generated locally and gitignored.

| Service | Stands in for | Notes |
|---|---|---|
| `app` | App Service container | Built from this repository. Only `/home` persists, owned by root as on App Service |
| `db` | Azure Database for PostgreSQL 16 | TLS required for every network login; the app verifies the server certificate and host name |
| `blob` | Azure Blob Storage | Azurite answering on `<account>.blob.core.windows.net` with a test certificate, because the Azure connector only talks to the public endpoint |

There is no Redis and no worker, as on App Service. The containers `source` and `exports` are
created on start.

## Use

```bash
cd deploy/app-service
./setup.sh                   # test CA, certificates and local secrets in .certs/ and .env
docker compose up -d --build
./smoke.sh                   # health, login page, a static asset, API, database TLS, Blob storage
docker compose down -v       # remove containers and all harness data
```

The app listens on `http://127.0.0.1:8090` (set `HARNESS_HTTP_PORT` to change it). Sign in with
`LABEL_STUDIO_USERNAME` and `LABEL_STUDIO_PASSWORD` from `.env`. To test an image built elsewhere, set
`HARNESS_IMAGE` and run `docker compose up -d` without `--build`.

The app trusts only the harness CA, so an unexpected outbound HTTPS call fails instead of leaving the machine.
This is a convenience, not proof that no egress happens.
