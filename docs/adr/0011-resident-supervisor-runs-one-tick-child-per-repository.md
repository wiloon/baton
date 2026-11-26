---
status: proposed
---

# A resident supervisor runs one tick child per repository

`baton.service` is a oneshot started by `baton.timer` every 10 minutes, and systemd never starts a
second instance while one runs: a host works on one item at a time and idles between items. A
resident process, `python -m baton serve`, replaces the timer. It only schedules: it never calls
GitHub or an agent CLI, and it persists nothing it needs to read back. Tracked in #271.

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
  `scheduling.max_parallel` (default 1). The rotation and every timer below are in memory.
- After exit 0 a repository is eligible again `serve.poll_seconds` (default 60, minimum 10) later.
  After exit 80 it is eligible at once, at most `chain_max` times in a row; the next one waits
  `poll_seconds` and the count restarts. A child that ran an item and one that found nothing both
  exit 0, so both wait.
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
- Shutdown behaviour (what SIGTERM does to running children, and the unit's `KillMode` and
  `TimeoutStopSec`) is pending the owner's decision in #271.

## Considered options

- **Keep the timer, with one unit per repository:** parallel without new code, but keeps the idle
  gap and has no runner-wide limit on parallel work.
- **A supervisor that runs items in its own process:** one failure would stop all repositories,
  and a deploy would have to replace code in a process that is working.
- **A distinct "did an item" exit code:** would start the next item without waiting a poll, at the
  cost of another exit protocol. A wait of one poll is short enough.
