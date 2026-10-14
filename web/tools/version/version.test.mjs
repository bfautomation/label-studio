import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import test from "node:test";

const run = promisify(execFile);
const script = fileURLToPath(new URL("./version.mjs", import.meta.url));

async function withProject(callback) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "ls-version-"));
  const project = path.join(root, "web", "apps", "example");
  await fs.mkdir(project, { recursive: true });
  const env = { ...process.env };
  for (const name of ["GIT_DIR", "GIT_COMMIT", "GIT_BRANCH", "GIT_DATE", "GIT_MESSAGE", "BRANCH_OVERRIDE"]) {
    delete env[name];
  }
  try {
    await callback({ root, project, env });
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
}

async function generate(project, env) {
  await run(process.execPath, [script], { cwd: project, env });
  return JSON.parse(await fs.readFile(path.resolve(project, "../../dist/apps/example/version.json"), "utf8"));
}

test("source archives generate version metadata when Git is not installed", async () => {
  await withProject(async ({ project, env }) => {
    const info = await generate(project, { ...env, PATH: "" });
    assert.equal(info.commit, "unknown");
    assert.equal(info.branch, "unknown");
    assert.ok(Number.isFinite(Date.parse(info.date)));
  });
});

test("fallback reads worktree common references and preserves branch paths", async () => {
  await withProject(async ({ root, project, env }) => {
    const common = path.join(root, "git-common");
    const gitDir = path.join(common, "worktrees", "example");
    await fs.mkdir(gitDir, { recursive: true });
    await fs.writeFile(path.join(root, ".git"), "gitdir: git-common/worktrees/example\n");
    await fs.writeFile(path.join(gitDir, "commondir"), "../..\n");
    await fs.writeFile(path.join(gitDir, "HEAD"), "ref: refs/heads/feature/documents\n");
    await fs.writeFile(path.join(common, "packed-refs"), "abc123 refs/heads/feature/documents\n");
    const info = await generate(project, { ...env, PATH: "" });
    assert.equal(info.commit, "abc123");
    assert.equal(info.branch, "feature/documents");
  });
});

test("explicit metadata overrides are used without Git", async () => {
  await withProject(async ({ project, env }) => {
    const expected = { commit: "explicit", branch: "release", date: "2026-10-14T00:00:00Z", message: "A release" };
    const info = await generate(project, {
      ...env,
      PATH: "",
      GIT_COMMIT: expected.commit,
      BRANCH_OVERRIDE: expected.branch,
      GIT_DATE: expected.date,
      GIT_MESSAGE: expected.message,
    });
    assert.deepEqual(info, expected);
  });
});
