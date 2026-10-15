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

## Repository governance

AI Engineering (`@bfautomation/ai-engineering`) owns this public company fork and has write access.
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
