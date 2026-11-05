#!/usr/bin/env python3
"""Best-effort metadata ledger. No agent text or environment is persisted/exported."""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import hashlib
import re
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from baton import timing

VENDORS = ('claude', 'cursor', 'codex')
CREDENTIAL_STALE_SECONDS = 24 * 60 * 60
ROLES = ('spec', 'spec_review', 'review', 'fix', 'triage', 'scan', 'dev', 'probe')
STATUSES = ('success', 'failure', 'timeout', 'auth', 'quota', 'busy', 'interrupted')


def read(path):
    return json.loads(Path(path).read_text())


def atomic(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def save(path, value):
    atomic(path, json.dumps(value, sort_keys=True) + '\n')


def identity(pid, proc=Path('/proc')):
    # comm can contain spaces and parentheses. starttime is field 22.
    fields = (proc / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        raise ProcessLookupError(pid)
    return {'pid': int(pid), 'start': fields[19],
            'boot': (proc / 'sys/kernel/random/boot_id').read_text().strip()}


def health(record, now=None, identify=identity):
    now = time.time() if now is None else now
    try:
        if identify(record['owner']['pid']) != record['owner']:
            return 'interrupted'
    except (OSError, ValueError, KeyError):
        return 'interrupted'
    if now > record['deadline'] or now - record['heartbeat'] > 180:
        return 'interrupted'
    return 'running'


@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def usage(vendor, log, status):
    """Only completed single-turn/result usage is validated; never guess totals."""
    events = []
    reconnected = False
    try:
        with Path(log).open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                # After a reconnect Cursor's result covers far less than the run did (#596, #470).
                if vendor == 'cursor' and isinstance(row, dict) and (row.get('type'), row.get('subtype')) == ('connection', 'reconnecting'):
                    reconnected = True
                if isinstance(row, dict) and row.get('type') == ('turn.completed' if vendor == 'codex' else 'result'):
                    events.append(row)
    except OSError:
        pass
    result = {'usage_state': 'unavailable', 'cost_state': 'unavailable'}
    if len(events) != 1 or not isinstance(events[0].get('usage'), dict):
        return result
    raw = events[0]['usage']
    fields = {
        'codex': {'input_tokens': 'input_tokens', 'cache_read_tokens': 'cached_input_tokens',
                  'cache_write_tokens': 'cache_write_input_tokens', 'output_tokens': 'output_tokens',
                  'reasoning_tokens': 'reasoning_output_tokens'},
        'cursor': {'input_tokens': 'inputTokens', 'cache_read_tokens': 'cacheReadTokens',
                   'cache_write_tokens': 'cacheWriteTokens', 'output_tokens': 'outputTokens'},
        'claude': {'input_tokens': 'input_tokens', 'cache_read_tokens': 'cache_read_input_tokens',
                   'cache_write_tokens': 'cache_creation_input_tokens', 'output_tokens': 'output_tokens'},
    }[vendor]
    for target, source in fields.items():
        value = raw.get(source)
        if type(value) is int and 0 <= value <= 10**12:
            result[target] = value
    if vendor == 'codex':
        if 'input_tokens' not in result or 'cache_read_tokens' not in result:
            result.pop('input_tokens', None)
        elif result['cache_read_tokens'] <= result['input_tokens']:
            result['input_tokens'] -= result['cache_read_tokens']
        else:
            return {'usage_state': 'unavailable', 'cost_state': 'unavailable'}
    if any(k.endswith('_tokens') for k in result):
        result['usage_state'] = 'complete' if status == 'success' and not reconnected else 'partial'
    # Only Claude reports a cost. It stays in the local record: summary_payload never exports it.
    cost = events[0].get('total_cost_usd')
    if type(cost) in (int, float) and 0 <= cost <= 10**6:
        result.update(cost_usd=cost, cost_state='reported')
    return result


MODEL_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,63}')


def run_model(vendor, log, codex_home=None):
    """The model a run actually used, from its own log; 'unknown' when it isn't stated
    (Cursor's init says only Auto). Never the configured model: fallbacks change it."""
    model = None
    try:
        with Path(log).open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if vendor in ('claude', 'cursor') and row.get('type') == 'system' and row.get('subtype') == 'init':
                    model = row.get('model')
                    if vendor == 'cursor' and str(model).lower() == 'auto':
                        model = None
                    break
                if vendor == 'codex' and row.get('type') == 'thread.started' and row.get('thread_id'):
                    model = codex_model(str(row['thread_id']), codex_home)
                    break
    except OSError:
        pass
    return model if isinstance(model, str) and MODEL_RE.fullmatch(model) else 'unknown'


def codex_model(thread, codex_home=None):
    if not re.fullmatch(r'[A-Za-z0-9-]{1,80}', thread):
        return None
    sessions = Path(codex_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'sessions'
    for rollout in sessions.rglob(f'rollout-*-{thread}.jsonl'):
        with rollout.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get('type') == 'turn_context':
                    return (row.get('payload') or {}).get('model')
    return None


def must_fix_count(text):
    """Numbered items under a review's '## Must fix' heading; None without the heading."""
    count, inside = None, False
    for line in text.splitlines():
        if line.startswith('## '):
            inside = line[3:].strip().lower() == 'must fix'
            if inside:
                count = 0
        elif inside and re.match(r'\d+[.)]\s', line):
            count += 1
    return count


def fix_answers(text):
    """(adopted, rejected) from a fix reply's '## Must fix' section, whose numbered lines
    start with adopted or rejected; None without the section."""
    answers, inside = None, False
    for line in text.splitlines():
        if line.startswith('## '):
            inside = line[3:].strip().lower() == 'must fix'
            if inside:
                answers = [0, 0]
        elif inside:
            found = re.match(r'\d+[.)]\s+\**(adopted|rejected)\b', line, re.IGNORECASE)
            if found:
                answers[found.group(1).lower() == 'rejected'] += 1
    return tuple(answers) if answers else None


POSTMERGE_DAYS = 14


def postmerge_outcome(pr, merged_at, timeline, repo=None):
    """Merged revert PRs and `bug`-labelled issues in REPO that cross-reference PR within
    POSTMERGE_DAYS of its merge, from the PR's GitHub timeline. Most references are
    planned work (the runner's follow-ups, owner-action and side-bug issues, slices split
    off by the owner), so only the bug label marks a defect. Other repositories' issues
    (e.g. workflow issues here citing the PR) don't count."""
    reverts = issues = 0
    until = merged_at + POSTMERGE_DAYS * 86400
    for item in timeline:
        if not isinstance(item, dict) or item.get('event') != 'cross-referenced':
            continue
        source = (item.get('source') or {}).get('issue') or {}
        try:
            created = dt.datetime.fromisoformat(source['created_at'].replace('Z', '+00:00')).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if not merged_at < created <= until or source.get('number') == pr:
            continue
        if repo and (source.get('repository') or {}).get('full_name', repo) != repo:
            continue
        labels = {l.get('name') for l in source.get('labels') or () if isinstance(l, dict)}
        if source.get('pull_request'):
            if (source['pull_request'].get('merged_at') and
                    str(source.get('title', '')).lower().startswith('revert')):
                reverts += 1
        elif 'bug' in labels:
            issues += 1
    return reverts, issues


def postmerge(state, now, timeline_of, pause=1.0):
    """Check each merged episode of the last CONTRIB_DAYS days at most every 6 hours
    until POSTMERGE_DAYS have passed since its merge (a separate timer, never the tick
    or the minute collector). A failed read keeps the previous result."""
    root = Path(state) / 'telemetry'
    for path in sorted((root / 'tasks').glob('*.json')):
        task = read(path)
        if (task['task_status'] != 'merged' or not task.get('pr') or task.get('ended', 0) < now - CONTRIB_DAYS * 86400
                or task['repo'].startswith('telemetry-validation/')):
            continue
        out = root / 'postmerge' / (task['pipeline_id'] + '.json')
        saved = read(out) if out.exists() else {}
        if saved.get('final') or now - saved.get('checked_at', 0) < 6 * 3600:
            continue
        timeline = timeline_of(task['repo'], task['pr'])
        if timeline is None:
            continue
        reverts, issues = postmerge_outcome(task['pr'], task['ended'], timeline, task['repo'])
        save(out, dict(checked_at=now, reverts=reverts, issues=issues,
                       final=now >= task['ended'] + POSTMERGE_DAYS * 86400))
        time.sleep(pause)


def gh_timeline(repo, pr):
    try:
        result = subprocess.run(['gh', 'api', '--paginate', '--slurp', f'repos/{repo}/issues/{int(pr)}/timeline'],
                                capture_output=True, text=True, timeout=60, check=True)
        pages = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return [item for page in pages if isinstance(page, list) for item in page]


# Task events are deliberately bounded metadata, never agent prose.
EVENTS = ('started', 'run_completed', 'review', 'spec_review', 'fix', 'ci', 'verify', 'ci_fix', 'handoff', 'merged', 'attempt_completed')
REASONS = ('none', 'auto_merge_off', 'manual_checks', 'review_failed', 'review_limit',
           'fix_failed', 'needs_info', 'no_commit', 'push_failed', 'spec_failed',
           'spec_limit', 'spec_rejected', 'ci_not_green', 'merge_failed', 'implementation_failed',
           'verify_failed')
CI_RESULTS = ('pass', 'failure', 'no_checks', 'pending', 'timeout', 'cancelled', 'infra_error', 'unknown')


CONFIG_PATH = None


def fingerprints(directory):
    directory = Path(directory)
    def digest(paths):
        h = hashlib.sha256()
        for path in sorted(paths):
            h.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
        return h.hexdigest()
    sources = [directory / name for name in ('run.sh', 'verify.sh', 'telemetry.py')]
    sources += list(directory.glob('*-prompt.md')) + list(directory.glob('*settings.json'))
    sources += list((directory.parent / 'baton').glob('*.py'))   # the Python side (ADR-0005)
    configs = [CONFIG_PATH or directory.parent / 'config.toml']
    configs = [p for p in configs if p.is_file()]
    return {'workflow_version': digest([p for p in sources if p.is_file()]),
            'config_version': digest(configs)}


class Tasks:
    """Local episode index + immutable event outbox, independent of run delivery."""
    def __init__(self, state):
        self.root = Path(state) / 'telemetry'

    def path(self, pipeline):
        return self.root / 'tasks' / (str(uuid.UUID(pipeline)) + '.json')

    def attempt(self, pipeline, attempt_id, started_ms, result):
        task = read(self.path(pipeline))
        now = time.time()
        if not 0 <= started_ms <= int(now * 1000) or result not in (0, 1, 2, 75, 80):
            raise ValueError('invalid attempt metadata')
        path = self.root / 'attempts' / (str(uuid.UUID(attempt_id)) + '.json')
        with locked(path.with_suffix('.lock')):
            if not path.exists():
                save(path, dict(pipeline_id=task['pipeline_id'], started=started_ms / 1000,
                                ended=now, result=result))
        # A terminal snapshot may have been exported before cleanup finished. Emit a
        # new sequence so the completed attempt is eventually visible in that case.
        with locked(self.root / 'tasks.lock'):
            self._event(read(self.path(pipeline)), 'attempt_completed', attempt_id, {})

    def ensure(self, issue=0, pr=0, *, repo):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise ValueError('invalid repository')
        if not issue and not pr:
            return None
        for number in (issue, pr):
            if type(number) is not int or not 0 <= number <= 10**12:
                raise ValueError('invalid task number')
        namespace = hashlib.sha256(repo.encode()).hexdigest()[:16]
        keys = [self.root / 'task-index' / namespace / f'{kind}-{number}'
                for kind, number in (('issue', issue), ('pr', pr)) if number]
        with locked(self.root / 'tasks.lock'):
            candidates = [read(self.path(k.read_text().strip())) for k in keys if k.exists()]
            active = {t['pipeline_id']: t for t in candidates if t['task_status'] == 'active'}
            if len(active) > 1:
                raise ValueError('conflicting active task identities')
            task = next(iter(active.values()), None)
            if task is None:
                task = dict(pipeline_id=str(uuid.uuid4()), repo=repo, issue=issue, pr=pr,
                            task_status='active', reason='none', started=time.time(), sequence=0,
                            **fingerprints(Path(__file__).parent))
            task['issue'] = issue or task['issue']
            task['pr'] = pr or task['pr']
            save(self.path(task['pipeline_id']), task)
            for key in keys:
                atomic(key, task['pipeline_id'])
            if task['sequence'] == 0:
                self._event(task, 'started', 'started', {})
            return read(self.path(task['pipeline_id']))

    def event(self, pipeline, kind, key, **attrs):
        if kind not in EVENTS or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,180}', key):
            raise ValueError('invalid task event')
        validators = {
            'verdict': lambda v: v in ('pass', 'changes', 'accept', 'reject'),
            'reason': lambda v: v in REASONS,
            'ci_result': lambda v: v in CI_RESULTS,
            'head_sha': lambda v: bool(re.fullmatch(r'[a-f0-9]{40,64}', v)),
            'run_id': lambda v: str(uuid.UUID(v)) == v,
            'agent': lambda v: v in VENDORS,
            'round': lambda v: type(v) is int and 0 <= v <= 10000,
            'must_fix': lambda v: type(v) is int and 0 <= v <= 1000,
            'adopted': lambda v: type(v) is int and 0 <= v <= 1000,
            'rejected': lambda v: type(v) is int and 0 <= v <= 1000,
            'pr': lambda v: type(v) is int and 0 <= v <= 10**12,
            'duration_ms': lambda v: type(v) is int and 0 <= v <= 86400000,
            'workflow_version': lambda v: bool(re.fullmatch(r'[a-f0-9]{64}', v)),
            'config_version': lambda v: bool(re.fullmatch(r'[a-f0-9]{64}', v)),
        }
        if any(k not in validators or not validators[k](v) for k, v in attrs.items()):
            raise ValueError('invalid event metadata')
        with locked(self.root / 'tasks.lock'):
            task = read(self.path(pipeline))
            # A late callback cannot rewrite a terminal episode.
            if task['task_status'] != 'active':
                return
            self._event(task, kind, key, attrs)

    def _event(self, task, kind, key, attrs):
        event_id = str(uuid.uuid5(uuid.UUID(task['pipeline_id']), kind + ':' + key))
        path = self.root / 'events' / (event_id + '.json')
        if path.exists():
            saved = read(path)['task']
            if saved['sequence'] > task['sequence']:
                save(self.path(task['pipeline_id']), saved)
            return
        now = time.time()
        if kind == 'handoff':
            task['reason'] = attrs['reason']
            task['task_status'] = ('planned_handoff' if attrs['reason'] in
                                   ('auto_merge_off', 'manual_checks') else 'abnormal_handoff')
        elif kind == 'merged':
            task['task_status'] = 'merged'
        if task['task_status'] != 'active' and kind != 'attempt_completed':
            task['ended'] = now
        if attrs.get('pr'):
            task['pr'] = attrs['pr']
        if kind in ('review', 'spec_review'):
            counter = 'review_rounds' if kind == 'review' else 'spec_rounds'
            task[counter] = task.get(counter, 0) + 1
        task['sequence'] += 1
        task.update({k: v for k, v in attrs.items() if k in ('workflow_version', 'config_version')})
        task['updated'] = now
        # Recoverable immutable outbox. Replayed producer calls retain stable IDs.
        save(path, dict(event_id=event_id, event_kind=kind, timestamp=now,
                        task=task, attrs=attrs))
        save(self.path(task['pipeline_id']), task)

    def payloads(self, record, history=None):
        task, attrs = record['task'], record['attrs']
        fields = {k: task[k] for k in ('pipeline_id', 'repo', 'issue', 'pr', 'task_status', 'reason', 'sequence')}
        fields.update(attrs)
        fields.update(event_id=record['event_id'] + ':event', event_kind=record['event_kind'])
        fields.update({k: task[k] for k in ('workflow_version', 'config_version') if k in task})
        event = metadata_log('baton.task_event', record['timestamp'], fields)
        # Task rows are snapshots. Keep the latest sequence per episode, never sum snapshots.
        snapshot = dict(fields, event_id=record['event_id'] + ':task_summary',
                        duration_ms=int(max(0, task.get('ended', record['timestamp']) - task['started']) * 1000))
        if history is None:
            history = {kind: [read(p) for p in (self.root / kind).glob('*.json')]
                       for kind in ('runs', 'events', 'attempts')}
        runs = history['runs']
        runs = [r for r in runs if r.get('pipeline_id') == task['pipeline_id'] and
                r.get('ended', float('inf')) <= record['timestamp']]
        snapshot['review_rounds'] = task.get('review_rounds', 0)
        snapshot['spec_rounds'] = task.get('spec_rounds', 0)
        snapshot['runs'] = len(runs)
        snapshot['incomplete_runs'] = sum(r.get('usage_state') != 'complete' for r in runs)
        events = history['events']
        events = [e for e in events if e['task']['pipeline_id'] == task['pipeline_id']]
        attempts = history['attempts']
        attempts = [a for a in attempts if a['pipeline_id'] == task['pipeline_id']]
        snapshot.update(timing.summarize(task, runs, events, attempts, record['timestamp']))
        # Only expose a category when every run has a complete value for it.
        for field in ('input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'output_tokens'):
            if runs and all(r.get('usage_state') == 'complete' and field in r for r in runs):
                snapshot[field] = sum(r[field] for r in runs)
        return event, metadata_log('baton.task_summary', record['timestamp'], snapshot)

    def flush(self):
        deadline = time.monotonic() + 12
        paths = sorted((self.root / 'events').glob('*.json'))
        pending = [p for p in paths if not p.with_suffix('.sent').exists()]
        if not pending:
            return
        # Read the shared history once per batch, not once per event snapshot.
        history = {kind: [read(p) for p in (self.root / kind).glob('*.json')]
                   for kind in ('runs', 'attempts')}
        history['events'] = [read(p) for p in paths]
        for path in pending:
            if time.monotonic() > deadline:
                break
            with locked(path.with_suffix('.delivery-lock')):
                ack = path.with_suffix('.sent')
                if ack.exists():
                    continue
                for payload in self.payloads(read(path), history):
                    request = urllib.request.Request('http://127.0.0.1:4319/v1/logs',
                        json.dumps(payload).encode(), {'Content-Type': 'application/json'})
                    with urllib.request.urlopen(request, timeout=2) as response:
                        reply = json.load(response)
                        if response.status != 200 or reply.get('partialSuccess', {}).get('rejectedLogRecords', 0):
                            raise OSError('task event export unsuccessful')
                atomic(ack, 'accepted\n')


def metadata_log(body, timestamp, fields):
    values = [{'key': k, 'value': {'intValue': str(v)} if type(v) is int else {'stringValue': v}}
              for k, v in fields.items()]
    return {'resourceLogs': [{'resource': {'attributes': []}, 'scopeLogs': [{
        'scope': {'name': 'baton-runner'}, 'logRecords': [{
            'timeUnixNano': str(int(timestamp * 10**9)),
            'body': {'stringValue': body}, 'attributes': values}]}]}]}


def ci_result(checks, rc):
    """Check buckets are GitHub facts; no checks and infrastructure errors aren't passes."""
    if rc in (124, 137):
        return 'timeout'
    if rc not in (0, 1, 8) or not isinstance(checks, list):
        return 'infra_error'
    if not checks:
        return 'no_checks' if rc in (0, 1) else 'infra_error'
    buckets = {c.get('bucket') for c in checks if isinstance(c, dict)}
    if not buckets or buckets - {'pass', 'fail', 'pending', 'skipping', 'cancel'}:
        return 'unknown'
    if 'fail' in buckets:
        return 'failure'
    if 'pending' in buckets:
        return 'pending'
    if 'cancel' in buckets:
        return 'cancelled'
    return ('pass' if 'pass' in buckets else 'no_checks') if rc == 0 else 'unknown'


class Ledger:
    def __init__(self, state):
        self.state = Path(state)
        self.root = self.state / 'telemetry'

    def path(self, run_id):
        # Never accept a filename from CLI or agent output.
        return self.root / 'runs' / (str(uuid.UUID(run_id)) + '.json')

    def begin(self, vendor, role, pid, minutes, issue=0, pr=0, round_=0, *, repo, requested_model=''):
        if vendor not in VENDORS or role not in ROLES or not 1 <= minutes <= 1440:
            raise ValueError('invalid bounded run metadata')
        now = time.time()
        run_id = str(uuid.uuid4())
        stage = ('spec-review' if vendor == 'cursor' else 'spec-reply') if role == 'spec' else ('implement' if role == 'dev' else role)
        record = dict(run_id=run_id, agent=vendor, role=role, stage=stage, repo=repo, status='running',
                      trace_id=uuid.uuid4().hex, span_id=uuid.uuid4().hex[:16],
                      started=now, heartbeat=now, deadline=now + minutes * 60 + 60,
                      owner=identity(pid), issue=max(0, issue), pr=max(0, pr), round=max(0, round_))
        if requested_model and MODEL_RE.fullmatch(requested_model):
            record['requested_model'] = requested_model
        task = Tasks(self.state).ensure(issue, pr, repo=repo)
        if task:
            record['pipeline_id'] = task['pipeline_id']
        record.update(fingerprints(Path(__file__).parent))
        try:
            executable = 'cursor-agent' if vendor == 'cursor' else vendor
            output = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=3, check=True).stdout
            version = re.search(r'(?<![A-Za-z0-9])[0-9][0-9A-Za-z.+-]{0,63}', output)
            if version:
                record['cli_version'] = version.group()
        except (OSError, subprocess.SubprocessError):
            pass
        save(self.path(run_id), record)
        return record

    def end(self, run_id, status, log=None):
        path = self.path(run_id)
        with locked(path.with_suffix('.lock')):
            record = read(path)
            if record['status'] != 'running':
                return record
            record.update(status=status if status in STATUSES else 'failure', ended=time.time())
            record.update(usage(record['agent'], log, record['status']) if log else
                          {'usage_state': 'unavailable', 'cost_state': 'unavailable'})
            record['model'] = run_model(record['agent'], log) if log else 'unknown'
            record['actual_model'] = record['model']

            save(path, record)
            if record.get('pipeline_id'):
                Tasks(self.state).event(record['pipeline_id'], 'run_completed', record['run_id'],
                    run_id=record['run_id'], agent=record['agent'],
                    workflow_version=record['workflow_version'], config_version=record['config_version'])
            return record

    def reconcile(self):
        records = []
        for path in sorted((self.root / 'runs').glob('*.json')):
            record = read(path)
            if record['status'] == 'running' and health(record) != 'running':
                record = self.end(record['run_id'], 'interrupted')
            if record['status'] != 'running' and record.get('pipeline_id'):
                # Retry a callback interrupted after committing the terminal run.
                Tasks(self.state).event(record['pipeline_id'], 'run_completed', record['run_id'],
                    run_id=record['run_id'], agent=record['agent'],
                    workflow_version=record['workflow_version'], config_version=record['config_version'])
            records.append(record)
        return records

    def beat(self, run_id):
        path = self.path(run_id)
        with locked(path.with_suffix('.lock')):
            record = read(path)
            if record['status'] != 'running' or health(record) != 'running':
                return False
            record['heartbeat'] = time.time()
            save(path, record)
            return True

    def flush(self, records):
        # At-least-once delivery, stable event IDs and original timestamps. Queries
        # must deduplicate event_id: crash after HTTP 200 before ACK can replay.
        deadline = time.monotonic() + 12
        for record in records:
            if time.monotonic() >= deadline:
                break
            if record['status'] == 'running':
                continue
            ack = self.path(record['run_id']).with_suffix('.sent')
            with locked(ack.with_suffix('.delivery-lock')):
                if ack.exists():
                    continue
                payload = summary_payload(record)
                request = urllib.request.Request('http://127.0.0.1:4319/v1/logs',
                    json.dumps(payload).encode(), {'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=2) as response:
                    if response.status != 200:
                        raise OSError('OTLP export unsuccessful')
                    reply = json.load(response)
                    if reply.get('partialSuccess', {}).get('rejectedLogRecords', 0):
                        raise OSError('OTLP record rejected')
                trace = urllib.request.Request('http://127.0.0.1:4319/v1/traces',
                    json.dumps(run_span_payload(record)).encode(), {'Content-Type': 'application/json'})
                with urllib.request.urlopen(trace, timeout=2) as response:
                    reply = json.load(response)
                    if response.status != 200 or reply.get('partialSuccess', {}).get('rejectedSpans', 0):
                        raise OSError('OTLP span rejected')
                atomic(ack, 'accepted\n')


def summary_payload(record):
    # Explicit projection: PID, raw logs, errors and untrusted strings never leave.
    attrs = {'event_id': record['run_id'] + ':summary', 'baton.run_id': record['run_id'],
             'agent': record['agent'], 'baton.role': record['role'], 'baton.stage': record.get('stage', record['role']),
             'status': record['status'],
             'usage_state': record['usage_state'], 'cost_state': 'unavailable',
             'duration_ms': int(max(0, record['ended'] - record['started']) * 1000)}
    for key in ('pipeline_id', 'workflow_version', 'config_version', 'cli_version', 'requested_model', 'actual_model'):
        if key in record:
            attrs[key] = record[key]
    for key in ('issue', 'pr', 'round'):
        if record[key] > 0:
            attrs['baton.' + key] = record[key]
    for key in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens'):
        if key in record:
            attrs[key] = record[key]
    values = [{'key': k, 'value': {'intValue': str(v)} if type(v) is int else {'stringValue': v}}
              for k, v in attrs.items()]
    return {'resourceLogs': [{'resource': {'attributes': [
        {'key': 'service.name', 'value': {'stringValue': 'baton-runner'}}]},
        'scopeLogs': [{'scope': {'name': 'baton-runner'}, 'logRecords': [{
            'timeUnixNano': str(int(record['ended'] * 10**9)),
            'traceId': record['trace_id'], 'spanId': record['span_id'],
            'body': {'stringValue': 'baton.run_summary'}, 'attributes': values}]}]}]}


def run_span_payload(record):
    log = summary_payload(record)['resourceLogs'][0]
    return {'resourceSpans': [{'resource': log['resource'], 'scopeSpans': [{
        'scope': {'name': 'baton-runner'}, 'spans': [{
            'traceId': record['trace_id'], 'spanId': record['span_id'],
            'name': 'baton.run', 'kind': 1,
            'startTimeUnixNano': str(int(record['started'] * 10**9)),
            'endTimeUnixNano': str(int(record['ended'] * 10**9)),
            'attributes': log['scopeLogs'][0]['logRecords'][0]['attributes'],
            'status': {'code': 1 if record['status'] == 'success' else 2}}]}]}]}


def unit_state():
    output = subprocess.run(['systemctl', '--user', 'show', 'baton.timer',
        '-p', 'LoadState', '-p', 'UnitFileState', '-p', 'ActiveState', '-p', 'ActiveEnterTimestampMonotonic'],
        capture_output=True, text=True, timeout=5, check=True).stdout
    values = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if values.get('LoadState') != 'loaded' or not values.get('UnitFileState') or not values.get('ActiveState'):
        raise ValueError('unknown timer state')
    if values['UnitFileState'] not in ('enabled', 'enabled-runtime', 'disabled', 'static', 'indirect', 'alias', 'generated', 'transient', 'linked', 'linked-runtime', 'masked', 'masked-runtime') or values['ActiveState'] not in ('active', 'inactive', 'failed', 'activating', 'deactivating', 'reloading', 'refreshing', 'maintenance'):
        raise ValueError('unrecognized timer state')
    entered = int(values.get('ActiveEnterTimestampMonotonic', '0')) / 10**6
    return (int(values['UnitFileState'] in ('enabled', 'enabled-runtime')),
            int(values['ActiveState'] == 'active'), time.time() - time.monotonic() + entered if entered else 0)


def _runner_config(config):
    # bin/ and baton/ are siblings; collection never invokes bootstrap.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from baton.config import load
    return load(config)


def settings(config):
    return [int(_runner_config(config).scheduling.paused), None, None, None]


def quota_settings(config):
    quota = _runner_config(config).quota
    return [quota.quota_cap_claude, quota.quota_cap_codex, quota.quota_cap_cursor,
            quota.quota_five_hour_max, quota.quota_stale_minutes]


def quota_metrics(state, config, emit, now):
    """Account quota, never inferred from token counts or successful probes.

    Window state: 0 unknown, 1 fresh, 2 stale, 3 reset pending, 4 unsupported,
    5 invalid. Gate: 0 unknown, 1 within quota, 2 5h limit, 3 weekly limit,
    4 cooldown. This is a quota gate, not a prediction of role/PR selection.
    """
    try:
        claude_cap, codex_cap, cursor_cap, five_cap, stale_minutes = quota_settings(config)
        caps = {'claude': claude_cap, 'codex': codex_cap, 'cursor': cursor_cap}
    except (OSError, ValueError, subprocess.SubprocessError):
        caps, five_cap, stale_minutes = {}, math.nan, math.nan
    for vendor in VENDORS:
        labels = '{agent="' + vendor + '"}'
        gate, blocked, states = 0, 0, []
        try:
            snap = read(state / ('quota-' + vendor + '.json'))
            if not isinstance(snap, dict) or not isinstance(snap.get('windows', {}), dict):
                raise ValueError('invalid quota snapshot')
            malformed = False
        except FileNotFoundError:
            snap, malformed = {}, False
        except (OSError, ValueError):
            snap, malformed = {}, True
        for window in ('five_hour', 'seven_day'):
            wl = '{agent="' + vendor + '",window="' + window + '"}'
            raw = snap.get('windows', {}).get(window)
            # Cursor reports only a weekly window, and only once its usage API answered.
            status = 4 if vendor == 'cursor' and raw is None else (5 if malformed else 0)
            used = reset = observed = math.nan
            cap = five_cap if window == 'five_hour' else caps.get(vendor, math.nan)
            if raw is not None:
                try:
                    values = (raw['used'], raw['resets_at'], raw.get('observed_at', 0))
                    if any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) for v in values):
                        raise ValueError('invalid quota window')
                    used, reset, observed = values
                    if not 0 <= used <= 1 or reset <= 0 or observed < 0 or observed > now:
                        raise ValueError('invalid quota window')
                    # Legacy snapshot timestamps cannot establish individual
                    # window freshness after partial merges. Show them as unknown.
                    status = (3 if reset <= now else 0 if not observed or not math.isfinite(stale_minutes) else
                              2 if now - observed > stale_minutes * 60 else 1)
                except (TypeError, KeyError, ValueError):
                    used = reset = observed = math.nan
                    status = 5
            states.append(status)
            if status == 1 and math.isfinite(cap) and used >= cap:
                blocked = blocked or (2 if window == 'five_hour' else 3)
            emit('quota_window_state', status, wl)
            emit('quota_used_ratio', used if status in (1, 2) else math.nan, wl)
            emit('quota_remaining_ratio', 1 - used if status in (1, 2) else math.nan, wl)
            emit('quota_runner_cap_ratio', cap, wl)
            emit('quota_runner_headroom_ratio', max(0, cap - used) if status in (1, 2) else math.nan, wl)
            emit('quota_resets_at_seconds', reset, wl)
            emit('quota_observed_at_seconds', observed if observed > 0 else math.nan, wl)
        if vendor in caps and math.isfinite(stale_minutes):
            known = [s for s in states if s != 4]
            gate = blocked or (1 if known and set(known) == {1} else 0)
        try:
            if count(state / (vendor + '-cooldown-until')) > now:
                gate = 4
        except (OSError, ValueError):
            gate = 0
        emit('quota_gate', gate, labels)


def credential_metrics(state, emit, now):
    """Expose the latest provider auth check; successful checks age to Stale."""
    states = {'authenticated': 1, 'auth_expired': 2, 'probe_failed': 3}
    for vendor in VENDORS:
        value, observed = 0, math.nan
        try:
            record = read(state / ('credential-' + vendor + '.json'))
            result = record['result']
            observed = record['observed_at']
            if (result not in states or isinstance(observed, bool)
                    or not isinstance(observed, (int, float)) or not math.isfinite(observed)
                    or observed <= 0 or observed > now):
                raise ValueError('invalid credential status')
            value = states[result]
            if result != 'auth_expired' and now - observed > CREDENTIAL_STALE_SECONDS:
                value = 4
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            value, observed = 5, math.nan
        labels = '{agent="' + vendor + '"}'
        emit('credential_state', value, labels)
        emit('credential_observed_at_seconds', observed, labels)


def count(path):
    try:
        value = path.read_text().strip()
    except FileNotFoundError:
        return 0
    if not value.isdecimal():
        raise ValueError('invalid numeric state')
    return int(value)


def retrospective_running(state):
    """Sample the independent worker without creating state or waiting for it.

    The worker holds this lock across both catch-up checks and task analysis.
    A persistent lock file alone does not imply activity; flock is authoritative.
    """
    try:
        stream = (Path(state) / 'retrospectives' / 'worker.lock').open('r')
    except FileNotFoundError:
        return 0
    with stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 1
        fcntl.flock(stream, fcntl.LOCK_UN)
    return 0


def interval_activity(records, previous, now, emit):
    """Any invocation overlap, including runs completed between observations.

    A long/failed observation gap cannot establish idle for the entire interval.
    Do not silently extend a sampling bucket across an outage.
    """
    known = records is not None and math.isfinite(previous) and 0 < now - previous <= 150
    for vendor in VENDORS:
        active = any(r['agent'] == vendor and r['started'] < now and
                     (r['status'] == 'running' or r['ended'] > previous)
                     for r in (records or [])) if known else False
        emit('activity_in_interval', int(active) if known else math.nan,
             '{agent="' + vendor + '"}')
    emit('activity_interval_start_seconds', previous if known else math.nan)


def activity_metrics(records, emit, now, state=None):
    """Stable per-vendor state plus metadata only for reconciled live invocations."""
    running = [r for r in (records or []) if r['status'] == 'running']
    for vendor in VENDORS:
        emit('running_by_agent', sum(r['agent'] == vendor for r in running)
             if records is not None else math.nan, '{agent="' + vendor + '"}')
    for record in running:
        # JSON quoting also escapes quotes, backslashes and newlines in Prom labels.
        labels = {key: str(record.get(key, '')) for key in
                  ('agent', 'role', 'stage', 'repo', 'issue', 'pr', 'run_id')}
        if not labels['repo'] and state is not None and record.get('pipeline_id'):
            with contextlib.suppress(OSError, ValueError, KeyError):
                labels['repo'] = read(Tasks(state).path(record['pipeline_id']))['repo']
        labels = '{' + ','.join(key + '=' + json.dumps(value, ensure_ascii=False)
                                for key, value in labels.items()) + '}'
        emit('current_execution_started_seconds', record['started'], labels)
    emit('activity_observed_at_seconds', now if records is not None else math.nan)
    previous = math.nan
    if state is not None:
        path = Path(state) / 'telemetry' / 'activity-last-observed'
        # During upgrade, the existing successful collection timestamp is a
        # valid interval boundary. Subsequent failures must not advance it.
        source = path if path.exists() else path.with_name('last-success')
        with contextlib.suppress(OSError, ValueError):
            previous = float(source.read_text())
    interval_activity(records, previous, now, emit)
    if state is not None and records is not None:
        atomic(path, str(now))



CONTRIB_DAYS = 30
TOKEN_FIELDS = ('input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'output_tokens', 'reasoning_tokens')


def label(**values):
    return '{' + ','.join(f'{k}="{v}"' for k, v in values.items()) + '}'


def author_of(task, runs):
    """(vendor, model) of the run that implemented TASK: its first successful dev run,
    else its first dev run; ('owner', 'unknown') for a PR without one; None when the
    episode never reached implementation (spec rounds, including their docs PR)."""
    devs = sorted((r for r in runs if r.get('role') == 'dev'), key=lambda r: r['started'])
    dev = next((r for r in devs if r.get('status') == 'success'), devs[0] if devs else None)
    if dev:
        return dev['agent'], dev.get('model', 'unknown')
    if any(r.get('role') in ('spec', 'spec_review') for r in runs) or not task.get('pr'):
        return None
    return 'owner', 'unknown'


def contribution_metrics(state, emit, now):
    """Per-vendor contribution over the last CONTRIB_DAYS days (#227). Collect and
    display only: nothing in scheduling reads these. Every counter has its own n."""
    root = Path(state) / 'telemetry'
    since = now - CONTRIB_DAYS * 86400
    runs = [read(p) for p in (root / 'runs').glob('*.json')]
    by_task = {}
    for run in runs:
        by_task.setdefault(run.get('pipeline_id'), []).append(run)
    reviews, fixes = {}, {}
    for path in (root / 'events').glob('*.json'):
        record = read(path)
        if record['event_kind'] in ('review', 'fix'):
            (reviews if record['event_kind'] == 'review' else fixes).setdefault(
                record['task']['pipeline_id'], []).append(record)
    counts = {}

    def add(name, labels, value=1):
        key = (name, label(**labels))
        counts[key] = counts.get(key, 0) + value

    for path in (root / 'tasks').glob('*.json'):
        task = read(path)
        if (task['task_status'] == 'active' or task.get('ended', 0) < since
                or task['repo'].startswith('telemetry-validation/')):
            continue
        task_runs = by_task.get(task['pipeline_id'], [])
        author = author_of(task, task_runs)
        if author is None:
            continue
        vendor, model = author
        add('contrib_tasks', dict(author=vendor, author_model=model, outcome=task['task_status']))
        if task['task_status'] != 'merged':
            add('contrib_handoffs', dict(author=vendor, author_model=model, reason=task['reason']))
        else:
            add('contrib_review_rounds', dict(author=vendor, author_model=model), task.get('review_rounds', 0))
            outcome = root / 'postmerge' / (task['pipeline_id'] + '.json')
            if outcome.exists():
                outcome = read(outcome)
                checked = 'final' if outcome['final'] else 'pending'
                add('contrib_postmerge_checked', dict(author=vendor, author_model=model, state=checked))
                add('contrib_postmerge_reverted', dict(author=vendor, author_model=model, state=checked),
                    int(outcome['reverts'] > 0))
                add('contrib_postmerge_with_issues', dict(author=vendor, author_model=model, state=checked),
                    int(outcome['issues'] > 0))
            add('contrib_duration_seconds', dict(author=vendor, author_model=model), int(task['ended'] - task['started']))
            for run in task_runs:
                state_ = 'complete' if run.get('usage_state') == 'complete' else 'incomplete'
                add('contrib_merged_runs', dict(author=vendor, author_model=model, usage_state=state_))
                if state_ == 'complete':
                    for field in TOKEN_FIELDS:
                        if field in run:
                            add('contrib_merged_tokens', dict(author=vendor, author_model=model, agent=run['agent'],
                                model=run.get('model', 'unknown'), type=field[:-7]), run[field])
        first = sorted(reviews.get(task['pipeline_id'], []), key=lambda r: r['timestamp'])[:1]
        if first:
            add('contrib_first_review', dict(author=vendor, author_model=model, verdict=first[0]['attrs']['verdict']))
        for review in reviews.get(task['pipeline_id'], []):
            attrs = review['attrs']
            add('contrib_reviews', dict(reviewer=attrs.get('agent', 'unknown'), verdict=attrs['verdict']))
            if 'must_fix' in attrs:
                add('contrib_must_fix_reviews', dict(reviewer=attrs['agent']))
                add('contrib_must_fix', dict(reviewer=attrs['agent']), attrs['must_fix'])
        # A fix answers the review of the same round; its answers count for that reviewer.
        by_round = {r['attrs'].get('round'): r['attrs'].get('agent', 'unknown') for r in reviews.get(task['pipeline_id'], [])}
        for fix in fixes.get(task['pipeline_id'], []):
            reviewer = by_round.get(fix['attrs'].get('round'))
            if reviewer is None:
                continue
            add('contrib_must_fix_answered_rounds', dict(reviewer=reviewer))
            for answer in ('adopted', 'rejected'):
                add('contrib_must_fix_answers', dict(reviewer=reviewer, answer=answer), fix['attrs'][answer])
    # Model dimension (#245): every run that ended in the window, task or not.
    for run in runs:
        if run.get('status') == 'running' or run.get('ended', 0) < since or run.get('repo', '').startswith('telemetry-validation/'):
            continue
        labels = dict(agent=run['agent'], role=run['role'], model=run.get('model', 'unknown'))
        add('model_runs', dict(labels, status=run['status']))
        if run.get('usage_state') == 'complete':
            add('model_complete_runs', labels)
            for field in TOKEN_FIELDS:
                if field in run:
                    add('model_tokens', dict(labels, type=field[:-7]), run[field])
    for (name, labels), value in sorted(counts.items()):
        emit(name, value, labels)


def collect(ledger, config, output):
    now = time.time()
    metrics = []
    ok = True

    def emit(name, value, labels=''):
        if isinstance(value, float) and math.isnan(value):
            value = 'NaN'
        metrics.append(f'baton_{name}{labels} {value}')

    def attempt(fn, fallback):
        nonlocal ok
        try:
            return fn()
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            ok = False
            return fallback

    enabled, active, entered = attempt(unit_state, (math.nan, math.nan, math.nan))
    emit('timer_enabled', enabled)
    emit('timer_active', active)
    emit('timer_active_since_seconds', entered)
    cfg = attempt(lambda: settings(config), [None] * 4)
    emit('paused', cfg[0] if cfg[0] is not None else 'NaN')
    date = dt.datetime.now().strftime('%Y-%m-%d')  # the host's local timezone, every collect
    for vendor, cap in zip(VENDORS, cfg[1:]):
        labels = '{agent="' + vendor + '"}'
        emit('cap_configured', int(cap is not None) if cfg[0] is not None else 'NaN', labels)
        emit('daily_cap', cap if cap is not None else 'NaN', labels)
        prefix = '' if vendor == 'claude' else vendor + '-'
        emit('runs_today', attempt(lambda: count(ledger.state / f'{prefix}runs-{date}'), math.nan), labels)
        emit('cooldown_until_seconds', attempt(lambda: count(ledger.state / (vendor + '-cooldown-until')), math.nan), labels)

    quota_metrics(ledger.state, config, emit, now)
    credential_metrics(ledger.state, emit, now)
    attempt(lambda: contribution_metrics(ledger.state, emit, now), None)

    def lock_held():
        with (ledger.state / 'lock').open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return 0
            except BlockingIOError:
                return 1
    held = attempt(lock_held, math.nan)
    emit('lock_held', held)
    tick_path = ledger.root / 'last-scheduled-tick.json'
    tick = attempt(lambda: read(tick_path)['started'], math.nan) if tick_path.exists() else math.nan
    # A never-created timestamp is unknown; collection itself can still succeed.
    # Timer active_since is the baseline for the future no-tick alert.
    emit('last_scheduled_tick_seconds', tick)
    retrospective = attempt(lambda: retrospective_running(ledger.state), math.nan)
    emit('retrospective_running', retrospective)
    emit('retrospective_observed_at_seconds', now if math.isfinite(retrospective) else math.nan)
    records = attempt(ledger.reconcile, None)
    activity_metrics(records, emit, now, ledger.state)
    running = [r for r in (records or []) if r['status'] == 'running']
    emit('running', len(running) if records is not None else math.nan)
    emit('execution_healthy', int(held == 1 and any(health(r) == 'running' for r in running)) if records is not None and not math.isnan(held) else math.nan)
    emit('execution_deadline_seconds', max((r['deadline'] for r in running), default=0))
    emit('execution_heartbeat_seconds', max((r['heartbeat'] for r in running), default=0))
    # Export failure is separate from state-read success; retry on next minute.
    try:
        if records is None:
            raise OSError('unknown ledger state')
        ledger.flush(records)
        Tasks(ledger.state).flush()
        delivered = 1
    except (OSError, ValueError):
        delivered = 0
    emit('summary_export_success', delivered)
    emit('state_collect_success', int(ok))
    if ok:
        atomic(ledger.root / 'last-success', str(now))
    try:
        last = float((ledger.root / 'last-success').read_text())
    except (OSError, ValueError):
        last = math.nan
    emit('state_last_success_seconds', last)
    atomic(output, '\n'.join(metrics) + '\n', mode=0o644)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runner-config', type=Path)
    parser.add_argument('--state', type=Path, default=Path.home() / '.local/state/baton')
    sub = parser.add_subparsers(dest='action', required=True)
    begin = sub.add_parser('begin')
    begin.add_argument('vendor', choices=VENDORS)
    begin.add_argument('role', choices=ROLES)
    for name in ('pid', 'minutes', 'issue', 'pr', 'round'):
        begin.add_argument(name, type=int)
    begin.add_argument('--repo', required=True)
    begin.add_argument('--requested-model', default='')
    attempt = sub.add_parser('attempt')
    attempt.add_argument('pipeline')
    attempt.add_argument('attempt_id')
    attempt.add_argument('started_ms', type=int)
    attempt.add_argument('result', type=int)
    end = sub.add_parser('end')
    end.add_argument('run_id')
    end.add_argument('status', choices=STATUSES)
    end.add_argument('log')
    beat = sub.add_parser('heartbeat')
    beat.add_argument('run_id')
    sub.add_parser('tick')
    task = sub.add_parser('task')
    task.add_argument('kind', choices=EVENTS)
    task.add_argument('--repo', required=True)
    task.add_argument('--issue', type=int, default=0)
    task.add_argument('--pr', type=int, default=0)
    task.add_argument('--pipeline')
    task.add_argument('--key', required=True)
    for field in ('verdict', 'reason', 'head-sha', 'run-id', 'agent', 'ci-result'):
        task.add_argument('--' + field)
    task.add_argument('--round', type=int)
    task.add_argument('--must-fix', type=int)
    task.add_argument('--adopted', type=int)
    task.add_argument('--rejected', type=int)
    task.add_argument('--duration-ms', type=int)
    sub.add_parser('must-fix')
    sub.add_parser('fix-answers')
    sub.add_parser('postmerge')
    ci = sub.add_parser('ci-result')
    ci.add_argument('rc', type=int)

    collector = sub.add_parser('collect')
    collector.add_argument('--config', type=Path, required=True)
    collector.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    global CONFIG_PATH
    CONFIG_PATH = args.runner_config
    ledger = Ledger(args.state)
    if args.action == 'begin':
        record = ledger.begin(args.vendor, args.role, args.pid, args.minutes, args.issue, args.pr, args.round,
                              repo=args.repo, requested_model=args.requested_model)
        print(record['run_id'], record['trace_id'], record['span_id'], record.get('pipeline_id', ''))
        subprocess.Popen([sys.executable, __file__, '--state', str(args.state), 'heartbeat', record['run_id']],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    elif args.action == 'attempt':
        Tasks(args.state).attempt(args.pipeline, args.attempt_id, args.started_ms, args.result)
    elif args.action == 'ci-result':
        try:
            checks = json.load(sys.stdin)
        except ValueError:
            checks = None
        print(ci_result(checks, args.rc))
    elif args.action == 'fix-answers':
        answers = fix_answers(sys.stdin.read())
        if answers is not None:
            print(*answers)
    elif args.action == 'postmerge':
        postmerge(args.state, time.time(), gh_timeline)
    elif args.action == 'must-fix':
        count = must_fix_count(sys.stdin.read())
        if count is not None:
            print(count)
    elif args.action == 'task':
        store = Tasks(args.state)
        pipeline = args.pipeline
        if not pipeline:
            record = store.ensure(args.issue, args.pr, repo=args.repo)
            if record is None:
                return
            pipeline = record['pipeline_id']
        attrs = {k: getattr(args, k) for k in ('verdict', 'reason', 'head_sha', 'run_id', 'agent', 'ci_result', 'round', 'must_fix', 'adopted', 'rejected', 'duration_ms')
                 if getattr(args, k) is not None}
        if args.pr:
            attrs['pr'] = args.pr
        store.event(pipeline, args.kind, args.key, **attrs)
        print(pipeline)
    elif args.action == 'end':
        ledger.end(args.run_id, args.status, args.log)
    elif args.action == 'heartbeat':
        while ledger.beat(args.run_id):
            time.sleep(60)
    elif args.action == 'tick':
        save(ledger.root / 'last-scheduled-tick.json', {'started': time.time()})
    else:
        collect(ledger, args.config, args.output)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        # Never print potentially sensitive parser/network diagnostics.
        sys.exit(1)
