---
status: accepted
---

# Screenshot review is its own run inside the review step

With `screenshot_review` on (#50), the pool reviewer looks at a PR's manual-check screenshots in
the same run as the code review. That ties the visual check to the code reviewer's model, the most
expensive one we route (ADR 0004). The visual check also competes for attention with the diff, and
it cannot use a model chosen for screen content such as GPT-6.1 Terra. We decided that the
screenshot look becomes a **separate agent run with its own role, `screenshot_review`**, started
by the review step in the same round as the code review. Its findings join that round's verdict,
so the existing fix loop consumes them unchanged.

```text
review round N  (repo.screenshot_review and "## Manual checks" is not none)
  code review run         role=review              -> PR review comment
  screenshot review run   role=screenshot_review   -> PR comment with "## Screenshots" verdicts
  round verdict = changes if either run reports a Must fix
fix                        reads both, re-shoots every manual-check item (unchanged from #50)
```

## Considered options

- **A. Keep the look inside the code review (#50 as is):** no extra session, but the look always
  runs on the code reviewer's model and effort, and shares its attention with the diff.
- **B. A new pipeline stage after dev/fix:** most flexible, since it could later take e2e videos
  and traces, baseline diffs or nightly checks on main. It also needs new state, checkpoints,
  telemetry stages, quota accounting and its own handoff into fix, and nothing needs that yet.
- **C. Its own run inside the review step (chosen):** gets B's main benefits: its own model and
  effort route, a vendor pick and a focused prompt. It reuses the round, the verdict and the fix
  loop. When a later need such as e2e evidence or main-branch checks outgrows the review step,
  the run moves out into a stage of its own (B), keeping its role, route and prompt.

## Consequences

- `screenshot_review` is a role in the model routes (ADR 0004) and in vendor preferences. The
  telemetry role allowlists list it.
- The code review prompt no longer carries the screenshot instructions (`Review.shots_step`
  moves to the new run).
- The screenshot reviewer follows the code reviewer's independence rule: not the vendor that
  authored the PR, and for same-vendor Codex, a different model.
- Cursor reviews of owner PRs stay as they are. The setting remains per repository and off by
  default.
