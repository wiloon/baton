---
status: accepted
---

# The supervisor orders work by priority across repositories

Priority was per repository (ADR 0010): under `baton serve` (ADR 0011) the supervisor started
children in plain rotation, and each child picked the best item of its own repository. With fewer
slots than repositories, a slot could go to a repository with only a `P3` while another
repository's `P0` waited. Tracked in #291.

The supervisor now lists the open issues of every due repository when free slots are contended,
and fills them by global priority. This amends ADR 0011's "the supervisor never calls GitHub" and
ADR 0010's "Priority is per repository".

## Selection

- Only repositories that are due (`Supervisor.eligible`) take part. Listing runs only when more
  due, not-running repositories compete than there are free slots; otherwise each gets a plain
  `--serve-child` as before.
- Free slots are filled, one child per repository, in this order (rotation breaks ties):
  1. Repositories with in-flight work: `--serve-child --no-new-issue`.
  2. Any other due repository with no child for one hour (from supervisor start):
     `--serve-child --no-new-issue` (housekeeping without starting an outranked candidate).
  3. Candidate issues by (priority, rotation order of the repository, issue number):
     `--serve-child --issue N`.
  4. The remaining due repositories (nothing to rank, or the listing failed): `--serve-child`.
- **In-flight work** is what the child's resume steps would find: an open PR labelled
  `agent-review`, an agent-branch PR labelled `agent-fix`, or local `pr-opening-*`,
  `review-pending-*` / comment-post episodes, fix-push episodes, or pending specs.
  `slot-deferred-*`, recovery files, `ready-label-pending-*` and settlement records alone are not
  in-flight. A repository with in-flight work ranks ahead of every new issue anywhere; ADR 0010's
  within-repository rule stands for a new `P0` of that repository.
- There is no preemption of work already in progress (#301 later) and no per-repository weight
  (#300 later).

## Shared eligibility

The read-only part of the child's selection (`ready-for-agent`, milestone, excluded labels, open
blockers, owner check, no open PR on the issue's agent branches, no pending spec, and the
`max_pending_prs` count with the same formula as `next_issue()`, including the `P0` exemption)
lives in `baton.eligibility` and is called by both the supervisor and the child. The child alone
runs writes, agent CLIs, vendor quota, cooldowns and work locks. Owner authorization is cached in
memory per (repository, issue, `updatedAt`) for the supervisor pass; the child always checks
again. Listings are never cached across passes.

## The child

`--issue N` and `--no-new-issue` are valid only with `--serve-child` and not together. They do not
set `force_issue` or `manual`: scheduled semantics (`p0_pass`, chaining, exit codes) stand.
`p0_pass` is unchanged in every mode. With `--issue N` the only new non-`P0` issue the child may
start is N; with `--no-new-issue` it starts no new non-`P0` issue. Neither mode scans.

An assigned child that took the repository lock and ran no item exits **81**. The repository is
due at once, and that assignment is suppressed for `serve.poll_seconds`. Exit 0 and 80 keep
ADR 0011's poll wait and `chain_max`. Failures, lock busy, maintenance and pause are unchanged.
ADR 0011 rejected a "did an item" exit code because one poll's wait was harmless; with a global
order that wait would let a lower priority elsewhere take the slot of work that only had to wait
for a vendor.

## Budget and failure

Listing calls time out at 20 s each and the pass is bounded at 60 s; repositories not listed in
time count as failed (group 4). A stop is checked between calls; children are reaped after the
pass. About three calls per repository per listing pass plus one per new or changed candidate;
passes happen only under contention. A failed listing for one repository does not stop the others.

The legacy `baton.timer` path (`walk()`) keeps plain rotation as the rollback path.

## Revision record

- Issue #291, round 1. Status moved to accepted with the owner's decisions on supervisor
  selection, fresh state each pass, no preemption, no weight, and exit 81 for an assigned child
  that ran no item.
