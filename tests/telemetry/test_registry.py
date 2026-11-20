"""Effective vendor registry in telemetry collect and begin (#103)."""
import importlib.util
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
        self.assertIn('baton_vendor_info{agent="claude",display_name="Claude",quota="pool"} 1', text)
        self.assertIn('baton_vendor_info{agent="cursor",display_name="Cursor",quota="extra"} 1', text)

    def test_disabled_vendor_omitted_from_fresh_scrape(self):
        text = full_text()
        text = re.sub(r"\n\[vendors\.cursor\].*?(?=\n\[quota\])", "\n", text, flags=re.S)
        text = re.sub(r'\ncursor = \{[^\n]+\}\n', "\n", text)
        text = text.replace('dev = ["cursor", "codex", "claude"]', 'dev = ["codex", "claude"]')
        text = re.sub(r'\nmodel_cursor\s*=\s*"[^"]*"\s*\n', "\n", text)
        self.config.write_text(text)
        output = self.root / 'metrics.prom'
        with patch.object(t, 'unit_state', return_value=(1, 1, 0)):
            t.collect(self.ledger, self.config, output)
        metrics = output.read_text()
        self.assertIn('baton_vendor_info{agent="claude"', metrics)
        self.assertNotIn('baton_vendor_info{agent="cursor"', metrics)
        self.assertNotIn('baton_runs_today{agent="cursor"}', metrics)

    def test_failed_config_omits_registry_and_marks_collect_failure(self):
        self.config.write_text('PAUSED=not-a-number')
        output = self.root / 'metrics.prom'
        before = (self.root / 'telemetry' / 'last-success')
        before.parent.mkdir(parents=True, exist_ok=True)
        before.write_text('12345\n')
        with patch.object(t, 'unit_state', return_value=(1, 1, 0)):
            t.collect(self.ledger, self.config, output)
        metrics = output.read_text()
        self.assertNotIn('baton_vendor_info', metrics)
        self.assertIn('baton_state_collect_success 0', metrics)
        self.assertEqual(before.read_text().strip(), '12345')

    def test_begin_rejects_undeclared_vendor(self):
        text = full_text()
        text = re.sub(r"\n\[vendors\.cursor\].*?(?=\n\[quota\])", "\n", text, flags=re.S)
        path = self.root / 'trimmed.toml'
        path.write_text(text)
        t.CONFIG_PATH = path
        with patch.object(t, 'identity', return_value=OWNER):
            with self.assertRaises(ValueError):
                self.ledger.begin('cursor', 'review', 123, 60, repo='test/repo')

    def test_historical_run_ends_after_vendor_removed_from_registry(self):
        run = None
        t.CONFIG_PATH = self.config
        with patch.object(t, 'identity', return_value=OWNER):
            run = self.ledger.begin('cursor', 'review', 123, 60, repo='test/repo')
        text = full_text()
        text = re.sub(r"\n\[vendors\.cursor\].*?(?=\n\[quota\])", "\n", text, flags=re.S)
        trimmed = self.root / 'trimmed.toml'
        trimmed.write_text(text)
        t.CONFIG_PATH = trimmed
        log = self.state / 'run.jsonl'
        log.write_text('{"type":"result","usage":{"inputTokens":1,"outputTokens":2}}\n')
        ended = self.ledger.end(run['run_id'], 'success', log)
        self.assertEqual(ended['status'], 'success')
        self.assertEqual(ended['agent'], 'cursor')