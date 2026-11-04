# baton telemetry

This is the runner's observability. It has three parts:
- Grafana Alloy collectors on the runner host;
- a metadata ledger in `bin/telemetry.py`;
- a Grafana dashboard.

Only metadata leaves the host: event names, counts, durations, token usage and
bounded labels. Prompts, tool input and output, errors and account identifiers
stay on the host.

| File | What it is |
| --- | --- |
| `config.alloy` | The collector's entry point. Everything deployment-specific comes from the environment (`telemetry.env.example`). |
| `codex.alloy` | Codex's native OTLP/HTTP, received on a loopback address the caller supplies. |
| `claude.alloy` | Claude Code's native OTLP metrics, on `127.0.0.1:4320`. |
| `runner.alloy` | The runner's own summaries, task events and run spans, on `127.0.0.1:4319`. |
| `grafana/dashboard.json` | The dashboard. Its host and data sources are variables. |

## Configuration

`config.alloy` names no deployment. These variables are listed in
`telemetry.env.example`:
- the host name;
- the Loki push URL;
- the Tempo OTLP URL;
- the Prometheus remote write URL and its credential;
- a CA file for the backends' TLS.

Keep the credential in a file only its owner can read (mode 0600). It never
belongs in a template or in git.

The collector needs Alloy's experimental stability flag
(`--stability.level=experimental`) for delta-to-cumulative conversion. Run it
as a user service of the runner's account, beside the runner. It runs
whether or not the runner's timer is on.

- **Listeners:** OTLP listens on `127.0.0.1:4318`. Alloy's management
  endpoint listens on `127.0.0.1:12346`.
- **Self metrics:** Alloy's own metrics go out every 30 seconds with
  `job=baton-alloy` and `instance=<BATON_TELEMETRY_HOST>`.
- **Collector down:** alert on stale or absent series. A collector whose
  remote write has failed cannot report its own failure.

## Data policy

`codex.alloy` sends every log and trace through the metadata policy before any
exporter. Metrics get a separate policy for bounded labels and are then
converted from delta to cumulative. Never wire an exporter straight to the
receiver, and keep live debugging and debug exporters off.

Logs and traces:
- **Kept:**
  - known event names;
  - numeric usage and duration;
  - tool and status enums;
  - verified model names;
  - validated run and conversation identifiers.
- **Removed:** unknown fields and allowed fields with the wrong type. A numeric
  string is converted only when it matches a strict numeric pattern.
- **Log bodies:** replaced by their allowed event name. Arbitrary severity and
  event-name strings are removed.
- **Spans:**
  - unknown span names are normalized;
  - status messages, trace state and links are cleared;
  - trace, span and parent identity and times are kept;
  - span-event names are fixed and their attributes removed.

Resources, scope and context:
- **Scope metadata** is stripped.
- **Resource identity** is bounded: `codex_exec`, the host and the agent.
- **Run, issue and pipeline context** stays on logs and traces only.

Metrics:
- **Labels kept:** model, token type, status or success, and known tool names.
- **Dropped:** metric descriptions, units, exemplars and unselected metric
  families.

Unknown model or tool values are removed until someone reviews them, so a CLI
upgrade can require a policy update. This is a metadata policy for a
loopback-only SDK input, not a general gateway for untrusted OTLP.

Codex's `codex.turn.token_usage` is exported as a cumulative histogram:
- **Consumption:** use its Prometheus `_sum` for token consumption, never `_count`.
- **Categories:** input includes cached input. `total` and `reasoning_output`
  are never extra consumption categories.
- **Lost state:** the delta processor keeps its state in memory. A collector
  restart, or 24 hours without data, can reset the cumulative series.
- **Stream limit:** it tracks at most 1,000 streams.
- **Exact per-run totals** come from the runner's summaries.

## Runner ledger and delivery

The runner records one UUID per CLI invocation, with these fields where it
knows them:
- vendor, role and stage;
- issue or PR;
- review round.

For Codex, the native exporters and the TRACEPARENT and resource context are set
on that CLI child only. They use HTTP JSON, port 4318,
`log_user_prompt=false` and a 1 s metric interval. The finished run span uses
the same trace and parent identity. A run is instrumented when `bin/telemetry-enabled`
exists. Removing it stops instrumenting new runs, but records already written are
still collected and retried.

`telemetry.py` keeps a private ledger under `$STATE/telemetry/`.

**Heartbeat.** A detached heartbeat checks the supervising process every 60 s,
using its start identity and the boot ID. All inherited file descriptors are
closed, so it can't hold the runner lock.

**Reconciliation.** The collector turns any of these into one terminal
`interrupted` record:
- a missing or reused PID;
- a reboot;
- a heartbeat older than 180 s;
- a passed vendor deadline (the timeout plus a 60 s kill grace).

A late end callback can never turn that record back into a success.

**Delivery.** Summaries (event ID `<run UUID>:summary`) and run spans go through
`runner.alloy`, which allows only listed types and values. Delivery is
**at least once**:
- **ACK file:** an ACK file stops retries after a success.
- **Replays:** a crash after the HTTP request was accepted, but before the ACK
  was written, can replay the event. Deduplicate Loki aggregations on the event
  ID. A replay reaches Tempo with the same trace and span ID.
- **What an ACK means:** Alloy accepted the data, not that the backend stored it.
- **Outages:** an outage never blocks an agent run. Each collection spends a
  bounded time on the backlog.

Usage per summary; exact run and issue consumption uses summaries only:

| Agent | Source | Categories |
| --- | --- | --- |
| Codex | one `turn.completed.usage` | `input_tokens - cached_input_tokens` is non-cached input; cached, cache-write, output and reasoning are kept separately |
| Cursor | one `result.usage` | `inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens` |
| Claude | one `result.usage` | `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` |

**When usage is unavailable:**
- a run with several terminal usage records;
- a token field that is malformed, a boolean, negative or missing. Usage is never
  zero-filled.

**Partial and complete:**
- A failed invocation that has a valid usage record is `partial`.
- `usage_state=complete` means the invocation finished with one usage result;
  a category that result lacks stays unavailable.

**Model and cost:** the actual model and the cost are not in summaries, and
configured model aliases are not exported as the actual model. Claude's cost
stays in the local record.

## Tasks

The ledger also keeps:
- task episodes, as UUIDs;
- per-repository issue and PR indexes;
- an outbox of immutable events.

**Episodes:** repeated ticks reuse the active episode. A merge, a planned
hand-off or an abnormal one ends it, and later work starts a new one. A ledger
started halfway through a task holds only what it observed.

**Events:** hooks emit these:
- spec and code review verdicts, with round and Git head;
- CI observations, with PR and head SHA;
- hand-offs and merges.

**CI snapshots:** a snapshot separates pass, failure, `no_checks`, pending,
timeout, cancellation and read errors. A head that changes during the watch is
unknown. Telemetry reads have a 15 s limit and never change what the runner
decides.

**What a summary carries:**
- the task ID;
- SHA-256 fingerprints of the workflow (runner, helpers, prompts, settings) and
  of the config;
- the CLI version.

**Task totals:** snapshots are taken per task ID as the latest observation,
never summed. A category total is exported only when every finished run at that
timestamp has complete usage for it, and the count of incomplete runs stays
visible.

## Host state metrics

The state collector (`telemetry.py collect`, every minute) writes bounded
gauges to a textfile for node_exporter: `/var/lib/baton-metrics/baton.prom`,
world-readable. They carry no run, PID, issue or PR labels.

**Read failures:** a failed read gives NaN or a collection failure and keeps the
last success; it never reads as disabled or zero.

**Inputs:** the collector reads only these, and it collects while the timer is
off or paused:
- the numeric settings in `config.toml`;
- today's vendor run counters;
- cooldown files;
- the timer's properties;
- the runner lock.

### Quota

`baton_quota_*` gauges are labelled by `agent`. Window data also carries
`window="five_hour|seven_day"`.
- **`used_ratio`, `remaining_ratio`:** account usage and balance, 0–1.
- **`runner_cap_ratio`, `runner_headroom_ratio`:** the configured cap and
  `max(cap - used, 0)`, in account-quota units.
- **`resets_at_seconds`, `observed_at_seconds`:** the provider's reset time and
  the last observation of that window.
- **`window_state`:**

  | Value | State |
  | --- | --- |
  | 0 | Unknown |
  | 1 | Fresh |
  | 2 | Stale |
  | 3 | Reset pending |
  | 4 | Unsupported |
  | 5 | Invalid |

  A stale balance keeps its last observation. The other states that aren't
  Fresh give NaN.
- **`gate`** (per agent):

  | Value | Gate |
  | --- | --- |
  | 0 | Unknown |
  | 1 | Within quota |
  | 2 | 5h limit |
  | 3 | Weekly limit |
  | 4 | Cooldown |

  "Within quota" needs fresh data. The gate is an observation, not a prediction
  of dispatch.

`baton.quota record` stamps each window it actually receives. Partial updates
leave the other windows alone, and a failed probe refreshes nothing.

`baton_credential_state` and `baton_credential_observed_at_seconds` are labelled
by `agent`:

| Value | State |
| --- | --- |
| 0 | Unknown |
| 1 | OK |
| 2 | Authentication expired |
| 3 | Probe failed |
| 4 | Stale |
| 5 | Invalid |

An authentication failure stays until a later probe or run succeeds. Any other
state goes Stale after 24 hours without a check.

### Activity

`baton_running_by_agent{agent}` counts live, reconciled invocations.
`baton_current_execution_started_seconds` exists only while a run is live. It is
labelled with agent, role, stage, repo, issue, PR and run ID.

`baton_activity_in_interval{agent}` is 1 when an invocation overlapped the
preceding successful observation interval, including one that started and ended
between two polls. `baton_activity_observed_at_seconds` tracks successful
observations.

**Unknown, never Idle:** a failed read, stale data, or an interval over 150 s.

**Block edges** are approximate, so use run summaries for exact times.

**What it covers:** "Running" includes waiting for tools or the network. CLI runs
started outside the runner aren't counted.

**Backfill:** missed samples are not backfilled.

## Claude Code metrics

`baton.dispatch` sets Claude Code's exporter variables on the CLI child only,
and only for an instrumented run (`Telemetry.child_env` in `baton/hooks.py`):
- metrics go to `otlp`;
- logs and traces are `none`;
- the session ID, account UUID and version are off.

- **Series kept:** `claude_code.{token.usage, cost.usage, session.count,
  lines_of_code.count, commit.count, pull_request.count, active_time.total}`.
  Prometheus names end in `_total`.
- **Labels:**
  - **Datapoint labels kept:** `type`, `model` (`claude-<family>-<version>`
    only) and `query_source` (`main|subagent|auxiliary`). Everything else,
    including `session.id`, `user.*` and `organization.id`, is dropped on the
    host.
  - **Resource labels:** `service.name=claude-code`,
    `host.name=<BATON_TELEMETRY_HOST>`, `agent=claude`.
- **Delta to cumulative:** delta temporality is converted to cumulative before
  remote write.
- **Cost:** `cost.usage` is Claude Code's estimate at API list prices. Under a
  subscription it is a ratio, not a bill.
- **Logs and traces** stay off until their content gets the review Codex's got.

## Dashboard

`grafana/dashboard.json` takes its host and its Prometheus, Loki and Tempo data
sources from variables.

**Top panels:** it opens with Current executions and Agent activity, which ignore
the Agent and Role filters so that a filter can't hide an active agent.

**Task tables:**
- they span every vendor;
- they show the latest 500 matching records in Loki;
- a task ID links to Loki Explore, and a run's trace ID to Tempo.

**Quota overview:** it is per provider and shows:
- account balance;
- weekly headroom in percentage points;
- resets and window states;
- the age of each observation.

## Tests

`tests/telemetry/` (see its README) runs offline. The real-Alloy tests are
skipped without an `alloy` binary (`ALLOY_BIN`). They plant a marker in every
place content could hide, and assert three things:
- the marker reaches neither an exporter nor Alloy's diagnostics;
- trace parents and run IDs survive;
- metrics lose their high-cardinality IDs.
