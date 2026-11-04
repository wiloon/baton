"""bin/tmp-sweep.sh's offline shell tests, and how the runner hands it each repository's prefixes."""
from pathlib import Path
import subprocess

from conftest import FULL
from baton import config, tmp_sweep

ROOT = Path(__file__).resolve().parents[1]


def test_the_shell_tests_pass():
    result = subprocess.run(['bash', str(ROOT / 'tests/tmp-sweep.sh')], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.endswith('tmp-sweep: ok\n'), result.stderr


def test_the_timer_sweeps_every_repositorys_prefixes(tmp_path):
    text = FULL.read_text() + '\n[[repos]]\n' + '''gh_repo = "acme/shop"
owner = "acme-owner"
agent_milestone = ""
ci_fixable_checks = []
scan_areas = []
env_files = "~/env"
secret_env_file = "~/env/.secrets"
secret_env_keys = []
tmp_prefixes = ["shop", "app"]
ci_command = "make"
prompt_dir = ""
verify_hook = ""
e2e_hook = ""
gate_hook = ""
nightly_hook = ""
gate_target = ""
gate_option = ""
[repos.areas]
[repos.env]
[repos.prompt_notes]
'''
    path = tmp_path / 'config.toml'
    path.write_text(text)
    c = config.load(path, home=tmp_path)
    assert tmp_sweep.prefixes(c.repos) == 'app shop'
    assert tmp_sweep.prefixes(c.repos[:1]) == 'app'


def test_a_finished_task_is_swept_with_its_repositorys_prefixes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from baton import session, tick
    c = config.load(FULL, home=tmp_path)
    calls = []
    monkeypatch.setattr(session, 'run_process', lambda args, **kw: calls.append((args, kw['env'])))
    me = SimpleNamespace(env=SimpleNamespace(tools={'PATH': '/usr/bin'}, repo=c.repos[0]), self_dir=tmp_path)
    tick.Tick.sweep(me, 7)
    (args, env), = calls
    assert args == [str(tmp_path / 'tmp-sweep.sh'), '7']
    assert env == {'PATH': '/usr/bin', 'BATON_TMP_PREFIXES': 'app'}


def test_a_prefix_must_be_a_plain_name(tmp_path):
    import pytest
    path = tmp_path / 'config.toml'
    path.write_text(FULL.read_text().replace('tmp_prefixes = ["app"]', 'tmp_prefixes = ["app-*"]'))
    with pytest.raises(config.ConfigError, match='tmp_prefixes'):
        config.load(path, home=tmp_path)
