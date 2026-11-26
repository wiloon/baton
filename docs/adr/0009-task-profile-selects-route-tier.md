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
- The profile maps to one tier per role. Code applies floors that judgement cannot lower: a
  change to an owner-review path, or a profile with high risk, never gets `light` review.
- A tier entry follows ADR 0004's inheritance: an absent `ROLE_TIER` entry for a vendor inherits
  that vendor's standard entry. Follow-up rounds keep using `ROLE_followup`, because a follow-up
  round re-checks earlier findings whatever the task's size.
- Same-vendor rules (ADR 0002, ADR 0004's distinct-model policies) use the resolved model of the
  chosen tier, so a tier never turns a review into a same-model review.
- The profile also gives a **vendor preference**: for each role, an order of the configured
  vendors that suit this task, with a reason. It is used like the owner's `[preferences]` order,
  which it precedes for this task: only to break ties between vendors that may run. Code applies
  every hard rule first: quota caps and cooldowns, review independence (ADR 0002 and the issue
  author marker), vendor role grants (ADR 0007) and the same-vendor model policies. A preference
  never makes an ineligible vendor run and never makes a step wait for a preferred vendor that
  is blocked. A vendor name that is not configured is dropped.
- So judgement decides three things for a step, all within what the owner configured: the order
  among eligible vendors, and through the tier the model and the effort. It names no model and
  no effort value.
- The intake decision point starts in observation mode: the runner records the profile, the
  tier and the vendor order it would use, and dispatches the standard tier with the owner's
  vendor order. Going live needs evaluation evidence from observation (tokens per task, review
  rounds, bounce rate per tier and per vendor), per ADR 0008. Tier and vendor preference can go
  live separately.

## Considered options

- **Keep role-only routes (status quo):** predictable, but cost and depth do not follow the task.
- **Let the advisor name a model and effort directly:** most flexible, but a judgement error
  could pick a model the owner never priced or a same-model reviewer, and model IDs would leak
  into prompts. Rejected.
- **Rule-based sizing from labels or diff size:** cheap, but an issue's size is known before
  any diff exists, and labels are often missing. Rules remain the fallback when the advisor has
  no answer: the standard tier.
- **Let the advisor pick the vendor outright:** it could pick a vendor that is out of quota or
  that wrote the code under review, and one wrong call would stall a step. Rejected: the advisor
  orders eligible vendors, code decides eligibility.
- **Separate judgements for model and effort (two dimensions instead of one tier):** finer, but
  it multiplies the routes the owner must price before there is any evidence that three tiers
  are too coarse. Deferred until observation data says so.
- **Tiers and a vendor order chosen by the advisor from owner-configured routes (chosen).**

## Consequences

- ADR 0004 gains a tier dimension: route = role × tier × phase × vendor.
- Telemetry records the tier with every run, and the recommended vendor order next to the
  vendor that ran, so the improvement loop (ADR 0001) can compare tiers and vendors per kind of
  task.
- There is no evidence yet that one vendor suits a kind of task better than another, so the
  first vendor preferences are guesses. Observation is what turns them into evidence.

## Revision record

- Owner, 2026-11-24: let a task's profile choose a configured route tier for every vendor, so
  each step's model and effort fit the task without wasting tokens; judgement never names a
  model.
- Owner, 2026-11-26: the profile also orders the eligible vendors per role, so vendor, model and
  effort all follow the task; code keeps quota, independence and role grants. The split
  recommendation moved to spec review (#272).
