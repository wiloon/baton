---
status: proposed
---

# Model routes reference aliases, one table for every vendor

Model choice was spread over about 40 flat `[models]` keys with a different naming scheme per
vendor (`model_review`, `model_codex_review`, `model_cursor_review`). Reasoning effort could be set
only for Codex and only per role, not per round. Several modules also read `model_codex_*` fields
directly. A model generation change (6.1 → 6.2, #61) touched the baseline, a config default and a
family regex, and found a telemetry allowlist that had silently drifted. We decided that model
identifiers appear once, as **aliases**, and that every run takes its model and effort from a
**route**: role × round × vendor → alias + effort. One resolver answers every "which model and
effort" question, so adding a model generation means changing only the alias lines.

```toml
[models]                      # non-routing settings stay here: budgets, minutes, cache dirs
fallback_models_codex = ["codex_prev", "codex_strong"]

[models.aliases]              # the only place a model identifier is written
codex_strong = "gpt-6.2-astra"
codex_fast   = "gpt-6.2-sol"
claude_strong = "opus"

[models.routes.review]        # first round
codex  = { model = "codex_strong", effort = "high" }
claude = { model = "claude_strong", subagent_model = "claude_fast" }

[models.routes.review_followup]
codex  = { model = "codex_fast", effort = "low" }
```

## Rules

- A route, a fallback list and a retrospective model name an alias, never a raw identifier. An
  unknown alias is a `ConfigError`.
- `ROLE_followup` exists for every role. When it is missing, the first-round route applies. This
  replaces today's special case where only Claude has a spec follow-up model.
- `effort` is accepted only where the vendor's CLI takes it: Codex (`-c model_reasoning_effort`)
  and Claude Code (`--effort`). Cursor encodes effort in the model identifier
  (`grok-4.7-high`), so a Cursor route sets no effort and its alias names the level.
- Retrospectives keep their family check (Opus / Sol / Grok 4.7). The check runs on the resolved
  identifier, so it still works when an alias changes underneath it.

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
- `telemetry/codex.alloy` still lists model identifiers, because Alloy cannot read
  `config.toml`. A test keeps every Codex alias value in that allowlist, so they cannot drift
  apart again.
- `vendors.pick`, `Review` and `dispatch.configure` stop reading `model_codex_*` fields and ask
  the resolver instead. The reviewer ≠ author model rule compares resolved identifiers.
