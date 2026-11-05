"""Live activity must distinguish empty ledgers, failed reads and real invocations."""
import importlib.util
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('telemetry_activity', Path(__file__).resolve().parents[2] / 'bin/telemetry.py')
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)

class ActivityTests(unittest.TestCase):
    def collect(self, records):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.toml'
            config.write_text((Path(__file__).resolve().parents[2] / 'config.example.toml').read_text())
            ledger = t.Ledger(root)
            with patch.object(t, 'unit_state', return_value=(1, 1, 100)), \
                 patch.object(ledger, 'reconcile', return_value=records), \
                 patch.object(ledger, 'flush'), patch.object(t.Tasks, 'flush'):
                t.collect(ledger, config, root / 'metrics')
            return (root / 'metrics').read_text()

    def test_idle_has_explicit_zero_for_all_vendors_and_no_execution_rows(self):
        result = self.collect([])
        for vendor in t.VENDORS:
            self.assertIn('baton_running_by_agent{agent="' + vendor + '"} 0', result)
        self.assertNotIn('baton_current_execution_started_seconds', result)

    def test_failed_ledger_is_unknown_instead_of_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').write_text((Path(__file__).resolve().parents[2] / 'config.example.toml').read_text())
            ledger = t.Ledger(root)
            with patch.object(t, 'unit_state', return_value=(1, 1, 100)), \
                 patch.object(ledger, 'reconcile', side_effect=ValueError('corrupt')):
                t.collect(ledger, root / 'config', root / 'metrics')
            result = (root / 'metrics').read_text()
            self.assertIn('baton_state_collect_success 0', result)
            self.assertIn('baton_running_by_agent{agent="claude"} NaN', result)
            self.assertIn('baton_activity_observed_at_seconds NaN', result)

    def test_concurrent_vendors_and_multiple_runs_preserve_task_identity(self):
        values = []
        records = [dict(agent=agent, role='dev', stage='implement', repo='acme/app',
                        issue=208, pr=0, run_id=str(i), started=100+i, status='running')
                   for i, agent in enumerate(('claude', 'codex', 'codex'))]
        records.append(dict(agent='cursor', status='success'))
        t.activity_metrics(records, lambda *args: values.append(args), 200)
        self.assertIn(('running_by_agent', 2, '{agent="codex"}'), values)
        self.assertIn(('running_by_agent', 0, '{agent="cursor"}'), values)
        executions = [v for v in values if v[0] == 'current_execution_started_seconds']
        self.assertEqual(len(executions), 3)
        self.assertIn('issue="208"', executions[0][2])
        self.assertIn('repo="acme/app"', executions[0][2])

    def test_inflight_legacy_run_resolves_repo_from_task_without_guessing(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            task = t.Tasks(state).ensure(421, 0, repo='acme/app')
            record = dict(agent='codex', status='running', started=1,
                          pipeline_id=task['pipeline_id'], issue=421)
            values = []
            t.activity_metrics([record], lambda *args: values.append(args), 2, state)
            labels = next(v[2] for v in values if v[0] == 'current_execution_started_seconds')
            self.assertIn('repo="acme/app"', labels)
            self.assertNotIn('repo', record)

    def test_prometheus_labels_escape_untrusted_repo_text(self):
        values = []
        t.activity_metrics([dict(agent='claude', status='running', started=1,
                                repo='a"\\\nb')], lambda *args: values.append(args), 2)
        label = next(v[2] for v in values if v[0] == 'current_execution_started_seconds')
        self.assertNotIn('\n', label)
        self.assertIn('\\"', label)

class RetrospectiveActivityTests(unittest.TestCase):
    def test_missing_and_leftover_lock_are_idle_without_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertEqual(t.retrospective_running(state), 0)
            self.assertFalse((state / 'retrospectives').exists())
            root = state / 'retrospectives'
            root.mkdir()
            (root / 'worker.lock').touch()
            self.assertEqual(t.retrospective_running(state), 0)

    def test_actual_worker_lock_is_active_then_idle(self):
        from baton.retrospectives import worker_lock
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with worker_lock(state / 'retrospectives') as acquired:
                self.assertTrue(acquired)
                self.assertEqual(t.retrospective_running(state), 1)
            self.assertEqual(t.retrospective_running(state), 0)

    def test_read_failure_is_not_idle(self):
        with patch.object(Path, 'open', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                t.retrospective_running(Path('/unused'))

class IntervalActivityTests(unittest.TestCase):
    def values(self, records, previous=100, now=160):
        result = {}
        t.interval_activity(records, previous, now,
            lambda name, value, labels='': result.__setitem__((name, labels), value))
        return {v: result[('activity_in_interval', '{agent="' + v + '"}')] for v in t.VENDORS}

    def record(self, agent='claude', started=120, ended=140, status='success'):
        return dict(agent=agent, started=started, ended=ended, status=status)

    def test_short_completed_run_is_active_even_when_no_process_is_running(self):
        records = [self.record()]
        self.assertEqual(self.values(records), {'claude': 1, 'cursor': 0, 'codex': 0})
        emitted = []
        t.activity_metrics(records, lambda *args: emitted.append(args), 160)
        self.assertIn(('running_by_agent', 0, '{agent="claude"}'), emitted)

    def test_previous_interval_runs_do_not_make_idle_intervals_active(self):
        self.assertEqual(self.values([self.record(ended=100)]),
                         {'claude': 0, 'cursor': 0, 'codex': 0})

    def test_long_run_and_concurrent_vendors_are_preserved(self):
        records = [self.record('claude', 80, 120), self.record('codex', 90, 200, 'running')]
        self.assertEqual(self.values(records), {'claude': 1, 'cursor': 0, 'codex': 1})

    def test_failed_first_and_long_gap_observations_are_unknown(self):
        for records, previous, now in [(None, 100, 160), ([], float('nan'), 160),
                                       ([], 100, 300), ([], 160, 160)]:
            self.assertTrue(all(math.isnan(v) for v in self.values(records, previous, now).values()))

    def test_failed_read_does_not_advance_interval_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            path = state / 'telemetry' / 'activity-last-observed'
            t.atomic(path, '100')
            t.activity_metrics(None, lambda *args: None, 130, state)
            self.assertEqual(path.read_text(), '100')
            emitted = []
            t.activity_metrics([self.record()], lambda *args: emitted.append(args), 160, state)
            self.assertIn(('activity_in_interval', 1, '{agent="claude"}'), emitted)
            self.assertEqual(path.read_text(), '160')

    def test_upgrade_uses_existing_successful_observation_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            t.atomic(state / 'telemetry' / 'last-success', '100')
            emitted = []
            t.activity_metrics([self.record()], lambda *args: emitted.append(args), 160, state)
            self.assertIn(('activity_in_interval', 1, '{agent="claude"}'), emitted)

if __name__ == '__main__':
    unittest.main()
