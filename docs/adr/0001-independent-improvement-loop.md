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
This is a lifecycle boundary, not a host-isolation guarantee. A completed episode can be assessed
while the runner executes the next episode. Analysis uses its own workspace and worker lock,
without holding the runner's execution lock; shared vendor capacity still requires coordination
and workload priority.

Reuse the existing durable task index and event outbox for handoff. An opt-in systemd timer wakes
a separate consumer to reconcile completed episodes and its own processing state. Explicit task
terminal states determine eligibility; evidence-settling delays do not establish completion.
This avoids introducing a broker or database for the initial single-host volume and tolerates
missed wake-ups through reconciliation. A synchronous callback would couple task completion to
the consumer; a standalone message broker would add operations and dual-write failure handling.
If lower latency becomes necessary, add an event notification while retaining periodic recovery.
