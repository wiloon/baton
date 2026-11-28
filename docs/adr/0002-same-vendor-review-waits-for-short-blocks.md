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

The pipeline never stalls on review independence. This is permanent and applies to every
reviewer-capable adapter (the test stub excepted). When no other vendor can review and only
long blocks stand in the way, a vendor reviews code it wrote: on a configured model that is
provably distinct from every recorded writer model, or, when there is none or a model is
unknown, on its `review` route's model in a fresh session (`same-model-fresh-context`). ADR
0004 gives the order in which models are tried. The screenshot review is stricter and still
requires a distinct model (ADR 0005).

Spec review also avoids the vendor that last wrote the issue outside the spec loop, as named by
the issue's author marker: in round 1, and in a later round after such an edit. That vendor is
then a last resort under the same short-block wait. The marker is untrusted and only a
preference: when no other vendor can run for the long term, the marker's vendor runs even without
a same-vendor policy or a provably distinct model, using a distinct route model when one exists.
The rules for the previous round's vendor are unchanged.

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
  most one extra issue above the cap. Existing PR work keeps priority over new issues. `P0`
  issues are the exception to both of these last two rules (ADR 0010).

## Revision record

- Issue #99: let a quota-deferred PR lend its slot so work eligible for a free vendor can start
  while the PR waits for independent review.
- Issue #103: scope fallback and short-block waiting to role-eligible vendors so a vendor
  outside the registry or allowlist cannot run or hold up the step.
- Issue #148: use each vendor's effective 5-hour cap for short quota blocks so one vendor
  can use its full 5-hour window while others keep their reserve.
- Issue #221 (owner rule 2026-11-24, extended 2026-11-26, permanent): the pipeline never
  stalls on review independence. Every vendor, Claude and Cursor included, reviews its own code
  as the last resort, on a distinct model when one is configured and otherwise on the same
  model in a fresh session. This replaces the 2026-11-24 entry that let Claude do so on a
  distinct model only and kept the same-model fallback to Codex.
- Issue #109, round 1: agy declares `SAME_VENDOR_REVIEW = True`, `RECORDS_AUTHOR_MODEL = True`,
  `SAME_VENDOR_SAME_MODEL = False` (a flag issue #221 later removed). Its `distinct_models` strips the effort
  suffix, and config requires agy's `review` route model to be distinct from its `dev`, `fix`
  and `fix_followup` route models, so the never-stall fallback exists.
- Issue #194: exempt `P0` issues from the one-extra-issue bound and from PR work going first, so an
  urgent bug starts at the next pick (ADR 0010).
- Issue #274: spec review also avoids the issue's author vendor, so no vendor reviews a revision
  it wrote while another eligible vendor has quota; the marker alone never stalls the step.
