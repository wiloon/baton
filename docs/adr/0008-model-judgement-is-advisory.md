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
in #125. An advisor action that can resume a merge or supply an answer for a reviser to act on
starts in observation. Rules and owner-only reasons never call the advisor.

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
- The routine advisor sends issue and PR content to an external provider. A repository can turn
  the routine advisor off, and its decision points then use the rules. #125 keeps raw diffs,
  CI log excerpts and local repository file contents out of advisor payloads pending an owner
  decision about data egress. Diff size and CI check status can provide context within the
  existing redaction boundary.
- Advisor-authored answers carry separate provenance and have advisory authority in the spec
  loop. The owner must decide which kinds of answers may be used live before the runner can
  publish one and resume revision.
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
