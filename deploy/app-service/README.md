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
./persistence.sh             # work and sign-ins survive the app container being replaced (restarts the app)
docker compose down -v       # remove containers and all harness data
```

Open `https://localhost:8443` (set `HARNESS_HTTPS_PORT` to change it); the browser will warn about the harness's own
certificate authority unless you trust `.certs/ca.crt`. Sign in with `LABEL_STUDIO_USERNAME` and
`LABEL_STUDIO_PASSWORD` from `.env`. To test an image built elsewhere, set `HARNESS_IMAGE` and run
`docker compose up -d` without `--build`.

The app trusts only the harness CA, so an unexpected outbound HTTPS call fails instead of leaving the machine.
The proof is separate: `egress/sitecustomize.py` (harness only) records every name the app's Python processes look
up, and `smoke.sh` fails if any of them lies outside the harness.

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

`app.env` also carries the `WEBSITES_*` settings, which App Service reads rather than the app. App Service
configuration outside the app settings:

- Startup command `label-studio-uwsgi`.
- HTTPS only; the front door adds `X-Forwarded-Proto`, which the app trusts.

## What persists

App Service replaces the container on every restart and deployment; only `/home`, PostgreSQL and Blob storage
survive. `./persistence.sh` proves nothing is lost: it creates a project with an uploaded file and an
annotation, replaces the app container and checks everything with the sign-in from before. Sign-ins survive because
sessions are signed with `SECRET_KEY`, which therefore must never change between restarts.

Back up:

- the PostgreSQL database;
- `/home/label-studio/media` (uploaded files and exports) and `/home/label-studio/export`;
- the `source` and `exports` Blob containers.

Nothing else needs keeping: the app regenerates its version file at start-up, and uWSGI's process file lives in
`/tmp`. The harness cannot reproduce App Service's network-backed `/home`, so its file permissions are checked on the
real platform.

## Upgrading

The container applies pending database migrations every time it starts, under a lock so two containers never migrate
at once. To back up before the structure changes:

1. Compare the deployed image with the candidate: `./schema-changes.sh <deployed image> <candidate image>`. It needs
   Docker but no database access. Exit code 0 means no schema change, 1 means a schema change, 2 means it failed.
   The first deployment has nothing to compare and no data to protect.
2. On a schema change, back up the database before deploying.
3. Deploy, and look for `Migrations completed` in the container log.
4. If start-up fails after a schema change, restore the database and redeploy the previous image together: the
   previous image does not work against the changed structure.
