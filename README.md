<p align="center"><img src="docs/logo.svg" alt="baton" width="160"></p>

# baton

baton is an unattended coding-agent runner for GitHub repositories. A systemd timer wakes it every
ten minutes. Each time, it takes one step on an issue or pull request: review an issue's spec,
implement it, review the PR, fix what the review or CI found, and merge. Each step hands the work
to Claude Code, Codex or Cursor's CLI, whichever has the most of its subscription quota to spare.

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
  merge         auto_merge on, CI green, "## Manual checks" none -> merge; else ready-for-human
```

**One step per tick.** A tick takes the configured repositories in turn. In each one it does the
first thing it finds, in this order:

1. finish a PR whose creation was interrupted;
2. a review the last run deferred;
3. a PR the owner labelled `agent-review`;
4. an agent PR the owner labelled `agent-fix`;
5. the next `ready-for-agent` issue (or a `claude-found` + `needs-triage` issue).

When a step ends, the tick can go straight on to the next one, up to `chain_max`. With
nothing to do and `scan` on, Claude scans one of the repository's `scan_areas` for bugs and files
what it finds as an issue.

**Picking a vendor.** For each run, baton drops the vendors that are resting after a limit or an
auth failure, that are over their quota caps, or, for a review, that wrote any of the PR (when
every vendor did, the same vendor reviews on a different model). From the rest it takes the one
furthest behind its weekly pace. `[preferences]` only breaks ties. Quota readings come
from the vendors' CLIs, refreshed by a one-turn probe run when older than `quota_stale_minutes`.

**Guard rails.** Claude Code uses a PreToolUse hook (`bin/guard.sh`) and permission settings
(`bin/*settings.json`) to reject known dangerous operations. Codex uses its CLI sandbox;
Cursor runs with `--force --trust` and checks for changes after a read-only run. These controls
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
| `claude-found` | baton | an issue a code scan filed |
| `needs-triage` | baton | with `claude-found`: waiting for Cursor's or the owner's verdict |

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

## Requirements

- Linux with systemd user units (`loginctl enable-linger` so they run without a login session)
- git, bash, jq, GNU coreutils (`timeout` and `realpath -m`),
  [uv](https://docs.astral.sh/uv/) (it installs Python 3.15 for you)
- [gh](https://cli.github.com/), logged in as an account that can push branches, open PRs and
  edit labels in the watched repositories
- at least one agent CLI, logged in with its subscription:
  - Claude Code (`claude`)
  - Codex (`codex`)
  - Cursor (`cursor-agent`) as an optional extra vendor (`vendor_extra = ["cursor"]`)
- whatever the watched repository's own checks need (its toolchain; `mise.toml` here pins one
  for everything under `~/baton`, edit it to suit)

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
#    Set [quota].vendor_pool to the CLIs you installed, e.g. ["codex"] or ["claude"].
#    Cursor is optional: vendor_extra = ["cursor"] enables it.
#    For a first run, set [scheduling].auto_merge = false to inspect the PR yourself.
#    Then clone that repository where baton expects it:
git clone https://github.com/OWNER/NAME ~/baton/repos/OWNER/NAME

# 3. Create the labels in that repository
for l in ready-for-agent agent-wip ready-for-human needs-info agent-review agent-fix claude-found needs-triage; do
  gh label create "$l" -R OWNER/NAME 2>/dev/null || true
done

# 4. Check
uv run python -m baton --validate     # the configuration
uv run python -m baton --preflight    # gh login and the enabled vendors' CLIs

# 5. Try one issue by hand: label it ready-for-agent, then
uv run python -m baton 42             # 42 = the issue number

# 6. Run on a timer
mkdir -p ~/.config/systemd/user
cp systemd/baton.service systemd/baton.timer systemd/baton-tmp-sweep.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now baton.timer baton-tmp-sweep.timer
loginctl enable-linger "$USER"
```

Logs are in `~/.local/state/baton/logs` (`runner.log`, plus one file per agent run). Set
`paused = true` in `config.toml` to stop the timer's ticks while keeping manual runs.

Inspect an issue with `uv run python -m baton.audit --repo OWNER/NAME 42`, or inspect global
quota-probe overhead with `uv run python -m baton.audit probes` (when telemetry is enabled).
Issue totals exclude probes. The audit separates elapsed time, recorded active attempts, model
execution, local checks, CI waits and deferred waits. Timing coverage is unavailable for older
history and partial when only some attempts were recorded; missing observations stay unknown.
Model/check/CI times are components of active time, so do not add them to active time again.
The last-attempt duration measures the most recent invocation, including a resumed invocation.

### More commands

```text
python -m baton [N]              an issue: the same pipeline the timer runs
python -m baton pr N             an agent PR with owner feedback, as with agent-fix
python -m baton review N         review PR N, as with agent-review
python -m baton scan [N|list]    scan area N (default: the next one) and keep the draft
python -m baton file-scan DRAFT  file a kept draft as claude-found + needs-triage
```

With several `[[repos]]`, put `--repo OWNER/NAME` before the command.

### systemd units

| Unit | What it does |
| --- | --- |
| `baton.timer` / `.service` | a tick every 10 minutes |
| `baton-tmp-sweep.*` | hourly: removes the agents' stale `/tmp` files |
| `baton-postmerge.*` | every 6 hours: finds reverts of, and issues about, merged agent PRs (telemetry) |
| `baton-e2e-nightly.*` | nightly: runs the repository's `nightly_hook` on `origin/main`, notifies when red |

The units assume the checkout is at `~/baton` and uv at `/usr/bin/uv`; adjust `ExecStart` if not.

## Configuration

`config.example.toml` documents every key. Fields are required except the optional Cursor role
overrides; the example has a working default for each. Edit the `[[repos]]` entry, choose your installed vendors in `[quota]`, and
choose whether `[scheduling].auto_merge` should be enabled. The optional
`[linear]` and `[telegram]` tables are at the end of the file.

Cursor uses `model_cursor` by default. Optional `model_cursor_dev`, `model_cursor_fix`,
`model_cursor_review`, `model_cursor_spec_review`, `model_cursor_spec` and `model_cursor_scan`
override individual roles; empty or omitted overrides keep the default. Run records distinguish
the requested model from the actual model; Cursor Auto's actual model remains unknown when its
CLI does not report it. Existing host configurations need no migration for these overrides.

Each vendor choice logs its preference order, weekly usage/cap, time to reset, pace score and
quota/cooldown gate. Lower pace scores mean further behind the weekly spending pace. Cursor is
an extra vendor outside pool balancing: a configured preference can choose it before comparing
pool pace. This policy remains subject to quota limits and independent-review eligibility.

## Telemetry

Optional. Grafana Alloy collectors on the host forward the runner's metrics, events and run traces,
plus Claude Code's and Codex's own OTLP metrics, to Prometheus, Loki and Tempo. A dashboard shows
current runs, quota headroom per vendor and the task history. Only metadata leaves the host.
Setup: [telemetry/README.md](telemetry/README.md).

## A note on history

baton was extracted from a private infrastructure repository where it ran for months against one
project. Comments and test names still cite issue numbers (`#284`, `(#327)`) from that repository;
they are not issues here.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
