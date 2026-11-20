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
review round N  (repo.screenshot_review, manual checks, and eligible PR/reviewer)
  code review run         role=review              -> PR review comment
  screenshot review run   role=screenshot_review   -> PR comment with "## Screenshots" verdicts
  after both succeed: round verdict = changes if either run reports a Must fix,
                      or the screenshot review lists an open design finding (#96)
fix                        reads both, answers Must fix and design findings, re-shoots every manual-check item
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

- `screenshot_review` is a read-only agent role with its own base model route (ADR 0004) and
  telemetry run and verdict records. The vendor preference role is `screenshot_reviewer`.
- The code review prompt no longer carries the screenshot instructions (`Review.shots_step`
  moves to the new run).
- The screenshot reviewer uses the code reviewer's author/fixer exclusions. An excluded vendor
  with a screenshot fallback policy (Codex) can review only as a last resort under ADR 0002,
  with a known model distinct from every recorded writer model for that vendor. Check the
  screenshot route at selection and preflight, and verify the reported model before
  publishing. This role requires distinct models even in a
  fresh session.
- Skip screenshot review on owner-authored PRs and when the code review adapter has the
  screenshot-skip policy (Cursor). Select from declared, role-allowed vendors with image input
  support (Claude and Codex). The setting remains per repository and off by default.
- After a successful code review (`pass` or `changes`), checkpoint its result before starting
  screenshot review. A deferral resumes that step using the saved code verdict. A failed review
  hands off the PR via `review_failed`.
- Design findings (#96) are settled between the screenshot reviewer and the fixer. The runner,
  not the reviewer's `REVIEW:` line, decides that an open `## Design findings` item keeps the
  loop going; `REVIEW: pass` still means "no Must fix". The fixer answers each finding under
  its own `## Design findings` reply section, and the next screenshot review judges the answers.
  When the rounds run out with only design findings open, the PR goes to the merge gate (ADR
  0006), which leaves the blocked checks to the owner, instead of a `review_limit` hand-off.
  When feedback is cut, the design findings survive with the Must fix section.
- The runner tracks the design exchange itself and does not trust a later review to repeat it.
  A fix comment carries its round (`<!-- baton-fix by=<vendor> round=<n> -->`), so the next
  screenshot review is given the answers to its own previous findings. That review must judge
  every previous finding exactly once under `## Design answers`. A finding with no valid,
  single answer stays open. A held finding the fixer adopted stays open. A held finding the
  fixer rejected is a disagreement for the owner. The loop's checkpoint carries open findings
  and disagreements (with the text of the checks they block) across rounds and a deferred
  resume, so a later review that omits them, or a later round with no screenshot review, cannot
  drop them.
- A fix run that commits nothing is not a `no_commit` hand-off when its reply answers every
  design finding (all `rejected`) and the feedback had no Must fix item: the next round's
  screenshot review judges the rejections. The fixer still re-shoots as the fix rules say.
- Design findings, and the fixer's answers given to the next screenshot review, are complete
  numbered records: they are never cut inside an item. If they do not fit
  `review_context_max_chars` whole, the PR goes to the owner (`review_failed`) instead of the
  loop sending a partial exchange.

## Revision record

- Issue #67, round 1: specify read-only access, distinct-model verification, the owner and Cursor
  exclusions, and how a split review round resumes and hands off.
- Issue #103: express candidate and skip rules through role eligibility and adapter capabilities
  so the existing review workflow applies to the configured registry.
- Issue #96: open design findings also make the round's verdict `changes` and reach the fixer,
  so a design problem the owner decided the pipeline settles cannot end the loop as `pass`.
- Issue #96, round 2: a rejection-only fix round is a valid outcome, the runner validates the
  design answers and carries open findings and disagreements across rounds, and an exchange
  that does not fit the context budget is handed off whole instead of cut, so an incomplete
  exchange can never clear a check.
