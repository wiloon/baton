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
[models]                      # non-routing settings stay here: budgets, minutes, cache dirs
fallback_models_codex = ["codex_prev", "codex_strong"]

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

- Route model and subagent fields, fallback lists, the Claude quota probe setting and per-vendor
  retrospective model fields reference aliases. Unknown alias references are a `ConfigError`.
- Config validation requires a base vendor entry for each of `dev`, `fix`, `review`,
  `spec_review`, `spec` and `scan` for Claude, Codex and Cursor, which dispatch supports.
  Unknown roles, vendors and route fields are errors. Each entry requires a `model` field.
  Codex alone accepts `model = ""` to request its CLI default and omit `-m`. Cursor's
  `auto` is an alias value passed to its CLI.
- When the caller selects follow-up, use that role's `ROLE_followup` entry for that vendor.
  If the entry or table is absent, inherit the complete base entry for that vendor, including
  effort and subagent model. A present entry replaces the complete base entry. An omitted or
  empty effort or subagent field uses the CLI default for that field.
- Codex receives effort via `-c model_reasoning_effort` and Claude Code via `--effort`.
  Any effort field on a Cursor route is a `ConfigError` because the model identifier carries
  the effort level. Only Claude routes support a subagent model.
- Retrospectives keep the Opus / Sol / Grok 4.7 family checks and analyst effort policy.
  Each omitted per-vendor field defaults to a documented built-in alias for that vendor's
  current default identifier. Supply those definitions once as built-ins in the alias catalog
  and overlay user definitions by name. This applies to an omitted section and to individual
  omissions, including when retrospectives are disabled. Validate all three resolved
  identifiers at load and pass the resolved identifier to the analyst. The deprecated
  `retrospectives.model` keeps its existing validation and opt-in semantics when `enabled`
  is absent. It supplies no model to the analyst.

## Route selection

The production caller selects the phase and passes it through selection, eligibility checks
and dispatch where applicable. An absent follow-up entry preserves the base assignment.

| Role | When to select follow-up |
| --- | --- |
| `review` | Review round greater than one, including Cursor, except Codex reviewing Codex-authored work under the existing PR/fixer exclusion rules. |
| `spec_review` | Spec-review round greater than one, except Codex reviewing a preceding Codex revision. |
| `spec` | Round-one Claude revision of a Claude review. Later Claude revisions after Claude review use the base entry, as do other revisions. A retry uses the original phase. |
| `fix` | Fixing agent review feedback from review round greater than one. Fixes from round-one review, owner feedback, CI and verification repairs use the base entry. |
| `dev`, `scan` | Always use the base entry. |

Quota probes and retrospective analysts use their dedicated alias settings and have no
follow-up phase. The same-vendor Claude spec paths keep the baseline's alternating
assignments: the first review uses the base review route, later Claude reviews use the
follow-up review route, and Claude revisions after Claude reviews use the opposite assignment.
The picker resolves the Claude revision route for that round before admitting the fallback.
If an inherited entry or another route resolves to the preceding review's model, or the
comparison cannot establish two known, distinct models, Claude is ineligible for that revision.
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
  requests. Code review retains the known different-model Codex fallback and the known
  same-model fallback in a fresh session. Spec review and revision after Codex require
  two known, different Codex models. Same-vendor fallbacks retain ADR 0002's wait for
  short cross-vendor quota blocks. Post-run eligibility and review mode use the actual
  model reported by the run, which may differ after a capacity fallback.
- Carry the actual Claude main-session model from CLI output through dispatch and the runner's
  round record so revision selection can use the preceding review's reported model. Compare
  model identities, accounting for Claude CLI shorthand and concrete IDs for the same model.
  Missing or ambiguous main-session evidence counts as unknown. Before publishing a Claude
  revision after a Claude review, verify that both reported main-session models are known and
  different. If verification fails, use the existing unverified-spec handoff and publish no
  revision or `same-vendor-different-model` note.

## Revision record

- Issue #62, round 1: defined per-vendor inheritance, explicit CLI defaults and caller
  phase selection, preserved existing review eligibility, and covered probe and
  retrospective alias defaults to keep the shipped assignments intact.
- Issue #62, round 2: require distinct Claude models at selection and after dispatch so
  inherited routes and CLI fallbacks cannot turn a spec revision into same-model review.
