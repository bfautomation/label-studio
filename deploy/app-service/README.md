# App Service harness

A local copy of the Azure App Service runtime for this image, for testing the image before it is
deployed. It holds no environment-specific values: everything it needs is generated locally and gitignored.

| Service | Stands in for | Notes |
|---|---|---|
| `frontdoor` | App Service's front door | Terminates HTTPS on `https://localhost:8443`, forwards plain HTTP to the app and always sets `X-Forwarded-Proto: https`. Gives up after 230 s, as App Service does |
| `app` | App Service container | Built from this repository and started as `label-studio-uwsgi`: uWSGI alone on port 8000, no nginx, Django serving static files and uploads. Not published to the host. Only `/home` persists, owned by root as on App Service |
| `db` | Azure Database for PostgreSQL 16 | TLS required for every network login; the app verifies the server certificate and host name |
| `blob` | Azure Blob Storage | Azurite answering on `<account>.blob.core.windows.net` with a test certificate, because the Azure connector only talks to the public endpoint |

There is no Redis and no worker, as on App Service. The containers `source` and `exports` are
created on start.

## Use

```bash
cd deploy/app-service
./setup.sh                   # test CA, certificates and local secrets in .certs/ and .env
docker compose up -d --build
./smoke.sh                   # serving, pages, security headers and cookies, API, database TLS, Blob storage, media
docker compose down -v       # remove containers and all harness data
```

Open `https://localhost:8443` (set `HARNESS_HTTPS_PORT` to change it); the browser will warn about the harness's own
certificate authority unless you trust `.certs/ca.crt`. Sign in with `LABEL_STUDIO_USERNAME` and
`LABEL_STUDIO_PASSWORD` from `.env`. To test an image built elsewhere, set `HARNESS_IMAGE` and run
`docker compose up -d` without `--build`.

The app trusts only the harness CA, so an unexpected outbound HTTPS call fails instead of leaving the machine.
This is a convenience, not proof that no egress happens.

## App Service settings

`app.env` holds every app setting that is neither secret nor environment-specific; the harness loads it unchanged and
App Service copies it. The remaining app settings:

| Setting | Kind | Value on App Service |
|---|---|---|
| `POSTGRE_HOST` | per environment | PostgreSQL server name |
| `LABEL_STUDIO_HOST`, `CSRF_TRUSTED_ORIGINS` | per environment | `https://` plus the app's host name |
| `ALLOWED_HOSTS` | per environment | the app's host name |
| `AZURE_BLOB_ACCOUNT_NAME` | per environment | annotation storage account; the fallback when a storage connection has no key of its own |
| `POSTGRE_PASSWORD` | secret | Key Vault reference |
| `SECRET_KEY` | secret | Key Vault reference; must stay the same across restarts, because it signs sessions |
| `AZURE_BLOB_ACCOUNT_KEY` | secret | Key Vault reference, so rotating the key needs no change inside Label Studio |

`LABEL_STUDIO_USERNAME`, `LABEL_STUDIO_PASSWORD` and `REQUESTS_CA_BUNDLE` are harness only.

The app verifies the PostgreSQL certificate against the image's own trust store (`PGSSLROOTCERT=/etc/ssl/cert.pem`),
which includes the authorities Azure Database for PostgreSQL uses. `POSTGRE_HOST` must therefore be the server's real
name, `<server>.postgres.database.azure.com`, not an IP address or the private-link alias. The harness mounts its
own CA over that file.

App Service configuration outside the app settings:

- Startup command `label-studio-uwsgi`, with `WEBSITES_PORT=8000`.
- `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`, so `/home` is persistent storage.
- HTTPS only; the front door adds `X-Forwarded-Proto`, which the app trusts.
