"""Configuration contract: side effects and invalid input."""
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import subprocess
import sys

import pytest

from baton import config

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / 'config.example.toml').read_text()


def write(tmp_path, text):
    path = tmp_path / 'config.toml'
    path.write_text(text)
    return path


def test_loading_is_typed_immutable_and_does_not_initialize(tmp_path, monkeypatch):
    home = tmp_path / 'missing-home'
    monkeypatch.setenv('AUTO_MERGE', '0')
    monkeypatch.setenv('MODEL_CODEX_DEV', 'ambient-model')
    before = dict(os.environ)
    c = config.load(ROOT / 'config.example.toml', home=home)
    assert c.scheduling.auto_merge is True
    assert c.models.model_codex_dev == 'gpt-6.1-sol'
    assert c.paths.clones == home / 'baton/repos'
    assert config.repo_dirs(c.paths, c.repos[0]).clone == home / 'baton/repos/acme/app'
    assert c.repos[0].gh_repo == 'acme/app' and c.repos[0].owner == 'acme-owner'   # the baseline's example
    assert c.paths.self_dir == ROOT / 'bin'
    assert not home.exists() and dict(os.environ) == before
    assert c.repos[0].secret_env_file == home / '.config/baton/env-files/acme/app/.secrets'
    with pytest.raises(FrozenInstanceError):
        c.scheduling.scan = True
    assert isinstance(c.models.fallback_models_codex, tuple)


@pytest.mark.parametrize('old,new', [
    ('auto_merge = true', 'auto_merge = 1'),
    ('chain_max = 5', 'chain_max = true'),
    ('chain_max = 5', 'chain_max = 0'),
    ('chain_max = 5', 'chain_max = "secret-value"'),
    ('quota_cap_claude = 0.9', 'quota_cap_claude = 1.1'),
    ('quota_cap_claude = 0.9', 'quota_cap_claude = nan'),
    ('quota_cap_claude = 0.9', 'quota_cap_claude = true'),
    ('vendor_pool = ["claude", "codex"]', 'vendor_pool = ["cursor"]'),
    ('vendor_pool = ["claude", "codex"]', 'vendor_pool = ["codex", "codex"]'),
    ('effort_codex_dev = ""', 'effort_codex_dev = "secret-value"'),
    ('reviewer = ["claude", "codex"]', 'reviewer = ["unknown"]'),
    ('reviewer = ["claude", "codex"]', 'reviewer = []'),
    ('chain_max = 5', 'chain_mxa = 5'),
    ('chain_max = 5', ''),
    ('cursor_branch_re = "^cursor/"', 'cursor_branch_re = "["'),
    ('self_dir = "bin"', 'self_dir = "$HOME/bin"'),
])
def test_invalid_fields_fail_without_creating_state_or_leaking_values(tmp_path, old, new):
    source = write(tmp_path, TEXT.replace(old, new))
    with pytest.raises(config.ConfigError) as error:
        config.load(source, home=tmp_path / 'missing')
    assert 'secret-value' not in str(error.value)
    assert not (tmp_path / 'missing').exists()


def test_relative_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    source = write(tmp_path, TEXT)
    monkeypatch.chdir('/')
    assert config.load(source).paths.self_dir == tmp_path / 'bin'


def test_cli_has_no_initialization_and_syntax_errors_hide_values(tmp_path):
    source = write(tmp_path, TEXT)
    result = subprocess.run([sys.executable, '-m', 'baton.config', '--config', str(source)],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == 'Configuration valid\n'
    source.write_text('secret = "secret-value')
    result = subprocess.run([sys.executable, '-m', 'baton.config', '--config', str(source)],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 2 and 'secret-value' not in result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ['config.toml']


def test_preferences_support_roles_and_areas(tmp_path):
    c = config.load(write(tmp_path, TEXT.replace('reviewer = ["claude", "codex"]',
                  'spec_reviewer_ui = ["codex", "claude"]')))
    assert ('spec_reviewer_ui', ('codex', 'claude')) in c.preferences


def test_load_never_calls_initialization_or_subprocess(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('configuration loading invoked an initialization side effect')
    monkeypatch.setattr(Path, 'mkdir', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    assert config.load(ROOT/'config.example.toml', home=tmp_path/'absent')


def test_cache_directories_must_be_writable_by_codex(tmp_path):
    with pytest.raises(config.ConfigError, match='must include'):
        config.load(write(tmp_path, TEXT.replace('gocache = "~/.cache/go-build"', 'gocache = "~/other-cache"')))


def test_fractional_budget_and_integer_quota_are_supported(tmp_path):
    c = config.load(write(tmp_path, TEXT.replace('budget_review = 4', 'budget_review = 0.5')
                          .replace('quota_cap_codex = 0.95', 'quota_cap_codex = 1')))
    assert c.models.budget_review == 0.5 and c.quota.quota_cap_codex == 1.0


@pytest.mark.parametrize('old,new', [
    ('reviewer = ["claude", "codex"]', 'reviewer_ = ["claude"]'),
    ('reviewer = ["claude", "codex"]', 'reviewer_unknown = ["claude"]'),
    ('path_prefix = ["~/.local/share/mise/shims", "~/.local/bin"]', 'path_prefix = []'),
    ('path_prefix = ["~/.local/share/mise/shims", "~/.local/bin"]', 'path_prefix = [""]'),
])
def test_invalid_preference_suffix_and_empty_paths(tmp_path, old, new):
    with pytest.raises(config.ConfigError):
        config.load(write(tmp_path, TEXT.replace(old, new)))
