"""Offline checks for the probe's persisted inventory (not Alloy acceptance)."""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('probe', Path(__file__).with_name('probe-codex.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeInventoryTest(unittest.TestCase):
    def test_content_never_persisted_but_presence_detected(self):
        secret = probe.SENTINEL + '_synthetic_secret'
        payload = {'resourceLogs': [{'scopeLogs': [{'logRecords': [{
            'body': {'stringValue': secret},
            'attributes': [
                {'key': 'tool_output', 'value': {'stringValue': secret}},
                {'key': 'input_tokens', 'value': {'intValue': '123'}},
                {'key': 'unknown_nested', 'value': {'kvlistValue': {'values': [
                    {'key': 'response', 'value': {'stringValue': secret}}
                ]}}},
            ]
        }]}]}]}
        inventory = probe.inspect_payload(payload)
        saved = json.dumps(inventory)
        self.assertNotIn(probe.SENTINEL, saved)
        self.assertNotIn('synthetic_secret', saved)
        self.assertEqual(len(inventory['content_paths']), 3)
        self.assertEqual(inventory['usage'], [{'key': 'input_tokens', 'value': 123.0}])

    def test_trace_parent_and_trace_identity_are_separate(self):
        inventory = probe.inspect_payload({'traceId': probe.PARENT_TRACE, 'parentSpanId': 'other'})
        self.assertEqual(inventory['external_trace_matches'], 1)
        self.assertEqual(inventory['trace_parent_matches'], 0)


if __name__ == '__main__': unittest.main()
