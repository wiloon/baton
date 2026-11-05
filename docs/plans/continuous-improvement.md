# Continuous improvement of BATON

Status: agreed direction; implementation has not started. First delivery:
[automatic task retrospectives](../specs/task-retrospectives.md).

Tracking: [plan #3](https://github.com/wiloon/baton/issues/3),
[Stage 1 implementation #4](https://github.com/wiloon/baton/issues/4).

## Background and problem

The owner currently selects a small issue in a workload repository, submits it to a Linux runner,
then opens a separate agent session to review execution logs, monitoring, model selection and
token consumption. Useful findings lead to BATON changes, a PR, a release tag and deployment;
the next workload task starts another iteration.

The repeated investigation instructions and evidence collection consume owner time. Findings,
baselines and outcomes are not yet connected into a durable improvement process. A cheaper
individual model call does not establish lower end-to-end cost or preserved task quality.

## Outcomes

- Automatically assess completed and handed-off work without repeating investigation prompts.
- Preserve evidence, uncertainty and decisions across sessions and releases.
- Reduce consumption and avoidable waiting per acceptably completed task, while monitoring
  correctness, rework, human intervention and regressions.
- Progressively automate proposal, validation and promotion as the evaluation becomes reliable.

## Architecture and evidence

The [ADR](../adr/0001-independent-improvement-loop.md) separates the execution and improvement
loops. The [glossary](../../GLOSSARY.md) defines episodes, retrospectives and experiments.

The execution loop remains responsible for authorized workload tasks. The improvement loop reads
its evidence, produces a retrospective, groups repeated hypotheses, evaluates candidate changes
and eventually promotes successful versions. Its own invocations and consumption are accounted
for separately, with workload execution taking priority. Retrospectives of completed episodes may
run concurrently with later workload episodes, using independent workspaces and locks. Existing
durable task records/events provide handoff; periodic reconciliation recovers missed work without
a new database or message broker.

Existing foundations include the local telemetry ledger, issue audit, selection explanations,
workflow/config fingerprints, post-merge outcome observations and systemd timers. Exact task
usage comes from run records; monitoring provides supporting context. Deployment-specific names,
credentials and configuration remain outside committed documentation.

Local source inspection establishes these capabilities; it does not establish that any particular
host has telemetry enabled, complete history or current collectors. First deployment must check
coverage on the target host.

## Delivery stages

| Stage | Deliverable | Depends on | Exit condition |
| --- | --- | --- | --- |
| 1. Automatic retrospectives | Local JSON and Markdown evidence reports plus bounded agent analysis, manually invokable and timer-driven | Existing ledger | Merged and handed-off episodes are processed reliably; repeated scans are safe; missing data and analysis failures remain visible |
| 2. Consolidated proposals | Deduplicated hypotheses and selected optimization issues/PRs | Stage 1 reports reviewed against real runs | Each proposal cites evidence, expected benefit and a verification plan; repeated reports do not create duplicate issues |
| 3. Verified experiments | Baseline/candidate comparisons, quality gates and recorded retain/withdraw decisions | Stage 2; representative evaluation cases | Results include task quality, total consumption and uncertainty; failed experiments stop within budget |
| 4. Controlled promotion | Release/tag/deployment automation, limited rollout and restoration of a known-good version | Stage 3; reliable deployment and health checks | Version/config attribution, rollout limits and restoration are demonstrated before automatic promotion is enabled |

Issue selection stays owner-controlled through the existing authorization mechanism initially.
Automatic admission from an owner-curated workload queue can follow once reliable evaluation and
stop conditions exist. The improvement loop does not grant itself queue authorization.

Only Stage 1 is specified for implementation now. Later stages are direction, not authorization
to publish changes or deploy releases. Split their implementation tickets when preceding results
resolve the design questions.

## Evaluation principles

- Compare whole task outcomes, including retries, fixes and human intervention. Report model
  execution time separately from local checks, CI waits and deferred waits.
- Preserve uncached input, cache reads, cache writes and output separately. Reasoning totals and
  vendor totals must not be added again to overlapping categories.
- Subscription quota, token consumption and API-price estimates are distinct measures. Cross-vendor
  token totals alone are not an economic comparison.
- Include probe, retrospective and experiment overhead when judging overall benefit, while keeping
  those costs outside workload episode totals.
- Prefer one causal change per experiment. Different workload issues can have different difficulty;
  one subsequent cheap task cannot prove that a change caused savings.
- Combine fixed representative evaluations, reserved evaluation cases and subsequent real workload
  observations. Replayed tasks must have external writes disabled or isolated.
- CI and review passes are quality signals, not proof of correctness. Post-merge regressions are
  delayed signals; missing observations are unknown, not a clean outcome.
- An optimization can be rejected or deferred. Every iteration need not produce a PR.

## Operating boundaries

Set separate limits for analysis calls, daily evaluation consumption, concurrent experiments and
candidate repair attempts before enabling each capability. A stage's implementation specifies
actual limits rather than inheriting an unbounded agent loop. Evaluation and promotion criteria
are reviewed separately from the changes under test.

The first delivery keeps reports on the host. Publishing findings requires a separate sanitization
step because local evidence can identify private repositories and contain task content.

## Progress and follow-up decisions

- [x] Record background, terms, architectural boundary and staged plan.
- [ ] Implement Stage 1 and its offline acceptance tests.
- [ ] Validate Stage 1 on the target host, including ledger coverage, concurrent workload execution and retrospective pause/capacity behavior.
- [ ] Compare the first three available eligible episode reports with owner review; include a
  handoff case when available, or use an offline fixture and record that live coverage gap.
- [ ] Use that feedback to define Stage 2 proposal thresholds and issue publication policy.

Stage 3 must define task categories, evaluation cases, quality tolerances, observation windows and
minimum evidence before making savings claims. Stage 4 must define release permissions, rollout
size, stop conditions and restoration checks. These remain open until the required evidence exists.
