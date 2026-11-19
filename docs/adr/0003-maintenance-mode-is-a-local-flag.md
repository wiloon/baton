---
status: accepted
---

# Maintenance mode is a local flag, not an API

Deploys to SER8 race the timer: the playbook's migrate step needs the runner lock, and a tick
that is running (or starts a moment later) makes the deploy fail. We drain the pipeline with a
maintenance flag: a file in the runner state directory, set and cleared by
`python -m baton.maintenance on|off|status`, which the deploy playbook runs over SSH. While the
flag is set, the tick, the nightly run and the retrospectives worker start no new work; work
already running finishes. `on --wait` blocks until the locks are free, so the deploy proceeds
only once the pipeline is idle. The flag carries an expiry, so a deploy that dies before `off`
cannot stop the pipeline for good. Tracked in #59.

## Considered options

- **REST API on a resident process:** the natural shape for "ask the runner to drain", and
  reachable without SSH. But baton has no resident process; the tick is a oneshot started by
  `baton.timer`. Adding a daemon only for this means a port, auth, supervision and a process
  that has to replace itself during the very deploy it is coordinating. Kept as a future
  direction in #60, which lists the needs that would justify a daemon.
- **Local flag plus CLI (chosen):** no new process; Ansible already has SSH to the host; the
  check sits next to the existing `scheduling.paused` check.
- **Stop the timers in the playbook only:** works (it is how v0.1.25 was deployed), but it does
  not cover manual runs, has no reason or expiry, and leaves the timer stopped if the deploy
  fails before the task that restarts it.
- **Reuse `scheduling.paused`:** that setting lives in Ansible-rendered `config.toml` and means a
  long-term operator pause; toggling it for a deploy means rendering config twice and conflating
  the two.

## Consequences

- The first deploy of the version that ships this still talks to the old version, which has no
  `baton.maintenance`; the playbook falls back to stopping `baton.timer` and waiting.
- Draining waits for the running item, which can take a long time (one agent call can run for
  tens of minutes); the deploy's wait timeout must allow for that and fails rather than
  interrupting work.
- If a resident process is built later (#60), its maintenance endpoint should keep these
  semantics (drain, never interrupt, expiry), and a new ADR supersedes the "no API" part of this
  one.
