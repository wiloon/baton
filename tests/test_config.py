"""Configuration contract: side effects and invalid input."""
from dataclasses import FrozenInstanceError
import os
import re
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
    assert c.models.route("dev", "codex").model == 'gpt-6.2-sol'
    assert c.paths.clones == home / 'baton/repos'
    assert config.repo_dirs(c.paths, c.repos[0]).clone == home / 'baton/repos/acme/app'
    assert c.repos[0].gh_repo == 'acme/app' and c.repos[0].owner == 'acme-owner'   # the baseline's example
    assert c.paths.self_dir == ROOT / 'bin'
    assert not home.exists() and dict(os.environ) == before
    assert c.repos[0].secret_env_file == home / '.config/baton/env-files/acme/app/.secrets'
    with pytest.raises(FrozenInstanceError):
        c.scheduling.scan = True
    assert isinstance(c.models.fallback_models_codex, tuple)


def test_repository_auto_merge_defaults_on_and_turns_off_per_repository(tmp_path):
    head, repo = TEXT.split('\n[[repos]]\n', 1)
    omitted = write(tmp_path, head + '\n[[repos]]\n' + repo.replace('\nauto_merge = true\n', '\n', 1))
    assert config.load(omitted, home=tmp_path).repos[0].auto_merge is True
    off = write(tmp_path, head + '\n[[repos]]\n' + repo.replace('\nauto_merge = true\n', '\nauto_merge = false\n', 1))
    c = config.load(off, home=tmp_path)
    assert c.scheduling.auto_merge is True and c.repos[0].auto_merge is False


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
    ('effort = "medium" }', 'effort = "secret-value" }'),
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


def test_the_shipped_config_routes_the_screenshot_review_for_all_three_vendors():
    c = config.load(ROOT / 'config.example.toml', home=Path('/nonexistent'))
    routes = {vendor: c.models.route('screenshot_review', vendor) for vendor in ('claude', 'codex', 'cursor')}
    assert routes['codex'] == config.ModelRoute('gpt-6.1-terra', 'low')
    assert routes['claude'] == config.ModelRoute('opus', 'low')
    assert routes['cursor'] == config.ModelRoute('auto')
    assert c.models.budget_screenshot_review == c.models.budget_review
    # The base route serves every round: a follow-up table is rejected, so none can replace it.
    assert c.models.route('screenshot_review', 'codex', followup=True) == routes['codex']


def test_a_missing_screenshot_review_base_entry_is_rejected(tmp_path):
    text = TEXT.replace('codex = { model = "codex_terra", effort = "low" }\n', '')
    with pytest.raises(config.ConfigError, match='models.routes.screenshot_review.codex'):
        config.load(write(tmp_path, text))


def test_a_screenshot_review_follow_up_table_is_rejected(tmp_path):
    text = TEXT + '\n[models.routes.screenshot_review_followup]\ncodex = { model = "codex_fast" }\n'
    with pytest.raises(config.ConfigError, match='models.routes'):
        config.load(write(tmp_path, text))


def test_the_screenshot_review_budget_is_required(tmp_path):
    with pytest.raises(config.ConfigError):
        config.load(write(tmp_path, TEXT.replace('budget_screenshot_review = 4\n', '')))


def test_screenshot_reviewer_preferences_take_the_role_and_area_keys(tmp_path):
    c = config.load(write(tmp_path, TEXT.replace('reviewer = ["claude", "codex"]',
                          'screenshot_reviewer = ["codex", "claude"]\nscreenshot_reviewer_ui = ["claude", "codex"]')))
    assert ('screenshot_reviewer', ('codex', 'claude')) in c.preferences
    assert ('screenshot_reviewer_ui', ('claude', 'codex')) in c.preferences


def test_an_unknown_screenshot_reviewer_area_is_rejected(tmp_path):
    with pytest.raises(config.ConfigError, match='preferences'):
        config.load(write(tmp_path, TEXT.replace('reviewer = ["claude", "codex"]',
                                                 'screenshot_reviewer_nowhere = ["claude"]')))


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


@pytest.mark.parametrize('old,new,field', [
    ('codex = { model = "codex_fast", effort = "medium" }', 'codex = { model = "missing" }', 'models.routes.dev.codex.model'),
    ('codex = { model = "codex_fast", effort = "medium" }\n', '', 'models.routes.dev.codex'),
    ('codex = { model = "codex_fast", effort = "medium" }', 'codex = {}', 'models.routes.dev.codex.model'),
    ('codex = { model = "codex_fast", effort = "medium" }', 'codex = { model = "codex_fast", effort = "medium", typo = "" }', 'models.routes.dev.codex'),
    ('cursor = { model = "cursor_auto" }', 'cursor = { model = "cursor_auto", effort = "" }', 'models.routes.dev.cursor'),
    ('fallback_models_codex = [', 'fallback_models_codex = ["missing", ', 'models.fallback_models_codex'),
    ('quota_probe_model_claude = "claude_probe"', 'quota_probe_model_claude = "missing"', 'quota.quota_probe_model_claude'),
    ('model_codex = "codex_fast"', 'model_codex = "missing"', 'retrospectives.model_codex'),
    ('[models]', '[models]\ncodex_model = ""', r'models.aliases.*models.routes'),
])
def test_model_route_config_rejects_invalid_references_and_entries(tmp_path, old, new, field):
    assert old in TEXT
    with pytest.raises(config.ConfigError, match=field):
        config.load(write(tmp_path, TEXT.replace(old, new, 1)))


def test_retrospective_aliases_default_and_preserve_legacy_opt_in(tmp_path):
    base = TEXT.split('[retrospectives]')[0].replace('codex_fast = "gpt-6.2-sol"', 'codex_fast = "gpt-6.1-sol"')
    for suffix, enabled in [('', False), ('[retrospectives]\nmodel = "legacy"\n', True),
                            ('[retrospectives]\nmodel = "legacy"\nenabled = false\n', False),
                            ('[retrospectives]\nmodel_codex = "codex_fast"\n', False)]:
        retro = config.load(write(tmp_path, base + suffix)).retrospectives
        assert (retro.model_claude, retro.model_codex, retro.model_cursor) == (
            'opus', 'gpt-6.1-sol', 'grok-4.7-high')
        assert retro.enabled is enabled


def test_alloy_model_filters_match_shipped_codex_config():
    cfg = config.load(ROOT / 'config.example.toml')
    identifiers = {route.model for _, vendor, route in cfg.models.routes if vendor == 'codex' and route.model}
    identifiers.update(cfg.models.fallback_models_codex)
    identifiers.add(cfg.retrospectives.model_codex)
    filters = re.findall(r'IsMatch\(attributes\["model"\], "\^\(([^"]+)\)\$"\)',
                         (ROOT / 'telemetry/codex.alloy').read_text())
    assert filters
    for pattern in filters:
        assert set(pattern.replace('[.]', '.').split('|')) == identifiers
