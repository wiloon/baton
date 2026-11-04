"""The shipped telemetry (Alloy configs, env example, Grafana dashboard) names no deployment
(tests/test_extraction.py checks the words).

Hosts, backend URLs, credentials and CA files come from the environment; the dashboard
picks its host and data sources from variables.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

TELEMETRY = Path(__file__).resolve().parents[2] / 'telemetry'
DASHBOARD = TELEMETRY / 'grafana/dashboard.json'


def panels(items):
    for panel in items:
        yield panel
        yield from panels(panel.get('panels', []))


class ShippedTelemetryTest(unittest.TestCase):
    def test_the_collector_reads_every_example_setting_from_the_environment(self):
        example = (TELEMETRY / 'telemetry.env.example').read_text()
        keys = re.findall(r'^([A-Z][A-Z0-9_]*)=', example, re.M)
        config = (TELEMETRY / 'config.alloy').read_text()
        self.assertTrue(keys)
        self.assertEqual([k for k in keys if f'sys.env("{k}")' not in config], [])

    @unittest.skipUnless(shutil.which(os.environ.get('ALLOY_BIN', 'alloy')), 'Alloy executable required')
    def test_the_collector_config_is_valid(self):
        result = subprocess.run([os.environ.get('ALLOY_BIN', 'alloy'), 'validate', '--stability.level=experimental',
                                 str(TELEMETRY / 'config.alloy')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.dashboard = json.loads(DASHBOARD.read_text())
        self.variables = {v['name']: v for v in self.dashboard['templating']['list']}

    def test_host_and_data_sources_are_variables(self):
        for name, kind in (('prometheus', 'prometheus'), ('loki', 'loki'), ('tempo', 'tempo')):
            self.assertEqual((self.variables[name]['type'], self.variables[name]['query']), ('datasource', kind))
        self.assertEqual(self.variables['host']['type'], 'query')

    def test_every_panel_and_query_uses_a_data_source_variable(self):
        fixed = []
        for panel in panels(self.dashboard['panels']):
            for item in [panel, *panel.get('targets', [])]:
                uid = (item.get('datasource') or {}).get('uid')
                if uid is not None and not uid.startswith('${'):
                    fixed.append(f"{panel.get('title')}: {uid}")
        self.assertEqual(fixed, [])

    def test_every_host_matcher_uses_the_host_variable(self):
        bad = []
        for panel in panels(self.dashboard['panels']):
            for target in panel.get('targets', []):
                query = target.get('expr') or target.get('query') or ''
                for matcher in re.findall(r'\b(?:hostname|host_name|host|instance)=~?"[^"]*"', query):
                    if '$host' not in matcher:
                        bad.append(f"{panel.get('title')}: {matcher}")
        self.assertEqual(bad, [])


if __name__ == '__main__':
    unittest.main()
