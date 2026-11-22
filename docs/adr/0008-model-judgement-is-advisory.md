---
status: accepted
---

# Model judgement is advisory; the runner keeps authority

Some steps need judgement that rule code handles poorly. Examples are deciding whether a stuck
task can recover or must go to the owner, whether a repeated review disagreement deserves another
round, and which eligible vendor suits a task. Each edge case has so far added another rule
branch or another handoff. We add model judgement at named **decision points**. The runner keeps
the execution loop. At a decision point the runner builds a bounded snapshot and the set of
currently allowed actions. An **advisor** returns one of those actions with a reason and evidence
references. The runner then revalidates the choice against current state and executes it. If the
advisor fails, times out, answers outside the set, or the state has changed since the snapshot,
the runner uses the existing rule instead.

Advisors cause no side effects. The runner applies every label change, push, comment, merge and
close. Code still enforces review independence, owner authorization, quota limits, vendor role
grants (ADR 0007), retry limits and actual CI results. An advisor cannot widen its own action set
or declare a failed check passed.

The **routine advisor** makes one stateless call to an API model outside the worker
subscriptions, DeepSeek by default. Scheduling judgement keeps working when a worker vendor's
five-hour window is full. Before a task handoff, the runner classifies the reason in code.
Rules handle known waits and safe retries. Owner-only reasons go directly to the owner.
For judgement calls, the routine advisor can choose among the applicable bounded recovery
actions. For a problem that needs investigation, it may recommend one retry of the failed
step or a handoff. The decision records can justify a separate follow-up for a backend with
read-only tools behind the same interface if these retries frequently fail.

A safe retry is a rule. For a GitHub read or write that failed in transit, or an expected comment
that is not visible yet, the runner may replay the saved operation within a bounded budget that
survives a restart. It repeats a write only after confirming that the write did not land and that
its inputs are unchanged. A landed write counts as done and superseded work is dropped. Anything
else goes to handoff, except the description requests below. The saved merge of an accepted
spec's ADR PR qualifies, because it repeats a merge the runner already decided on and checks
acceptance and CI again. That merge is pinned to the accepted head, or to the runner's own update
of it once verified as only a clean merge of the base branch. A new head alone is no such proof.
The update itself names the validated head as a condition, so GitHub refuses it and leaves the
branch untouched if someone pushed first. GitHub applies an update asynchronously, so an
unchanged head can't confirm that an update did not land. After an inconclusive response the
runner never resends that update. It waits for a head that passes the same verification and
otherwise hands off. One deadline, saved before the update is sent, bounds the request, that wait
and every call in it. A restart doesn't extend the deadline, and an answer that arrives after it
validates nothing. Other merge recovery keeps its decision point.

After a fix, a rejected or failed PR description request is a recoverable runner outcome. A
failed request that can be resumed may first use the safe retry budget. If it still can't be
applied, the runner discards it, reports why, and uses the usual evidence of progress to decide
whether review can continue. Review and merge preflight still check the live PR. This case needs
no advisor decision or handoff of its own.

Every advisor call starts a fresh session, and no conversation carries over to the next call.
Continuity lives in state files, GitHub and the decision record. Each call appends a **decision
record**: a snapshot reference, the allowed actions, the advisor, model and prompt versions, the
choice and reason, latency and usage. The outcome is filled in later. Advisors do not write their
own memory. A learned pattern reaches an advisor prompt only through the improvement loop
(ADR 0001), as a reviewed change in this repository.

A new decision point starts in **observation mode**. The runner records the recommendation and
then executes the rule. Letting a decision point change work requires its own decision, backed
by evaluation evidence from observation mode. The owner can enable a decision point live from
the start when its worst wrong choice costs at most one more bounded attempt followed by the
existing handoff, within the runner's normal authorization, review and retry rules. This covers
the review and spec round limits (#17) and the bounded recovery steps identified for live use
in #125. An advisor action that can resume a merge starts in observation. For spec questions,
the owner approved live use of complete grounded answers that restate decisions already made.
Reversible defaults stay in observation. Rules and owner-only reasons never call the advisor.

The global `off`, `observe` and `live` setting caps the mode of every decision point. In global
`live`, the runner uses each point's approved mode, so new points can be evaluated while the
round limits stay live. A repository can turn the advisor off. The runner persists at most one
advisor action per reason and task episode, and a recurrence after that action goes to handoff.

## Considered options

- **Rules only:** predictable, but every new edge case adds a branch or an owner handoff.
- **Resident supervisor agent** (one Claude Code session drives the pipeline): flexible. It
  competes with worker quota, though, and its context degrades over a long session. Recovery after
  a crash or upgrade is unclear, and its decisions are hard to replay or test.
- **One resumed session per task episode:** keeps context across a task, but hits context
  compaction and restart problems. An episode summary kept in state gives most of the benefit.
- **Advisory decision points (chosen):** keep the deterministic loop, and replace rule branches
  with model judgement one decision point at a time, each with a rule fallback.

## Consequences

- Advisor usage is recorded separately from workload usage.
- The routine advisor sends bounded issue and PR content to an external provider. Baseline
  context can include diff size and CI check status. Each repository must opt in separately
  to raw diff excerpts, CI log excerpts and local repository documents used as citation
  evidence. Each opt-in defaults to off. Redaction and payload limits apply to all fields.
  A repository can turn the routine advisor off and use the rules at every decision point.
- Advisor-authored answers carry separate provenance. In live mode the runner can publish a
  complete, cited grounded answer set and resume revision within the remaining budget.
  Independent spec review and normal merge gates still apply. Reversible defaults and new
  owner decisions lead to handoff, and a genuine owner answer takes precedence.
- Over time, rule code should shrink into fallback paths while the mechanism stays the same:
  state machine, quota and leases, permissions, review independence and side-effect execution.
  Later bounded agents, such as issue decomposition, follow the same contract. They return data
  and the runner acts on it.
- Suppose most new decisions turn out to need multi-step model loops rather than a single choice.
  Then revisit adopting an existing orchestration platform (#16) before turning BATON into a
  general agent orchestrator.

## Revision record

- Issue #17: record advisory decision points, the routine and escalation advisors, fresh sessions
  per call, and observation mode before live influence.
- Issue #17 (owner, 2026-11-20): the review and spec round limits go live without an observation
  period, because their actions are bounded to one extra round; every other decision point keeps
  the observation-first default.
- Issue #125 (owner direction, 2026-11-20): use the routine advisor at eligible task handoffs in
  place of a default Claude Code escalation session. Record the allowed live recovery steps
  per decision point so merge recovery and spec answers can start in observation alongside the
  existing live round limits.
- Issue #125 (round 1 owner answers): allow complete, cited grounded answers to resume revision
  within existing review gates to reduce handoffs. Keep reversible defaults in observation and
  require separate repository opt-ins for the three excerpt types to preserve the current data
  boundary when only the advisor is enabled.
- Issue #141: let the normal progress rule and preflight handle rejected description requests
  after a fix, so a bad replacement does not send a valid fix to the owner.
- Issue #151: define a safe retry, so category-1 replay can't duplicate a write or act on changed
  inputs. Place the saved ADR merge write under that rule, separate from advisor merge recovery,
  and state that #141's exception also covers failed description requests, per the owner.
- Issue #151 (spec round 2): require proof that an updated ADR head is the runner's clean update,
  so a concurrent push can't become the head that the merge pins.
- Issue #151 (spec round 3): make the ADR branch update conditional on the validated head, because
  checking the result afterwards can't undo an update to a head the runner never validated.
- Issue #151 (spec round 4, owner answer): never resend an ADR branch update after an inconclusive
  response, because a queued update leaves the old head visible and absence can't be confirmed.
- Issue #151 (spec round 5): give each ADR branch update one saved deadline that also bounds its
  calls, because a stalled call or a restart could otherwise postpone the handoff without limit.
