---
status: proposed
---

# Model routes reference aliases, one table for every vendor

Model choice was spread over about 40 flat `[models]` keys with a different naming scheme per
vendor (`model_review`, `model_codex_review`, `model_cursor_review`). Reasoning effort could be set
only for Codex and only per role, not per round. Several modules also read `model_codex_*` fields
directly. A model generation change (6.1 → 6.2, #61) touched the baseline, a config default and a
family regex, and found a telemetry allowlist that had silently drifted. We decided that model
identifiers appear once, as **aliases**. Dispatch takes its model and effort from a
**route**: role × tier × phase × vendor → model choice + effort (the tier dimension is ADR 0009's). Probes and retrospectives use
the same alias catalog. Updating a model identifier means changing its alias definition.

```toml
[models]                      # shared model settings and per-role budgets

[vendors.codex]               # excerpt: other required vendor settings omitted
fallback_models = ["codex_prev", "codex_strong"]

[models.aliases]              # the only place a model identifier is written
codex_strong = "gpt-6.2-astra"
codex_fast   = "gpt-6.2-sol"
codex_prev   = "gpt-6.1-sol"
claude_strong = "opus"
claude_fast   = "sonnet"

[models.routes.review]        # excerpt: other required role/vendor entries omitted
codex  = { model = "codex_strong", effort = "high" }
claude = { model = "claude_strong", subagent_model = "claude_fast" }

[models.routes.review_followup]
codex  = { model = "codex_fast", effort = "low" }
```

## Rules

- Route model and subagent fields, adapter fallback lists, configured quota probe models and per-vendor
  retrospective model fields reference aliases. Unknown alias references are a `ConfigError`.
- Each declared vendor needs a base entry for each route mapped from its allowed roles (ADR 0007).
  The mappings are `dev` to `dev`, `fixer` to `fix`, `reviewer` to `review`,
  `screenshot_reviewer` to `screenshot_review`, `spec_reviewer` to `spec_review`,
  `reviser` to `spec`, and `scan` to `scan`. Retrospectives have a dedicated model field.
  Reject unknown routes and fields, undeclared vendors and routes for roles the vendor cannot run.
  Each entry requires a `model` field. An adapter may accept `model = ""` for its CLI default
  (Codex omits `-m`). Cursor's `auto` is an alias value passed to its CLI.
- When the caller selects follow-up, use that role's `ROLE_followup` entry for that vendor.
  If the entry or table is absent, inherit the complete base entry for that vendor, including
  effort and subagent model. A present entry replaces the complete base entry. An omitted or
  empty effort or subagent field uses the CLI default for that field.
- `dev`, `fix`, `review`, `spec_review` and `spec` have optional tier tables `ROLE_light` and
  `ROLE_heavy` (ADR 0009); `ROLE` itself is the standard tier. Without follow-up, use the
  step's tier entry for that vendor, else the `ROLE` entry. A present tier entry replaces the
  complete `ROLE` entry, as a follow-up entry does. When follow-up is selected the tier is
  ignored: `ROLE_followup`, else the `ROLE` entry, never a tier entry. Tier entries need the
  same fields, aliases and vendor roles as other entries. There are no other tier tables
  (none for `scan` or `screenshot_review`, no `ROLE_standard`, no tiered follow-up).
- The adapter declares whether effort and subagent fields are supported. Codex receives effort
  via `-c model_reasoning_effort` and Claude Code via `--effort`. A Cursor model identifier
  carries the effort level. Claude supports subagent models.
- Retrospectives keep the Opus / Sol / Grok 4.7 family checks and analyst effort policy.
  Each omitted per-vendor field defaults to a documented built-in alias for that vendor's
  current default identifier. Supply those definitions once as built-ins in the alias catalog
  and overlay user definitions by name. This applies to an omitted section and to individual
  omissions, including when retrospectives are disabled. Validate resolved identifiers at load
  for declared vendors allowed to be analysts. An explicit per-vendor field requires the
  retrospective role. Pass the resolved identifier to the analyst. The deprecated
  `retrospectives.model` keeps its existing validation and opt-in semantics when `enabled`
  is absent. It supplies no model to the analyst.

## Route selection

The production caller selects the phase and passes it through selection, eligibility checks
and dispatch where applicable. An absent follow-up entry preserves the base assignment.

| Role | When to select follow-up |
| --- | --- |
| `review` | Review round greater than one. The writer-model fallback policy keeps the base route for a reviewer excluded by the existing PR/fixer rules (any vendor reviewing its own work). |
| `spec_review` | Spec-review round greater than one. The known-distinct-model spec policy keeps the base route after a revision by that same vendor (Codex, agy). |
| `spec` | Round-one same-vendor revision for the alternating-phase policy (Claude). Later same-vendor revisions for that policy and other revisions use the base entry. A retry uses the original phase. |
| `fix` | Fixing agent review feedback from review round greater than one. Fixes from round-one review, owner feedback, CI and verification repairs use the base entry. |
| `screenshot_review` | Always use the base entry. Validation rejects `screenshot_review_followup`. |
| `dev`, `scan` | Always use the base entry. |

Quota probes and retrospective analysts use their dedicated alias settings and have no
follow-up phase. The alternating-phase policy keeps the baseline assignments: the first
review uses the base review route, later reviews use the follow-up route, and same-vendor
revisions use the opposite assignment. The picker resolves the revision route for that
round before admitting the fallback.
If an inherited entry or another route resolves to the preceding review's model, or the
comparison cannot establish two known, distinct models, that vendor is ineligible for the revision.
The config remains valid and normal cross-vendor selection applies.

## Considered options

- **Keep flat keys, rename them consistently:** smallest change, but model identifiers would still
  be repeated per role, and round-specific effort needs yet another key family per vendor.
- **Aliases + routes (chosen):** two small tables. A generation change is one line per alias, and
  effort becomes a per-round decision.
- **Also route by task tier:** adopted later by ADR 0009 (`ROLE_light`, `ROLE_heavy`).
- **Also route by area (ui/api/chrome):** deferred. Visual checking will be a role of its own
  rather than an area override, and areas already rank vendors through `[preferences]`.

## Consequences

- One-way for hosts: the old flat keys are rejected with a `ConfigError` that names the new
  tables, with no compatibility layer. Hosts render `config.toml` from the baseline of the same
  checkout and run `--validate` before the pipeline, so a mismatch stops at deploy time, not at
  run time.
- The baseline keeps a literal `[models]` header before `[models.aliases]`. The w10n-config
  template splits the baseline on that header.
- `telemetry/codex.alloy` still lists model identifiers because Alloy cannot read
  `config.toml`. A test checks every model filter against the identifiers resolved from the
  shipped Codex routes, fallback list and retrospective setting.
- Vendor selection and preflight use the same resolved model and phase that dispatch
  requests. The writer-model policy (every reviewer-capable adapter, ADR 0002) allows code
  review of the vendor's own work. It takes the first model provably distinct from every
  recorded writer model from: the `review` route at the step's tier, `spec_review` at that
  tier, the base `screenshot_review` route, `review_followup`, `spec_review_followup`, then the
  vendor's `fallback_models`; an absent route is skipped. Only the model changes: effort and
  sub-agent model stay those of the `review` route. With no such model, no writer record, or an
  unknown model on either side, it runs the `review` route's model in a fresh session
  (`same-model-fresh-context`). The screenshot fallback requires a known
  model distinct from every recorded writer model (ADR 0005). The known-distinct-model spec
  policy (Codex, agy) requires two known, different models for same-vendor review and revision.
  It is enforced at selection and after the run, never by rejecting the config.
  Same-vendor fallbacks retain ADR 0002's wait for short cross-vendor quota blocks. Post-run eligibility and review
  mode use the actual model reported by the run, which may differ after a capacity fallback.
- Cursor is the exception to the last sentence, for writers and reviewers: `cursor-agent`
  reports only `Auto`, so a run that requested a pinned model records that requested model, and
  a run that requested `auto` records `unknown`. A pinned request served by another model is
  therefore labelled `same-vendor-different-model` without proof. The screenshot review is no
  exception (#383). Its Cursor route is pinned (`grok-4.7-high` as shipped; `auto` is rejected),
  and the same-vendor screenshot fallback still needs a known model distinct from every recorded
  writer model. A Cursor writer on `auto` records `unknown`, so while every Cursor `dev` and
  `fix` route, tier entries included, is `auto`, Cursor does not screenshot-review a PR it wrote.
  A host that pins a Cursor writer route to a model other than the screenshot route's makes Cursor
  a last-resort `same-vendor-different-model` screenshot reviewer without proof. Model IDs are
  compared as written, so a writer on `grok-4.7-xhigh` counts as distinct from `grok-4.7-high`
  although only the effort differs. There is no Cursor-specific exception.
- For alternating-phase revision (Claude), carry the actual main-session model from CLI output
  through dispatch and the runner's round record so selection can use the preceding review's
  reported model. Use the adapter's model identity comparison to account for shorthand and
  concrete IDs. Missing or ambiguous main-session evidence counts as unknown. Before publishing
  a same-vendor revision, verify that both reported main-session models are known and
  different. If verification fails, use the existing unverified-spec handoff and publish no
  revision or `same-vendor-different-model` note.

## Revision record

- Issue #62, round 1: defined per-vendor inheritance, explicit CLI defaults and caller
  phase selection, preserved existing review eligibility, and covered probe and
  retrospective alias defaults to keep the shipped assignments intact.
- Issue #62, round 2: require distinct Claude models at selection and after dispatch so
  inherited routes and CLI fallbacks cannot turn a spec revision into same-model review.
- Issue #67, round 1: define the base-only screenshot review route and its eligibility rules
  so selection, validation and dispatch use the screenshot model consistently.
- Issue #103: require routes only for declared, allowed roles and express phase and model
  evidence rules through adapter policies to keep existing assignments and safeguards.
- Issue #109, round 1: agy (Gemini only) follows the writer-model policy with effort-stripped
  model comparison. The actual model must come from the run (`init.model`, or a `cli.log`
  entry tied to the run's conversation id), never the requested alias. A run without it
  fails; there is no exception to this ADR.
- Issue #109, round 2: a `cli.log` display label is mapped to a model ID by a stated
  rule (`Gemini 4 Neon (High)` → `gemini-4-neon-high`) before the Gemini and
  requested-model checks. If the real CLI offers neither `init.model` nor a run-tied
  `cli.log` entry, agy is not enabled.
- ADR 0009 (owner, 2026-11-24): routes gain a tier dimension chosen by the task profile;
  an absent tier entry inherits the standard entry, and follow-up rounds keep `ROLE_followup`.
- Issue #237, spec round 1: wrote the tier rules into Rules. "Base entry" for an absent
  follow-up stays the `ROLE` table whatever the tier, so follow-up rounds resolve as before.
- Issue #324, round 1: agy can revise specs and follows the known-distinct-model spec policy.
  Equal or unknown models make it ineligible at run time and leave the config valid, as for the
  other vendors.
- Issue #221: the writer-model policy now covers every reviewer-capable adapter, tries the
  vendor's other configured models before the same model, and lets an unknown model fall back to
  a fresh session so review never stalls. Cursor records the requested pinned model because its
  CLI names no other; without that its runs could never count as distinct.
- Issue #383: Cursor takes images and can screenshot-review, so the statement that the
  screenshot review never relies on Cursor's requested model no longer holds. Record when it
  does: only with a pinned Cursor writer route (owner decision: pinned screenshot model, no
  Cursor-specific exception).
