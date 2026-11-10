"""Read-only episode snapshots and factual reports over the existing metadata ledger.

No raw prompts, tool output or runner logs enter this boundary. Token categories are
already normalized by telemetry; audit and timing remain the accounting authorities.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import uuid

from . import audit, timing

VERSION = 1
TERMINAL = {'merged', 'planned_handoff', 'abnormal_handoff'}
TOKENS = ('input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'output_tokens', 'reasoning_tokens')
TASK_FIELDS = ('pipeline_id', 'repo', 'issue', 'pr', 'task_status', 'reason', 'started', 'ended',
               'updated', 'sequence', 'spec_rounds', 'review_rounds', 'workflow_version', 'config_version')
RUN_FIELDS = ('run_id', 'pipeline_id', 'repo', 'agent', 'role', 'stage', 'round', 'status', 'started',
              'ended', 'requested_model', 'actual_model', 'model', 'cli_version', 'workflow_version',
              'config_version', 'usage_state', 'cost_state', 'cost_usd', *TOKENS)
ATTR_FIELDS = ('verdict', 'reason', 'ci_result', 'head_sha', 'run_id', 'agent', 'round', 'must_fix',
               'adopted', 'rejected', 'pr', 'duration_ms', 'workflow_version', 'config_version')
COUNTS = {*TOKENS, 'issue', 'pr', 'sequence', 'spec_rounds', 'review_rounds', 'round',
          'result', 'must_fix', 'adopted', 'rejected', 'duration_ms', 'reverts', 'issues'}
MEASURES = {'started', 'ended', 'updated', 'timestamp', 'checked_at', 'cost_usd'}
CODE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
REPO = re.compile(r'(?!\.{1,2}/)[A-Za-z0-9_.-]+/(?!\.{1,2}$)[A-Za-z0-9_.-]+')


class EvidenceError(ValueError):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


# The post-merge timer refreshes checked_at on every pass, even when nothing it
# observes has changed. Revision identity must ignore it, or each pass looks like
# new evidence and spends another analysis call on the same episode.
VOLATILE_POSTMERGE = ('checked_at',)
# A terminal episode this old with no attempt record at all predates attempt
# telemetry; waiting longer cannot produce the missing evidence.
ATTEMPT_RECORD_GRACE = 86400


def material(postmerge):
    return {k: v for k, v in postmerge.items() if k not in VOLATILE_POSTMERGE}


def source_digest(snapshot):
    return digest(dict(snapshot, postmerge=material(snapshot['postmerge'])))


def projection(row, fields):
    """Metadata only, with finite measurements and bounded identifier strings."""
    result = {}
    for key in fields:
        value = row.get(key)
        if key in COUNTS and type(value) is int and 0 <= value <= 10**12:
            result[key] = value
        elif key in MEASURES and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**15:
            result[key] = value
        elif key == 'final' and type(value) is bool:
            result[key] = value
        elif key not in COUNTS | MEASURES | {'final'} and isinstance(value, str) and (REPO.fullmatch(value) if key == 'repo' else CODE.fullmatch(value)):
            result[key] = value
    return result


def records(state, kind):
    result = []
    for path in sorted((Path(state) / 'telemetry' / kind).glob('*.json')):
        try:
            row = json.loads(path.read_text())
            if not isinstance(row, dict):
                raise ValueError()
        except (OSError, ValueError):
            # Silently skipping a corrupt running record could incorrectly establish idle.
            raise EvidenceError(f'unreadable {kind} metadata') from None
        result.append(row)
    return result


class Ledger:
    def __init__(self, state):
        self.tasks = {}
        self.events = records(state, 'events')
        for row in [*records(state, 'tasks'), *(e.get('task', {}) for e in self.events)]:
            if not isinstance(row, dict) or not row.get('pipeline_id') or not row.get('repo'):
                continue
            task = projection(row, TASK_FIELDS)
            try:
                uuid.UUID(task['pipeline_id'])
                if not REPO.fullmatch(task['repo']):
                    raise ValueError()
            except (KeyError, ValueError):
                raise EvidenceError('invalid task identity') from None
            key = (task['repo'], task['pipeline_id'])
            if key not in self.tasks or task.get('sequence', 0) > self.tasks[key].get('sequence', 0):
                # Replay snapshots in memory; never repair or mutate the source index.
                self.tasks[key] = task
        self.runs = records(state, 'runs')
        self.attempts = records(state, 'attempts')
        self.postmerge = {}
        for path in sorted((Path(state) / 'telemetry' / 'postmerge').glob('*.json')):
            try:
                self.postmerge[path.stem] = projection(json.loads(path.read_text()),
                                                       ('checked_at', 'reverts', 'issues', 'final'))
            except (OSError, ValueError, AttributeError):
                raise EvidenceError('unreadable postmerge metadata') from None

    def snapshot(self, key):
        task = self.tasks[key]
        pipe = key[1]
        runs = [projection(r, RUN_FIELDS) for r in self.runs if r.get('pipeline_id') == pipe
                and r.get('repo', key[0]) == key[0] and r.get('role') not in ('probe', 'retrospective')]
        runs.sort(key=lambda r: (r.get('started', 0), r.get('run_id', '')))
        events = []
        for e in self.events:
            t = e.get('task', {})
            if t.get('pipeline_id') == pipe and t.get('repo', key[0]) == key[0]:
                events.append(dict(**projection(e, ('event_id', 'event_kind', 'timestamp')),
                                   attrs=projection(e.get('attrs') or {}, ATTR_FIELDS)))
        events.sort(key=lambda e: (e.get('timestamp', 0), e.get('event_id', '')))
        attempts = [projection(a, ('pipeline_id', 'started', 'ended', 'result')) for a in self.attempts
                    if a.get('pipeline_id') == pipe]
        attempts.sort(key=lambda a: (a.get('started', 0), a.get('ended', 0)))
        return dict(task=task, runs=runs, events=events, attempts=attempts,
                    postmerge=self.postmerge.get(pipe, {}))


def eligible(snapshot, now, settle_seconds):
    task, runs, attempts = snapshot['task'], snapshot['runs'], snapshot['attempts']
    if task.get('task_status') not in TERMINAL:
        return 'active_episode'
    if 'started' not in task or 'ended' not in task:
        return 'missing_terminal_timing'
    if any(r.get('status') == 'running' for r in runs):
        return 'attached_invocation_running'
    if any('ended' not in r or 'started' not in r for r in runs):
        return 'invocation_completion_unknown'
    latest_run = max((r['ended'] for r in runs), default=task['ended'])
    if not any(a.get('ended', 0) >= max(task['ended'], latest_run) and 'started' in a for a in attempts):
        if not attempts and now - task['ended'] > ATTEMPT_RECORD_GRACE:
            return 'attempt_records_missing'
        return 'attempt_completion_unknown'
    times = [task.get('updated', task['ended']), task['ended'], latest_run,
             *(a.get('ended', 0) for a in attempts), *(e.get('timestamp', 0) for e in snapshot['events']),
             snapshot['postmerge'].get('checked_at', 0)]
    if now - max(times) < settle_seconds:
        return 'evidence_settling'
    return ''


def measurements(runs):
    """Known sums plus per-category coverage; absent categories never become zero."""
    result = {}
    for field in (*TOKENS, 'cost_usd'):
        values = [r[field] for r in runs if field in r]
        complete = bool(runs) and len(values) == len(runs) and all(r.get('usage_state') == 'complete' for r in runs)
        result[field] = dict(value=sum(values) if values else None, reported_runs=len(values),
                             total_runs=len(runs), state='complete' if complete else 'partial' if values else 'unavailable')
    return result


def evidence(snapshot):
    task, runs, events, attempts = (snapshot[k] for k in ('task', 'runs', 'events', 'attempts'))
    verdicts = {e['attrs']['run_id']: e['attrs']['verdict'] for e in events
                if 'run_id' in e['attrs'] and 'verdict' in e['attrs']}
    runs = [dict(r, actual_model=r.get('actual_model', r.get('model', 'unknown')),
                 verdict=verdicts.get(r.get('run_id'), 'unavailable')) for r in runs]
    groups = {}
    for r in runs:
        key = (r.get('stage', 'unknown'), r.get('round', 0), r.get('agent', 'unknown'), r['actual_model'])
        groups.setdefault(key, []).append(r)
    versions = {field: sorted({r[field] for r in [task, *runs, *(e['attrs'] for e in events)] if field in r})
                for field in ('workflow_version', 'config_version', 'cli_version')}
    quality = dict(observed_events=len(events), by_kind={}, ci_results={}, verdicts={})
    for e in events:
        kind = e.get('event_kind', 'unknown')
        quality['by_kind'][kind] = quality['by_kind'].get(kind, 0) + 1
        for source, target in (('ci_result', 'ci_results'), ('verdict', 'verdicts')):
            if source in e['attrs']:
                value = e['attrs'][source]
                quality[target][value] = quality[target].get(value, 0) + 1
    return dict(task=task, runs=runs, versions=versions, usage=measurements(runs),
                vendor_totals=audit.totals(runs),
                groups=[dict(stage=k[0], round=k[1], vendor=k[2], model=k[3], runs=len(v), usage=measurements(v))
                        for k, v in sorted(groups.items())],
                timing=timing.summarize(task, runs, events, attempts), events=events, quality_signals=quality,
                postmerge=snapshot['postmerge'] or {'state': 'unavailable'},
                selection_explanations={'state': 'unavailable', 'reason': 'not_in_structured_ledger'},
                probe_overhead={'scope': 'global', 'audit': 'python -m baton.audit probes',
                                'included_in_episode': False})


def markdown(report):
    data = report['evidence']
    task = data['task']
    def measured(item):
        if item['value'] is None:
            return 'unknown'
        return ('>=' if item['state'] == 'partial' else '') + str(item['value'])
    lines = [f"# Retrospective: {task['repo']} / {task['pipeline_id']}", '',
             f"Outcome: {task['task_status']} ({task.get('reason', 'unavailable')})",
             f"Issue: #{task.get('issue', 0)}; PR: #{task.get('pr', 0)}",
             f"Source digest: {report['source_digest']}", f"Observation cutoff: {report['observed_at']}",
             f"Analysis: {report['analysis_state']} ({report.get('analysis_reason', '')})", '',
             '## Workload usage', '', '| Category | Known total | Reporting runs | Coverage |',
             '| --- | --- | --- | --- |']
    for field, item in sorted(data['usage'].items()):
        lines.append(f"| {field} | {measured(item)} | {item['reported_runs']}/{item['total_runs']} | {item['state']} |")
    lines += ['', 'Partial usage is a lower bound. Reasoning tokens overlap output and must not be added again.',
              'Probe overhead and retrospective usage are separate from workload totals.', '',
              '## Analyst selection', '', '```json',
              json.dumps(report.get('selection'), indent=2, sort_keys=True), '```', '',
              '## Analyst assessment', '']
    assessment = report.get('assessment')
    if assessment:
        lines += [assessment['summary'], '']
        for index, h in enumerate(assessment['hypotheses'], 1):
            lines += [f"### Hypothesis {index} ({h['confidence']})", '', h['proposed_change'], '',
                      'Evidence: ' + ', '.join(h['evidence_refs']), '',
                      'Expected benefit: ' + h['expected_benefit'], '',
                      'Validation: ' + h['validation_plan'], '']
        if not assessment['hypotheses']:
            lines += ['No actionable finding.', '']
    else:
        lines += ['No validated assessment is available. See the analysis state and attempt history.', '']
    lines += ['## Evidence and coverage', '',
              'Missing model-selection explanations and post-merge observations remain unknown.', '',
              '```json', json.dumps(data, indent=2, sort_keys=True), '```', '',
              '## Analysis attempts (separate overhead)', '', '```json',
              json.dumps(report.get('analysis_attempts', []), indent=2, sort_keys=True), '```', '']
    return '\n'.join(lines)
