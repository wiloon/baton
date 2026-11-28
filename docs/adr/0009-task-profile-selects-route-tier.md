---
status: proposed
---

# A task profile selects the route tier

Routes (ADR 0004) pick a model and effort from role, round and vendor only. A one-line docs fix
and a cross-module refactor get the same model and effort, so small tasks spend more than they
need and hard ones can get too little. The owner wants each step of a task to run on a model and
effort that fits the task, on every vendor, without wasting tokens. We decided that each task
gets a **task profile**, and the profile chooses a **route tier** (`light`, `standard` or
`heavy`) for each step. The owner still writes every model and effort in config, per tier, so
judgement picks one of three configured tiers and never names a model.

```toml
[models.routes.dev]                 # the standard tier, as today
claude = { model = "claude_fast", effort = "medium" }

[models.routes.dev_light]           # optional; an absent tier entry inherits the standard one
claude = { model = "claude_fast", effort = "low" }

[models.routes.review_heavy]
codex  = { model = "codex_strong", effort = "xhigh" }
```

## Rules

- The task profile is produced at the **intake** decision point (ADR 0008), when the runner
  picks up an issue, and refreshed when its spec is accepted, against the current main branch.
  It records size, kind (docs, test, bugfix, feature, refactor) and risk, each with a reason.
  The runner stores it with the task episode. Whether an issue is split is decided at spec
  review (#272), not by the profile.
- Intake keeps ADR 0008's contract: its one action besides handoff records a profile, which the
  reply carries and the runner validates as a whole. The profile comment the runner posts is
  advisor-authored text and never counts as owner input. Linked ADR text is sent only under the
  repository's local-documents opt-in (ADR 0008).
- Data boundary for git history (owner, #238): for each path an issue names, intake may send the
  commit count and last-change date on the main branch. Commit subjects, bounded and redacted,
  are sent only under a separate per-repository opt-in that defaults to off, and decision records
  don't keep them. Commit authors and message bodies are never sent.
- The profile maps to one tier per role, for `dev`, `fix`, `review`, `spec_review` and
  `spec`. `screenshot_review` always uses its base route (ADR 0004, ADR 0005), and `scan` is
  not a step of a task, so it has no profile. Code applies floors that judgement cannot lower: a
  change to an owner-review path, or a profile with high risk, never gets `light` `review` or
  `spec_review`. At intake no change exists yet, so the paths the issue names stand for it. The
  record keeps both the advised and the floored tier.
- A tier entry follows ADR 0004's inheritance: an absent `ROLE_TIER` entry for a vendor inherits
  that vendor's standard entry. Follow-up rounds keep using `ROLE_followup` (or, when absent,
  the standard entry), because a follow-up round re-checks earlier findings whatever the task's
  size.
- Same-vendor rules (ADR 0002, ADR 0004's distinct-model policies) judge the resolved model of
  the chosen tier by the adapter's existing policy. For code review, a tier that resolves to a
  writer's model leads to another configured model and otherwise to a fresh session recorded as
  `same-model-fresh-context` (ADR 0004). The spec and screenshot policies still require a
  distinct model.
- The profile does not choose the vendor. Vendor selection stays with code: quota caps and
  cooldowns, the owner's `[preferences]` order, and review independence (the reviewer of an
  issue or a PR is a different vendor from its author whenever one can run; ADR 0002). Judgement
  decides the model and the effort of a step, through the tier, and nothing else.
- The intake decision point starts in observation mode: the runner records the profile and the
  tier it would use, and dispatches the standard tier. Going live needs evaluation evidence from
  observation (tokens per task, review rounds, bounce rate per tier), per ADR 0008.

## Considered options

- **Keep role-only routes (status quo):** predictable, but cost and depth do not follow the task.
- **Let the advisor name a model and effort directly:** most flexible, but a judgement error
  could pick a model the owner never priced or a same-model reviewer, and model IDs would leak
  into prompts. Rejected.
- **Rule-based sizing from labels or diff size:** cheap, but an issue's size is known before
  any diff exists, and labels are often missing. Rules remain the fallback when the advisor has
  no answer: the standard tier.
- **Let the profile also order or pick the vendor:** considered and dropped (owner,
  2026-11-26). What matters about the vendor is independence between author and reviewer, which
  code already enforces; there is no evidence that one vendor suits a kind of task better.
- **Tiers chosen by the advisor from owner-configured routes (chosen).**

## Consequences

- ADR 0004 gains a tier dimension: route = role × tier × phase × vendor.
- Telemetry records the tier with every run, so the improvement loop (ADR 0001) can compare
  tiers per vendor.

## Revision record

- Owner, 2026-11-24: let a task's profile choose a configured route tier for every vendor, so
  each step's model and effort fit the task without wasting tokens; judgement never names a
  model.
- Owner, 2026-11-26: the profile chooses model and effort only (through the tier). The vendor is
  not a judgement call: code keeps author and reviewer on different vendors. The split
  recommendation moved to spec review (#272).
- Issue #237, spec round 1: named the tiered roles and why the others have none; stated that
  follow-up never uses a tier entry; and corrected the same-vendor rule, which had claimed
  more than ADR 0004's writer-model policy allows (a same model in a fresh session).
- Issue #221: a tier that resolves to the writer's model no longer rules out same-vendor code
  review, because the owner decided the pipeline never stalls on review independence.
- Issue #238, spec round 1: said how a structured profile fits the one-action advisor contract,
  kept its comment out of owner input and linked ADRs behind the opt-in, applied the floor to
  `spec_review` too (its review is as much a gate as code review), and defined the floor's input
  and what is recorded, so implementations can't differ on authority or the data boundary.
- Issue #238 (owner answer to spec round 1): set the data boundary for git history, which ADR
  0008's baseline and opt-ins did not cover: counts and dates, plus commit subjects only by
  opt-in, so intake can judge size and risk without sending free text by default.
