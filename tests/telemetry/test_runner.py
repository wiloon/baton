"""Offline lifecycle, usage, scheduling and fault tests; no agent calls/secrets."""
import contextlib
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('runner_telemetry', Path(__file__).resolve().parents[2] / 'bin/telemetry.py')
t = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(t)
OWNER = {'pid': 123, 'start': '999', 'boot': 'boot-a'}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.ledger = t.Ledger(self.state)

    def begin(self, vendor='codex'):
        with patch.object(t, 'identity', return_value=OWNER):
            return self.ledger.begin(vendor, 'review', 123, 60, 142, 168, 1, repo='test/repo')

    def log(self, events):
        path = self.state / 'run.jsonl'
        path.write_text('\n'.join(json.dumps(e) for e in events))
        return path

    def test_pid_reuse_boot_heartbeat_deadline_and_zombie(self):
        run = self.begin()
        self.assertEqual(t.health(run, identify=lambda _: OWNER), 'running')
        for owner in (dict(OWNER, start='1000'), dict(OWNER, boot='boot-b')):
            self.assertEqual(t.health(run, identify=lambda _: owner), 'interrupted')
        self.assertEqual(t.health(run, run['heartbeat'] + 181, lambda _: OWNER), 'interrupted')
        run['heartbeat'] = run['deadline'] + 1
        self.assertEqual(t.health(run, run['deadline'] + 1, lambda _: OWNER), 'interrupted')
        proc = self.state / 'proc'
        (proc / '123').mkdir(parents=True)
        (proc / 'sys/kernel/random').mkdir(parents=True)
        (proc / 'sys/kernel/random/boot_id').write_text('boot-a')
        (proc / '123/stat').write_text('123 (a name (test)) S ' + '0 ' * 18 + '999 0')
        self.assertEqual(t.identity(123, proc), OWNER)
        (proc / '123/stat').write_text('123 (zombie) Z ' + '0 ' * 18 + '999 0')
        with self.assertRaises(ProcessLookupError):
            t.identity(123, proc)

    def test_reconciliation_is_terminal_and_summary_excludes_private_content(self):
        run = self.begin()
        log = self.log([{'type': 'turn.completed', 'usage': {'input_tokens': 100,
            'cached_input_tokens': 80, 'output_tokens': 7}, 'result': 'PRIVATE_MARKER'}])
        ended = self.ledger.end(run['run_id'], 'success', log)
        self.assertEqual(ended['input_tokens'], 20)
        self.assertEqual(ended['cache_read_tokens'], 80)
        self.assertEqual(ended['usage_state'], 'complete')
        self.assertEqual(self.ledger.end(run['run_id'], 'interrupted'), ended)
        self.assertNotIn('PRIVATE_MARKER', json.dumps(t.summary_payload(ended)))
        self.assertNotIn('owner', json.dumps(t.run_span_payload(ended)))
        run2 = self.begin()
        self.ledger.reconcile()  # nonexistent PID on test machine
        self.assertEqual(t.read(self.ledger.path(run2['run_id']))['status'], 'interrupted')
        self.assertEqual(self.ledger.end(run2['run_id'], 'success', log)['status'], 'interrupted')

    def test_a_cursor_reconnect_makes_its_usage_partial(self):
        result = {'type': 'result', 'usage': {'inputTokens': 10, 'cacheReadTokens': 40, 'cacheWriteTokens': 9, 'outputTokens': 1754}}
        reconnect = {'type': 'connection', 'subtype': 'reconnecting'}
        self.assertEqual(t.usage('cursor', self.log([result]), 'success')['usage_state'], 'complete')
        used = t.usage('cursor', self.log([reconnect, result]), 'success')
        self.assertEqual((used['usage_state'], used['output_tokens']), ('partial', 1754))

    def test_single_result_semantics_partial_and_unavailable_fields(self):
        fixtures = [
            ('claude', {'input_tokens': 11, 'cache_read_input_tokens': 40, 'cache_creation_input_tokens': 9, 'output_tokens': 8}, 11),
            ('cursor', {'inputTokens': 11, 'cacheReadTokens': 40, 'cacheWriteTokens': 9, 'outputTokens': 8}, 11),
            ('codex', {'input_tokens': 51, 'cached_input_tokens': 40, 'cache_write_input_tokens': 9, 'output_tokens': 8, 'reasoning_output_tokens': 3}, 11),
        ]
        for vendor, usage, expected in fixtures:
            event = {'type': 'turn.completed' if vendor == 'codex' else 'result', 'usage': usage}
            path = self.log([event])
            result = t.usage(vendor, path, 'success')
            self.assertEqual(result['input_tokens'], expected)
            self.assertEqual(result['cache_write_tokens'], 9)
            self.assertEqual(t.usage(vendor, path, 'timeout')['usage_state'], 'partial')
            self.assertEqual(t.usage(vendor, self.log([event, event]), 'success')['usage_state'], 'unavailable')
        path = self.log([{'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': True}}])
        self.assertEqual(t.usage('codex', path, 'success')['usage_state'], 'unavailable')

    def test_settings_runtime_caps_no_cap_and_invalid_reads(self):
        config = self.state / 'config.toml'
        config.write_text((Path(__file__).resolve().parents[2] / 'config.example.toml').read_text())
        self.assertEqual(t.settings(config), [0, None, None, None])
        config.write_text(config.read_text().replace('paused = false', 'paused = true'))
        self.assertEqual(t.settings(config), [1, None, None, None])
        config.write_text('PAUSED=not-a-number')
        with self.assertRaises(ValueError):
            t.settings(config)

    def test_midnight_refresh_and_unknowns_do_not_look_disabled(self):
        config = self.state / 'config.toml'
        config.write_text((Path(__file__).resolve().parents[2] / 'config.example.toml').read_text())
        today = t.dt.datetime.now().strftime('%Y-%m-%d')
        (self.state / ('codex-runs-' + today)).write_text('4')
        (self.state / 'codex-runs-2000-01-01').write_text('99')
        output = self.state / 'metrics.prom'
        with patch.object(t, 'unit_state', return_value=(0, 0, 0)):
            t.collect(self.ledger, config, output)
        metrics = output.read_text()
        self.assertIn('baton_runs_today{agent="codex"} 4', metrics)
        self.assertIn('baton_timer_enabled 0', metrics)
        self.assertIn('baton_state_collect_success 1', metrics)
        (self.state / ('codex-runs-' + today)).unlink()
        with patch.object(t, 'unit_state', return_value=(0, 0, 0)):
            t.collect(self.ledger, config, output)
        self.assertIn('baton_runs_today{agent="codex"} 0', output.read_text())
        with patch.object(t, 'unit_state', side_effect=OSError):
            t.collect(self.ledger, config, output)
        self.assertIn('baton_timer_enabled NaN', output.read_text())
        self.assertIn('baton_state_collect_success 0', output.read_text())
        self.assertEqual(output.stat().st_mode & 0o777, 0o644)

    def test_delivery_retries_stable_identity_and_ack(self):
        run = self.begin()
        ended = self.ledger.end(run['run_id'], 'interrupted')
        requests = []
        @contextlib.contextmanager
        def sink(request, **_):
            requests.append(json.loads(request.data))
            class Response:
                status = 200
                def read(self): return b'{}'
            yield Response()
        with patch.object(t.urllib.request, 'urlopen', side_effect=OSError):
            with self.assertRaises(OSError):
                self.ledger.flush([ended])
        with patch.object(t.urllib.request, 'urlopen', sink):
            self.ledger.flush([ended])
            self.ledger.flush([ended])
        self.assertEqual(len(requests), 2)  # one summary and one run span
        self.assertEqual(requests[0], t.summary_payload(ended))

@unittest.skipUnless(__import__('shutil').which(__import__('os').environ.get('ALLOY_BIN', 'alloy')), 'Alloy executable required')
class RunnerAlloyTests(unittest.TestCase):
    def test_runner_policy_keeps_summary_and_trace_but_drops_content(self):
        from test_alloy import AlloyHarness, av, MARKER, HOST
        record = dict(run_id='12345678-1234-1234-1234-123456789abc',
            trace_id='1234567890abcdef1234567890abcdef', span_id='1234567890abcdef',
            agent='codex', role='review', status='success', usage_state='complete',
            issue=142, pr=168, round=1, started=1700000000, ended=1700000001,
            input_tokens=12, output_tokens=3)
        logs, traces = t.summary_payload(record), t.run_span_payload(record)
        for data, group, scope, rows in ((logs, 'resourceLogs', 'scopeLogs', 'logRecords'),
                                          (traces, 'resourceSpans', 'scopeSpans', 'spans')):
            resource = data[group][0]
            resource['resource']['attributes'].append(av('private', MARKER))
            resource[scope][0]['scope']['name'] = MARKER
            row = resource[scope][0][rows][0]
            row['attributes'] += [av('private', MARKER), av('cost_state', MARKER)]
        traces['resourceSpans'][0]['scopeSpans'][0]['spans'][0].update(
            name=MARKER, status={'message': MARKER},
            events=[{'name': MARKER, 'attributes': [av('private', MARKER)], 'timeUnixNano': '1700000000000000000'}])
        with AlloyHarness('runner') as harness:
            harness.send('logs', logs)
            harness.send('traces', traces)
            import time
            deadline = time.monotonic() + 8
            while len(harness.received) < 2 and time.monotonic() < deadline:
                time.sleep(.05)
            output = json.dumps(harness.received)
            self.assertNotIn(MARKER, output)
            self.assertIn(record['run_id'], output)
            self.assertIn('baton.run_summary', output)
            self.assertIn('baton.run', output)
            self.assertNotIn(MARKER, ''.join(harness.diagnostics))
            self.assertEqual(len(harness.received), 2)
            self.assertIn(json.dumps(av('host.name', HOST)), output)


if __name__ == '__main__':
    unittest.main()
