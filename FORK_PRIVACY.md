# Privacy rules for contributing to this fork

This repository is a **public** fork. Anything pushed to it can be read by anyone, and in practice it is permanent:
later commits do not remove it from history, and other people's clones, forks and caches keep their own copies.
Write every contribution so that a stranger reading it learns nothing about the company, its clients, its people,
its plans or its infrastructure beyond what the code itself needs.

These rules apply to people and to coding agents alike. Agents also follow [For agents](#for-agents).

## What counts as published

Everything you push or post, including the parts that are easy to forget:

- code, comments, tests and test data, documentation and configuration;
- commit messages and the **author and committer name and email** stamped on every commit;
- branch and tag names;
- pull request titles, descriptions and review comments, and issues;
- CI workflow files, and the logs and artifacts of workflows that run in public.

## Keep it public-safe

| Category | Examples of what stays out | What to write instead |
|---|---|---|
| Personal data | people's names, email addresses or usernames written into content, machine names, home-folder paths such as `/Users/<name>/` | a GitHub username and no-reply address (see [Git identity](#git-identity)); relative paths |
| Client or real data | real files, records or screenshots, or test data shaped like a real client's | clearly fictional data (`fictional-0001.txt`, `example.com`) |
| Company and product information | internal project or product names, product plans, client names, ticket numbers or aliases, internal tool names | a description of the behaviour itself ("limit the upload size") |
| Infrastructure identifiers | subscription, tenant or account IDs, resource and environment names, internal domains, IP addresses, region choices | generic placeholders (`<registry>`, `<app host name>`) and settings read from the environment |
| Security set-up and procedures | how access is restricted, who holds which role, admin and recovery procedures, monitoring gaps | the general capability ("access can be limited by network"), with the specifics kept privately |
| Credentials | passwords, keys, tokens, connection strings, certificates, even expired or test ones | variables supplied at run time, and secrets generated locally and ignored by git |

When a line is borderline, keep it out and ask a repository owner.

## Where things belong

- **This public fork:** changes to Label Studio and generic documentation of how they work, useful to anyone running
  it.
- **A private repository:** anything specific to how the company runs Label Studio: environment configuration,
  operating guides and runbooks, deployment pipelines that name company resources, evidence and test results from
  company environments, and notes linking work to tickets.
- **Nowhere:** credentials and real data.

## Git identity

Your GitHub account is public: pull requests, reviews and comments show its username. The goal is to publish nothing
beyond what that account already shows. Commits carry their own name and email, so give them your GitHub username and
your GitHub no-reply address. That keeps a working email address, which attracts spam and phishing, and your
machine's name, which git uses when no email is set, out of the history. GitHub shows the no-reply address under
Settings → Emails; it looks like `<id>+<username>@users.noreply.github.com`.

```bash
git config user.name "<GitHub username>"
git config user.email "<id>+<username>@users.noreply.github.com"
```

These commands set the identity for this repository only. Without them git may fall back to a global setting or to
your machine's name.

## Writing for a public repository

- **Names:** branches, commits and files describe the change (`limit-upload-size`), never the ticket or project behind
  it.
- **Comments and docs:** explain what the code does and why that behaviour is right; leave out who needed it and for
  which project.
- **Test data:** obviously fictional, generated in the test where possible, and never derived from real material.
- **Pull requests:** describe the change and how it was tested. Link to private tickets only from private places.

## Before every push

Push only when every item below is checked against what is about to be published, not against what you remember
writing. Compare with `origin/main` (or the branch you are targeting):

1. **Identity.** `git log --format='%h %an <%ae> | %cn <%ce>' origin/main..HEAD` shows only your GitHub username and
   no-reply address on every line.
2. **Messages.** `git log --format='%B' origin/main..HEAD` reads as a description of changes, with no ticket,
   project, client or person named.
3. **Content.** `git diff origin/main...HEAD` has been read line by line against
   [Keep it public-safe](#keep-it-public-safe), including tests, fixtures and configuration.
4. **Branch name.** `git branch --show-current` describes the change only.

## If something slips through

- **Not pushed yet:** fix it in history, not with a new commit on top. Reword or edit with an interactive rebase;
  `git rebase --exec 'git commit --amend --no-edit --reset-author' origin/main` re-stamps every commit with the
  current identity.
- **Already pushed:** treat it as published for good. Rotate any credential at once, tell a repository owner, and let
  them decide on removal. Rewriting shared history needs their approval, and copies elsewhere may remain.

## For agents

Coding agents read [`AGENTS.md`](AGENTS.md), which points here. In addition to everything above:

- Bring nothing from private sources into the repository: tickets, private documents, chats, working notes and agent
  memory stay out of code, comments, docs, commit messages and pull requests.
- Keep working notes, plans and evidence outside the repository.
- Write pull request titles, descriptions, review comments and issues from the diff and the test results alone.
  Summaries drafted from a conversation or a ticket pick up private context, so check each one against
  [Keep it public-safe](#keep-it-public-safe) before posting it.
- Run [Before every push](#before-every-push) on your own changes and report the result to the human you work with.
- Push, open pull requests or post comments only when that human has explicitly asked for it.
- When unsure whether something is public-safe, ask the human instead of deciding.

## Reviewing

Every review checks privacy as well as correctness: the diff against [Keep it public-safe](#keep-it-public-safe),
the commit messages, the commit identities and the branch name. Request changes for any finding; a fix before merge
is cheap, a fix after merge is not possible.
