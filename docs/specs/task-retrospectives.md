# Automatic task retrospectives

Status: implemented with offline tests; target-host acceptance pending. Stage 1 of the
[continuous improvement plan](../plans/continuous-improvement.md), governed by the
[independent-loop ADR](../adr/0001-independent-improvement-loop.md).

Implementation issue: [#4](https://github.com/wiloon/baton/issues/4).

## Problem statement

After each workload task the owner starts an agent session, repeats the investigation context
and asks it to inspect execution, model choices and consumption. BATON already records much of
the evidence, but does not automatically assemble or interpret it.

## Solution

Provide an independently invoked retrospective command and a periodic catch-up service. For each
eligible task episode, produce a deterministic evidence summary and a bounded agent assessment.
Keep JSON and readable Markdown locally, with durable progress and explicit failure states.
Reports explain what happened, what remains unknown and which hypotheses merit investigation.

## User stories

1. As the owner, I want a retrospective after a task merges, so I can inspect the result without
   starting another investigation session.
2. As the owner, I want planned and abnormal handoffs assessed too, so failures and manual work
   are not excluded from the evidence.
3. As the owner, I want stage/round/vendor/model consumption and selection explanations, so I can
   locate avoidable work and understand why a model ran.
4. As the owner, I want incomplete measurements and unknown models identified, so missing data is
   not mistaken for savings.
5. As the owner, I want facts separated from hypotheses, with evidence references and a validation
   proposal, so I can decide which optimization deserves implementation.
6. As the owner, I want safe catch-up after downtime and retry after analysis failure, so automation
   does not require routine supervision or repeatedly spend quota on the same episode.
7. As the owner, I want analysis overhead and deferred work visible, so the improvement loop's own
   cost and operational failures are reviewable.

## Implementation decisions

### Episode identity and eligibility

Use the ledger's repository and pipeline ID, not issue number alone. Multiple episodes of the same
issue remain distinct. The existing terminal task statuses are `merged`, `planned_handoff` and
`abnormal_handoff`; an interrupted invocation alone does not close an active episode.

Manual mode selects an episode; catch-up mode scans eligible episodes. Provide explicit config,
state/output locations and repository filtering so offline fixtures and multiple repositories work
without production access. Command spelling is finalized in implementation and documented in help.

Require an explicit terminal episode and no live invocation attached to that episode. Inactivity,
a stopped runner or an interrupted invocation never substitutes for a terminal state. A handoff
ends this automation episode, not necessarily the underlying issue's resolution. If liveness or
completion cannot be established from available records, defer with an observable reason.

Apply a configurable evidence-settling interval (initial default: five minutes since that episode's
newest attached evidence). This is neither host-wide nor improvement-loop inactivity, and is not
proof of completion. Prefer explicit attempt-completion records when available; a final callback
can arrive after the merge event. Take a consistent snapshot and recheck its digest before
committing the report. Repeatedly changing evidence is deferred; later evidence revises the report.
A subsequent active episode does not make a previously completed episode ineligible.

### Evidence and output contract

Produce versioned JSON and Markdown containing:

- Episode identity, outcome/handoff reason, issue/PR references, observation cutoff and source digest.
- Available workflow/config fingerprints, CLI versions, requested/actual models; retain multiple
  observed versions rather than attributing a mixed episode to one version.
- Run-level and stage/round/vendor/model summaries, usage coverage, timing coverage, review/CI
  observations and available selection explanations. Missing explanations remain unavailable.
- Distinct uncached input, cache read, cache write and output categories. Mark partial totals as
  partial/lower bounds and report coverage; never turn absent categories into zero.
- Facts with evidence references; hypotheses with confidence, proposed change, expected direction
  of benefit and a falsifiable validation plan; explicitly allow no actionable finding.
- Analysis status, failure/defer reason, analyst model, timestamps and separately recorded usage.

Reuse the audit/ledger interpretation and timing semantics rather than building a conflicting
accounting model. A probe is global overhead, not part of one episode. State that known probe
overhead is separate and link to the existing probe audit; this release need not apportion it.

Generate the evidence report without agent access. Then run an optional, explicitly configured
analyst using a bounded summary and relevant metadata. A missing analyst configuration produces
an evidence-only report with `analysis_state=not_configured`. Automated analysis must be configured
and exercised before Stage 1 is considered validated on the target host.

Stage 1 has no unrestricted raw-log or monitoring access from the analyst. Include available model
selection explanations only through a bounded, sanitized extraction; unknown formats stay unknown.
Follow-up investigations can request more evidence later. Deterministic facts remain intact even
if analysis returns invalid output or contradicts the evidence.

### Processing, retries and late evidence

Use separate retrospective state and outputs; source ledger records are read-only. Serialize
catch-up workers and publish report generations atomically, so a crash cannot mark a half-written
report complete. Commit the processing marker only after validated artifacts exist.

Deduplicate by repository, pipeline ID, source digest and report/analyzer version. An unchanged
successful report causes no new model call. A newer evidence digest creates a report revision;
retain prior revisions and identify the current one. Evidence-only output is not mistaken for a
successful analysis. A crash around a model response can cause an ambiguous retry; record the
attempt and its known usage and do not claim exactly-once model execution.

Initial automation limits: one analyst invocation at a time, at most one new invocation per timer
activation, at most three invocations per day and at most two attempts per report revision. Each
call has a maximum 10-minute deadline, at most 20,000 characters of serialized evidence input and
at most 20,000 characters of accepted analysis output. These are configurable, validated limits.
Where the CLI supports additional token/cost limits, apply and record them; character limits are
not a guarantee of token consumption. Failed and ambiguous calls count toward attempt/daily limits.
After exhaustion, retain the evidence report and expose a terminal analysis failure; an explicit
manual retry can reset the revision's attempt allowance while retaining history and daily limits.

The analyst may assess completed episode A while workload episode B runs. Use a separate
analysis workspace, state and worker lock; never acquire the runner's global execution lock for
the duration of analysis. Serialize retrospective workers without blocking workload scheduling.
Read stable evidence snapshots and keep source ledger records read-only.

Coordinate shared vendor quota/auth/cooldown and supported CLI/session constraints without
assuming ample host hardware implies independent vendor capacity. Capacity reservations/checks
must be safe under concurrent starts; unavailable capacity defers analysis rather than failing or
blocking workload execution. Keep workload priority and separate analysis budgets. Document and
test an explicit retrospective pause control; the workload runner being busy or paused does not
by itself pause retrospectives. Analysis invocations must be distinguishable from workload/probe
invocations, cannot attach to a workload episode and cannot recursively trigger retrospectives.

The analyst role uses only Opus (Claude Code), GPT Sol (Codex) and Grok 4.7 (Cursor).
The shared vendor-policy module supplies review-style quota pacing, preferences, cooldowns and
capacity checks to both workload reviews and retrospectives; author/contribution constraints
remain review-specific. Retro candidates additionally require fresh authenticated capacity.
A nonblocking per-vendor lease is shared with workload dispatch and quota probes. Selection,
per-vendor rejection reasons and requested/observed models are recorded. Sonnet and Auto are not
eligible and no implicit weaker-model fallback is configured. A failed vendor is deprioritized
on the next activation without resetting the shared retry/daily/call budgets. Each adapter uses
restricted evidence-only execution with the same output validation and telemetry normalization.
The USD CLI cap exists only for Claude; other adapters record the available time/call/size limits.
See the [operating guide](../retrospectives.md) for migration and CLI limitations.

### Invocation and deployment

Ship manual invocation, catch-up invocation and opt-in systemd service/timer templates. Reuse the
existing persistent task index and immutable event outbox as the handoff medium; a separate
retrospective processing journal tracks pending/deferred/success/failed work and report revisions.
The timer wakes the consumer to reconcile durable terminal episodes and evidence, not infer
completion from silence. It must recover missed work after downtime without relying on receipt
of a transient notification. Reuse existing ledger recovery semantics and test a crash between
terminal-event persistence and task-index update; a terminal event must not be permanently missed.

Stage 1 needs no new database or message broker and no synchronous analyst callback on the
execution path. Event-driven wake-up can be added later if latency requires it, retaining periodic
reconciliation as recovery. Document installation, scan interval and settling delay, output
discovery, limits, separate pause behavior and retry procedure. No new monitoring backend or
credential is required for evidence generation.

Post-merge quality observations already available locally may enrich a report, with observation
time and coverage. Their later arrival produces another evidence revision; it does not imply a
clean outcome at initial merge time. Automated revision analysis shares the same budgets.

## Testing decisions and acceptance criteria

Test the public command against a temporary ledger and a fake analyst process. Exercise actual
artifact creation, reruns and failure recovery without live vendor or GitHub calls. Prefer this
single observable seam over mocks of each internal helper. Existing audit, telemetry and timing
fixtures provide prior art for ledger semantics.

- [ ] A merged fixture produces valid versioned JSON and readable Markdown with the same facts.
- [ ] Planned and abnormal handoffs produce reports; active episodes and standalone interrupted
  runs are skipped with an observable reason.
- [ ] An inactive-looking but active episode is never assessed; episode A can be assessed while
  episode B runs, without holding the runner lock or sharing its workspace.
- [ ] Restart reconciliation and a crash between terminal-event and task-index writes do not
  permanently lose work; vendor-capacity contention and retrospective pause defer analysis safely.
- [ ] Identical scans and concurrent workers do not duplicate reports or successful analysis.
- [ ] Two repositories sharing an issue number, and two episodes of one issue, remain distinct.
- [ ] A delayed attempt callback cannot silently produce a stale final report; changed evidence
  produces a traceable revision with the previous report retained.
- [ ] Mixed versions, unknown actual models, missing/partial token categories and timing gaps
  remain explicit; cached/reasoning categories are not double-counted.
- [ ] Fake analyst success produces evidence-linked hypotheses or an explicit no-finding result.
- [ ] Timeout, malformed output, unavailable vendor and missing analyst configuration preserve
  deterministic reports and expose distinct analysis states.
- [ ] Crash recovery, per-revision retries, daily limits and retrospective pause/capacity deferral are exercised;
  no unbounded retry or recursive workload report is possible.
- [ ] Analysis usage is tracked separately from workload totals, including known failed-attempt
  usage and explicit unknown usage for ambiguous attempts.
- [ ] Source ledger records remain unchanged; report output stays local; analyst input contains
  no raw prompt/tool output, credentials or unrestricted logs.
- [ ] Documentation and opt-in service/timer pass configuration/help checks, and existing required
  repository checks pass.

Target-host acceptance then checks telemetry coverage, performs one catch-up run, checks a second
run causes no duplicate analysis and compares reports with owner review as defined in the roadmap.
Host deployment and live analysis need their normal operational authorization; offline tests do not
substitute for this acceptance.

## Out of scope

Automatic optimization issue publication, code changes, workload admission, PR merge, release tags,
deployment and rollback; automatic model-policy changes; unrestricted remote monitoring/raw-log
investigation; proving savings from different individual tasks. These belong to later stages.
