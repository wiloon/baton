---
status: accepted
---

# The screenshot review can clear manual checks

With `screenshot_review` on, the screenshot review (ADR 0005) looks at every manual-check
screenshot and can send a PR back for visual breakage. But it could not clear a PR: the merge
gate handed every PR whose `## Checks to verify` is not `none` to the owner, even when every
screenshot passed. The owner still opened every UI PR, and the review only filtered out the
obviously broken ones. The owner wants the pipeline to be the same for every repository: the
screenshot review checks a UI change, and only what it is still unsure about reaches the owner.

We decided that the screenshot reviewer gives **each manual check one verdict**, and the merge
gate reads them:

```text
screenshot review comment, ## Checks (numbered as the prompt numbered the PR's checks)
  1. verified                 the screenshots show it and it looks right
  2. broken — <why>           they show it is wrong (also a Must fix)
  3. owner — <why>            no screenshot can show it, or the reviewer is not sure

merge gate (every repository), after review passed and CI is green
  every check verified          -> merge; the screenshots still go to Telegram
  any check not verified        -> ready-for-human, listing only those checks
  no usable verdicts            -> ready-for-human, as before
```

"No usable verdicts" covers every case where the runner can't be sure: no screenshot review in
the last round (setting off, owner-authored PR, Cursor review), a `## Checks` section without
exactly one valid verdict per check, a PR description whose checks changed after the review,
a description whose checks section is missing, repeated or empty (no item to verify), and one
that carries both the old and the new heading. Missing information never merges: zero items
verified is not "every check verified". `none` is a separate path and is unchanged. There is no
per-repository way around this gate (issue #95).

## Considered options

- **A. Keep the owner as the gate for every manual check:** safe, but the screenshot review only
  ever adds work and the owner still opens every UI PR.
- **B. Merge whenever the screenshot review says `pass`:** no new format, but `pass` only means
  "nothing looked broken". A check no screenshot can show (a real sign-up, a sound, an animation)
  would merge unseen.
- **C. One verdict per check (chosen):** the reviewer must say for each check whether the
  screenshots prove it. Unprovable or uncertain checks stay with the owner, and the owner sees
  only those.

## Consequences

- The prompt numbers the PR's manual checks and asks for a `## Checks` section; "when in doubt,
  `owner`" is part of the instructions. The parser reads the form loosely (markdown emphasis, a
  list marker, `1:`) and the content strictly: a missing, repeated, unknown or out-of-range
  number reads as no verdicts.
- Checks the owner wrote into the issue's own `## Checks to verify` are judged like any other: the
  owner decided (2026-11-19) that a check the screenshots prove needs no second look. The spec
  step still keeps them word for word, so the reviewer judges the owner's own wording.
- A UI change can now reach `main` without the owner's look. The screenshots still reach the
  owner on Telegram after the merge, captioned "merged after screenshot review".
- A broken verdict in a review that passed (an inconsistent reply) is treated as not verified.

## Revisions

- Issue #94: screenshot clearance needs at least one parsed check and a complete verdict set
  on a well-formed checks section (exactly one, under either heading). An empty section used to
  compare `[] == []` and could clear the gate with no check verified.
- Issue #94: unreadable verdicts are "no usable verdicts" in the hand-off and the tracker too,
  not only in the gate: the runner no longer reports every check as "left by the screenshot
  review" when it could not read the verdicts; the owner gets all current checks under the
  no-verdicts wording. Under `defer`, an empty saved remainder (every check verified) is final
  and adds nothing to the tracking issue.
- Issue #94: the saved remainder belongs to the merge that succeeded. Every `defer` merge attempt
  replaces it before merging (written after usable verdicts, removed otherwise) and removes it
  when the merge fails, so a file left by a failed attempt cannot hide a retry's checks from the
  tracking issue.
- Issue #94: the saved remainder also records the checks it was computed from. The tracker uses
  it only when the merged PR's body has one well-formed checks section with exactly those checks;
  otherwise it sends every current check. Under `defer` the merge goes ahead even when the checks
  changed between the merge step's two reads of the body, so a remainder computed from the
  earlier read could otherwise hide a check added in between.
- Issue #94: the section is named `## Checks to verify`, and the tracking issue `Checks to try`,
  because after the screenshot review only part of the list is still "manual". The old names
  (`## Manual checks`, `Manual checks to try`) stay readable, so open PRs, issues and trackers
  written before the rename keep working; the runner writes only the new name. One module
  (`baton/checks.py`) reads and writes the section, and shared prompt rules live in
  `bin/partials/`.
- Issue #95: `manual_checks = "defer"` is removed, superseding the `defer` path of #48 and #52
  and the `defer` revisions above. The owner wants one pipeline for every repository, and after
  the screenshot review what is left is exactly what the agents could not settle, so it waits
  for the owner. A config that still sets `manual_checks` (either value) is rejected as an
  unknown field, and the runner no longer reads or writes the `Checks to try` tracking issue
  (under either title). Both checks-section headings stay readable.
