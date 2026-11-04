# Security

Security fixes target the latest revision of `main`. Older releases have no separate maintenance
window.

## Reporting a vulnerability

Use **Security → Report a vulnerability** on this repository to send a private report when that
feature is available. Include the affected revision, the execution role and vendor, a minimal
reproduction, and the expected and observed permissions. Use dummy credentials in examples.
If private reporting is unavailable, open an issue asking for a private reporting channel without
including exploit details or credentials. Rotate any exposed credential immediately.

## Execution boundary

baton executes generated code, repository tests, package-manager scripts and configured hooks.
Treat these as code running with the runner account's access. Agent output and repository content
can contain prompt injection.

| Vendor | Controls | Limits |
| --- | --- | --- |
| Claude Code | CLI permissions and `bin/guard.sh` reject known operations | Shell command filters do not constrain everything an allowed interpreter, test or build can do. |
| Codex | CLI read-only or workspace-write sandbox; network configured by role | Reads and child processes follow the installed CLI's sandbox policy; dev/fix can use the network and configured cache directories. |
| Cursor | GitHub credential overrides; changes checked after read-only runs | `--force --trust` is not host isolation. Restoring tracked files after a run cannot undo arbitrary side effects. |

Setting invalid GitHub tokens in a child process does not isolate credential files, SSH agents,
other ambient credentials, or the runner's home directory. The runner itself needs GitHub write
permissions to publish artifacts and merge eligible PRs. Repository secrets explicitly configured
for dev/fix are intentionally accessible to those runs.

Use a dedicated OS account on a disposable host or VM. Give it only the repositories, test
credentials and outbound access it needs. Keep production credentials, unrelated repositories,
host-management sockets and personal SSH agents outside that environment. Scope the runner's
GitHub token to the watched repositories and required operations. Keep integration credential
files owner-readable only (`chmod 600`), and keep raw run logs private.

Host-side verify and gate hooks keep their definitions outside a PR, but execute the code being
tested. They do not create an execution sandbox. Review the first runs with `auto_merge = false`.

## External contributions

Anyone may report a bug or propose a change through a fork PR. Only a `ready-for-agent` label
whose latest label event was applied by the configured `owner` authorizes an external issue.
baton checks all event pages and skips issues when authorization cannot be verified; scheduled
and manual issue entry points use this rule. GitHub users with write or triage access can still
apply labels in the UI, but their `ready-for-agent` labels do not authorize baton. The owner
must remove and reapply those labels. The label authorizes using the issue body as task input:
inspect that body first and account for later edits by its author. Scanner labels alone
do not authorize external issues. Fork PRs are reviewed manually; baton does not automatically check out
and run them on its privileged host. Only trusted account comments are used as agent feedback.

CI for fork PRs runs on GitHub-hosted runners with read-only repository permissions. Keep custom
secrets out of that workflow and require maintainer approval for outside contributors. Do not
switch to `pull_request_target` to execute contributor code with write permissions or secrets.
