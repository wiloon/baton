---
status: accepted
---

# A resident supervisor runs one tick child per repository

`baton.service` is a oneshot started by `baton.timer` every 10 minutes, and systemd never starts a
second instance while one runs: a host works on one item at a time and idles between items. A
resident process, `python -m baton serve`, replaces the timer. It schedules children and, under
contention, lists GitHub read-only to order work by priority across repositories (ADR 0012, #291).
It never writes to GitHub or a repository's state, never calls an agent CLI, and persists nothing
it needs to read back. Tracked in #271.

This amends ADR 0003, which records that baton has no resident process. `serve` offers no API, so
ADR 0003's "no API" part and its local maintenance flag stand.

## Scheduling

- A **child** is `python -m baton --repo OWNER/NAME --serve-child`: a scheduled (not manual) tick
  of that one repository. It honours maintenance and `scheduling.paused`, takes its own work locks
  and sends the `tick` telemetry event.
- A child exits 80 only when it consumed the repository's `chain-next` flag (a partial slice
  merged); it does not re-exec. The `baton.timer` path keeps the re-exec while it remains as the
  rollback path.
- The supervisor starts eligible repositories in rotation, one child per repository, at most
  `scheduling.max_parallel` (default 1). When more due repositories compete than free slots, it
  ranks by global priority and may pass `--issue N` or `--no-new-issue` (ADR 0012). The rotation
  and every timer below are in memory.
- After exit 0 a repository is eligible again `serve.poll_seconds` (default 60, minimum 10) later.
  After exit 80 it is eligible at once, at most `chain_max` times in a row; the next one waits
  `poll_seconds` and the count restarts. A plain child that ran an item and one that found nothing
  both exit 0, so both wait. An assigned child (`--issue` / `--no-new-issue`) that ran no item
  exits 81: due at once, that assignment suppressed for one poll (ADR 0012).
- Any other exit (including a signal, or a child that could not be started) is a failure: the
  repository waits `poll_seconds`, doubled for each further consecutive failure, at most one hour.
  Exit 0 or 80 resets it.
- `config.toml` is read again on every loop. While it is invalid, nothing starts. A running child
  of a removed repository finishes and is not restarted.
- A child scans its repository whenever that repository is idle and due, even while another
  repository has work. The multi-repository timer tick scans only when no repository had work.
  `scan.lock`, `scan_interval_hours` and `scan_max_waiting` still bound scans.

## Deploy and shutdown

- The supervisor keeps running the code it started with. Every deploy runs, in order:
  `maintenance on --wait`, checkout, migrate, `systemctl --user try-restart baton-serve`,
  `maintenance off`.
- SIGTERM drains and never interrupts, as ADR 0003 asks of a resident process: the supervisor
  starts nothing new, does not signal its running children, waits for them and then exits.
- The unit sets `KillMode=mixed`, so a stop signals only the supervisor, and `TimeoutStopSec=2h`
  (the deploy's drain timeout), so a stuck child cannot block a stop forever. When that expires,
  systemd kills everything left in the unit. It also kills the children if the supervisor itself
  dies.
- `systemctl --user kill baton-serve` is the immediate abort: it signals every process in the
  unit, children and agent CLIs included. It is not a stop: unless a stop is already pending,
  `Restart=always` starts the supervisor again.

## Considered options

- **Keep the timer, with one unit per repository:** parallel without new code, but keeps the idle
  gap and has no runner-wide limit on parallel work.
- **A supervisor that runs items in its own process:** one failure would stop all repositories,
  and a deploy would have to replace code in a process that is working.
- **A distinct "did an item" exit code:** would start the next item without waiting a poll, at the
  cost of another exit protocol. A wait of one poll is short enough for a child that did run an
  item; ADR 0012 adds exit 81 only for an assigned child that ran none, so a lower priority
  elsewhere cannot keep the slot of work that only waited for a vendor.
- **Forward SIGTERM to the children:** stops quickly, but loses work and quota and needs cleanup
  code in the tick for `agent-wip` and worktrees.

## Revision record

- Issue #271, round 1. The owner decided that SIGTERM drains without signalling the children,
  with `KillMode=mixed` and `TimeoutStopSec=2h`, to keep ADR 0003's drain-and-never-interrupt
  semantics; status moved to accepted.
- Issue #291 / ADR 0012: the supervisor lists GitHub read-only under contention and uses exit 81
  for an assigned child that ran no item.
