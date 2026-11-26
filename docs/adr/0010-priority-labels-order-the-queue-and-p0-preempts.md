---
status: accepted
---

# Priority labels order the issue queue, and P0 preempts

The owner could say whether an issue is wanted and what it waits on, but not which ready issue
goes first. Labels `P0`–`P3` on the target repository's issues now order each repository's
candidates by (priority, issue number); an unlabelled issue ranks after `P3`. Priority never makes
an issue a candidate: only the owner's `ready-for-agent` does, also for a scan finding, which waits
for that label after its verdict.

`P0` is for urgent bugs that must be handled at once (owner, 2026-11-25). A `P0` candidate starts
before the resumed work of lower-priority issues and is not limited by `max_pending_prs`. The
number of `P0` issues over the cap is not bounded: only labelled issues get this, and the owner
controls how many there are.

## Considered options

- **`P0` only reorders and respects the cap:** keeps the cap a hard limit, but an urgent bug waits
  for the owner to review or merge unrelated PRs.
- **`P0` may use at most one slot above the cap:** bounds the load, but a second urgent bug waits
  the same way.
- **`P0` is not limited by the cap (chosen):** every urgent bug starts at the next pick.

## Consequences

- Open `P0` PRs count toward `max_pending_prs` like any other, so they can hold back lower-priority
  issues until they leave the count.
- This amends ADR 0002: existing PR work keeps priority over new issues except new `P0` issues, and
  the "one extra issue above the cap" bound does not cover `P0` issues.
- Nothing running is interrupted. Blockers, `ready-for-agent`, owner authorization and vendor quota
  apply to `P0` as to any issue, and a `P0` that cannot run now does not hold up other work.
- Priority is per repository. The rotation across repositories is unchanged, so a `P0` can wait
  while other repositories take their turn.

## Revision record

- Issue #194: first recorded.
- Issue #290: an unlabelled issue ranks after `P3` (it ranked as `P2`); scan findings are
  candidates only with the owner's `ready-for-agent`.
