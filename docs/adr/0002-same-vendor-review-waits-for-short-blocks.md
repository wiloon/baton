---
status: accepted
---

# Same-vendor review waits out short quota blocks

A cross-check by another vendor catches more than one vendor checking itself with a different
model, so same-vendor review (Codex reviewing Codex, Claude revising Claude's spec) is a last
resort. We allow it only when every other vendor that could do the job is blocked for the long
term: weekly quota at its cap, a dead credential, or a cooldown longer than a configurable
threshold. If any of them is blocked only for the short term (5-hour window full, or a short
cooldown), the item defers and waits for that vendor rather than falling back. The rule is
symmetric across vendors: Claude out of its 5-hour window makes a Codex-written PR wait for
Claude just as Codex out of its 5-hour window makes Claude's work wait for Codex.

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
- Applies to every role that has a same-vendor fallback: code review, spec review and spec
  revision.
