"""Task identity, replay, factual CI classification and filtered event coverage."""
import copy
import json
import re
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from test_runner import t, OWNER


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.store = t.Tasks(self.state)

    def test_episode_survives_ticks_binds_pr_and_resumes_after_handoff(self):
        first = self.store.ensure(123, repo='test/repo')
        self.assertEqual(first['pipeline_id'], self.store.ensure(123, repo='test/repo')['pipeline_id'])
        same = self.store.ensure(123, 456, repo='test/repo')
        self.assertEqual(first['pipeline_id'], self.store.ensure(0, 456, repo='test/repo')['pipeline_id'])
        self.store.event(same['pipeline_id'], 'handoff', 'manual', reason='manual_checks')
        self.store.event(same['pipeline_id'], 'merged', 'late')
        ended = t.read(self.store.path(same['pipeline_id']))
        self.assertEqual(ended['task_status'], 'planned_handoff')
        second = self.store.ensure(123, 456, repo='test/repo')
        self.assertNotEqual(first['pipeline_id'], second['pipeline_id'])
        other_repo = self.store.ensure(123, repo='acme/other')
        self.assertNotEqual(second['pipeline_id'], other_repo['pipeline_id'])

    def test_deduplication_and_outbox_recovers_state_write_failure(self):
        task = self.store.ensure(123, repo='test/repo')
        pipeline = task['pipeline_id']
        original = t.save
        def fail_task(path, value):
            if Path(path) == self.store.path(pipeline):
                raise OSError('simulated power loss')
            original(path, value)
        with patch.object(t, 'save', side_effect=fail_task):
            with self.assertRaises(OSError):
                self.store.event(pipeline, 'handoff', 'review-limit', reason='review_limit')
        self.store.event(pipeline, 'handoff', 'review-limit', reason='review_limit')
        self.assertEqual(t.read(self.store.path(pipeline))['task_status'], 'abnormal_handoff')
        self.assertEqual(len(list((self.store.root / 'events').glob('*.json'))), 2)

    def end_run(self, vendor, row):
        ledger = t.Ledger(self.state)
        with patch.object(t, 'identity', return_value=OWNER), patch.object(t.subprocess, 'run', side_effect=OSError):
            run = ledger.begin(vendor, 'review', 123, 60, 123, 456, 1, repo='test/repo')
        log = self.state / 'run.jsonl'
        log.write_text(json.dumps(row))
        return ledger.end(run['run_id'], 'success', log)

    def test_the_cost_a_claude_run_reports_stays_in_the_local_record_only(self):
        record = self.end_run('claude', {'type': 'result', 'total_cost_usd': 1.5993142,
                                         'usage': {'input_tokens': 20, 'output_tokens': 21147}})
        self.assertEqual(record['cost_usd'], 1.5993142)
        self.assertEqual(record['cost_state'], 'reported')
        row = t.summary_payload(record)['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['attributes']
        fields = {a['key']: next(iter(a['value'].values())) for a in row}
        self.assertEqual(fields['cost_state'], 'unavailable')
        self.assertNotIn('cost_usd', fields)

    def test_a_vendor_that_reports_no_cost_has_none(self):
        record = self.end_run('cursor', {'type': 'result', 'usage': {'inputTokens': 1, 'outputTokens': 2}})
        self.assertNotIn('cost_usd', record)
        self.assertEqual(record['cost_state'], 'unavailable')

    def test_run_links_and_snapshot_missing_usage_is_not_zero(self):
        ledger = t.Ledger(self.state)
        with patch.object(t, 'identity', return_value=OWNER), patch.object(t.subprocess, 'run', side_effect=OSError):
            run = ledger.begin('codex', 'review', 123, 60, 123, 456, 1, repo='test/repo')
        log = self.state / 'run.jsonl'
        log.write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':80,'output_tokens':7}}))
        ledger.end(run['run_id'], 'success', log)
        self.assertEqual(t.summary_payload(t.read(ledger.path(run['run_id'])))['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['attributes'][0]['key'], 'event_id')
        records = [t.read(p) for p in (self.store.root / 'events').glob('*.json')]
        record = next(r for r in records if r['event_kind'] == 'run_completed')
        event, summary = self.store.payloads(record)
        def fields(p):
            return {a['key']: next(iter(a['value'].values())) for a in p['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['attributes']}
        row = fields(summary)
        self.assertEqual(row['pipeline_id'], run['pipeline_id'])
        self.assertEqual(row['input_tokens'], '20')
        self.assertEqual(row['incomplete_runs'], '0')
        self.assertNotIn('cache_write_tokens', row)
        with patch.object(t, 'identity', return_value=OWNER), patch.object(t.subprocess, 'run', side_effect=OSError):
            run2 = ledger.begin('codex', 'fix', 123, 60, 123, 456, repo='test/repo')
        ledger.end(run2['run_id'], 'interrupted')
        self.store.event(run['pipeline_id'], 'handoff', 'failed', reason='fix_failed')
        record = next(t.read(p) for p in (self.store.root / 'events').glob('*.json') if t.read(p)['event_kind']=='handoff')
        row = fields(self.store.payloads(record)[1])
        self.assertEqual(row['incomplete_runs'], '1')
        self.assertNotIn('input_tokens', row)
        self.assertNotIn('owner', json.dumps(event))

    def test_reconcile_retries_run_callback_after_terminal_commit(self):
        ledger = t.Ledger(self.state)
        with patch.object(t, 'identity', return_value=OWNER), patch.object(t.subprocess, 'run', side_effect=OSError):
            run = ledger.begin('codex', 'review', 123, 60, 123, 456, repo='test/repo')
        with patch.object(t.Tasks, 'event', side_effect=OSError):
            with self.assertRaises(OSError):
                ledger.end(run['run_id'], 'interrupted')
        ledger.reconcile()
        events = [t.read(p) for p in (self.store.root / 'events').glob('*.json')]
        self.assertEqual(sum(e['event_kind'] == 'run_completed' for e in events), 1)
        ledger.reconcile()
        self.assertEqual(len(list((self.store.root / 'events').glob('*.json'))), 2)

    def test_ci_no_checks_timeout_cancel_pending_and_errors(self):
        for buckets, rc, expected in [([],0,'no_checks'), ([],2,'infra_error'),
            (['pass'],0,'pass'), (['pass'],1,'unknown'), (['fail'],1,'failure'),
            (['pending'],8,'pending'), (['cancel'],1,'cancelled'),
            (['skipping'],0,'no_checks'), (['pass'],124,'timeout'),
            (['new'],0,'unknown'), (None,1,'infra_error')]:
            checks = None if buckets is None else [{'bucket': b} for b in buckets]
            self.assertEqual(t.ci_result(checks,rc),expected)

    def test_metadata_validation_and_fingerprints(self):
        task = self.store.ensure(123, repo='test/repo')
        for attrs in ({'reason':'PRIVATE'}, {'command':'PRIVATE'}, {'round':True}, {'head_sha':'PRIVATE'}):
            with self.assertRaises(ValueError):
                self.store.event(task['pipeline_id'], 'review', 'one', **attrs)
        directory = self.state / 'bin'
        directory.mkdir()
        (directory/'run.sh').write_text('version one')
        (directory.parent/'config.toml').write_text('PRIVATE')
        before = t.fingerprints(directory)
        (directory.parent/'config.toml').write_text('PRIVATE TWO')
        after = t.fingerprints(directory)
        self.assertEqual(before['workflow_version'],after['workflow_version'])
        self.assertNotEqual(before['config_version'],after['config_version'])
        self.assertNotIn('PRIVATE',json.dumps(after))

    def test_verify_and_ci_fix_events_are_bounded(self):
        task = self.store.ensure(123, 456, repo='test/repo')
        pipeline = task['pipeline_id']
        self.store.event(pipeline, 'verify', 'pr:456:head:failure', ci_result='failure', duration_ms=12500)
        self.store.event(pipeline, 'ci_fix', 'pr:456:head', agent='codex', head_sha='a'*40)
        self.store.event(pipeline, 'handoff', 'verify-failed', reason='verify_failed')
        records = [t.read(p) for p in (self.store.root/'events').glob('*.json')]
        verify = next(r for r in records if r['event_kind'] == 'verify')
        event, _ = self.store.payloads(verify)
        attrs = {a['key']: next(iter(a['value'].values())) for a in event['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['attributes']}
        self.assertEqual(attrs['duration_ms'], '12500')
        self.assertEqual(attrs['ci_result'], 'failure')
        self.assertEqual(t.read(self.store.path(pipeline))['reason'], 'verify_failed')
        with self.assertRaises(ValueError):
            self.store.event(pipeline, 'verify', 'bad-duration', duration_ms=-1)


@unittest.skipUnless(__import__('shutil').which(__import__('os').environ.get('ALLOY_BIN','alloy')), 'Alloy required')
class TaskAlloyTests(unittest.TestCase):
    def test_probe_models_and_attempt_timing_survive_real_collector(self):
        from test_alloy import AlloyHarness
        record = dict(run_id='aaaaaaaa-0000-4000-8000-000000000001', trace_id='0'*32, span_id='0'*16,
                      agent='cursor', role='probe', stage='probe', status='success', usage_state='complete',
                      started=1., ended=2., issue=0, pr=0, round=0,
                      requested_model='auto', actual_model='unknown')
        payload = t.summary_payload(record)
        row = payload['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
        row['attributes'] += [{'key': 'active_ms', 'value': {'intValue': '1234'}},
                              {'key': 'timing_state', 'value': {'stringValue': 'partial'}}]
        with AlloyHarness('runner') as harness:
            harness.send('logs', payload)
            deadline = time.monotonic() + 8
            while not harness.received and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(harness.received)
            output = json.dumps(harness.received)
            for expected in ('requested_model', 'actual_model', 'auto', 'unknown', 'probe', 'active_ms', '1234', 'partial'):
                self.assertIn(expected, output)

    def test_verify_event_survives_allowlist(self):
        from test_alloy import AlloyHarness
        with tempfile.TemporaryDirectory() as directory:
            store = t.Tasks(directory)
            task = store.ensure(123, 456, repo='test/repo')
            store.event(task['pipeline_id'], 'verify', 'pr:456:failure', ci_result='failure', duration_ms=12500)
            record = next(t.read(p) for p in (store.root/'events').glob('*.json') if t.read(p)['event_kind'] == 'verify')
            with AlloyHarness('runner') as harness:
                harness.send('logs', store.payloads(record)[0])
                deadline = time.monotonic() + 8
                while not harness.received and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(harness.received)
                output = json.dumps(harness.received)
                self.assertIn('verify', output)
                self.assertIn('12500', output)

    def test_events_and_snapshots_survive_allowlist_without_prose(self):
        from test_alloy import AlloyHarness, av, MARKER
        with tempfile.TemporaryDirectory() as directory:
            store = t.Tasks(directory)
            task = store.ensure(123, 456, repo='test/repo')
            store.event(task['pipeline_id'],'review','round:1:'+'a'*40, verdict='changes',agent='codex',round=1,head_sha='a'*40)
            event = next(t.read(p) for p in (store.root/'events').glob('*.json') if t.read(p)['event_kind']=='review')
            payloads=store.payloads(event)
            for payload in payloads:
                row=payload['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
                row['attributes'] += [av('command',MARKER),av('reason',MARKER),av('config_version',MARKER)]
            with AlloyHarness('runner') as harness:
                for payload in payloads:
                    harness.send('logs',payload)
                deadline=time.monotonic()+8
                while len(harness.received)<2 and time.monotonic()<deadline:
                    time.sleep(.05)
                output=json.dumps(harness.received)
                self.assertEqual(len(harness.received),2)
                self.assertNotIn(MARKER,output)
                self.assertIn('baton.task_event',output)
                self.assertIn('baton.task_summary',output)
                self.assertIn(task['pipeline_id'],output)
                self.assertIn('changes',output)
                self.assertIn('a'*40,output)
                self.assertNotIn(MARKER,''.join(harness.diagnostics))


class SummaryContractTest(unittest.TestCase):
    def test_every_summary_attribute_is_one_the_collector_keeps(self):
        alloy = (Path(__file__).resolve().parents[2] / 'telemetry/runner.alloy').read_text()
        kept = set(json.loads(re.search(r'keep_keys\(attributes, (\["event_id"[^\]]*\])\)', alloy).group(1)))
        record = {'run_id': 'r1', 'trace_id': '0' * 32, 'span_id': '0' * 16, 'agent': 'codex', 'role': 'review', 'status': 'success',
                  'usage_state': 'reported', 'started': 1.0, 'ended': 2.0, 'issue': 7, 'pr': 8, 'round': 2,
                  'input_tokens': 3}
        row = t.summary_payload(record)['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['attributes']
        keys = {a['key'] for a in row}
        self.assertTrue({'baton.issue', 'baton.pr', 'baton.round'} <= keys, keys)
        self.assertEqual(keys - kept, set())
