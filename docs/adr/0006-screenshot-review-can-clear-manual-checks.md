---
status: accepted
---

# The screenshot review can clear manual checks

With `screenshot_review` on, the screenshot review (ADR 0005) looks at every manual-check
screenshot and can send a PR back for visual breakage. But it could not clear a PR: the merge
gate handed every PR whose `## Manual checks` is not `none` to the owner, even when every
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

merge gate (manual_checks = "gate"), after review passed and CI is green
  every check verified          -> merge; the screenshots still go to Telegram
  any check not verified        -> ready-for-human, listing only those checks
  no usable verdicts            -> ready-for-human, as before
manual_checks = "defer"         -> merge; only the unverified checks go to "Manual checks to try"
```

"No usable verdicts" covers every case where the runner can't be sure: no screenshot review in
the last round (setting off, owner-authored PR, Cursor review), a `## Checks` section without
exactly one valid verdict per check, a PR description whose checks changed after the review, and
an issue body the runner can't read. Missing information never merges.

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
  `owner`" is part of the instructions. The parser is strict: a missing, repeated, unknown or
  out-of-range number reads as no verdicts.
- Checks written into the issue's own `## Manual checks` are always the owner's: the owner asked
  to see them. The spec step already keeps them word for word.
- A UI change can now reach `main` without the owner's look. The screenshots still reach the
  owner on Telegram after the merge, captioned "merged after screenshot review".
- A broken verdict in a review that passed (an inconsistent reply) is treated as not verified.
