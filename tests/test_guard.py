"""bin/guard.sh's offline shell tests (tests/guard.sh)."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_the_shell_tests_pass():
    result = subprocess.run(['bash', str(ROOT / 'tests/guard.sh')], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.startswith('PASS: guard decisions'), result.stderr
