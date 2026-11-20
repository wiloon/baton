"""Effective vendor registry in telemetry collect and begin (#103)."""
import importlib.util
import io
import sys
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baton import config
from conftest import full_text

BIN = Path(__file__).resolve().parents[2] / 'bin'
spec = importlib.util.spec_from_file_location('registry_telemetry', BIN / 'telemetry.py')
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
OWNER = {'pid': 123, 'start': '999', 'boot': 'boot-a'}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, t, 'CONFIG_PATH', t.CONFIG_PATH)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config.toml'
        self.config.write_text(full_text())
        self.ledger = t.Ledger(self.state)

    def test_collect_exports_vendor_info_for_declared_vendors(self):
        output = self.root / 'metrics.prom'
        with patch.object(t, 'unit_state', return_value=(1, 1, 0)):
            t.collect(self.ledger, self.config, output)
        text = output.read_text()
        self.assertIn('baton_daily_cap{agent="claude"} NaN', text)
        self.assertIn('baton_cap_configured{agent="claude"} 0', text)
        self.assertIn('baton_quota_runner_cap_ratio{agent="claude",window="seven_day"} 0.9', text)
        self.assertIn('baton_vendor_info{agent="claude",display_name="Claude",quota="pool"} 1', text)
        self.assertIn('baton_vendor_info{agent="cursor",display_name="Cursor",quota="extra"} 1', text)

    def without_cursor(self):
        text = re.sub(r"\n\[vendors\.cursor\].*?(?=\n\[quota\])", "\n", full_text(), flags=re.S)
        text = re.sub(r'^cursor = \{[^\n]+\}\n', "", text, flags=re.M)
        text = text.replace('dev = ["cursor", "codex", "claude"]', 'dev = ["codex", "claude"]')
        text = re.sub(r'^model_cursor = .*\n', "", text, flags=re.M)
        self.config.write_text(text)
        self.assertEqual(config.load(self.config).declared(), ('claude', 'codex'))

    def test_disabled_vendor_omitted_from_fresh_scrape(self):
        self.without_cursor()
        output = self.root / 'metrics.prom'
        with patch.object(t, 'unit_state', return_value=(1, 1, 0)):
            t.collect(self.ledger, self.config, output)
        metrics = output.read_text()
        self.assertIn('baton_vendor_info{agent="claude"', metrics)
        self.assertNotIn('baton_vendor_info{agent="cursor"', metrics)
        self.assertNotIn('agent="cursor"', metrics)

    def test_failed_config_omits_registry_and_marks_collect_failure(self):
        self.config.write_text(full_text().replace('dev = ["cursor", "codex", "claude"]', 'dev = [["claude"]]'))
        output = self.root / 'metrics.prom'
        before = (self.state / 'telemetry' / 'last-success')
        before.parent.mkdir(parents=True, exist_ok=True)
        before.write_text('12345\n')
        with patch.object(t, 'unit_state', return_value=(1, 1, 0)):
            t.collect(self.ledger, self.config, output)
        metrics = output.read_text()
        self.assertNotIn('agent=', metrics)
        self.assertIn('baton_state_collect_success 0', metrics)
        self.assertEqual(before.read_text().strip(), '12345')

    def begin_cli(self, vendor, runner_config=True):
        args = ['telemetry.py', '--state', str(self.state)]
        if runner_config:
            args += ['--runner-config', str(self.config)]
        args += ['begin', vendor, 'review', '123', '60', '7', '8', '1', '--repo', 'test/repo']
        with patch.object(sys, 'argv', args), patch.object(t, 'identity', return_value=OWNER), \
                patch.object(t.subprocess, 'run', side_effect=OSError), patch.object(t.subprocess, 'Popen'):
            t.main()

    def test_begin_rejects_undeclared_vendor(self):
        self.without_cursor()
        with self.assertRaises(SystemExit) as rejected:
            self.begin_cli('cursor')
        self.assertEqual(rejected.exception.code, 2)
        self.assertFalse(self.ledger.root.exists())
        self.begin_cli('codex')
        self.assertEqual(len(list((self.ledger.root / 'runs').glob('*.json'))), 1)

    def test_begin_requires_config_and_rejects_a_broken_file(self):
        with self.assertRaises(SystemExit) as missing:
            self.begin_cli('claude', runner_config=False)
        self.assertEqual(missing.exception.code, 2)
        self.config.write_text('[vendors')
        with self.assertRaises(SystemExit) as broken:
            self.begin_cli('claude')
        self.assertEqual(broken.exception.code, 2)
        self.assertFalse(self.ledger.root.exists())

    def test_historical_run_and_events_export_after_vendor_removal(self):
        self.begin_cli('cursor')
        run_path, = (self.ledger.root / 'runs').glob('*.json')
        self.without_cursor()
        log = self.state / 'run.jsonl'
        log.write_text('{"type":"result","usage":{"inputTokens":1,"outputTokens":2}}\n')
        ended = self.ledger.end(run_path.stem, 'success', log)
        self.assertEqual(ended['status'], 'success')
        self.assertEqual(ended['agent'], 'cursor')

        def accept(_request, timeout):
            response = io.BytesIO(b'{}')
            response.status = 200
            return response

        output = self.root / 'metrics.prom'
        with patch.object(t, 'unit_state', return_value=(1, 1, 0)), \
                patch.object(t.urllib.request, 'urlopen', side_effect=accept):
            t.collect(self.ledger, self.config, output)
        self.assertIn('baton_summary_export_success 1', output.read_text())
        self.assertNotIn('agent="cursor"', output.read_text())
        self.assertTrue(run_path.with_suffix('.sent').exists())
        completed = [p for p in (self.ledger.root / 'events').glob('*.json')
                     if t.read(p)['event_kind'] == 'run_completed']
        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0].with_suffix('.sent').exists())
