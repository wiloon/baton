# Task retrospectives

The retrospective command reads the local telemetry ledger and writes versioned JSON and Markdown
reports. It uses explicit terminal task states (`merged`, `planned_handoff`, `abnormal_handoff`),
not inactivity. Handoff ends an automation episode; the underlying issue may still need work.
No GitHub calls, optimization PRs, tags or deployments are made by this command.

## Run locally

From the BATON checkout:

```sh
uv run --frozen python -m baton.retrospectives --config config.toml catch-up
uv run --frozen python -m baton.retrospectives --config config.toml --repo acme/app episode PIPELINE_UUID
```

`--state DIR` selects a source runner state for an offline copy. `--work-state DIR` selects the
independent processing journal (default `STATE/retrospectives`). `--output DIR` selects reports
(default `WORK_STATE/reports`). Keep these locations separate from source telemetry and use the
same work-state for every invocation against the same host, including manual runs. Each line on
stdout is JSON with an outcome, reason and report path when published. Exit 2 means invalid local
configuration/metadata or an I/O failure. Deferred analysis and individual model failures remain
visible in reports and do not fail workload execution.

The source task index and immutable event outbox are reconciled in memory, including a terminal
event persisted before its index update. Source files are never repaired or rewritten. A corrupt
metadata file stops the scan rather than silently hiding potentially live work. Complete
associated run records and a completed attempt covering terminal execution are required. Older
telemetry without attempt-completion evidence is deferred as `attempt_completion_unknown`;
update/fix the producer rather than treating silence as successful completion. A `running` record
is conservatively deferred until the regular telemetry collector reconciles it.

The initial settling delay is five minutes since that episode's latest evidence. It is a buffer
for final accounting, not a completion signal or host-wide idle requirement. The scan reads stable
snapshots, checks the digest again before publishing results, and processes later evidence as a
new revision. An unrelated active task does not prevent a completed episode's retrospective.

## Configure the analyst

Evidence generation works without vendor access. The optional `[retrospectives]` section is
backwards compatible and analysis is off unless `enabled = true`. When an old configuration
omits `enabled`, a nonempty legacy `model` opts in; explicit `enabled = false` always disables analysis. Candidates are restricted to **Claude Opus via Claude Code**, **GPT Sol via Codex** and
**Grok 4.7 via Cursor**. Sonnet, Auto and other model families are rejected in the role model
fields. A deprecated `model = "sonnet"` from an old deployment is an opt-in flag only: its value is
never forwarded to a CLI; the new strong-model fields supply actual requests.

```toml
[retrospectives]
enabled = true
model_claude = "opus"
model_codex = "gpt-6.1-sol"
model_cursor = "grok-4.7-high"
effort = "high"
paused = false
settle_seconds = 300
daily_calls = 3
revision_attempts = 2
timeout_seconds = 600
input_chars = 20000
output_chars = 20000
budget_usd = 1.0
```

The machine-wide `quota.vendor_pool` / `quota.vendor_extra` still enables vendors. The new
`preferences.retrospective` order overrides `preferences.reviewer`; when absent the reviewer
order is reused. The shared `baton.vendor_policy` module supplies cooldown/capacity checks,
quota pacing, close-tie preferences and the review policy for extras. Workload review retains its
own author/contribution constraints; retrospectives assess all eligible candidates without
contribution weighting. Quota caps and unavailable credentials always take precedence over
preferences. A lease race reselects another available vendor without waiting for the runner.

A failed vendor is a last resort on the next activation for that report revision. All vendors
share one invocation per activation, the UTC daily cap and the revision attempt allowance;
switching vendors does not create extra attempts. If none is available, save the evidence with
per-vendor selection reasons and defer. Model selection never falls back to Sonnet or Auto.

Analysis uses a fresh scratch directory with a bounded evidence-only prompt. Claude uses safe
mode with tools/MCP/skills/hooks/memory disabled, and no session persistence. Codex uses ephemeral
read-only execution, ignores user config/rules, disables command execution, browsing, apps,
plugins, memories and collaboration, and omits project/skill instructions. Cursor uses ask mode,
an isolated CLI configuration linking its existing authentication file, and explicit deny rules
for shell, file reads/writes, web fetch and MCP. No adapter runs in a task checkout, receives
repository secrets or inherits workload telemetry context. Unexpected tool-call events or a
reported model outside the approved families invalidate the assessment while preserving known
usage and deterministic evidence. These CLI controls do not provide host isolation.

The bounded summary includes only allowlisted ledger metadata, measurements and a bounded subset
of run records. Raw logs or unrestricted monitoring are unavailable. Selection explanations for
the original workload remain unavailable when they exist only in free-form runner logs; analyst
selection itself is structured in each report/attempt. The invocation flags follow the
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference),
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) and
[Cursor permissions/configuration](https://prod.cursor.com/docs/cli/reference/permissions).
Verify deployed CLI versions, authentication and the explicit Grok model ID during host acceptance.
GNU `timeout` is required, as for workload execution.

Before analysis, the selected vendor must be enabled and have a recent authenticated credential
observation and fresh quota windows (weekly for Cursor; five-hour and weekly for the pool vendors) below configured caps, with no active cooldown. Missing or
stale capacity evidence defers analysis; retrospectives do not run quota probes themselves. Enable
the independent quota timer described below if the reports remain deferred. An unavailable executable or
unsupported CLI flag preserves the evidence and consumes an attempt rather than retrying forever.

The default limits are one analysis per activation, three per UTC day and two attempts per report
revision. One worker runs at a time. Failed and ambiguous attempts count. Calls have a maximum
600-second deadline, at most 20,000 input characters including instructions, at most 20,000 accepted
assessment characters, and Claude's configured `--max-budget-usd`. The CLI transport is separately
bounded. Character and USD limits do not guarantee a fixed token count or subscription charge.
The USD limit is enforced by Claude's CLI only; Codex/Cursor are bounded by time, serialized
input/output size and call counts, their CLIs do not enforce `budget_usd`. Each attempt records
which budget controls were available. `effort` selects high/xhigh reasoning for Claude/Codex;
Cursor's reasoning level comes from its explicit high/xhigh model ID.
Requested model, observed actual models, CLI version, limits and known usage are recorded per
attempt separately from workload totals. An unknown actual model stays unknown rather than being
inferred from the requested alias; this is particularly relevant to Cursor and ephemeral Codex
output. A provider-reported Sonnet model is never accepted as an Opus/Grok/Sol result.
Reasoning tokens are not added again to output totals;
unknown and partial usage remain explicit. The USD figure is a reported API-equivalent cost,
not a claim about subscription billing.

## Parallel work and pause

Retrospectives never acquire the workload runner's global lock. Their workspace, processing state
and worker lock are independent. Workloads, quota probes and analyses share a **nonblocking
per-vendor lease**: one CLI invocation per vendor at a time, across repositories. Thus a Claude
retrospective and a Codex workload can run concurrently. Same-vendor work waits or selects another
vendor; a workload start race yields instead of blocking on a lock. This initial account/session
constraint avoids concurrent quota check/start races, but does not preempt an already running
analysis. Keep analysis deadlines/budgets small if workload latency is more important; stop or
pause retrospectives before a time-sensitive workload. Other hosts and owner sessions do not
participate in these local leases, so provider-reported limits remain authoritative.

Workload scheduling being busy or paused does not pause retrospectives. Set
`retrospectives.paused = true` or create `WORK_STATE/paused` to defer new analysis calls while
continuing evidence generation. Remove the file or reset configuration to resume. An in-flight
call finishes within its deadline; stopping the systemd service terminates its control group.

## Recovery, reports and retries

Find current reports through `REPORTS/<identity-hash>/<pipeline-uuid>/current.json`. Its
`generation` field names a directory containing `report.json` and `report.md`. The marker is
updated only after both artifacts are written and validated. Revisions and earlier generations
are retained. The source digest, schema, analyzer version and analysis configuration determine
revision identity. Unchanged successful reports make no new model call.

`WORK_STATE/attempts/*.json` is the durable analysis journal. An attempt is recorded before
starting the CLI; a response is validated and saved before publishing the completed report.
Restart can reuse a successful saved assessment even if report publication was interrupted.
An interrupted attempt is marked ambiguous with unknown usage and counts against budgets;
a new call waits until its deadline/watchdog grace has elapsed. Exactly-once provider invocation
is not promised. The independent GNU timeout watchdog bounds an orphaned CLI after worker death;
the vendor lease is inherited by the watchdog, and the supplied service also uses
`KillMode=control-group`.

After per-revision attempts are exhausted, inspect the failure and explicitly retry:

```sh
uv run --frozen python -m baton.retrospectives --config config.toml episode PIPELINE_UUID --retry
```

This resets that revision's attempt allowance, retaining history and the daily cap. Changing
analysis configuration or receiving new evidence creates a new revision; the daily cap still
applies. Report files stay local and can contain private repository identities and hypotheses;
review/sanitize them before sharing. Global probe overhead remains separate; inspect it with
`python -m baton.audit probes`.

## Enable the timer on the target host

Normal workload calls and retrospective calls publish provider quota observations to the same
`STATE/quota-VENDOR.json` files. Claude/Codex observations are extracted when present; ephemeral
Codex responses without limits do not refresh their age. Cursor usage is queried from its API.
Successful authentication is tracked separately. Each quota update holds a per-vendor storage
lock across read/merge/write, rejects older observations per window and atomically replaces the
file using a unique temporary name. Readers see a complete generation without acquiring a lock.

The optional `baton-quota.timer` checks every five minutes after completion, independently of
both workload scheduling and retrospective analysis. It probes only enabled vendors whose required
quota windows are missing, reset or older than `[quota].quota_stale_minutes` (default example: 30),
or whose authenticated credential observation has expired. It acquires the shared vendor lease
and rechecks before probing, yielding to active model calls and respecting vendor cooldowns.
Cursor probes query its API without a model call; Claude/Codex probes make a small model request.
Failed or incomplete probes back off from `quota_probe_retry_minutes` (default 5) up to
`quota_probe_max_retry_minutes` (default 60). A probe attempt does not refresh quota observations.
Normal calls that provide fresh data clear the retry backoff at the next check. No work is required
in the main loop for either timer to operate. Manual workload selection retains its existing
on-demand probes as a fallback.

Install and activate the quota worker alongside the retrospective timer:

```sh
cp systemd/baton-quota.service systemd/baton-quota.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now baton-quota.timer
systemctl --user start baton-quota.service
journalctl --user -u baton-quota.service
```

Install the supplied units as user services, adjusting the checkout/uv paths if needed:

```sh
mkdir -p ~/.config/systemd/user
cp systemd/baton-retrospectives.service systemd/baton-retrospectives.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now baton-retrospectives.timer
systemctl --user list-timers baton-retrospectives.timer
journalctl --user -u baton-retrospectives.service
```

The timer runs two minutes after boot, then one minute after each service completion (with ten
seconds of scheduling accuracy). Catch-up scans durable records on each activation, so downtime
requires no lost in-memory notification to be replayed. The five-minute evidence-settling delay
is additional. Timer installation is opt-in and is not performed by the CLI.

For host acceptance, verify ledger/attempt coverage, configure and exercise the analyst, run
catch-up twice to check deduplication, then compare the first three eligible reports with owner
review. Include a handoff when available or record the fixture-only coverage gap. Offline tests
cannot establish provider compatibility or actual host telemetry coverage.
