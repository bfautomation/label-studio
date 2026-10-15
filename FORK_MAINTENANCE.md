# Maintaining the company fork

## Branches

The integration branch is being renamed from `company/main` to `main`. Until a repository administrator
completes the GitHub rename, use `company/main` wherever the instructions below say `main`.
Fork checks accept both names during this transition.

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

Resolve conflicts, run the fork checks, and open an internal PR into `main`.
Use merge commits to preserve upstream ancestry. Do not rebase or force-push shared integration branches.
Choose and validate upstream release tags separately when preparing a deployment.

Create upstream contribution branches from `upstream/develop`, then port only the relevant fix.
The preservation branch includes multiple independent changes and is not intended as a single upstream PR.

## Compatibility decisions in the first update

- Upstream uses uv, Bun, and Vite. The fork follows those build tools while retaining its version metadata fixes.
- Upstream now always embeds Data Manager annotator profiles. The fork preserves that response shape and batches user lookups across a page. Updater profiles retain the existing feature flag behavior.
- Document batches use per-file jobs on the `high` queue. Their own status and retry code checks those jobs; upstream's single-job import health check is skipped for document batches.
- The original fork migration `0035_projectimport_document_upload_fields` remains unchanged. Incoming upstream migration `0037_projectimport_progress_fields` depends on it and alters the existing `job_id` column instead of adding it again. This supports fresh installations and upgrades from the original fork. A database already migrated by unmodified upstream requires a separate migration plan.

## Validation

`.github/workflows/fork-checks.yml` runs checks for PRs into and pushes to `main`, without HumanSignal organization secrets:

- Document import, import status, Data Manager, Redis, version, and migration tests on SQLite and PostgreSQL.
- Standalone converter tests.
- Upload dialog tests, frontend version tests, and a production frontend build.

For deployment, additionally validate the complete container stack, converter/model setup, storage, and restore procedure in staging.
