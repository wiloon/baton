---
status: accepted
---

# Vendor roles are an allowlist

Preferences order otherwise eligible vendors. When an owner trusts a vendor for only
some steps, the runner needs to enforce that boundary even when its other choices are
blocked.

Declare enabled vendors and their allowed roles under `[vendors.<id>]`. The ID is the
registered adapter ID and remains the identity in state and telemetry. Adapter capabilities
determine which roles the owner can grant. Selection and dispatch require the grant,
including on retries, pinned selections and same-vendor fallback. Preferences only order
eligible vendors. When an order is inherited from another role, filter it to the destination
role's eligible vendors.

Adapter modules declare capabilities and policies. The runner selects behaviour through the
registered adapter for the role. Configuration declares which vendors are enabled, grants
their roles and supplies existing settings such as routes and quota limits. The same registry
of adapters supplies validation and dispatch. Adding an adapter requires a catalog entry.

## Considered options

- **Preferences alone:** keeps more work moving during a quota block but can send a task
  to a vendor the owner has excluded for that kind of work.
- **Hard role allowlist (chosen):** enforces the owner's assignment at the cost of waiting
  when all eligible vendors are blocked.

## Consequences

- Validate role coverage for required workflow steps and enabled screenshot review and
  retrospectives. A scheduled scan without an eligible vendor skips with a diagnostic.
- Keep existing author and fixer exclusions and the model evidence requirements in
  ADRs 0002, 0004 and 0005. A temporary block defers the review. All eligible reviewers
  excluded by authorship or fixer identity, with no qualifying fallback, hands it off
  to the owner.
- Disabling a vendor takes a config edit, including removal of its explicit route, model and
  preference references. Historical attribution survives disablement.
- A pending reply-protocol fix request belongs to the adapter that produced it. If another
  eligible fixer is selected, it starts a fresh request using the current PR description and
  keeps the original fix base for verifying local commits. Existing unmarked pending requests
  can be resumed by the adapter that provided the legacy reply protocol (Cursor).

- Retrospective analysis takes its model from the fixed `model_<id>` setting when the
  config has one (claude, codex, cursor). An adapter without such a field uses
  `ANALYST_ALIAS` resolved through the merged alias catalog (`BUILTIN_ALIASES` plus the host's
  `[models.aliases]`, as today), with no override; an unknown alias is a `ConfigError`. A
  `retrospectives.model_<id>` key for an adapter without that field is a `ConfigError`, not a
  `TypeError`. The adapter's own model-family check applies.
  Every registered adapter, the stub included, defines the analyst constants, so code that
  iterates `agents.ADAPTERS` never hits a missing attribute. Each adapter also contributes its
  analyst environment keep/strip/set constants; the runner applies their union.

## Revision record

- Issue #115: define the analyst model source for adapters without a `model_<id>` field,
  because no new config fields are allowed.
- Issue #115 (round 2): the alias resolves through the merged catalog, not `BUILTIN_ALIASES`
  alone, because the stub's `stub_fast` is not a built-in alias.
- Issue #103: record the hard role allowlist and its effect on selection, waiting and handoff.
- Issue #106: route behaviour through registered adapter capabilities so another adapter can
  use existing workflow roles without adding vendor branches or new configuration fields.
- Issue #113: record who owns a pending reply fix so adapter reselection cannot replay another
  fixer's request or skip verification of its unpushed commits.
