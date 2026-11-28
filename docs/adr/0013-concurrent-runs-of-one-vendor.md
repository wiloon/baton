---
status: accepted
---

# Concurrent runs of one vendor are not serialised

Until #299, every agent run held an exclusive, nonblocking flock on `vendor-leases/<vendor>`,
shared with quota probes and the retrospective analyst: one CLI invocation per vendor per host.
Another repository's child that picked the same vendor deferred (rc 75, "vendor capacity
reserved") and workload selection skipped the vendor. The lease dated from the single-tick
runner, where it only kept retrospectives off the owner's own account use. Under `baton serve`
(ADR 0011) it meant two repositories could not both use Claude, though two CLI processes on one
account do not conflict as such. The owner decided that concurrent tasks may use the same vendor.
Where that is unsafe, the cause is fixed; the vendor is not serialised again.

What stays:

- **A run marker.** `dispatch()` holds a shared flock on `vendor-leases/<vendor>` for the whole
  run, the denial retry included. It excludes no other run.
- **The analyst's test.** Before choosing a vendor, the retrospective analyst tries an exclusive
  nonblocking flock on the marker and releases it at once. Failure means a workload is running:
  the candidate is blocked with `vendor_capacity` and the next one is tried. A run blocks on the
  shared lock only for the length of that test. The analyst holds nothing during the analysis;
  analyses stay serialised among themselves by the retrospective worker lock.
- **A probe lock.** Quota probes take an exclusive nonblocking flock on
  `quota-probes/<vendor>.lock`. A probe that loses returns without checking or starting a CLI,
  so concurrent probes spend one request. Probes neither take nor test the run marker.

## Consequences

- Workload priority over analyses (ADR 0001) now means that an analysis does not start while a
  workload of that vendor runs. A workload neither waits nor defers for an analysis, and a running
  analysis is not preempted, so the two can overlap. Steady workloads can postpone analyses.
- A same-vendor review (ADR 0002) no longer waits because the other vendor is merely running.
- Quota admission stays a check at pick time against the stored snapshot. Runs started from the
  same under-cap snapshot may together overshoot the weekly `cap` and `five_hour_max`, bounded by
  the number of runs in flight. The owner accepts this; there is no in-flight accounting,
  reservation or cost estimate. For more headroom, lower the caps.
- Probes now run while a run of the vendor is in flight, so a stale snapshot during a long run
  spends a probe request: at most one per `quota_stale_minutes` from picks, since each probe
  records `probed_at`. The quota worker keeps its backoff.
- Run counts, cooldowns and quota/credential snapshots were already safe write by write.
- Concurrent runs share the CLIs' home-directory state, below.

## Vendor CLI home state

Runs share the service user's home. baton sets no per-run `CLAUDE_CONFIG_DIR`, `CODEX_HOME` or
similar, which would cut a run off from its login. The findings come from baton's adapter code and
public sources, as of 2026-11-27. No concurrent run of a real CLI was made, and the installed CLI
versions and home directories were not inspected. A verdict is *safe*, *unsafe* or *unverified*.

### claude — unverified

- Shared files: `~/.claude/.credentials.json` (the stored `/login`, rewritten on OAuth token
  refresh) and the CLI's other user state under `~/.claude` and `~/.claude.json`.
- Evidence: user-filed reports of refresh-token rotation races between concurrent sessions, for
  example <https://github.com/anthropics/claude-code/issues/54443> and
  <https://github.com/anthropics/claude-code/issues/61923>. Found no confirmation from the vendor
  that current versions are safe or unsafe; not reproduced here.
- With `[vendors.claude] env_file`, a run gets `CLAUDE_CODE_OAUTH_TOKEN` (a setup-token) and does
  not use the stored login, which avoids the credential file.

### codex — unsafe at token refresh

- Shared files: `~/.codex/auth.json` (ChatGPT login, rewritten on refresh) and
  `~/.codex/sessions/**/rollout-*-<thread id>.jsonl`.
- Evidence: OpenAI's guide to CI authentication (<https://learn.chatgpt.com/docs/auth/ci-cd-auth.md>)
  says "Use one `auth.json` per runner or per serialized workflow stream." and "Do not share the
  same file across concurrent jobs or multiple machines.", and that Codex refreshes a bundle older
  than about 8 days, or after a 401, and writes the new tokens back. Not reproduced here.
- Session files are one per thread and baton reads them by thread id: safe.
- Copying `auth.json` per run would not help: each copy would rotate the same refresh token.

### cursor — unverified

- Shared files: `~/.config/cursor/auth.json` (may only point at the OS secret store, where the
  token then lives) and `cli-config.json`. The analyst already uses its own config directory with
  a link to `auth.json`.
- Evidence: a forum report of concurrent headless `cursor-agent` invocations failing at start
  (<https://forum.cursor.com/t/concurrent-headless-cursor-agent-invocations-fail-without-delay/142677>).
  Found nothing that settles whether current versions write these files safely.

### agy — unverified

- Shared state: the login in the OS keyring (`~/.gemini/antigravity-cli/antigravity-oauth-token`
  for SSH sessions), `~/.gemini/antigravity-cli/cli.log` and the updater's files under
  `~/.gemini/antigravity-cli/`.
- Evidence: baton passes settings on the command line (`--settings`), so it writes none to the
  home directory, and it filters `cli.log` by conversation id, so reading it is safe. Found no
  report of a token refresh race, and nothing that says refresh is safe.
