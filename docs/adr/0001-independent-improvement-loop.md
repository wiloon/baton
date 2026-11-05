---
status: accepted
---

# Run the improvement loop independently of task execution

BATON's owner repeatedly reviews completed coding tasks to identify scheduler, model and token
optimizations. We will automate that work in an improvement loop with its own invocation,
state and resource limits. The execution loop supplies evidence; a retrospective or experiment
failure must not change a task's outcome or block its progress. See the
[roadmap](../plans/continuous-improvement.md) for delivery stages.

## Considered options

- **Continue manual reviews:** little implementation work, but repeated setup and inconsistent
  evidence make sustained evaluation expensive.
- **Optimize inline during execution:** immediate feedback, but couples task availability to
  evaluation and makes it harder to attribute behavior to a stable runner version.
- **Independent improvement loop (chosen):** reuses the ledger while keeping evaluation budgets,
  retries and candidate changes separate. It needs durable processing state and explicit handling
  of evidence that arrives after an episode ends.

## Consequences

The first release produces local retrospectives using the existing ledger and a bounded analysis
invocation. Later releases can propose changes, evaluate experiments and promote releases.
Candidate changes run from a separate checkout or worktree; the executing runner is not replaced
by its own in-flight task. Evaluation criteria and release permissions have separate review from
the candidate changes they judge.

The initial deployment uses another process on the existing runner host, not a new service stack.
This is a lifecycle boundary, not a host-isolation guarantee. A shared host requires coordination
of agent invocations, quota and execution priority.
