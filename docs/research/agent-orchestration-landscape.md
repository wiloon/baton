# Coding-agent orchestration alternatives and model-assisted scheduling

Research date: 2026-11-05. Status: investigation, not an adoption or architecture decision.

## Recommendation

Evaluate Agent Orchestrator and Paperclip before extending BATON into a general agent-management
platform. If a candidate meets the execution requirements, prefer adopting it with a small adapter
for subscription capacity and repository conventions over maintaining a second complete platform.
Gas Town is the most direct reference for an AI coordinator supervising coding workers.

For BATON, evaluate model recommendations for ambiguous decisions while retaining deterministic
admission, state transitions and execution controls. Begin with observation mode and measured
comparisons, rather than replacing the scheduler with an unrestricted agent.

These are recommendations from documentation review. No candidate was installed, benchmarked or
validated against BATON workloads during this investigation. Release activity establishes active
maintenance, not unattended reliability or suitability for this deployment.

## Requirements that determine whether replacement is worthwhile

BATON currently joins several concerns:

- GitHub issue admission, spec review, implementation, PR review, repair, merge or explicit handoff.
- Existing Claude Code, Codex and Cursor CLI runtimes and their subscription authentication.
- Subscription capacity scheduling using five-hour/weekly windows, resets, cooldowns and pacing.
- Reviewer independence from implementation contributions, including fallback policy.
- Isolated worktrees, interrupted-run recovery, bounded retries and human acceptance gates.
- Repository-owned verification and browser acceptance hooks.
- Durable execution evidence and an independently budgeted retrospective worker.

Distinguish subscription utilization from reported monetary spend. A platform advertising budgets
has not thereby demonstrated the ability to balance the existing CLI subscriptions. Likewise,
agent support does not establish identical support for workers, reviewers and unattended sessions.

The question is how much continuing maintenance adoption removes after adapters, upgrades,
migration, hosting and operational support are included.

## Candidate comparison

The capabilities column summarizes the linked primary sources. Fit and verification columns are
our assessment and open questions, not claims that features are absent.

| Candidate | Documented capabilities | Potential fit | Verification still required |
| --- | --- | --- | --- |
| [Agent Orchestrator](https://github.com/OrchestratorInc/agent-orchestrator) | A main agent plans and starts workers in separate worktrees. Tracks PRs, reviews and CI; routes failures back to the responsible worker. Lists Claude Code, Codex and Cursor among supported agents. | First coding-workflow candidate to trial: substantial overlap with BATON's main loop. | Unattended headless Linux operation; role-specific agent compatibility; existing subscription login; capacity resets/pacing; reviewer independence; recovery and repository hooks. |
| [Paperclip](https://github.com/paperclipai/paperclip) | Self-hosted task/agent management with roles, delegation, heartbeats, schedules, approval stages, durable history, budget controls and plugins. | First broader management-platform candidate to trial, especially if agents later perform research or operations as well as coding. | How GitHub remains the task source; exact coding/review/repair behavior; subscription capacity versus recorded-spend enforcement; migration and service overhead. |
| [Gas Town](https://github.com/gastownhall/gastown) | An AI Mayor coordinates workers; persistent work tracking, worktrees, monitoring/recovery roles, verification/merge queues and a configurable dispatch capacity governor. | Strong reference for an AI coordinator and a possible alternative when multi-project worker coordination dominates. | Cost of adopting its Beads/task and operational model; subscription-specific scheduling; evidence/accounting fit; required changes to repository workflows. |
| [OpenHands Agent Canvas](https://github.com/OpenHands/OpenHands) | Self-hosted control center with multiple agent types and local, remote or cloud backends; scheduled and webhook-triggered automations. | Candidate when runtime management, sandbox backends and a unified interface are primary needs. | Exact issue-to-PR/review/repair workflow, agent authentication modes, capacity policy, interrupted-work recovery and retrospective integration. |
| [GitHub Agentic Workflows](https://github.com/github/gh-aw) | Markdown/YAML definitions compile into GitHub Actions; multiple AI engines, sandboxed agent jobs and validated safe-output operations. | Potential replacement for individual automations such as triage, review, CI investigation and scheduled reporting. | Workflow composition, stateful sessions/recovery, existing CLI subscription compatibility, self-hosted runner requirements and cross-workflow capacity sharing. |
| [Symphony](https://github.com/openai/symphony) | Continuously obtains tracker work and runs isolated agent implementations; the demonstrated workflow includes CI/review evidence and PR delivery. | Useful orchestration specification and reference architecture. | Official README calls it an engineering preview and the reference implementation experimental. Evaluate tracker/provider adaptation and operational reliability before treating it as a mature replacement. |

### Maintenance and naming observations

- The former `ComposioHQ/agent-orchestrator` URL now redirects to OrchestratorInc. Its current
  [releases](https://github.com/OrchestratorInc/agent-orchestrator/releases) show continued delivery;
  the present product emphasizes a desktop fleet workspace, so older CLI-first articles need
  checking against the current implementation.
- Paperclip has an active [release history](https://github.com/paperclipai/paperclip/releases).
  Its durable activity and spending limits are relevant, but do not establish subscription pacing.
- [Open SWE's current repository](https://github.com/langchain-ai/open-swe) introduces Robin,
  built on Deep Agents and LangGraph. The README warns of breaking changes and does not guarantee
  stability; deploying it as supplied requires Managed Deep Agents. It is a design reference,
  not a first-choice drop-in candidate on this evidence.
- The older [Gabriel Koerich Bash orchestrator](https://github.com/gabrielkoerich/orchestrator-sh)
  is explicitly superseded by [orch](https://github.com/gabrielkoerich/orch), which describes itself
  as an experiment. The old [routing documentation](https://orchestrator.gabrielkoerich.com/routing/)
  demonstrates a bounded LLM classifier with JSON output and deterministic fallback. That pattern
  is useful; its example models and maintenance status should not be carried into a new deployment.

## Can BATON itself become a coordinating agent?

Yes, selectively. The current runner already coordinates model-powered workers through a
predefined workflow. Giving a model authority to recommend context-sensitive next actions would
add a different kind of coordination; it does not remove the need for execution infrastructure.

[Anthropic's architecture discussion](https://www.anthropic.com/research/building-effective-agents)
distinguishes predefined workflows from agents that dynamically choose their process and tool use,
and identifies the trade-off between predictability and flexible but more costly decisions.
Gas Town's Mayor provides a concrete example of an AI coordinator alongside persistent work and
monitoring machinery. Neither source proves that model-directed scheduling improves BATON.

| Decision | Possible model contribution | Responsibility retained by runner code |
| --- | --- | --- |
| Task planning | Identify missing information and propose decomposition. | Owner authorization, dependencies, work admission and scope limits. |
| Worker/model routing | Rank eligible options using task context and historical outcomes. | Installed/allowed models, credentials, fresh capacity, reviewer exclusions and reservation. |
| Repeated review or repair | Explain whether disagreement warrants another attempt, investigation or human handoff. | Retry/deadline limits, actual checks, allowed actions and terminal state recording. |
| Retrospective selection | Nominate episodes and explain likely investigation value. | Budget, sampling, priority/aging, deduplication and the independent worker. |
| Failure handling | Classify the likely cause and recommend a bounded next step. | Verified process/check outcomes, durable state and authorized execution. |

The quota reset time, locking, CI result and task authorization do not require semantic reasoning.
Configurable policies, queue aging and statistical estimates may improve some decisions without
a model call. Avoid consulting a model on every idle timer activation.

A candidate path is:

1. Code constructs a bounded snapshot and the set of currently eligible actions.
2. A model proposes one action with evidence references and a reason.
3. Code validates the proposal against current state and constraints; stale or invalid proposals
   cannot execute. Unavailable or failed inference falls back to the current policy.
4. Observation mode records the proposal without changing execution.
5. Compare outcomes and overhead before considering bounded live influence.

Use decision/event identity to avoid repeated calls on unchanged state. Account for advisor usage
separately. Recorded reasons are explanations to investigate, not proof of optimality.

Preserve the accepted [independent improvement-loop ADR](../adr/0001-independent-improvement-loop.md).
A workload scheduling advisor and an asynchronous retrospective analyst have different lifecycles;
review any proposal that couples retrospective completion to workload progress as a new decision.

## Evaluation plan

First compare Agent Orchestrator and Paperclip on a small, fixed set of representative scenarios:
a routine code change, a CI repair, a review disagreement, a manual browser handoff, a provider
quota deferral and a process restart. Pin versions and use isolated repositories/workspaces so
comparison runs cannot publish competing changes to production repositories.

Record supported capabilities and gaps, adapter effort, operational footprint, manual intervention,
quality/rework, recovery, elapsed versus active time, and separately reported usage categories.
Unknown or partial measurements remain unknown. A task stopped for hearing or browser acceptance
must remain an explicit handoff; a CI pass alone does not establish completion.

Compare adopt, adapt a small layer, replace only selected automations, and retain BATON. A model
advisor experiment should follow the platform evaluation before substantial platform-specific
implementation, while lightweight contract/design work can proceed independently.

## Tracking

- Existing [#15](https://github.com/wiloon/baton/issues/15): selective retrospective/audit triggers,
  runner rules, advisory nominations, sampling and budget allocation. Do not duplicate this work.
- [#16](https://github.com/wiloon/baton/issues/16): evaluate existing platforms and compare adoption,
  a small adapter, partial replacement and retaining BATON.
- [#17](https://github.com/wiloon/baton/issues/17): design bounded model-assisted scheduling and
  evaluate recommendations in observation mode before considering live influence.

This document records research and options. Detailed work and acceptance checklists belong in
GitHub issues; production adoption and changed scheduling authority require their own decision.
