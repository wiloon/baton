"""bin/tmp-sweep.sh's offline shell tests, and how the runner hands it each repository's prefixes."""
from pathlib import Path
import subprocess

from conftest import FULL
from baton import config, scratch, tmp_sweep

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
    dirs = config.repo_dirs(c.paths, c.repos[0])
    calls = []
    monkeypatch.setattr(session, 'run_process', lambda args, **kw: calls.append((args, kw['env'])))
    me = SimpleNamespace(env=SimpleNamespace(tools={'PATH': '/usr/bin'}, repo=c.repos[0], dirs=dirs), self_dir=tmp_path)
    tick.Tick.sweep(me, 7)
    (args, env), = calls
    assert args == [str(tmp_path / 'tmp-sweep.sh'), '7']
    assert env == {'PATH': '/usr/bin', 'BATON_TMP_PREFIXES': 'app', 'BATON_SCRATCH_DIRS': str(dirs.scratch)}


SECOND_REPO = '''
[[repos]]
gh_repo = "acme/shop"
owner = "acme-owner"
agent_milestone = ""
ci_fixable_checks = []
scan_areas = []
env_files = "~/env"
secret_env_file = "~/env/.secrets"
secret_env_keys = []
tmp_prefixes = ["shop"]
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


def two_repositories(tmp_path, monkeypatch):
    """A config of two repositories with the shipped scripts; /tmp and the state root under tmp_path."""
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('BATON_TMP_ROOT', str(tmp_path / 'tmp'))
    (tmp_path / 'tmp').mkdir()
    path = tmp_path / 'config.toml'
    path.write_text(FULL.read_text().replace('self_dir = "bin"', f'self_dir = "{ROOT / "bin"}"') + SECOND_REPO)
    cfg = config.load(path)
    return path, cfg, [config.repo_dirs(cfg.paths, repo) for repo in cfg.repos]


def task_files(dirs, n):
    spec = scratch.path(dirs, 'spec', n, 1)
    files = [scratch.path(dirs, 'dev', n), scratch.path(dirs, 'fix', n), scratch.path(dirs, 'review', n, 1),
             scratch.path(dirs, 'screenshot-review', n, 2), scratch.path(dirs, 'verify-fix-pr', n),
             scratch.path(dirs, 'verify-fix-issue', n), Path(f'{spec}.body'), Path(f'{spec}.comment')]
    files.append(Path(f'{scratch.path(dirs, "fix", n)}.body'))
    for file in files:
        file.write_text('x')
    return files


def test_cleaning_up_a_task_removes_only_its_own_repositorys_scratch_files(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    from baton import tick
    _, cfg, (app, shop) = two_repositories(tmp_path, monkeypatch)
    mine, longer, other = task_files(app, 3), task_files(app, 30), task_files(shop, 3)
    old_tmp = tmp_path / 'tmp' / 'baton-fix-3.md'
    old_tmp.write_text('from before the upgrade')
    me = SimpleNamespace(env=SimpleNamespace(tools=dict(os.environ), repo=cfg.repos[0], dirs=app),
                         self_dir=ROOT / 'bin')
    tick.Tick.sweep(me, 3)
    assert [f.name for f in mine if f.exists()] == []
    assert all(f.exists() for f in longer + other)
    assert not old_tmp.exists() and app.scratch.is_dir()


def test_the_timer_removes_idle_entries_from_every_scratch_directory(tmp_path, monkeypatch):
    import os
    path, _, repos = two_repositories(tmp_path, monkeypatch)
    idle, fresh = [], []
    for dirs in repos:
        root = scratch.directory(dirs)
        for name in ('review-3-1.md', '.hidden', 'any name at all', 'a-directory'):
            entry = root / name
            entry.mkdir() if name == 'a-directory' else entry.write_text('x')
            idle.append(entry)
        fresh.append(root / 'fix-4.md')
        fresh[-1].write_text('x')
        for entry in [*idle, root]:
            os.utime(entry, (0, 0))
    assert tmp_sweep.main(['--config', str(path)]) == 0
    assert [str(e) for e in idle if e.exists()] == []
    assert all(e.exists() for e in fresh)
    assert all(dirs.scratch.is_dir() for dirs in repos)


def test_a_prefix_must_be_a_plain_name(tmp_path):
    import pytest
    path = tmp_path / 'config.toml'
    path.write_text(FULL.read_text().replace('tmp_prefixes = ["app"]', 'tmp_prefixes = ["app-*"]'))
    with pytest.raises(config.ConfigError, match='tmp_prefixes'):
        config.load(path, home=tmp_path)
