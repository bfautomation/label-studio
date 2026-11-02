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
./capacity.sh                # how large an import, sync or export fits in one request (takes a few minutes)
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

## Media and tasks from Blob storage

Media files and the JSON task files that refer to them live in a Blob container (`source` in the harness). Label
Studio only reads it.

1. Put each media file in the container and leave it unchanged there.
2. Write a JSON task file for it into a folder of its own, for example:

   ```json
   {"data": {"audio": "azure-blob://source/<path to the media file>"}}
   ```

   `audio` must use this `azure-blob://<container>/…` form. It never expires and holds no secret; when a signed-in
   annotator opens the task, Label Studio turns it into a link to itself and streams the file. The other fields are
   whatever the project's labelling configuration asks for.
3. The project needs an import connection on that container: container prefix set to the task-file folder, import
   method "Tasks - Treat each JSON or JSONL file as a task definition", no account key (the app's own
   `AZURE_BLOB_ACCOUNT_KEY` is used). Without a connection on that container the media cannot play, even for tasks
   imported through the API.
4. An admin presses Sync on the import connection. Syncing again only adds new task files; it never duplicates tasks.

Exports keep `audio` exactly as imported, so results can be matched back to their media.

## Exporting final labels

Export connections write to storage only when someone presses Sync; they never copy labels as people save them and
never delete anything. Making labels final is therefore one deliberate step:

1. Annotators and reviewers finish and stop editing. Label Studio cannot lock a project, so this is an agreement,
   not a setting. Nothing reaches the export container while they work.
2. An admin adds an export connection on the export container (`exports` in the harness; once per project, a prefix
   of its own, "can delete objects" off) and presses Sync. Label Studio writes one file per annotation, named by the
   annotation's number; each holds the annotation and its task, including the `audio` reference.

Pressing Sync again writes the labels as they are at that moment and overwrites the earlier files. Deleting an
annotation in Label Studio leaves its file in storage.

## Limits without a worker

Without Redis or a worker, imports, storage syncs and exports run inside the web request that starts them.
uWSGI stops a request after 220 s and App Service's front door gives up after about 230 s; a request that runs
longer fails part-way. `./capacity.sh` measures how long each takes at growing sizes and stops a scenario at the
first size slower than the budget (110 s, half the limit). It runs unchanged against App Service;
those results replace the harness numbers below.

Harness, 2 November 2026 (database and storage on the same machine), 4 uWSGI workers:

| Tasks | Import (with annotations) | JSON export | Storage sync |
|---:|---:|---:|---:|
| 100 | 0.1 s | 0.1 s | 0.3 s |
| 1,000 | 0.7 s | 1.0 s | 1.6 s |
| 10,000 | 6.4 s | 11.2 s (10 MiB) | 15.3 s |

All three grow in proportion to the number of tasks and stay far below the budget at 10,000. Saving one annotation
takes about 0.04 s. While a long request runs it occupies one of the 4 workers.

Without Redis the fork's document upload (`/api/projects/<id>/upload-files`) refuses with 503, "Document upload queue
is unavailable", and creates nothing; ordinary imports are unaffected.

## Accounts

There is no email and no shell into the container on App Service, so accounts are managed in the app: through the
organization's invite link, and through Django admin at `/admin/`, which superusers can open.

### First deployment: the first admin

1. Store a strong password as a Key Vault secret.
2. For the first deployment only, add two app settings: `LABEL_STUDIO_USERNAME` set to the first admin's email, and
   `LABEL_STUDIO_PASSWORD` set to a Key Vault reference to that secret.
3. Deploy. At start-up the container creates the organization and this admin as a superuser; the log shows
   `label-studio init completed`.
4. Sign in as that admin and change the password, so the Key Vault copy is no longer the live one.
5. Give a second person the invite link (see below). Once they have signed up, open `/admin/` → Users → that person,
   tick **Staff status** and **Superuser status**, and save. Two superusers mean either can reset the other's
   password.
6. Remove both app settings, delete the Key Vault secret and restart. Both admins can still sign in. While the
   settings are present, start-up re-creates that admin if the account has been deleted.
7. Reset the invite link.

### Day to day

- **Inviting people:** share the organization's invite link and reset it once everyone has joined. After a reset the
  old link no longer works.
- **Offboarding:** in `/admin/` → Users, untick **Active**. Removing someone from the organization does the same. From
  then on their browser session, existing API tokens and attempts to sign in or sign up again are all refused.
- **Forgotten password:** another superuser sets a new one in `/admin/` → Users → that user → change password.
- **Changing `SECRET_KEY`:** signs everyone out and stops every personal access token working; people sign in again
  and create new tokens.

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
