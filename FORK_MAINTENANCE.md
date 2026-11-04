# Maintaining the company fork

## Branches

- `feature/document-workflow` preserves the original three custom commits before the first upstream update.
- `main` integrates upstream with the document workflow and company fixes. Target internal pull requests here.
- `maintenance/sync-upstream-*` branches contain upstream updates for review and validation.
- `develop` retains the original published history for now. It is not the current company integration branch.
- `upstream/develop` is the local remote-tracking reference for HumanSignal's development branch.

The first upstream update incorporates HumanSignal commit `1478690ff55816e952c1141c39c333dcc56d7f73`.
This is a development snapshot, not a production release recommendation.

## Updating upstream

```bash
git fetch upstream develop --tags
git switch main
git pull --ff-only origin main
git switch -c maintenance/sync-upstream-YYYY-MM-DD
git merge --no-commit upstream/develop
```

Resolve conflicts so that every row of [Behaviour divergences from upstream](#behaviour-divergences-from-upstream)
still holds, run the fork checks, and open an internal PR into `main`.
Use merge commits to preserve upstream ancestry. Do not rebase or force-push shared integration branches.
Choose and validate upstream release tags separately when preparing a deployment.

Create upstream contribution branches from `upstream/develop`, then port only the relevant fix.
The preservation branch includes multiple independent changes and is not intended as a single upstream PR.

## Compatibility decisions in the first update

- Upstream uses uv, Bun, and Vite. The fork follows those build tools while retaining its version metadata fixes.
- Upstream now always embeds Data Manager annotator profiles. The fork preserves that response shape and batches user lookups across a page. Updater profiles retain the existing feature flag behavior.
- Document batches use per-file jobs on the `high` queue. Their own status and retry code checks those jobs; upstream's single-job import health check is skipped for document batches.
- The original fork migration `0035_projectimport_document_upload_fields` remains unchanged. Incoming upstream migration `0037_projectimport_progress_fields` depends on it and alters the existing `job_id` column instead of adding it again. This supports fresh installations and upgrades from the original fork. A database already migrated by unmodified upstream requires a separate migration plan.

## Behaviour divergences from upstream

The fork changes these upstream behaviours on purpose. Keep each one when merging upstream, and change its guard test
only when the divergence itself is meant to change. Paths are under `label_studio/` unless shown otherwise.

| Divergence | Why | Lives in | Guarded by |
|---|---|---|---|
| Django sends `X-Frame-Options: SAMEORIGIN` and, over HTTPS, HSTS for a year | Pages are protected without a reverse proxy in front | `core/settings/base.py` | `core/tests/test_security_headers.py` |
| Usage analytics, vendor error reporting (Sentry) and the version check are off unless configured (`COLLECT_ANALYTICS`, `SENTRY_DSN`, `FRONTEND_SENTRY_DSN`, `LATEST_VERSION_CHECK`) | Nothing leaves the deployment unless deliberately enabled | `core/settings/base.py`, `core/settings/label_studio.py` | `core/tests/test_settings.py` |
| Storage connections never hand browsers pre-signed links (`ALLOW_PRESIGNED_URLS=false`) | Media is always streamed through the app, with its sign-in and permission checks | `core/settings/base.py`, `io_storages/proxy_api.py`, `io_storages/azure_blob/form_layout.yml` | `tests/test_presign_storage_data.py`, `io_storages/tests/test_proxy_api.py` |
| Export connections write to storage only on Sync (`EXPORT_STORAGE_SYNC_ON_SAVE=false`) | Labels leave the app in one deliberate step, never as they are saved or deleted | `core/settings/base.py`, `io_storages/*/models.py` | `io_storages/tests/test_export_on_sync_only.py` |
| Uploaded and exported files are deleted with their records, including when a project is deleted | Deleting data leaves no copy behind in storage | `core/utils/db.py`, `data_import/models.py`, `data_export/models.py`, `projects/functions/__init__.py`, `projects/api.py`, `organizations/functions.py` | `tests/data_import/test_file_upload_cleanup.py`, `data_export/tests/test_file_cleanup.py` |
| Removing a member from the organization deactivates them | Their sessions, API tokens and new sign-ins are refused | `organizations/api.py` | `organizations/tests/test_member_removal.py` |
| Request logs carry the path without the query string | Query strings can carry secrets such as invite tokens | `core/utils/formatter.py`, `deploy/uwsgi.ini` | `core/tests/test_log_formatter.py`; `deploy/app-service/smoke.sh` |
| The `django` logger does not propagate to the root logger | Both use the console handler, so every line would print twice | `core/settings/base.py` | `core/tests/test_settings.py` |

Upstream tests of the original behaviour switch it back on with the `allow_presigned_urls` and
`export_storage_sync_on_save` fixtures (`tests/conftest.py`, `io_storages/tests/conftest.py`). Keep those fixtures on
the tests that use them when merging upstream changes to those tests.

## Validation

`.github/workflows/fork-checks.yml` runs checks for PRs into and pushes to `main`, without HumanSignal organization secrets:

- Document import, import status, Data Manager, Redis, version, and migration tests, and the guard tests of the
  [behaviour divergences](#behaviour-divergences-from-upstream), on SQLite and PostgreSQL.
- Standalone converter tests.
- Upload dialog tests, frontend version tests, and a production frontend build.

`deploy/app-service/qualify.sh IMAGE` runs an image in a local copy of its Azure App Service runtime (see
[`deploy/app-service/README.md`](deploy/app-service/README.md)) and checks serving, security headers, database TLS,
Blob storage, media, persistence across container replacement, and that the app contacts nothing outside it.
`azure-pipelines-image.yml` runs it on every push to `main` and publishes the image only after approval. Run it on a
freshly built image before merging an upstream update.

For deployment, additionally validate the converter/model setup and the restore procedure in staging.

## Repository governance

AI Engineering (`@bfautomation/ai-engineering`) owns this public company fork and has write access.
Because the fork is public, every contribution follows [`FORK_PRIVACY.md`](FORK_PRIVACY.md).
The `default-branch` ruleset targets GitHub's default branch, currently `main`, and requires:

- A pull request with one current CODEOWNER approval, including approval of the latest reviewable push.
- Dismissal of stale approvals and resolution of review conversations.
- The GitHub Actions `repository-policy` check on an up-to-date branch. It requires configuration validation,
  both database test jobs, and the frontend tests/build to succeed; skipped or cancelled jobs fail the gate.
- No force pushes or default-branch deletion, and no standing ruleset bypass.

Use rebase merges for ordinary company changes. Use merge commits for upstream synchronization to retain
upstream commit identities and ancestry. Squash merging and mandatory linear history are deliberately
excluded from this fork's configuration. Merged PR branches are deleted automatically; the separate
`feature/document-workflow` preservation branch remains available.

Workflow tokens default to read-only and cannot approve PRs. The fork-owned workflow pins external actions
to full reviewed commit SHAs and validates those pins. Dependency alerts, Dependabot security updates,
secret scanning and push protection are enabled. Recheck these settings after transfers or policy changes.

Inherited HumanSignal publishing, release, command-dispatch, support, documentation indexing, cache cleanup,
and upstream-only integration workflows are disabled in this fork's GitHub Actions settings. Their source is
retained to minimize upstream merge conflicts. Fork checks and CodeQL remain enabled. Review newly imported
workflows during upstream updates before enabling them or supplying credentials.

Repository administrators can change the ruleset, but have no standing bypass. Any emergency policy change
must be explicitly authorized, recorded, time-bounded, and reverted after the incident. These GitHub settings
are maintained outside Git; changing this document alone does not enforce them.
