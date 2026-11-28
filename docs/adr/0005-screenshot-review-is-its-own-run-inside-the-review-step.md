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
- The runner reads `## Design findings` and `## Design answers` (#96) only from the screenshot
  review comment this run posted, bound by its digest (`Review.current_screenshot`). Round
  numbers repeat across loops on one PR, so a same-round comment of an earlier loop, or any
  comment the run did not post, never creates, resolves or holds a finding.
- Skip screenshot review on owner-authored PRs. Which vendor did the code review never decides
  whether it runs. Select from declared, role-allowed vendors with image input support (Claude,
  Codex and Cursor). The setting remains per repository and off by default.
- An adapter without a guard hook (Cursor, #383) keeps the screenshot run read-only by its
  invocation: `--mode ask`, never `--force`, and a runner-generated permission set that allows
  `Read` only on the worktree, the PR's shots directory and the run's context file, and denies
  Shell, Write, WebFetch and MCP. The permission config and the CLI's working directory are
  runner-owned temporary directories outside the worktree, so no file is written into the PR
  checkout and the PR's own `.cursor` files are not the run's project config. The reviewer
  returns the comment as its final message and the adapter writes the review file; it is not cut
  by a reply cap. The post-run worktree check and reset (`READONLY_RESET`) stay as a second
  layer. The host enables the role only after the real CLI is shown to read a shot and refuse a
  file outside the allowed paths.
- When authorship and fixer exclusions leave no eligible screenshot reviewer, the PR goes to the
  owner right after the code review (ADR 0007), also when the code verdict is `changes`, so no
  fix runs in that loop. A temporary block defers the screenshot step instead (ADR 0002). Both
  apply whichever vendor did the code review.
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
  raised it and its number there); the id never changes, so a later round's finding with the same
  number is a different finding. The loop's checkpoint holds every open finding by id (with the
  text of the checks it blocks) and every disagreement, across rounds and a deferred resume. From
  the checkpoint the runner renders one `Open design findings` block, by id, into every fix
  feedback (also a round with no screenshot review, whose fixer otherwise sees only the code
  review) and into every screenshot prompt after round 1. The fixer answers and the reviewer
  judges by id, never by the comment's local number. A loop that starts without a checkpoint gets
  a fresh loop id, kept in the checkpoint across a deferred resume, and a fix comment carries the
  loop and its round (`<!-- baton-fix by=<vendor> loop=<id> round=<n> -->`), so a review reads
  only the answers this loop's fixer gave since the previous screenshot review. Round numbers
  restart at 1 when the same PR starts a new loop (after the owner's feedback, for example), so a
  comment of another loop, or one with no loop id, is never an answer. A round's fixer runs with
  that round's number, after its screenshot review, so the replies since the previous screenshot
  review are the fix comments with `previous screenshot round <= round < current round` (the fix
  that answered round 1's review is `round=1`; with no previous screenshot review the lower bound
  is round 1). That review must judge every open id in the block exactly once under `## Design
  answers`. An id with no valid, single answer stays open, however many rounds pass. Only the
  fixer's *current* answer decides a disposition: the answer to that id in the fix replies since
  the previous screenshot review, which must be exactly one valid line (an id answered twice, even
  identically, or in two of those replies, counts as unanswered). A round with no screenshot
  review does not move that window, and its fixer still gets the block and is asked to answer
  every id in it. If it answers an id an earlier reply in the window already answered, neither
  answer counts: the next reviewer sees none, a `held` makes no disagreement, and the id stays
  open. This is accepted because it fails safe: the finding still blocks its checks, although
  the fixer's position is lost. An earlier round's answer is
  history: the block shows it, labelled as earlier, but it never makes a disagreement and never
  closes an id. A held finding whose current answer is `rejected` is a disagreement for the owner;
  a held finding the fixer adopted, did not answer, or answered ambiguously stays open. Only a
  valid `resolved` answer closes an id, and nothing closes a disagreement, so a later review that
  omits one, or a round with no screenshot review, cannot drop it.
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
- A fix comment whose run the runner saw end in `RESULT: needs-info` carries `outcome=needs-info`
  in its marker (`<!-- baton-fix by=<vendor> … outcome=needs-info -->`), written by the runner from
  the outcome it observed, never by the agent. Collecting the owner's feedback for a fix, the
  runner treats only owner-authored fix comments without that attribute (including every older,
  unmarked one) as the point after which feedback is new; a `needs-info` reply does not move it,
  so the owner's decision before the stop still reaches the next fixer. A run that posts no reply
  (empty reply, or the post fails) leaves no marker and so moves nothing either.

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
- Issue #178: a fix comment records a runner-observed `needs-info` outcome in its marker, and such
  a comment does not end the owner feedback window, because both posting paths drop the agent's
  `RESULT:` line and a label-only retry after a question otherwise lost the owner's decision.
- Issue #321: record what the two-replies rule does across a round with no screenshot review (the
  rule is unchanged), because the multi-round tests pin that outcome and it was only implied.
- Issue #384: remove the skip after a Cursor (or agy) code review. It came from #67, when Cursor
  reviewed only owner-authored PRs, which have no agent screenshots. Those PRs now have their own
  skip, and Cursor also reviews agent PRs, so the rule only blocked screenshot reviews that
  should run (wiloon/enx#1293). Also record that the existing no-eligible-reviewer hand-off and
  deferral now reach rounds those vendors review.
- Issue #383: Cursor joins the screenshot reviewers, because it can read PNGs and the pool of
  Claude and Codex alone can run dry. Record how an adapter without a guard hook stays
  read-only (owner decision: ask mode, deny by default, no `--force`).
