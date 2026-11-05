"""Local task retrospectives, independently invokable from the workload runner.

python -m baton.retrospectives --config config.toml catch-up
python -m baton.retrospectives --config config.toml episode PIPELINE_ID [--repo OWNER/NAME]
"""
import argparse
from contextlib import contextmanager
from dataclasses import asdict
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import uuid

from . import analyst_pool, vendor_policy
from .config import ConfigError, load
from .retrospective_analyst import ANALYZER_VERSION, analyze, prompt_for
from .retrospective_evidence import EvidenceError, Ledger, VERSION, digest, encoded, eligible, evidence, markdown


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write(encoded(value) + '\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


@contextmanager
def worker_lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'worker.lock').open('a') as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def publish(output, key, revision, report):
    """A generation is visible only with both artifacts; the current marker is last."""
    episode = output / digest(key)[:16] / key[1]
    parent = episode / 'revisions' / revision
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.generation-', dir=parent) as tmp:
        staging = Path(tmp)
        atomic(staging / 'report.json', report)
        text = markdown(report)
        with (staging / 'report.md').open('w') as file:
            os.chmod(file.name, 0o600)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        # Validate artifacts before publishing their marker, including crash recovery reruns.
        if read(staging / 'report.json') != report or not (staging / 'report.md').read_text():
            raise EvidenceError('invalid report artifacts')
        generation = parent / str(uuid.uuid4())
        os.replace(staging, generation)
        sync_directory(parent)
    pointer = dict(revision=revision, generation=str(generation.relative_to(episode)),
                   analysis_state=report['analysis_state'])
    atomic(episode / 'current.json', pointer)
    return generation


def current(output, key, revision):
    episode = output / digest(key)[:16] / key[1]
    pointer = read(episode / 'current.json', {})
    if pointer.get('revision') != revision:
        return None
    generation = (episode / pointer['generation']).resolve()
    if not generation.is_relative_to(episode.resolve()):
        raise EvidenceError('invalid report pointer')
    try:
        report = read(generation / 'report.json')
        if (isinstance(report, dict) and report.get('revision') == revision
                and report.get('schema_version') == VERSION
                and (generation / 'report.md').read_text() == markdown(report)):
            return report
    except (OSError, ValueError, KeyError, TypeError):
        pass  # Output artifacts are recoverable from source evidence and the attempt journal.
    return None


def journal(root):
    attempts = []
    for path in sorted((root / 'attempts').glob('*.json')):
        row = read(path)
        if row.get('state') == 'started':
            # The previous worker no longer holds the lock; the result is unknown. A
            # watchdog may still be terminating its child: defer new calls until its deadline.
            row.update(state='ambiguous', usage={'usage_state': 'unavailable'})
            atomic(path, row)
        attempts.append(row)
    return sorted(attempts, key=lambda a: (a['started'], a['attempt_id']))


def report_for(snapshot, source_digest, revision, now):
    facts = [dict(id='episode', source='task', value=snapshot['task']),
             dict(id='usage', source='runs', note='normalized categories; coverage is explicit'),
             dict(id='timing', source='task/events/attempts', note='union intervals; unknown stays absent'),
             dict(id='quality', source='events/postmerge', note='observed signals do not prove correctness')]
    return dict(schema_version=VERSION, analyzer_version=ANALYZER_VERSION, source_digest=source_digest,
                revision=revision, observed_at=now, evidence=evidence(snapshot), facts=facts,
                analysis_state='pending', analysis_reason='', assessment=None, analysis_attempts=[])


def same_source(state, key, source_digest):
    ledger = Ledger(state)
    return key in ledger.tasks and digest(ledger.snapshot(key)) == source_digest


def run(config, state, root, output, repo='', pipeline='', retry=False):
    now = time.time()
    with worker_lock(root) as acquired:
        if not acquired:
            return [dict(state='busy', reason='retrospective_worker_running')]
        attempts = journal(root)
        ledger = Ledger(state)
        keys = [k for k in ledger.tasks if (not repo or k[0] == repo) and (not pipeline or k[1] == pipeline)]
        keys.sort(key=lambda k: (ledger.tasks[k].get('ended', float('inf')), k))
        if pipeline and not keys:
            return [dict(state='skipped', pipeline_id=pipeline, reason='episode_not_found')]
        results, calls = [], 0
        settings = config.retrospectives
        for key in keys:
            snapshot = ledger.snapshot(key)
            reason = eligible(snapshot, now, settings.settle_seconds)
            if reason:
                results.append(dict(repo=key[0], pipeline_id=key[1], state='deferred', reason=reason))
                continue
            source = digest(snapshot)
            policy = asdict(settings)
            policy.pop('paused')
            policy.pop('model')
            policy['enabled'] = analyst_pool.enabled(config)
            revision = digest(dict(key=key, source=source, schema=VERSION, analyzer=ANALYZER_VERSION, policy=policy))
            if not same_source(state, key, source):
                results.append(dict(repo=key[0], pipeline_id=key[1], state='deferred', reason='evidence_changed'))
                continue
            report = report_for(snapshot, source, revision, now)
            mine = [a for a in attempts if a['revision'] == revision]
            previous = current(output, key, revision)
            if previous and (previous['analysis_state'] == 'success' or
                             (not analyst_pool.enabled(config) and previous['analysis_state'] == 'not_configured')):
                results.append(dict(repo=key[0], pipeline_id=key[1], state='unchanged'))
                continue
            if previous:
                report['observed_at'] = previous['observed_at']
            # A durable successful attempt also recovers a crash before report publication.
            success = next((a for a in reversed(mine) if a['state'] == 'success'), None)
            if success:
                report.update(analysis_state='success', assessment=success['assessment'])
            elif not analyst_pool.enabled(config):
                report['analysis_state'] = 'not_configured'
            else:
                reset_path = root / 'retries' / (revision + '.json')
                reset = read(reset_path, {}).get('after', 0)
                if retry:
                    reset = now
                    atomic(reset_path, {'after': reset})
                count = sum(a['started'] >= reset for a in mine)
                day = dt.datetime.fromtimestamp(now, dt.UTC).date().isoformat()
                used = sum(a['day'] == day for a in attempts)
                reason = ('retrospectives_paused' if settings.paused or (root / 'paused').exists() else
                          'revision_attempts_exhausted' if count >= settings.revision_attempts else
                          'daily_budget' if used >= settings.daily_calls else
                          'activation_budget' if calls else
                          'ambiguous_attempt_pending' if any(a['state'] == 'ambiguous' and a['deadline'] + 5 > now
                                                            for a in attempts) else '')
                prompt = prompt_for(report, settings.input_chars)
                if not reason and prompt is None:
                    reason = 'input_limit'
                report['analysis_state'] = 'failed' if reason == 'revision_attempts_exhausted' else 'deferred'
                report['analysis_reason'] = reason
                report['analysis_attempts'] = mine
                if not reason:
                    failed = {a['vendor'] for a in mine if a['state'] != 'success'}
                    with analyst_pool.reserve(config, state, now, failed) as (analyst, available, selection):
                        report['selection'] = selection
                        reason = selection['reason']
                        if reason:
                            report.update(analysis_state='deferred', analysis_reason=reason)
                        else:
                            # Publish evidence once before a model call; unchanged deferred
                            # scans must not create a new generation on every timer tick.
                            report.update(analysis_state='deferred', analysis_reason='analysis_pending')
                            if previous != report:
                                publish(output, key, revision, report)
                            attempt_id = str(uuid.uuid4())
                            path = root / 'attempts' / (attempt_id + '.json')
                            attempt = dict(attempt_id=attempt_id, revision=revision, repo=key[0], pipeline_id=key[1],
                                           kind='retrospective', vendor=analyst.vendor, requested_model=analyst.model,
                                           effort=analyst.effort, selection=selection,
                                           started=time.time(), deadline=time.time() + settings.timeout_seconds,
                                           day=day, state='started', usage={'usage_state': 'unavailable'},
                                           limits=policy)
                            atomic(path, attempt)
                            attempts.append(attempt)
                            mine.append(attempt)
                            calls += 1
                            # Never run in a task checkout, and never register this call as a workload.
                            with tempfile.TemporaryDirectory(prefix='analyst-', dir=root) as directory:
                                result = analyze(config, analyst, prompt, Path(directory), report['facts'], available.fileno())
                            attempt.update(result, ended=time.time())
                            atomic(path, attempt)
                            vendor_policy.observe_failure(state, analyst.vendor, result, time.time())
                            report.update(analysis_state='success' if result['state'] == 'success' else 'failed',
                                          analysis_reason='' if result['state'] == 'success' else result['state'],
                                          assessment=result.get('assessment'))
            report['analysis_attempts'] = mine
            if not same_source(state, key, source):
                results.append(dict(repo=key[0], pipeline_id=key[1], state='deferred', reason='evidence_changed'))
                continue
            # Successful unchanged reports need no new generation or model call.
            if previous == report:
                results.append(dict(repo=key[0], pipeline_id=key[1], state='unchanged'))
                continue
            generation = publish(output, key, revision, report)
            results.append(dict(repo=key[0], pipeline_id=key[1], state=report['analysis_state'],
                                reason=report['analysis_reason'], report=str(generation / 'report.md')))
        return results or [dict(state='idle', reason='no_matching_episodes')]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'config.toml')
    parser.add_argument('--state', type=Path, help='Source runner state (read-only telemetry)')
    parser.add_argument('--work-state', type=Path, help='Independent retrospective journal and lock')
    parser.add_argument('--output', type=Path, help='Report root (defaults to WORK_STATE/reports)')
    parser.add_argument('--repo', default='', help='Filter by OWNER/NAME')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('catch-up', help='Reconcile all eligible durable episodes; at most one model call')
    episode = sub.add_parser('episode', help='Assess one pipeline ID')
    episode.add_argument('pipeline_id', type=uuid.UUID)
    episode.add_argument('--retry', action='store_true', help='Reset revision attempts, retaining daily budget and history')
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
        state = (args.state or config.paths.state).resolve()
        root = (args.work_state or state / 'retrospectives').resolve()
        output = (args.output or root / 'reports').resolve()
        telemetry = state / 'telemetry'
        if root == state or root in state.parents or root == telemetry or root.is_relative_to(telemetry) or (
                output == state or output in state.parents or output == telemetry or output.is_relative_to(telemetry)):
            raise ConfigError('retrospective paths must not overwrite source state/telemetry')
        results = run(config, state, root, output, args.repo,
                      str(args.pipeline_id) if args.command == 'episode' else '', getattr(args, 'retry', False))
    except (ConfigError, EvidenceError) as error:
        print(f'Retrospective failed: {error}', file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        # Never expose metadata contents, credentials or vendor diagnostics on stderr.
        print('Retrospective failed: invalid configuration, metadata or local I/O; inspect local state.', file=sys.stderr)
        return 2
    for result in results:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main())
