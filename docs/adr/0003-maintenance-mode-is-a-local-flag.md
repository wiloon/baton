---
status: accepted
---

# Maintenance mode is a local flag, not an API

Deploys to SER8 race the timer: the playbook's migrate step needs the work locks, and a tick
that is running (or starts a moment later) makes the deploy fail. We drain the pipeline with a
maintenance flag: a file in the runner state directory, set and cleared by
`python -m baton.maintenance on|off|status`, which the deploy playbook runs over SSH. While the
flag is active, scheduled and manual ticks, nightly runs and the installed retrospectives worker
start no new work. Work already running finishes. A short-lived admission lock serializes flag
changes with the workers' check and acquisition of their work locks. `on --wait` sets the flag
before probing those locks, so the deploy can start checking out the new version once the
pipeline is idle. The flag carries an expiry so an abandoned deploy cannot stop the pipeline
indefinitely. Tracked in #59.

## Work locks

There is no single runner lock (#270). The work locks are:

- one **repository lock** per repository, `<state>/repos/OWNER/NAME/lock`, held while a tick
  prepares and runs that repository's item. A tick holds at most one at a time;
- `<state>/housekeeping.lock`, held only for the runner-wide housekeeping;
- `<state>/scan.lock`, held for a whole scan, together with the scanned repository's lock;
- the retrospectives worker's lock.

A tick takes each lock without waiting, through the admission lock, so the flag is checked again
before every repository it moves to. The nightly run and `baton.migrate` exclude all ticks by
taking every repository lock, all or none; `baton.migrate` also takes the housekeeping and scan
locks. The repository locks in question are those of the configured repositories plus any lock
file already under `<state>/repos`, so a repository dropped from the configuration mid-run is
still waited for. `on --wait` returns once all the work locks above are free, and `status` lists
each of them.

ADR 0011 (#271) adds a resident supervisor, `baton serve`, without an API. It amends the
statements below that baton has no resident process and that a chained tick re-execs: a
supervisor child exits instead, and its successor passes the same admission check. The timer path
keeps the re-exec while it remains.

## Considered options

- **REST API on a resident process:** the natural shape for "ask the runner to drain", and
  reachable without SSH. But baton has no resident process; the tick is a oneshot started by
  `baton.timer`. Adding a daemon only for this means a port, auth, supervision and a process
  that has to replace itself during the very deploy it is coordinating. Kept as a future
  direction in #60, which lists the needs that would justify a daemon.
- **Local flag plus CLI (chosen):** Ansible already has SSH to the host. Each work starter
  checks the flag and acquires its work lock under the same admission lock. A chained tick
  re-enters through the same check after re-exec.
- **Stop the timers in the playbook only:** works (it is how v0.1.25 was deployed), but it does
  not cover manual runs, has no reason or expiry, and leaves the timer stopped if the deploy
  fails before the task that restarts it.
- **Reuse `scheduling.paused`:** that setting lives in Ansible-rendered `config.toml` and means a
  long-term operator pause; toggling it for a deploy means rendering config twice and conflating
  the two.
- **Wait for the work locks in `baton.migrate`:** a bounded wait could address a busy lock at
  migrate time. Checkout runs earlier, while an active tick may still read prompts, settings,
  `verify.sh` and `guard.sh` from the checkout. Draining before checkout keeps those files on the
  same version for the duration of the run and also waits for the retrospectives worker's lock.

## Consequences

- The first deploy of the version that ships this still talks to the old version, which has no
  `baton.maintenance`. The deployment playbook follow-up covers stopping the relevant timers,
  waiting for their services to finish, and restoring the timers on exit.
- Draining waits for the running item, which can take a long time (one agent call can run for
  tens of minutes); the deploy's wait timeout must allow for that and fails rather than
  interrupting work. During a drain, the flag covers the wait budget plus the TTL. A successful
  drain restarts the TTL so the deploy has a full window for checkout and migrate.
- If a resident process is built later (#60), its maintenance endpoint should keep these
  semantics (drain, never interrupt, expiry), and a new ADR supersedes the "no API" part of this
  one.

## Revision record

- Issue #59, round 1. Serialized admission with flag changes so late starters cannot take a work
  lock after the drain, defined expiry across the wait and deploy, and documented why a wait in
  migrate alone leaves checkout exposed. Scoped the playbook to a separate repository follow-up.
- Issue #270, round 1. Replaced the single runner lock with per-repository work locks plus
  housekeeping and scan locks, so ticks for different repositories may overlap; drain, nightly,
  migrate and `status` cover the whole set.
- Issue #271, round 1. Noted that ADR 0011 amends "no resident process" and the chained re-exec,
  because `baton serve` replaces the timer; the "no API" decision is unchanged.
