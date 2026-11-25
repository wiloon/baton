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
**route**: role × phase × vendor → model choice + effort. Probes and retrospectives use
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
| `review` | Review round greater than one. The writer-model fallback policy keeps the base route for a reviewer excluded by the existing PR/fixer rules (Codex reviewing Codex-authored work). |
| `spec_review` | Spec-review round greater than one. The known-distinct-model spec policy keeps the base route after a revision by that same vendor (Codex). |
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
  requests. The writer-model policy (Codex) allows code review with a known different model
  or a known same model in a fresh session. Its screenshot fallback requires a known
  model distinct from every recorded writer model (ADR 0005). The known-distinct-model spec
  policy (Codex) requires two known, different models for same-vendor review and revision.
  Same-vendor fallbacks retain ADR 0002's wait for short cross-vendor quota blocks. Post-run eligibility and review
  mode use the actual model reported by the run, which may differ after a capacity fallback.
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
