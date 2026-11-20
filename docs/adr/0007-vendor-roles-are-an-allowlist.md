---
status: proposed
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

## Revision record

- Issue #103: record the hard role allowlist and its effect on selection, waiting and handoff.
