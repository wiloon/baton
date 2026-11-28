<p align="center"><img src="docs/logo.svg" alt="baton" width="160"></p>

# baton

baton is an unattended coding-agent runner for GitHub repositories. A systemd timer wakes it every
ten minutes. Each time, it takes one step on an issue or pull request: review an issue's spec,
implement it, review the PR, fix what the review or CI found, and merge. Each step hands the work
to Claude Code, Codex, Cursor or Antigravity (`agy`, Gemini models) CLI, whichever has the most of its subscription quota to spare.

## Why it exists

It grew out of one developer's setup. A MacBook Air ran out of CPU and memory with several agents
running on it, while a small always-on Linux box sat idle. Three coding-agent subscriptions,
Claude, ChatGPT (Codex) and Cursor, each had a weekly quota that was either wasted or used up too
early. baton moves the agent work to the idle box, spends the quotas evenly through the week, and
has one vendor's work reviewed by another.

## How it works

```text
issue labelled ready-for-agent
  spec review   an agent checks the issue is clear enough to build; another run revises it
                until it is, or it goes to the owner after max_spec_rounds
  dev           implement in a fresh worktree, run the checks, push, open a PR
  review        a vendor that wrote none of the PR reviews it
  fix           answer the review or a failing CI check (up to max_review_rounds)
  merge         [scheduling].auto_merge and the repository's [[repos]] auto_merge both true,
                CI green, "## Checks to verify" none, or every check verified by the screenshot review -> merge; else ready-for-human
                (screenshot_review = true: a second, separate run checks the manual-check screenshots after the code review;
                 every check verified -> merge; otherwise only the unverified ones go to the owner, ADR 0006)
```

**One step per tick.** A tick takes the configured repositories in turn. In each one it does the
first thing it finds, in this order:

1. finish a PR whose creation was interrupted;
2. work of `P0` issues: first what is already in flight (a deferred review, an `agent-fix` PR, a
   saved push or spec step), then a new `P0` issue;
3. a review the last run deferred;
4. a PR the owner labelled `agent-review`;
5. an agent PR the owner labelled `agent-fix`;
6. the next `ready-for-agent` issue.

**Priority.** The labels `P0`, `P1`, `P2` and `P3` (any letter case) on an issue order its
repository's queue: `P0` first, then by issue number within one priority. An issue with none ranks
after `P3`; with several, the highest counts. Create the labels in each repository you want to use them
in. Priority never makes an issue a candidate: `ready-for-agent`, the milestone, the excluded
labels, open blockers and owner authorization still decide that. Work already on a PR goes before a
new issue, and `max_pending_prs` limits new issues, with one exception: `P0`, for urgent bugs. A
`P0` issue goes before the resumed work of lower-priority issues and is not limited by
`max_pending_prs`, however many `P0` issues there are (their PRs count toward it as usual). A
running step is never interrupted, and a `P0` that cannot run now does not hold up other work.
Under `baton serve`, priority is global across repositories: when free slots are contended the
supervisor lists each due repository and starts the best ready work first (`P0` anywhere before
`P1`, and so on). Equal priority is broken by each repository's `weight` (an integer ≥ 1 in
`[[repos]]`, default 1; higher first), then by rotation. Priority always beats weight: a `P1` in a
weight-1 repository goes before a `P2` in a weight-5 one. Weight does not expire, does not change
quota spend or vendor choice, and has no effect when free slots are not contended, on the legacy
timer path, or inside a child's own `next_issue()`. In-flight work of a repository keeps its slot
ahead of any new issue elsewhere
([ADR 0012](docs/adr/0012-supervisor-orders-work-by-priority-across-repositories.md)). The legacy
timer path still turns per repository. A run by hand on one issue or PR has no `P0` step
([ADR 0010](docs/adr/0010-priority-labels-order-the-queue-and-p0-preempts.md)).

When a step ends, the tick can go straight on to the next one, up to `chain_max`. With
nothing to do and `scan` on, Claude scans one of the repository's `scan_areas` for bugs and files
what it finds as an issue. Such a finding waits for the owner: a verdict that accepts it only removes
`needs-triage`, and no tick takes it until the owner applies `ready-for-agent`.

**How many issues run at once.** `max_pending_prs` (per repository) is how many `agent-wip` issues
may be in flight before baton starts a new one; a new `P0` issue starts anyway. A PR waiting for the owner (`ready-for-human`) is
not counted. A PR whose next agent step was deferred for vendor quota or cooldown lends its slot
until that step runs again without a deferral, so another vendor can start the next issue. At most
`max_pending_prs + 1` counted issues are in flight through normal scheduling (`P0` issues are not
bound by this). An `agent-wip` issue
with no open PR holds no slot: unless its newest PR on either agent branch is a merge not yet
settled, baton removes `agent-wip` (also when the issue has no PR at all, such as an interrupted or
deferred dev run) and logs `released agent-wip (no open PR)`; an unsettled merge keeps the label but
is not counted, and a failed lookup leaves the label and retries next tick. When the cap blocks a start, the log lists each in-flight issue, its PR, and the
saved deferral reason.

**After you close a PR.** Baton never closes a PR, so closing an issue's PR without merging rejects
that work. The next dev run of the issue (picked, relabelled or `python -m baton N`) starts again from
`main`: an `agent-N`/`cursor-N` worktree whose HEAD shares a commit outside `main` with a closed PR's
head is saved and removed. The saved work stays in the runner's clone under
`refs/baton/backup/issue-N/<sha>`, together with any branch tip baton moves; uncommitted work is a
commit whose parent holds the staged changes (`git show <sha>:<file>`). Other worktrees are resumed as
they are. The replacement push may overwrite the remote branch only while it still holds exactly the
closed PR's head (`--force-with-lease`), or fast-forward it; a remote that diverged, or a replacement that merged
the closed work back in, is not pushed and the issue is handed to you.

**Picking a vendor.** For each run, baton drops the vendors that are resting after a limit or an
auth failure, that are over their quota caps, or, for a review, that wrote any of the PR (when
no other vendor can review, a vendor reviews its own code: on a different model when one is
configured, otherwise on the same model in a fresh session; the screenshot review still needs a
different model). That same-vendor fallback waits
while another vendor is only briefly blocked, such as a full 5-hour window, and runs only when
the others are out for the week, logged out, or resting past `same_vendor_wait_max_minutes`
([ADR 0002](docs/adr/0002-same-vendor-review-waits-for-short-blocks.md)). From the rest it takes the one
furthest behind its weekly pace. `[preferences]` only breaks ties. Quota readings come
from the vendors' CLIs, refreshed by a one-turn probe run when older than `quota_stale_minutes`.

**Guard rails.** Claude Code uses a PreToolUse hook (`bin/guard.sh`) and permission settings
(`bin/*settings.json`) to reject known dangerous operations. Codex uses its CLI sandbox;
Cursor runs with `--force --trust` and checks for changes after a read-only run. Antigravity (`agy`) runs
in `--sandbox` with an exact command allowlist and is reset after a failed or read-only run. These controls
have different coverage. Shell filters and post-run checks do not provide host isolation:
an allowed program or a repository's tests can perform other operations with the runner's access.
Run baton under a dedicated account on a disposable host or VM, with credentials scoped to the
watched repositories. See [SECURITY.md](SECURITY.md) for the trust boundary.
Host-side `verify_hook` and `gate_hook` commands keep the check definitions outside a PR;
the code they test still executes with the host account's permissions.

### Labels

Create these labels in each watched repository (see the quick start).

| Label | Set by | Meaning |
| --- | --- | --- |
| `ready-for-agent` | configured owner | baton may take this issue, including an external contributor's report |
| `agent-wip` | baton | baton is working on it; removed when the work ends |
| `ready-for-human` | baton | the owner must decide or check something; baton leaves it alone |
| `needs-info` | owner | waiting for information; baton skips it |
| `agent-review` | owner | review an eligible same-repository PR by the owner or configured Cursor account |
| `agent-fix` | owner | the owner left feedback on an agent PR; fix it |
| `claude-found` | baton | an issue a code scan filed; without `ready-for-agent` it waits for the owner |
| `needs-triage` | baton | a scan issue or a side bug the runner filed, waiting for triage |
| `follow-up` | baton | non-blocking nits from a merged PR; a PR that implements a follow-up issue files no further nits |

The unattended queue accepts issues opened by `owner` or the configured Cursor accounts.
It also accepts external issues after the configured `owner` applies `ready-for-agent`; the
configured milestone and handoff labels still apply. baton checks the label's latest event across
all pages: another account's label, a removal, or unreadable history does not authorize work.
This applies to scheduled and manual issue runs. If another account applied the label, the owner
must remove and reapply it. GitHub cannot restrict a single label to one user, so baton enforces
this execution rule itself. Scanner labels alone do not admit external issues.
Review context keeps comments from trusted accounts; human fix feedback comes only from `owner`.
External contributions are welcome through issues and fork PRs; see [CONTRIBUTING.md](CONTRIBUTING.md).
Fork PRs require maintainer review and are not run by baton's unattended agent pipeline.

## Related projects and scheduling research

See the [orchestration landscape review](docs/research/agent-orchestration-landscape.md) for
existing alternatives, replacement criteria and the proposed boundary for model-assisted scheduling.

## Requirements

- Linux with systemd user units (`loginctl enable-linger` so they run without a login session)
- git, bash, jq, GNU coreutils (`timeout` and `realpath -m`),
  [uv](https://docs.astral.sh/uv/) (it installs Python 3.15 for you)
- [gh](https://cli.github.com/), logged in as an account that can push branches, open PRs and
  edit labels in the watched repositories
- at least one agent CLI, logged in with its subscription:
  - Claude Code (`claude`)
  - Codex (`codex`)
  - Cursor (`cursor-agent`) as an optional extra vendor (`[vendors.cursor]` with `quota = "extra"`)
  - Antigravity (`agy`) as an extra vendor (`[vendors.agy]` with `quota = "extra"`) or a pool vendor
    (`quota = "pool"`): baton reads its Gemini 5-hour and weekly windows from `agy -p /quota`. Gemini
    models only: a model that is not `gemini-*` is a config error, and its review route needs a model
    distinct from its dev and fix routes (an effort suffix alone is not distinct). It can hold
    `reviser` (with a `[models.routes.spec]` entry); revision is not limited to pool vendors
- whatever the watched repository's own checks need (its toolchain; `mise.toml` here pins one
  for everything under `~/baton`, edit it to suit)

Each CLI is an adapter registered in `baton/agents.py` (`ADAPTERS`). An adapter module declares
the capabilities and policies of its CLI as constants: the roles it can run, its quota source,
its review and fix protocol, and, for retrospectives, its analyst command, approved model family,
environment and budget enforcement. Config only enables a vendor (`[vendors.<id>]`) and grants it
roles from the adapter's supported set; a grant for a role the adapter lacks is a config error.
`reviser` may be granted to an extra vendor the same way as to a pool vendor. Preferences order
the vendors that are eligible for a role and never widen the grant. The retrospective model comes
from `retrospectives.model_<id>` for Claude, Codex and Cursor; any other adapter uses its built-in
alias, resolved through `[models.aliases]`. Adding an adapter means adding its module and one
`ADAPTERS` entry, with no new config fields.

The watched repository explains its conventions (tests, CI command, commit style) in its own
`AGENTS.md`; the agents read it like any other agent would.

## Quick start

```sh
# 1. Install
git clone https://github.com/wiloon/baton ~/baton
cd ~/baton
cp config.example.toml config.toml

# 2. Point it at a repository: edit the [[repos]] entry in config.toml
#    (gh_repo, owner, ci_command, ...).
#    Declare each CLI under [vendors.<id>] with adapter, roles, quota ("pool" or
#    "extra"), cap (a weekly fraction) and max_minutes (a positive integer).
#    To disable Cursor in the example, remove [vendors.cursor] and its fields,
#    delete each cursor entry under [models.routes.*], remove "cursor" from
#    [preferences].dev, and delete [retrospectives].model_cursor.
#    In a customized config, remove Cursor from any other preference orders
#    and remove a preference key if deleting Cursor leaves its order empty.
#    Both [scheduling] and the [[repos]] entry must allow auto-merge.
#    For a first run, set [scheduling].auto_merge = false to inspect the PR yourself.
#    Set auto_merge = false in a [[repos]] entry to merge that repository's PRs yourself.
#    Then clone that repository where baton expects it:
git clone https://github.com/OWNER/NAME ~/baton/repos/OWNER/NAME

# 3. Create the labels in that repository
for l in ready-for-agent agent-wip ready-for-human needs-info agent-review agent-fix claude-found needs-triage follow-up; do
  gh label create "$l" -R OWNER/NAME 2>/dev/null || true
done

# 4. Check
uv run python -m baton --validate     # the configuration
uv run python -m baton --preflight    # gh login and the enabled vendors' CLIs

# 5. Try one issue by hand: label it ready-for-agent, then
uv run python -m baton 42             # 42 = the issue number

# 6. Run unattended: the resident supervisor (see "Supervisor" below)
mkdir -p ~/.config/systemd/user
cp systemd/baton-serve.service systemd/baton.service systemd/baton.timer systemd/baton-tmp-sweep.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now baton-serve.service baton-tmp-sweep.timer
loginctl enable-linger "$USER"
```

Logs are in `~/.local/state/baton/logs` (`runner.log`, plus one file per agent run). Set
`paused = true` in `config.toml` to stop unattended ticks while keeping manual runs.

Inspect an issue with `uv run python -m baton.audit --repo OWNER/NAME 42`, or inspect global
quota-probe overhead with `uv run python -m baton.audit probes` (when telemetry is enabled).
Issue totals exclude probes. The audit separates elapsed time, recorded active attempts, model
execution, local checks, CI waits and deferred waits. Timing coverage is unavailable for older
history and partial when only some attempts were recorded; missing observations stay unknown.
Model/check/CI times are components of active time, so do not add them to active time again.
The last-attempt duration measures the most recent invocation, including a resumed invocation.

### More commands

```text
python -m baton serve            the resident supervisor: no --repo, no further arguments
python -m baton [N]              an issue: the same pipeline the supervisor runs
python -m baton pr N             an agent PR with owner feedback, as with agent-fix
python -m baton review N         review PR N, as with agent-review
python -m baton scan [N|list]    scan area N (default: the next one) and keep the draft
python -m baton file-scan DRAFT  file a kept draft as claude-found + needs-triage
```

With several `[[repos]]`, put `--repo OWNER/NAME` before the command. Manual runs keep working
beside the supervisor: the repository lock keeps them apart.

### Supervisor

`python -m baton serve` is a resident process that replaces the timer
([ADR 0011](docs/adr/0011-resident-supervisor-runs-one-tick-child-per-repository.md),
[ADR 0012](docs/adr/0012-supervisor-orders-work-by-priority-across-repositories.md)). It schedules
children and, when more due repositories compete than free slots, lists GitHub read-only to order
work by priority across repositories. It never writes to GitHub or a repository's state and never
calls an agent CLI. Each tick is a child process for one repository, which honours maintenance and
`scheduling.paused`, takes its own locks and scans when its repository is idle and a scan is due
(an assigned `--issue` / `--no-new-issue` child does not scan).

- **`[scheduling] max_parallel`** (default 1): children running at once, across all repositories,
  never two for one repository. Under contention the supervisor prefers in-flight work, then an
  hour-idle housekeeping turn, then the best candidate by priority (equal priority: higher
  `[[repos]] weight`, then rotation), then plain rotation.
- **`[serve] poll_seconds`** (default 60, at least 10): how often it looks, and how long a
  repository waits after its tick exits. After a merged partial slice the next tick starts at once,
  at most `chain_max` times in a row. An assigned child that ran no item exits 81: due at once, and
  that assignment is skipped for one poll so another repository can take the slot.
- A failing tick doubles its repository's wait each time, up to one hour; success resets it.
- `config.toml` is read again on every loop; while it is invalid nothing starts. Lowering
  `max_parallel` or removing a repository never stops a running tick.
- Its decisions are in `runner.log` (`serve: ...`) and the journal; `<state>/serve.json` holds its
  heartbeat and running repositories for telemetry.

**systemd:** `baton-serve.service` restarts it 30 seconds after any exit. Do not enable it together
with `baton.timer` (`systemctl --user disable --now baton.timer` first); the locks keep it safe if
both are. `baton.service` and `baton.timer` stay for one release as the rollback path.

**Shutdown:** `systemctl --user stop baton-serve` (or `restart`) drains and never interrupts: the
supervisor starts nothing new, does not signal its running ticks, waits for them and exits. After
2 hours (`TimeoutStopSec`) systemd kills what is left.

**Immediate abort:** `systemctl --user kill baton-serve` signals every process in the unit, ticks
and agent CLIs included. It is not a stop: unless a stop is already pending, `Restart=always` starts
the service again.

**Deploy:** the supervisor keeps running the code it started with, so every deploy runs, in order:

```sh
python -m baton.maintenance on --wait
# check out the new code, then migrate (python -m baton.migrate OWNER/NAME, when needed)
systemctl --user try-restart baton-serve
python -m baton.maintenance off
```

### Advisor (Claude, DeepSeek fallback)

When a PR review or a spec review runs out of rounds, the optional routine advisor
([ADR 0008](docs/adr/0008-model-judgement-is-advisory.md), #17) gets one fresh model call with
a redacted snapshot and the allowed actions (`extra_round` or `handoff`).
`[advisor] backend_order` (default `["claude", "deepseek"]`) names the backends: Claude Code goes
first and DeepSeek is the fallback. `["deepseek"]` uses DeepSeek only; `["claude"]` has no fallback.
Listing `claude` is the grant for the advisor: Claude must be declared under `[vendors]` but needs
no role. The call is a tool-less, isolated one-shot (`[advisor] claude = { model, effort }`, default
`claude_strong` at `medium`) that takes no vendor lease; its quota use and quota, auth and busy
rejections are accounted like any Claude run. Claude is skipped when it is undeclared, at its cap or
five-hour limit, in cooldown, or its credential has expired. The next backend is tried once when one
is skipped, fails or returns an unusable reply, never after a valid `handoff` or other well-formed
outcome. Records, handoff notes and answer comments name the backend that answered. An evidence
quote must be in the sent snapshot (runs of whitespace aside): mismatching quotes are dropped while
at least one matches, and the record and `runner.log` keep the first one, redacted and cut, as
`evidence_miss` (except for a snapshot with opted-in excerpts). In `mode = "live"` the
runner revalidates the state and grants at most one extra round per task episode. Failed
review, fix, spec, implementation, local verification and worktree steps can also send a
redacted step snapshot. Where the role and task allow it, `retry_step` repeats the failed step
and `retry_step_other_vendor` switches to an eligible alternate. Live mode grants at most one
retry per failure reason per episode. Verification keeps its existing repair budget. The
advisor is also consulted, in observation only, when CI is not green before
a merge, when a merge fails, and when an ADR PR cannot be merged. Those calls send the PR title,
description, the latest review and fix comments and, for an ADR, the acceptance text, all
redacted. The only action offered there is one bounded read-only retry; it appears in the
handoff text as "recommended, not executed (observation)" and never goes live, so the task is
always handed off. `mode = "observe"` records the advice and follows the existing rule. `off` (the default)
makes no call. `[[repos]] advisor = false` keeps a repository's issue and PR text off both
providers. Decisions go to `<state>/advisor/decisions.jsonl`; once a day a "Advisor digest"
notification lists them with how the extra rounds ended (merged or accepted). `DEEPSEEK_API_KEY` comes from the
environment or `[advisor] env_file` (mode 0600) and is never passed to agents.

When the runner picks up an issue, and again once its spec is accepted, the advisor also records a
task profile ([ADR 0009](docs/adr/0009-task-profile-selects-route-tier.md): size, kind, risk and a
tier per route) and comments it on the issue. This is observation only in either enabled mode: every
role still runs at the standard tier. The snapshot adds main's commit count and last-change date for
paths the issue names; `[[repos]] advisor_commit_subjects = true` also sends their recent commit
subjects.

### Maintenance mode

Drain the pipeline before a deploy ([ADR 0003](docs/adr/0003-maintenance-mode-is-a-local-flag.md)):

```text
python -m baton.maintenance [--config PATH] on [--wait] [--timeout DURATION] [--ttl DURATION] [--reason TEXT]
python -m baton.maintenance [--config PATH] off
python -m baton.maintenance [--config PATH] status
```

While the flag is on, ticks (supervisor, timer, manual and chained), the nightly run and the retrospectives
worker start no new work; they log `maintenance (REASON)` and exit 0 (retrospectives print
`{"state":"maintenance",...}`). Work already running finishes. `on --wait` returns 0 once every work
lock (each repository's, the housekeeping, scan and retrospectives locks) is free, and 75 when `--timeout` passes first (the flag
stays on) or when `off` or a newer `on` replaced the request. A DURATION is a positive integer with an
`s`, `m` or `h` suffix, such as `90s`, `30m` or `2h`. `--timeout` and `--ttl` default to `2h`. The flag
expires after the TTL so an abandoned deploy cannot stop the pipeline: plain `on` lasts the TTL, `on
--wait` lasts the timeout plus the TTL, and a successful wait restarts the full TTL when the drain
completes. A malformed flag counts as active for 2h after its modification time. The next tick removes
an expired flag and logs `maintenance expired`. `off` always succeeds; `status` shows on or off, the
reason and expiry, and whether each work lock is held. `baton.migrate` still runs while the flag is
on. `scheduling.paused` stays the operator's long-term pause.

Work locks ([ADR 0003](docs/adr/0003-maintenance-mode-is-a-local-flag.md), all under the state
directory, none ever waited for):

| Lock | Held by |
| --- | --- |
| `repos/OWNER/NAME/lock` | a tick or manual command while it works that repository; ticks for different repositories may overlap, and one finding it held logs `busy (another tick holds the lock)` and goes on |
| `housekeeping.lock` | a tick doing the runner-wide chores; skipped when held |
| `scan.lock` | the scan, one at a time; skipped when held |
| `retrospectives/worker.lock` | the retrospectives worker |

The nightly run and `baton.migrate` take every repository lock at once (migrate also the
housekeeping and scan locks) and give up with 75 when one is held; `--preflight` reports 75 then.

### Agent scratch files

Files the runner exchanges with an agent run (PR descriptions, review and fix replies, spec and
scan drafts) are written under `<state>/repos/OWNER/NAME/scratch/` (mode `0700`), so two
repositories with the same issue or PR number never share a file. Besides its worktree, this is
the only directory `bin/guard.sh` lets a Claude run write with the file tools; `/tmp` is not
allowed. The runner removes a task's files when it cleans up the task, and `baton-tmp-sweep.timer`
removes anything left there after a day without writes. A repository that overrides prompt
templates (`prompt_dir`) names these files with `{{REPLY_FILE}}` (fix), `{{REVIEW_FILE}}` (review)
and `{{OUT}}` (spec, scan), never a fixed `/tmp` path.

### systemd units

| Unit | What it does |
| --- | --- |
| `baton-serve.service` | the resident supervisor, one tick per repository ([Supervisor](#supervisor)) |
| `baton.timer` / `.service` | a tick every 10 minutes: the rollback path for one release |
| `baton-quota.*` | every 5 minutes after completion: refreshes only stale vendor quota observations |
| `baton-retrospectives.*` | every minute after completion: analyzes eligible task episodes |
| `baton-tmp-sweep.*` | hourly: removes the agents' stale `/tmp` and [scratch](#agent-scratch-files) files |
| `baton-postmerge.*` | every 6 hours: finds reverts of, and issues about, merged agent PRs (telemetry) |
| `baton-e2e-nightly.*` | nightly: runs the repository's `nightly_hook` on `origin/main`, notifies when red |

The units assume the checkout is at `~/baton` and uv at `/usr/bin/uv`; adjust `ExecStart` if not.

## Configuration

`config.example.toml` documents every key. Base model routes are required. Follow-up routes,
quota probe retry settings, and `[[repos]]` `auto_merge` are optional. The repository's `auto_merge` defaults to `true`. Set it to `false` to keep
that repository's PRs for manual merging. `owner_review_paths` (globs, default empty) keeps only the PRs that touch those files for the owner, after review and CI pass. Edit the `[[repos]]` entry, declare your installed vendors under `[vendors.<id>]`, and
choose whether `[scheduling].auto_merge` should be enabled. The optional
`[linear]` and `[telegram]` tables are at the end of the file. Their keys, like `DEEPSEEK_API_KEY`
and `CLAUDE_CODE_OAUTH_TOKEN`, come from the runner's environment or from a mode 0600 env file
(`[linear] env_file`, `[telegram] env_file`, `[advisor] env_file`, `[vendors.claude] env_file`)
and never reach other tools. `CLAUDE_CODE_OAUTH_TOKEN` is the one-year login `claude setup-token`
prints; set `[vendors.claude] env_file` (e.g. `~/.config/baton/claude.env`) and only the `claude`
processes baton starts (runs, quota probes, the retrospective analyst, the advisor) get it. A file
others can read stops Claude as an auth failure. Without it, Claude uses its stored `/login`.

Define model identifiers in `[models.aliases]` and reference those aliases in
`[models.routes.<role>]` for each vendor. An optional `<role>_followup` entry replaces the
vendor's whole base entry. If the vendor is omitted, its base entry carries over.
`dev`, `fix`, `review`, `spec_review` and `spec` also take optional `<role>_light` and
`<role>_heavy` entries ([route tiers](docs/adr/0009-task-profile-selects-route-tier.md)); `<role>` is the standard
tier, and follow-up runs ignore the tier.
Screenshot review is base-only. Every round uses `[models.routes.screenshot_review]`.
Set `[preferences].screenshot_reviewer` to order Claude and Codex for that run. The picker
checks `screenshot_reviewer_<area>`, `screenshot_reviewer`, `reviewer_<area>`, and
`reviewer` in that order.

Codex accepts `model = ""` for its CLI default. Cursor's shipped routes use the alias for `auto`.
Fallback lists, the Claude quota probe and retrospective model settings also use aliases.
Migrate legacy flat model keys before validating a host config. Run records distinguish
the requested model from the actual model when the CLI reports it.

Each vendor choice logs its preference order, weekly usage/cap, time to reset, pace score and
quota/cooldown gate. Lower pace scores mean further behind the weekly spending pace. Cursor is
an extra vendor outside pool balancing: a configured preference can choose it before comparing
pool pace. This policy remains subject to quota limits and independent-review eligibility.

## Telemetry

Optional. Grafana Alloy collectors on the host forward the runner's metrics, events and run traces,
plus Claude Code's and Codex's own OTLP metrics, to Prometheus, Loki and Tempo. A dashboard shows
current runs, quota headroom per vendor and the task history. Only metadata leaves the host.
Setup: [telemetry/README.md](telemetry/README.md).

## Task retrospectives

Generate local evidence reports and optional bounded Opus/Sol/Grok 4.7 analysis with
`uv run --frozen python -m baton.retrospectives --config config.toml catch-up`.
See [configuration, timer installation, parallel work and recovery](docs/retrospectives.md).
Analysis is disabled until explicitly enabled; timer installation is opt-in.

## Development roadmap

The [continuous improvement plan](docs/plans/continuous-improvement.md) describes an independent
loop for evaluating execution quality, model choices and consumption. Its first delivery
is [automatic task retrospectives](docs/specs/task-retrospectives.md); target-host validation is pending.

## A note on history

baton was extracted from a private infrastructure repository where it ran for months against one
project. Comments and test names still cite issue numbers (`#284`, `(#327)`) from that repository;
they are not issues here.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
