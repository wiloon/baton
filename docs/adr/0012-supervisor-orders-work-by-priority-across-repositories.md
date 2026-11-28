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
  1. Repositories with an in-flight item that is never preempted:
     `--serve-child --no-new-issue`.
  2. Any other due repository with no child for one hour (from supervisor start):
     `--serve-child --no-new-issue` (housekeeping; the resume bar below keeps it from resuming
     outranked work).
  3. One list of in-flight repositories and candidate issues, by (priority, in-flight before new,
     −weight of the repository for a candidate, rotation order of the repository, issue number):
     `--serve-child --no-new-issue` for an in-flight repository, `--serve-child --issue N` for a
     candidate.
  4. The remaining due repositories (nothing to rank, or the listing failed): `--serve-child`.
     A repository whose in-flight work is below the resume bar gets no turn here.
- **In-flight work** is what the child's resume steps would find: an open PR labelled
  `agent-review`, an agent-branch PR labelled `agent-fix`, or local `pr-opening-*`,
  `review-pending-*` / comment-post episodes, fix-push episodes, or pending specs.
  `slot-deferred-*`, recovery files, `ready-label-pending-*` and settlement records alone are not
  in-flight.
- **Weight** (`[[repos]] weight`, an integer ≥ 1, default 1, no expiry; #300) breaks ties between
  candidates of one priority, higher first, in item 3 only; priority always beats it, and an
  in-flight entry still goes before a new candidate of the same priority. In-flight entries and
  items 1, 2 and 4 keep plain rotation. It has no effect when nothing is ranked (no contention), on
  the legacy timer path or inside a child's own choice, and does not change quota spend or vendor
  choice. The supervisor reads it through one accessor, where a runtime override can be added
  (#293).
- A running child is never signalled for a higher priority.

## Waiting steps yield to higher priority (#301)

A new issue of strictly higher priority starts before another repository's waiting in-flight step
(owner, 2026-11-26). At equal priority the in-flight step goes first. The yielded work is not
changed: it waits.

- **Rank of an in-flight item** is the priority of its linked issue (ADR 0010; 4 without a
  priority label). The link is the number in the file name for a pending spec, and the PR's agent
  branch name for a PR or a `review-pending-<pr>` checkpoint. A `Fixes #N` reference and the
  checkpoint's `ISSUE` do not link, so the owner's own PR that mentions an issue is not preempted.
- **Never preempted:** `pr-opening-*`, comment-post episodes (including a checkpoint with a
  waiting or finished post) and fix-push episodes, which finish a half-done GitHub write; a PR
  with no agent branch; and an item whose PR or issue is no longer open (what is left is mostly
  cleanup).
- A repository competes at the best rank of its items; one never-preempted item puts it in group
  1. Preemption is across repositories only: within a repository ADR 0010 stands, so the non-`P0`
  candidates of a repository with in-flight work are not assigned. Its eligible new `P0` makes it
  compete as that new candidate.
- **Resume bar.** The best priority among the assignable candidates of other repositories that
  get none of this pass's free slots (suppressed assignments do not count). A child started from
  a successful listing receives it as `--resume-rank R` and resumes only never-preempted items and
  items of rank `R` or better, in every resume step. Without such a candidate there is no bar.
  The child ranks items with the same shared code; an item it cannot rank waits for a later
  child. A child with a bar counts as assigned: no scan, and exit 81 when it ran no item. The bar
  is part of the suppression key.
- Yielded work waits for as long as higher-priority work is waiting; there is no ageing. The
  hourly housekeeping turn still runs, and may delay a higher candidate by that one short child,
  but resumes nothing below the bar.
- The supervisor logs a yield when it starts or when the waiting item or the item ahead changes,
  not on every pass.

## Shared eligibility

The read-only part of the child's selection (`ready-for-agent`, milestone, excluded labels, open
blockers, owner check, no open PR on the issue's agent branches, no pending spec, and the
`max_pending_prs` count with the same formula as `next_issue()`, including the `P0` exemption)
lives in `baton.eligibility` and is called by both the supervisor and the child. The child alone
runs writes, agent CLIs, vendor quota, cooldowns and work locks. Owner authorization is cached in
memory by the supervisor per (repository, issue, `updatedAt`), across passes; an unreadable
history is not cached. The child always checks again. Listings are never cached across passes.

## The child

`--issue N` and `--no-new-issue` are valid only with `--serve-child` and not together. They do not
set `force_issue` or `manual`: scheduled semantics (`p0_pass`, chaining, exit codes) stand.
`--resume-rank R` is also valid only with `--serve-child`, alone or with either of them.
`p0_pass` is unchanged in every mode. With `--issue N` the only new non-`P0` issue the child may
start is N; with `--no-new-issue` it starts no new non-`P0` issue. Neither mode scans.

An assigned child that took the repository lock and ran no item exits **81**. The repository is
due at once, and that assignment is suppressed for `serve.poll_seconds`. Exit 0 and 80 keep
ADR 0011's poll wait and `chain_max`. Failures, lock busy, maintenance and pause are unchanged.
ADR 0011 rejected a "did an item" exit code because one poll's wait was harmless; with a global
order that wait would let a lower priority elsewhere take the slot of work that only had to wait
for a vendor.

## Budget and failure

Each listing call, retries included, ends within 20 s and the pass within 60 s; repositories not listed in
time count as failed (group 4). A stop is checked between calls; children are reaped after the
pass. About three calls per repository per listing pass plus one per new or changed candidate;
passes happen only under contention. A failed listing for one repository does not stop the others.
A repository with preemptible items costs one more call (its open issues' labels); if that call
fails, the listing failed.

The legacy `baton.timer` path (`walk()`) keeps plain rotation as the rollback path.

## Revision record

- Issue #291, round 1. Status moved to accepted with the owner's decisions on supervisor
  selection, fresh state each pass, no preemption, no weight, and exit 81 for an assigned child
  that ran no item.
- Issue #300, round 1. Added a per-repository weight, which #291 left for later: the owner wants
  to favour one project for a period. It orders only candidates of equal priority.
- Issue #301: a strictly higher-priority new issue now goes before another repository's waiting
  in-flight step (owner, 2026-11-26), replacing "in-flight first" and "no preemption". Spec
  review added the never-preempted items, the resume bar and the cross-repository limit. The
  weight from #300 stays a tie-break among candidates of equal priority.
