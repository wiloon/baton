"""The CI step (baton.merge) keeps its decisions and emits bounded metadata; no network."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from baton import hooks as telemetry, merge

BIN=Path(__file__).resolve().parents[2]/'bin'

FAKE_GH = '''#!/usr/bin/env python3
import os, pathlib, sys
scenario, seen = os.environ['SCENARIO'], pathlib.Path(os.environ['SEEN'])
args = ' '.join(sys.argv[1:])
if '--json headRefOid' in args:
    if scenario == 'head_change':
        if seen.exists():
            print('%040d' % 2, end='')
        else:
            seen.touch()
            print('%040d' % 1, end='')
    else:
        print('%040d' % 1, end='')
elif '--json bucket' in args:
    out, rc = {'pass': ('[{"bucket":"pass"}]', 0), 'head_change': ('[{"bucket":"pass"}]', 0),
               'failure': ('[{"bucket":"fail"}]', 1), 'timeout': ('[{"bucket":"pending"}]', 8)}.get(scenario, ('', 1))
    print(out)
    sys.exit(rc)
'''


class TaskHookTests(unittest.TestCase):
    def test_ci_wrapper_pass_failure_timeout_no_checks_and_head_change(self):
        # baton.ci green's answers (its watch logic: tests/test_ci.py); the hook is real.
        answers = {'pass': (True, 0, False), 'head_change': (True, 0, False), 'failure': (False, 1, False),
                   'network_error': (False, 1, False), 'timeout': (False, 124, False), 'no_checks': (True, 1, True)}
        for scenario, expected_rc, expected_ci in [('pass', 0, 'pass'), ('failure', 1, 'failure'), ('timeout', 1, 'timeout'),
                                                   ('no_checks', 0, 'no_checks'), ('head_change', 0, 'unknown'),
                                                   ('network_error', 1, 'infra_error')]:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'telemetry-enabled').touch()
                # Invoke the actual helper via a harmless symlink, preserving its file identity.
                (root / 'telemetry.py').symlink_to(BIN / 'telemetry.py')
                # Python's hook runs the real gh / timeout binaries: PATH stubs.
                (root / 'stubs').mkdir()
                (root / 'stubs' / 'timeout').write_text('#!/bin/sh\nshift\nexec "$@"\n')
                (root / 'stubs' / 'gh').write_text(FAKE_GH)
                for stub in (root / 'stubs').iterdir():
                    stub.chmod(0o755)
                env = {'SELF_DIR': str(root), 'STATE': str(root), 'CTX': str(root), 'LOGS': str(root),
                       'GH_REPO': 'telemetry-validation/app', 'SCENARIO': scenario, 'SEEN': str(root / 'seen'),
                       'PATH': str(root / 'stubs') + os.pathsep + os.environ['PATH']}
                green, rc, no_checks = answers[scenario]
                with mock.patch.dict(os.environ, env), mock.patch.object(
                        merge.ci, 'green', return_value={'green': green, 'rc': rc, 'no_checks': no_checks}):
                    hooks = telemetry.from_env(os.environ, issue=123, pr=456)
                    got = merge.Merger(dict(os.environ), hooks, 456, '123').ci_green(45)
                self.assertEqual(0 if got else 1, expected_rc)
                events = [json.loads(p.read_text()) for p in (root / 'telemetry/events').glob('*.json')]
                ci = next(e for e in events if e['event_kind'] == 'ci')
                self.assertEqual(ci['attrs']['ci_result'], expected_ci)
