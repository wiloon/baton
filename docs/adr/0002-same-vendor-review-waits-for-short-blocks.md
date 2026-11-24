---
status: accepted
---

# Same-vendor review waits out short quota blocks

A cross-check by another vendor catches more than one vendor checking itself with a different
model, so same-vendor review (Codex reviewing Codex, Claude revising Claude's spec) is a last
resort. We allow it only when every other vendor that could do the job is blocked for the long
term: weekly quota at its cap, a dead credential, or a cooldown longer than a configurable
threshold. If any of them is blocked only for the short term (5-hour window at or above its
effective cap, or a short cooldown), the item defers and waits for that vendor. The rule is
symmetric across vendors. Claude out of its 5-hour window makes a Codex-written PR wait for
Claude just as Codex out of its 5-hour window makes Claude's work wait for Codex.

For a vendor with a reported 5-hour window, use `[vendors.<id>] five_hour_max` when set and
`[quota] quota_five_hour_max` otherwise. A vendor without a 5-hour window has no 5-hour quota
block.

Apply this comparison to declared vendors eligible for the role (ADR 0007) and the
current task. A same-vendor fallback must be role-allowed and pass its adapter's model
eligibility policy (ADRs 0004 and 0005).

## Considered options

- **Fall back whenever the other vendor is blocked (previous behaviour):** keeps the pipeline
  moving, but a routine 5-hour exhaustion turns most work into self-review for hours at a time.
- **Wait for short blocks (chosen):** costs at most a few hours of latency per item and keeps
  cross-vendor review whenever the week still allows it.
- **Never review with the same vendor:** strongest review, but the pipeline stalls for days
  when the other vendors' weekly quota is gone.

## Consequences

- Block reasons now carry a horizon. Cooldown files hold only an expiry time, so a cooldown
  counts as short when it ends within `quota.same_vendor_wait_max_minutes` (default 360) and as
  long otherwise.
- A stale or missing quota snapshot counts as short: the item waits for the quota worker to
  refresh it before deciding to self-review.
- There is no overall wait limit. A 5-hour window resets on its own, and a vendor that stays
  blocked turns into a long block (weekly cap or long cooldown) that then allows the fallback.
- Applies to every role that has a same-vendor fallback: code review, screenshot review,
  spec review and spec revision.
- The wait applies to the deferred item. An agent PR waiting on quota or cooldown can lend its
  `max_pending_prs` slot to another issue in the same repository. The repository can admit at
  most one extra issue above the cap. Existing PR work keeps priority over new issues.

## Revision record

- Issue #99: let a quota-deferred PR lend its slot so work eligible for a free vendor can start
  while the PR waits for independent review.
- Issue #103: scope fallback and short-block waiting to role-eligible vendors so a vendor
  outside the registry or allowlist cannot run or hold up the step.
- Issue #148: use each vendor's effective 5-hour cap for short quota blocks so one vendor
  can use its full 5-hour window while others keep their reserve.
- 2026-11-24 (owner, temporary while Codex and Cursor were out of weekly quota): Claude may
  review Claude-written code too, on a distinct model only (Opus after Sonnet). Claude records
  the writer's model; `SAME_VENDOR_SAME_MODEL` keeps the same-model fresh-session fallback to
  Codex. Revert by setting Claude's `SAME_VENDOR_REVIEW` back to `False`.
