"""Quota observability: real config overrides, stale/partial/reset/unknown data."""
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from baton import quota as q

BIN = Path(__file__).resolve().parents[2] / 'bin'
def module(name):
    spec = importlib.util.spec_from_file_location(name, BIN / (name + '.py'))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj

t = module('telemetry')
NOW = 1793000000

class QuotaMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.config = self.state / 'config.toml'
        self.config.write_text((BIN.parent / 'config.example.toml').read_text().replace('quota_cap_claude = 0.9', 'quota_cap_claude = 0.7'))

    def metrics(self):
        values = {}
        t.quota_metrics(self.state, self.config,
                        lambda name, value, labels: values.__setitem__((name, labels), value), NOW)
        return values

    def value(self, values, name, vendor='claude', window=None):
        labels = '{agent="' + vendor + '"' + (',window="' + window + '"' if window else '') + '}'
        return values[(name, labels)]

    def record(self, five=.3, week=.62, now=NOW):
        q.record(self.state, 'claude', {'five_hour': {'used': five, 'resets_at': NOW + 600},
            'seven_day': {'used': week, 'resets_at': NOW + 200000}}, now)

    def test_account_remaining_is_distinct_from_runner_headroom_and_reads_overrides(self):
        self.record()
        values = self.metrics()
        self.assertAlmostEqual(self.value(values,'quota_remaining_ratio',window='seven_day'), .38)
        self.assertAlmostEqual(self.value(values,'quota_runner_headroom_ratio',window='seven_day'), .08)
        self.config.write_text(self.config.read_text().replace('quota_cap_claude = 0.7', 'quota_cap_claude = 0.6'))
        values = self.metrics()
        self.assertEqual(self.value(values,'quota_gate'), 3)
        self.assertEqual(self.value(values,'quota_runner_headroom_ratio',window='seven_day'), 0)

    def test_independent_collector_exports_quota_alongside_existing_state(self):
        self.record(five=.91)
        self.assertIn('paused = false', self.config.read_text())
        output = self.state / 'agent.prom'
        with patch.object(t, 'unit_state', return_value=(1, 1, NOW)), patch.object(t.time, 'time', return_value=NOW):
            t.collect(t.Ledger(self.state), self.config, output)
        metrics = output.read_text()
        self.assertIn('baton_quota_gate{agent="claude"} 2', metrics)
        self.assertIn('baton_quota_remaining_ratio{agent="claude",window="seven_day"} 0.38', metrics)
        self.assertIn('baton_state_collect_success 1', metrics)

    def test_failed_probe_and_partial_update_preserve_independent_window_age(self):
        self.record(now=NOW - 2000)
        q.mark_probed(self.state, 'claude', NOW)
        q.record(self.state, 'claude', {}, NOW)
        q.record(self.state, 'claude', {'five_hour': {'used': .4, 'resets_at': NOW + 600}}, NOW)
        values = self.metrics()
        self.assertEqual(self.value(values,'quota_window_state',window='five_hour'), 1)
        self.assertEqual(self.value(values,'quota_window_state',window='seven_day'), 2)
        self.assertEqual(self.value(values,'quota_observed_at_seconds',window='seven_day'), NOW - 2000)
        self.assertEqual(self.value(values,'quota_gate'), 0)

    def test_reset_pending_does_not_invent_full_balance(self):
        q.record(self.state,'claude',{'five_hour':{'used':.99,'resets_at':NOW - 1}},NOW - 100)
        values=self.metrics()
        self.assertEqual(self.value(values,'quota_window_state',window='five_hour'),3)
        self.assertTrue(math.isnan(self.value(values,'quota_remaining_ratio',window='five_hour')))

    def test_unknown_unsupported_and_legacy_are_not_fresh(self):
        values=self.metrics()
        self.assertEqual(self.value(values,'quota_window_state',window='five_hour'),0)
        self.assertEqual(self.value(values,'quota_window_state','cursor','five_hour'),4)
        (self.state/'quota-claude.json').write_text(json.dumps({'observed_at':NOW,'windows':{
            'seven_day':{'used':.2,'resets_at':NOW+1000}}}))
        q.record(self.state,'claude',{'five_hour':{'used':.1,'resets_at':NOW+1000}},NOW)
        values=self.metrics()
        self.assertEqual(self.value(values,'quota_window_state',window='seven_day'),0)
        self.assertTrue(math.isnan(self.value(values,'quota_remaining_ratio',window='seven_day')))

    def test_cursor_weekly_usage_is_visible_and_its_cap_blocks(self):
        q.record(self.state,'cursor',{'seven_day':{'used':.4,'resets_at':NOW+1000}},NOW)
        values=self.metrics()
        self.assertEqual(self.value(values,'quota_window_state','cursor','seven_day'),1)
        self.assertEqual(self.value(values,'quota_used_ratio','cursor','seven_day'),.4)
        self.assertEqual(self.value(values,'quota_window_state','cursor','five_hour'),4)
        self.assertEqual(self.value(values,'quota_gate','cursor'),1)
        q.record(self.state,'cursor',{'seven_day':{'used':.96,'resets_at':NOW+1000}},NOW)
        self.assertEqual(self.value(self.metrics(),'quota_gate','cursor'),3)

    def test_five_hour_block_and_cooldown(self):
        self.record(five=.90)
        self.assertEqual(self.value(self.metrics(),'quota_gate'),2)
        (self.state/'claude-cooldown-until').write_text(str(NOW+100))
        self.assertEqual(self.value(self.metrics(),'quota_gate'),4)

    def test_malformed_snapshot_and_invalid_config_are_unknown_not_zero(self):
        for content in ('{', '[]', '{"windows":[]}', '{"windows":{"five_hour":{"used":true}}}'):
            (self.state/'quota-claude.json').write_text(content)
            values=self.metrics()
            self.assertEqual(self.value(values,'quota_window_state',window='five_hour'),5)
            self.assertTrue(math.isnan(self.value(values,'quota_remaining_ratio',window='five_hour')))
        self.record()
        self.config.write_text(self.config.read_text().replace('quota_cap_codex = 0.95', 'quota_cap_codex = nan'))
        self.assertEqual(self.value(self.metrics(),'quota_gate'),0)

if __name__ == '__main__':
    unittest.main()
