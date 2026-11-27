# BATON

BATON executes coding tasks and uses evidence from completed work to improve future execution.

## Language

**Execution loop**:
The process that takes an authorized coding task through specification, implementation,
review and completion or handoff.

**Task episode**:
One continuous period of work on an issue or pull request, ending in completion or handoff.
Resuming work after a handoff starts another episode of the same task.
_Avoid_: Using issue and episode interchangeably

**Decision point**:
A named place in the execution loop where a choice needs judgement, offered as a fixed set of
currently allowed actions with a rule-based default.

**Advisor**:
A model that recommends one allowed action at a decision point. It does not change anything
itself.
_Avoid_: AI scheduler

**Supervisor**:
The resident `baton serve` process that schedules tick children, one per repository. Under
contention it lists GitHub read-only to order ready work by priority across repositories; it never
writes to GitHub or runs an agent CLI.

**Decision record**:
The stored account of one advisor recommendation: what it saw, what it could choose, what it
chose and why, and later what happened.

**Task profile**:
An assessment of a task made when it is picked up and refreshed when its spec is accepted:
its size, kind and risk, each with a reason.

**Route tier**:
One of three owner-configured levels (light, standard, heavy) of model and effort for a step;
the task profile chooses the tier, never the model.
_Avoid_: Model selection by the advisor

**Fallback backend**:
The advisor backend used when the primary one (Claude Code) cannot take a call, so judgement
keeps working when worker quota runs out.

**Observation mode**:
A decision point whose advisor recommendations are recorded but do not change the work.

**Improvement loop**:
The process that turns execution evidence into optimization hypotheses, evaluates changes
and retains improvements that preserve task quality.
_Avoid_: Self-modification

**Retrospective**:
An evidence-based assessment of a task episode's quality signals, execution, model choices
and consumption, with explicit uncertainty and optional optimization hypotheses.

**Optimization hypothesis**:
A proposed explanation of avoidable execution cost or quality loss, together with a change
and an observable way to test that explanation.
_Avoid_: Calling an untested hypothesis a proven improvement

**Improvement experiment**:
A bounded comparison of a candidate change against a baseline, with quality requirements,
success criteria and a decision to retain or withdraw the change.
