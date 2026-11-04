#!/usr/bin/env python3
"""Opt-in real Codex probe. Loopback sink; raw telemetry stays in memory only.

Run as the authenticated agent user. Creates temporary CODEX_HOME with an auth
symlink; never changes user configuration or the agent scheduler. The model may
make API requests. Only synthetic prompts/tool input are used. Output is a
sanitized inventory, not an OTLP replay fixture or a production content filter.
"""
import argparse
import collections
import contextlib
import gzip
import http.server
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import urllib.parse

SENTINEL = 'BATON_OTEL_PROBE_CONTENT_7b29'
PARENT_TRACE = '1234567890abcdef1234567890abcdef'
PARENT_SPAN = '1234567890abcdef'


def shape(value):
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    if isinstance(value, list):
        # Keep distinct shapes, never arbitrary payload values.
        unique = {json.dumps(shape(v), sort_keys=True) for v in value}
        return [json.loads(v) for v in sorted(unique)]
    return type(value).__name__


def inspect_payload(data):
    result = {'shape': shape(data), 'content_paths': [], 'attributes': {},
              'names': [], 'usage': [], 'trace_parent_matches': 0,
              'external_trace_matches': 0, 'events': [], 'metric_points': [], 'content_attribute_keys': []}
    def attributes(items):
        safe = {}
        for item in items:
            key, val = item.get('key', ''), item.get('value', {})
            if SENTINEL in json.dumps(val):
                result['content_attribute_keys'].append(key)
            if not isinstance(val, dict): continue
            for typ in ('intValue', 'doubleValue', 'boolValue'):
                if typ in val: safe[key] = val[typ]
            value = val.get('stringValue', '')
            if key in ('event.name', 'event', 'service.name', 'model', 'token_type', 'tool', 'tool_name', 'type'):
                if re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}', value) and SENTINEL not in value:
                    safe[key] = value
            if any(term in key.lower() for term in ('token', 'duration')):
                try: safe[key] = float(value)
                except (ValueError, TypeError): pass
        return safe

    def visit(v, path=''):
        if isinstance(v, dict):
            if 'logRecords' in v:
                for record in v['logRecords']:
                    result['events'].append(attributes(record.get('attributes', [])))
            if 'metrics' in v:
                for metric in v['metrics']:
                    entry = {'name': metric.get('name'), 'unit': metric.get('unit')}
                    for kind in ('sum', 'histogram', 'gauge', 'exponentialHistogram'):
                        if kind in metric:
                            source = metric[kind]
                            entry.update(kind=kind, temporality=source.get('aggregationTemporality'),
                                         monotonic=source.get('isMonotonic'))
                            entry['points'] = []
                            for point in source.get('dataPoints', []):
                                safe = {k: point[k] for k in ('asInt','asDouble','count','sum') if k in point}
                                safe['attributes'] = attributes(point.get('attributes', []))
                                entry['points'].append(safe)
                    result['metric_points'].append(entry)
            if 'resource' in v:
                result.setdefault('resources', []).append(attributes(v['resource'].get('attributes', [])))
            if 'key'  in v and 'value' in v:
                k = v['key']
                result['attributes'][k] = shape(v['value'])
                # Numeric measurements only; no arbitrary string attribute values.
                if any(term in k.lower() for term in ('token', 'duration', 'usage')):
                    val = v['value']
                    if isinstance(val, dict):
                        for typ in ('intValue', 'doubleValue'):
                            if typ in val:
                                try: result['usage'].append({'key': k, 'value': float(val[typ])})
                                except (TypeError, ValueError): pass
            if 'name' in v and isinstance(v['name'], str):
                # Known instrumentation names, not arbitrary event content.
                name = v['name']
                if name.startswith(('codex', 'openai', 'gen_ai', 'otel')):
                    result['names'].append(name)
            if v.get('traceId') == PARENT_TRACE:
                result['external_trace_matches'] += 1
            if v.get('parentSpanId') == PARENT_SPAN:
                result['trace_parent_matches'] += 1
            for k, item in v.items(): visit(item, path + '/' + k)
        elif isinstance(v, list):
            for item in v: visit(item, path + '/*')
        elif isinstance(v, str) and SENTINEL in v:
            result['content_paths'].append(path)
    visit(data)
    result['content_attribute_keys'] = sorted(set(result['content_attribute_keys']))
    result['names'] = sorted(set(result['names']))
    result['content_paths'] = sorted(set(result['content_paths']))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--alloy', action='store_true', help='run through the reviewed real Alloy filter')
    target.add_argument('--endpoint', help='send to an already verified loopback collector; exporter output is checked separately')
    options = parser.parse_args()
    if options.endpoint:
        url = urllib.parse.urlparse(options.endpoint)
        if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost') or url.path not in ('', '/'):
            parser.error('--endpoint must be a loopback HTTP collector base URL')
    with contextlib.ExitStack() as stack:
        run_probe(stack, options)


def run_probe(stack, options):
    harness = None
    if options.alloy:
        from test_alloy import AlloyHarness
        harness = stack.enter_context(AlloyHarness())
    captures = []
    class Sink(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if self.headers.get('Content-Encoding') == 'gzip': body = gzip.decompress(body)
            item = {'path': self.path, 'content_type': self.headers.get('Content-Type'), 'bytes': len(body)}
            try: item.update(inspect_payload(json.loads(body)))
            except (ValueError, UnicodeDecodeError): item['decode'] = 'non-json'
            captures.append(item)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{}')
        def log_message(self, *_args): pass
    with tempfile.TemporaryDirectory(prefix='baton-otel-probe-') as directory:
        root = Path(directory)
        auth = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))) / 'auth.json'
        if not auth.exists(): raise SystemExit('Codex auth.json missing')
        codex_dir = root/'codex'
        codex_dir.mkdir(mode=0o700)
        (codex_dir/'auth.json').symlink_to(auth)
        work = root/'work'
        work.mkdir()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Sink)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(server.server_close)
        stack.callback(server.shutdown)
        endpoint = options.endpoint.rstrip('/') if options.endpoint else f'http://127.0.0.1:{harness.port if harness else server.server_port}'
        env = os.environ.copy()
        env.update(CODEX_HOME=str(codex_dir), TRACEPARENT=f'00-{PARENT_TRACE}-{PARENT_SPAN}-01',
                   OTEL_RESOURCE_ATTRIBUTES='baton.run_id=12345678-1234-1234-1234-123456789abc,baton.role=review,baton.stage=review',
                   OTEL_METRIC_EXPORT_INTERVAL='1000')
        runs = []
        for analytics in (True, False):
            captures.clear()
            if harness: harness.received.clear()
            args = ['codex', 'exec', '--ignore-user-config', '--ignore-rules', '--strict-config',
                    '--ephemeral', '--skip-git-repo-check', '--json', '-s', 'read-only', '-C', str(work),
                    '-c', 'approval_policy="never"', '-c', 'otel.log_user_prompt=false',
                    '-c', f'analytics.enabled={str(analytics).lower()}']
            for field, signal in [('exporter','logs'), ('metrics_exporter','metrics'), ('trace_exporter','traces')]:
                args += ['-c', f'otel.{field}={{otlp-http={{endpoint="{endpoint}/v1/{signal}",protocol="json"}}}}']
            prompt = (f'This is an isolated telemetry capability probe. Run exactly one shell command: '
                      f'printf "{SENTINEL}\\n". Then reply PROBE_OK. Do not read files or run other tools.')
            try:
                completed = subprocess.run(args+[prompt], env=env, capture_output=True, text=True, timeout=120)
                rows = []
                for line in completed.stdout.splitlines():
                    try: row = json.loads(line)
                    except ValueError: continue
                    safe = {'type': row.get('type'), 'shape': shape(row)}
                    if row.get('type') == 'turn.completed': safe['usage'] = row.get('usage')
                    rows.append(safe)
                # Only constant diagnostic categories, never stderr content.
                diagnostic = {k: k in completed.stderr.lower() for k in
                              ('unknown field', 'error loading configuration', 'unauthorized', 'failed to export', 'invalid')}
                time.sleep(2)
                if harness:
                    captures = [{'path': path, **inspect_payload(payload)} for path, payload in harness.received]
                    assert SENTINEL not in json.dumps(harness.received), 'Private marker escaped Alloy policy'
                    assert SENTINEL not in ''.join(harness.diagnostics), 'Private marker entered Alloy diagnostics'
                runs.append({'analytics_enabled': analytics, 'returncode': completed.returncode,
                             'diagnostic_flags': diagnostic, 'jsonl': rows, 'requests': list(captures)})
            except subprocess.TimeoutExpired:
                runs.append({'analytics_enabled': analytics, 'timeout': True, 'requests': list(captures)})
        print(json.dumps({'codex_version': subprocess.check_output(['codex','--version'], text=True).strip(),
                          'scope': 'synthetic local sink only; no production settings changed',
                          'through_alloy': bool(harness), 'external_loopback_collector': bool(options.endpoint), 'runs': runs}, indent=2))


if __name__ == '__main__': main()
