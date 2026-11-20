"""Check how Grafana wires vendor discovery and display labels."""

import json
from pathlib import Path


def test_dashboard_discovers_vendors_by_host():
    path = Path(__file__).resolve().parents[2] / 'telemetry/grafana/dashboard.json'
    dashboard = json.loads(path.read_text())
    agent = next(variable for variable in dashboard['templating']['list'] if variable['name'] == 'agent')
    assert agent['type'] == 'query'
    assert agent['query']['query'] == 'label_values(baton_vendor_info{hostname="$host"}, agent)'
    assert agent['includeAll'] and agent['allValue'] == '.*'

    panels = {panel['title']: panel for panel in dashboard['panels']}
    timeline = panels['Agent activity']
    executions = panels['Current executions']
    for panel in (timeline, executions):
        expression = panel['targets'][0]['expr']
        assert 'baton_vendor_info{hostname="$host"}' in expression
        assert 'group_left(display_name)' in expression
        assert '${agent' not in expression

    assert timeline['targets'][0]['legendFormat'] == '{{display_name}}'
    join, organize = executions['transformations']
    assert join['options']['byField'] == 'agent'
    assert organize['options']['renameByName']['display_name'] == 'Agent'
