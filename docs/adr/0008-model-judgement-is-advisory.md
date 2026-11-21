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

There are two kinds of advisor:

- **Routine advisor.** This is one stateless call to an API model outside the worker
  subscriptions, DeepSeek by default. Scheduling judgement then does not compete with worker
  quota, and it keeps working when a worker vendor's five-hour window is full.
- **Escalation advisor.** Before the runner hands a task to the owner, it starts a fresh
  Claude Code headless session. The session has read-only tools (`gh` view and list, state files,
  logs, diffs) and a single structured decision as its output. It spends Claude subscription
  quota, so it runs only at handoff and never on idle ticks.

Every advisor call starts a fresh session, and no conversation carries over to the next call.
Continuity lives in state files, GitHub and the decision record. Each call appends a **decision
record**: a snapshot reference, the allowed actions, the advisor, model and prompt versions, the
choice and reason, latency and usage. The outcome is filled in later. Advisors do not write their
own memory. A learned pattern reaches an advisor prompt only through the improvement loop
(ADR 0001), as a reviewed change in this repository.

A new decision point starts in **observation mode**. The runner records the recommendation and
then executes the existing rule. Letting a decision point change work requires its own decision,
backed by evaluation evidence from observation mode. The owner may instead enable a decision point
live from the start when its action set is bounded so that the worst wrong choice costs one more
round followed by today's handoff. The review round limit and the spec round limit (#17) are
enabled that way: the advisor may grant one extra round per task episode, and a second limit hands
off without another call. `mode = "observe"` switches them back.

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
  the routine advisor off, and its decision points then use the rules.
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
