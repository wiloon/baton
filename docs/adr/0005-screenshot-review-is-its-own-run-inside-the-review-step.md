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
  Each finding gets a stable id when the runner first reads it, `D<round>.<n>` (the round that
  raised it and its number there); the id never changes, so a later round's finding with the
  same number is a different finding. The loop's checkpoint holds every open finding by id (with
  the text of the checks it blocks) and every disagreement, across rounds and a deferred resume.
  From the checkpoint the runner renders one `Open design findings` block, by id, into every fix
  feedback (also a round with no screenshot review, whose fixer otherwise sees only the code
  review) and into every screenshot prompt after round 1. The fixer answers and the reviewer
  judges by id, never by the comment's local number. A loop that starts without a checkpoint
  gets a fresh loop id, kept in the checkpoint across a deferred resume, and a fix comment
  carries the loop and its round (`<!-- baton-fix by=<vendor> loop=<id> round=<n> -->`), so a
  review reads only the answers this loop's fixer gave since the previous screenshot review.
  Round numbers restart at 1 when the same PR starts a new loop (after the owner's feedback,
  for example), so a comment of another loop, or one with no loop id, is never an answer. A round's fixer runs with that round's number, after its
  screenshot review, so the replies since the previous screenshot review are the fix comments
  with `previous screenshot round <= round < current round` (the fix that answered round 1's
  review is `round=1`; with no previous screenshot review the lower bound is round 1). That review must judge every open id in the block exactly
  once under `## Design answers`. An id with no valid, single answer stays open, however many
  rounds pass. Only the fixer's *current* answer decides a disposition: the answer to that id
  in the fix replies since the previous screenshot review, which must be exactly one valid line
  (an id answered twice, even identically, or in two of those replies, counts as unanswered).
  An earlier round's answer is history: the block shows it, labelled as earlier, but it never
  makes a disagreement and never closes an id. A held finding whose current answer is
  `rejected` is a disagreement for the owner; a held finding the fixer adopted, did not answer,
  or answered ambiguously stays open. Only a valid `resolved` answer closes an id, and nothing
  closes a disagreement, so a later review that omits one, or a round with no screenshot
  review, cannot drop it.
- A fix run that commits nothing is not a `no_commit` hand-off when its reply answers every
  design finding (all `rejected`) and the feedback had no Must fix item: the next round's
  screenshot review judges the rejections. The fixer still re-shoots as the fix rules say.
  Cursor's fix resumes after a deferred description update without a second agent run, so it
  saves its validated design answers with the pending request on the first pass and the resumed
  run decides from them; a resume with no saved answers is a `no_commit` hand-off, as before.
- Design findings, and the fixer's answers given to the next screenshot review, are complete
  numbered records: they are never cut inside an item. If they do not fit
  `review_context_max_chars` whole, the PR goes to the owner (`review_failed`) instead of the
  loop sending a partial exchange. The same holds where the fixer's reply is posted: a reply
  cap (Cursor's `REPLY_CAP`) cuts only the text outside the reply's `## Design findings`
  section, which is kept whole first; if that section alone does not fit, the fix hands off
  (`fix_failed`) instead of posting a partial answer that would parse as a complete one.

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
- Issue #96, round 3: findings carry stable `D<round>.<n>` ids and the runner, not the previous
  comment, supplies every open finding to each fixer and later screenshot review, so an omitted
  finding cannot lose its place, be confused with a renumbered one, or skip a round without
  screenshots; the whole-record rule also covers the posted fix reply under a reply cap.
- Issue #96, round 4: only the fixer's current answer (one valid line in the replies since the
  previous screenshot review) can turn a held finding into a disagreement, so a stale rejection
  from an earlier round cannot reach the owner; and Cursor's rejection-only decision survives
  its deferred resume through saved answers instead of an empty reply.
- Issue #96, round 5: the reply window is `previous screenshot round <= fix round < current
  round`, because the loop runs a round's fixer under the same round number as the screenshot
  review it answers; a window that started after the previous review's round would miss exactly
  the answers that review asked for.
- Issue #96, round 6: the reply window is also scoped to the active loop by a loop id in the fix
  comment's marker, because a new review loop on the same PR restarts its rounds at 1 and an
  earlier loop's `D1.1` answer would otherwise collide with the new loop's `D1.1`.
