"""Real Alloy integration tests. No credentials, external services or model calls.

Requires `alloy` on PATH (or ALLOY_BIN). Opens only ephemeral loopback ports.
"""
import copy
import gzip
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

MODULE = Path(__file__).resolve().parents[2] / 'telemetry/codex.alloy'
MARKER = 'BATON_PRIVATE_SENTINEL_81bd'
TRACE = '1234567890abcdef1234567890abcdef'
RUN = '12345678-1234-1234-1234-123456789abc'
HOST = 'example-host'  # the modules take the host name as an argument


def av(key, value):
    typ = 'boolValue' if isinstance(value, bool) else 'intValue' if isinstance(value, int) else 'stringValue'
    return {'key': key, 'value': {typ: str(value) if typ == 'intValue' else value}}


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class AlloyHarness:
    def __init__(self, kind='codex'):
        self.kind = kind

    def __enter__(self):
        self.received = []
        self.errors = []
        harness = self
        class Sink(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if self.headers.get('Content-Encoding') == 'gzip': body = gzip.decompress(body)
                try: harness.received.append((self.path, json.loads(body)))
                except (ValueError, UnicodeDecodeError): harness.errors.append('non-json output')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{}')
            def log_message(self, *_args): pass
        self.sink = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Sink)
        threading.Thread(target=self.sink.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory(prefix='baton-alloy-test-')
        self.port, self.http_port = free_port(), free_port()
        config = Path(self.tmp.name) / 'config.alloy'
        content = '''
logging { level = "error" }
livedebugging { enabled = false }
import.file "codex" { filename = %s }
codex.baton_codex_ingest "test" {
  listen_address = "127.0.0.1:%d"
  host_name = %s
  logs_output = [otelcol.exporter.otlphttp.sink.input]
  traces_output = [otelcol.exporter.otlphttp.sink.input]
  metrics_output = [otelcol.exporter.otlphttp.sink.input]
}
otelcol.exporter.otlphttp "sink" {
  client { endpoint = "http://127.0.0.1:%d" }
  encoding = "json"
  retry_on_failure { enabled = false }
}
''' % (json.dumps(str(MODULE)), self.port, json.dumps(HOST), self.sink.server_port)
        if self.kind == 'runner':
            content = content.replace('codex.alloy', 'runner.alloy').replace('codex.baton_codex_ingest', 'codex.baton_runner_ingest')
            content = content.replace('  metrics_output = [otelcol.exporter.otlphttp.sink.input]\n', '')
        if self.kind == 'claude':
            content = content.replace('codex.alloy', 'claude.alloy').replace('codex.baton_codex_ingest', 'codex.baton_claude_ingest')
            content = content.replace('  logs_output = [otelcol.exporter.otlphttp.sink.input]\n', '')
            content = content.replace('  traces_output = [otelcol.exporter.otlphttp.sink.input]\n', '')
        config.write_text(content)
        alloy = os.environ.get('ALLOY_BIN', 'alloy')
        self.proc = subprocess.Popen([alloy, 'run', '--disable-reporting', '--stability.level=experimental',
                                      f'--server.http.listen-addr=127.0.0.1:{self.http_port}',
                                      f'--storage.path={self.tmp.name}/state', str(config)],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.diagnostics = []
        self.reader = threading.Thread(target=lambda: self.diagnostics.extend(self.proc.stdout), daemon=True)
        self.reader.start()
        for _ in range(100):
            if self.proc.poll() is not None:
                self.__exit__(None, None, None)
                raise RuntimeError('Alloy failed to start: ' + ''.join(self.diagnostics)[-4000:])
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=0.1): break
            except OSError: time.sleep(0.1)
        else:
            self.__exit__(None, None, None)
            raise RuntimeError('Alloy receiver did not start')
        return self

    def send(self, signal, payload):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}/v1/{signal}',
                                     json.dumps(payload).encode(), {'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read() or '{}')
                if result.get('partialSuccess', {}).get('rejectedLogRecords', 0):
                    raise AssertionError('unexpected rejected records')
        except urllib.error.HTTPError as error:
            message = error.read().decode()
            error.close()
            raise AssertionError(message + ''.join(self.diagnostics)[-2500:]) from None

    def wait(self, predicate):
        for _ in range(100):
            if predicate(): return
            time.sleep(0.1)
        raise AssertionError('Alloy output not received')

    def __exit__(self, *_args):
        if getattr(self, 'proc', None):
            self.proc.terminate()
            try: self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired: self.proc.kill(); self.proc.wait()
            self.reader.join(timeout=2)
            self.proc.stdout.close()
        self.sink.shutdown()
        self.sink.server_close()
        self.tmp.cleanup()


def resource():
    return {'attributes': [av('service.name', MARKER), av('user.email', MARKER), av('baton.run_id', RUN),
                           av('baton.role', 'review'), av('baton.stage', 'review'), av('baton.issue', '142'), av('baton.pr', '168'),
                           {'key': 'unknown', 'value': {'kvlistValue': {'values': [av('nested', MARKER)]}}}]}


def scope():
    return {'name': MARKER, 'version': MARKER, 'attributes': [av('private', MARKER)]}


def metric_payload(value, start, end):
    point = {'startTimeUnixNano': str(start), 'timeUnixNano': str(end), 'count': '1', 'sum': value,
             'bucketCounts': ['1'], 'explicitBounds': [],
             'attributes': [av('token_type', 'input'), av('model', 'gpt-6.1-sol'),
                            av('baton.run_id', RUN), av('prompt', MARKER)],
             'exemplars': [{'filteredAttributes': [av('output', MARKER)], 'timeUnixNano': str(end), 'asDouble': value}]}
    return {'resourceMetrics': [{'resource': resource(), 'scopeMetrics': [{'scope': scope(), 'metrics': [
        {'name': 'codex.turn.token_usage', 'description': MARKER, 'unit': MARKER,
         'histogram': {'aggregationTemporality': 1, 'dataPoints': [point]}},
        {'name': MARKER, 'gauge': {'dataPoints': [{'asInt': '1', 'timeUnixNano': str(end)}]}}
    ]}]}]}


def claude_payload(value, start, end, model='claude-opus-5-5'):
    def point(extra):
        return {'startTimeUnixNano': str(start), 'timeUnixNano': str(end), 'asDouble': value,
                'attributes': [av('model', model), av('session.id', MARKER), av('user.email', MARKER),
                               av('organization.id', MARKER), av('user.account_uuid', MARKER),
                               av('terminal.type', MARKER), av('baton.run_id', RUN)] + extra}
    def total(name, extra):
        return {'name': name, 'description': MARKER, 'unit': MARKER,
                'sum': {'aggregationTemporality': 1, 'isMonotonic': True, 'dataPoints': [point(extra)]}}
    return {'resourceMetrics': [{'resource': resource(), 'scopeMetrics': [{'scope': scope(), 'metrics': [
        total('claude_code.token.usage', [av('type', 'cacheRead'), av('query_source', 'main')]),
        total('claude_code.cost.usage', [av('query_source', MARKER)]),
        total('claude_code.user_prompt.private', [av('type', 'input')]),
    ]}]}]}


@unittest.skipUnless(shutil.which(os.environ.get('ALLOY_BIN', 'alloy')), 'Alloy executable required')
class ClaudeMetricsTest(unittest.TestCase):
    def test_only_bounded_claude_metrics_leave_the_host(self):
        with AlloyHarness('claude') as h:
            stamp = time.time_ns()
            h.send('metrics', claude_payload(100, stamp-2000000, stamp-1000000))
            h.send('metrics', claude_payload(200, stamp-1000000, stamp))
            h.send('metrics', claude_payload(7, stamp-1000000, stamp, model=MARKER))
            h.wait(lambda: len(h.received) >= 3)
            self.assertFalse(h.errors)
            serialized = json.dumps(h.received)
            self.assertNotIn(MARKER, serialized)
            self.assertNotIn(RUN, serialized)
            self.assertNotIn('claude_code.user_prompt', serialized)
            metrics = [m for _, p in h.received for r in p['resourceMetrics'] for s in r['scopeMetrics'] for m in s['metrics']]
            tokens = [m['sum'] for m in metrics if m['name'] == 'claude_code.token.usage']
            self.assertEqual([x['aggregationTemporality'] for x in tokens], [2, 2, 2])
            opus = [x['dataPoints'][0] for x in tokens if 'claude-opus-5-5' in json.dumps(x)]
            # Export requests may arrive out of order; cumulative values follow sample time.
            opus.sort(key=lambda p: int(p['timeUnixNano']))
            self.assertEqual([p['asDouble'] for p in opus], [100, 300])
            self.assertEqual(sorted(a['key'] for a in opus[0]['attributes']), ['model', 'query_source', 'type'])
            resource_attrs = {a['key']: a['value']['stringValue'] for a in h.received[0][1]['resourceMetrics'][0]['resource']['attributes']}
            self.assertEqual(resource_attrs, {'service.name': 'claude-code', 'host.name': HOST, 'agent': 'claude'})


@unittest.skipUnless(shutil.which(os.environ.get('ALLOY_BIN', 'alloy')), 'Alloy executable required')
class AlloyIntegrationTest(unittest.TestCase):
    def test_invalid_allowlisted_values_are_removed(self):
        with AlloyHarness() as h:
            # A key allowlist alone would leak these malicious allowed-key values.
            attrs = [av('event.name', 'codex.tool_result'), av('tool_name', MARKER),
                     av('model', MARKER), av('duration_ms', MARKER), av('baton.issue', MARKER),
                     {'key': 'baton.run_id', 'value': {'arrayValue': {'values': [{'stringValue': MARKER}]}}}]
            h.send('logs', {'resourceLogs': [{'resource': resource(), 'scopeLogs': [{
                'scope': scope(), 'logRecords': [{'attributes': attrs, 'body': {'stringValue': MARKER}}]
            }]}]})
            h.wait(lambda: bool(h.received))
            self.assertNotIn(MARKER, json.dumps(h.received))
            self.assertNotIn(MARKER, ''.join(h.diagnostics))
            self.assertIn('codex.tool_result', json.dumps(h.received))
            self.assertIn(json.dumps(av('baton.issue', 142)), json.dumps(h.received))
            self.assertIn(json.dumps(av('baton.pr', 168)), json.dumps(h.received))

    def test_real_filter_and_delta_accumulation(self):
        with AlloyHarness() as h:
            stamp = time.time_ns()
            attrs = [av('event.name', 'codex.tool_result'), av('tool_name', 'exec_command'),
                     av('duration_ms', 123), av('model', 'gpt-6.1-sol'), av('success', True),
                     av('arguments', MARKER), av('output', MARKER), av('error', MARKER),
                     av('unknown', MARKER), av('input_token_count', MARKER)]
            record = {'timeUnixNano': str(stamp), 'body': {'kvlistValue': {'values': [av('body', MARKER)]}},
                      'severityText': MARKER, 'eventName': MARKER, 'traceId': TRACE,
                      'spanId': '1234567890abcdef', 'attributes': attrs}
            h.send('logs', {'resourceLogs': [{'resource': resource(), 'scopeLogs': [{'scope': scope(), 'logRecords': [record]}]}]})
            span = {'traceId': TRACE, 'spanId': '1234567890abcdef', 'parentSpanId': 'abcdef1234567890',
                    'traceState': MARKER, 'name': MARKER, 'kind': 1, 'startTimeUnixNano': str(stamp),
                    'endTimeUnixNano': str(stamp+1000000), 'attributes': attrs,
                    'status': {'code': 2, 'message': MARKER},
                    'events': [{'name': MARKER, 'timeUnixNano': str(stamp), 'attributes': attrs}],
                    'links': [{'traceId': TRACE, 'spanId': 'abcdef1234567890', 'traceState': MARKER, 'attributes': attrs}]}
            h.send('traces', {'resourceSpans': [{'resource': resource(), 'scopeSpans': [{'scope': scope(), 'spans': [span]}]}]})
            h.send('metrics', metric_payload(100, stamp-2000000, stamp-1000000))
            h.send('metrics', metric_payload(200, stamp-1000000, stamp))
            h.wait(lambda: len(h.received) >= 4)
            self.assertFalse(h.errors)
            serialized = json.dumps(h.received)
            self.assertNotIn(MARKER, serialized)
            self.assertNotIn(MARKER, ''.join(h.diagnostics))
            self.assertIn(RUN, serialized)  # run drilldown retained for logs/traces
            for path, payload in h.received:
                if path == '/v1/metrics':
                    self.assertNotIn(RUN, json.dumps(payload))
            logs = [p for path, p in h.received if path == '/v1/logs'][0]
            log = logs['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
            self.assertEqual(log['body']['stringValue'], 'codex.tool_result')
            log_host = {a['key']: a['value'] for a in logs['resourceLogs'][0]['resource']['attributes']}['host.name']
            self.assertEqual(log_host, {'stringValue': HOST})
            self.assertEqual(log['traceId'], TRACE)
            self.assertIn('duration_ms', json.dumps(log))
            spans = [p for path, p in h.received if path == '/v1/traces'][0]
            clean = spans['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
            self.assertEqual(clean['parentSpanId'], 'abcdef1234567890')
            self.assertFalse(clean.get('links'))
            histograms = [metric['histogram'] for path,p in h.received if path=='/v1/metrics'
                          for r in p['resourceMetrics'] for s in r['scopeMetrics'] for metric in s['metrics']]
            self.assertEqual([x['aggregationTemporality'] for x in histograms], [2, 2])
            histograms.sort(key=lambda x: int(x['dataPoints'][0]['timeUnixNano']))
            self.assertEqual([x['dataPoints'][0]['sum'] for x in histograms], [100, 300])


if __name__ == '__main__': unittest.main()
