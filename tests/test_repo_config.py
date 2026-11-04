"""The watched repositories are configuration (#326, #327): nothing about them is built into the runner."""
from pathlib import Path
import re

import pytest

from baton import config, dispatch, initialization, session, vendors

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / 'config.example.toml').read_text()

# Another project: two directories, its own owner, its own secret and tool variables.
OTHER = re.sub(r'(?ms)^\[\[repos\]\]\n.*?(?=^\[)', '''[[repos]]
gh_repo = "acme/shop"
owner = "alice"
agent_milestone = ""
ci_fixable_checks = []
scan_areas = []
env_files = "~/shop-env"
secret_env_file = "~/shop-env/.secrets"
secret_env_keys = ["SHOP_API_KEY"]
tmp_prefixes = []
ci_command = "make check"
prompt_dir = ""
verify_hook = ""
e2e_hook = ""
gate_hook = ""
nightly_hook = ""
gate_target = ""
gate_option = ""

''', TEXT)
OTHER = re.sub(r'(?ms)^\[repos\.areas\]\n.*?(?=^\[)', '[repos.areas]\nweb = "frontend"\nserver = "backend"\n\n', OTHER)
OTHER = re.sub(r'(?ms)^\[repos\.env\]\n.*?(?=^\[)', '[repos.env]\nSHOP_DATA = "~/data/shop.db"\nSHOP_MODE = "test"\n\n', OTHER)
OTHER = re.sub(r'(?m)^scan = true$', 'scan = false', OTHER)


def write(tmp_path, text):
    path = tmp_path / 'config.toml'
    path.write_text(text)
    return path


def load_other(tmp_path, text=OTHER):
    return config.load(write(tmp_path, text), home=tmp_path / 'home')


def test_repository_settings_live_in_one_repo_section(tmp_path):
    c = load_other(tmp_path)
    assert (c.repos[0].gh_repo, c.repos[0].owner) == ('acme/shop', 'alice')
    assert c.repos[0].areas == (('web', 'frontend'), ('server', 'backend'))
    assert c.repos[0].env == (('SHOP_DATA', str(tmp_path / 'home/data/shop.db')), ('SHOP_MODE', 'test'))
    assert c.repos[0].secret_env_file == tmp_path / 'home/shop-env/.secrets'
    assert not hasattr(c.scheduling, 'gh_repo') and not hasattr(c.environment, 'clerk_env')


def test_the_owner_is_configured_not_taken_from_the_repository_name(tmp_path):
    c = load_other(tmp_path)
    assert session.Session(c, initialization.Runtime(), {}, tmp_path).owner == 'alice'


def test_preference_areas_come_from_the_repository(tmp_path):
    c = load_other(tmp_path, OTHER.replace('reviewer = ["claude", "codex"]', 'dev_frontend = ["codex"]'))
    assert ('dev_frontend', ('codex',)) in c.preferences
    with pytest.raises(config.ConfigError, match='unknown role or area'):
        load_other(tmp_path, OTHER.replace('reviewer = ["claude", "codex"]', 'dev_ui = ["codex"]'))


@pytest.mark.parametrize('old,new', [
    ('gh_repo = "acme/shop"', 'gh_repo = "shop"'),
    ('owner = "alice"', 'owner = ""'),
    ('web = "frontend"', 'web = "other"'),
    ('SHOP_MODE = "test"', 'SHOP_MODE = 3'),
    ('SHOP_MODE = "test"', 'PATH = "/evil"'),
])
def test_invalid_repository_settings_are_refused(tmp_path, old, new):
    with pytest.raises(config.ConfigError):
        load_other(tmp_path, OTHER.replace(old, new))


def test_scan_areas_are_required_only_when_scanning(tmp_path):
    with pytest.raises(config.ConfigError, match='scan_areas'):
        load_other(tmp_path, OTHER.replace('scan = false', 'scan = true'))


def test_areas_follow_the_configured_directories(tmp_path):
    env = session.Session(load_other(tmp_path), initialization.Runtime(), {}, tmp_path)
    assert vendors.area_of(['web/a.ts', 'web/b.ts', 'README.md'], env=env) == 'frontend'
    assert vendors.area_of(['web/a.ts', 'server/b.go'], env=env) == 'other'
    assert vendors.area_of(['client/a.ts'], env=env) == 'other'
    assert vendors.mentioned_area('see server/main.go and web/x.ts and server/y.go', env=env) == 'other'
    assert vendors.mentioned_area('only server/main.go here', env=env) == 'backend'


def test_tools_get_the_repository_variables_and_never_its_secret_keys(tmp_path):
    c = load_other(tmp_path)
    env = initialization.child_environment(c, {'PATH': '/usr/bin', 'SHOP_API_KEY': 'ambient-secret'})
    assert env['SHOP_DATA'] == str(tmp_path / 'home/data/shop.db') and env['SHOP_MODE'] == 'test'
    assert 'SHOP_API_KEY' not in env


def test_secret_keys_reach_only_coding_runs(tmp_path):
    c = load_other(tmp_path)
    secrets = tmp_path / 'home/shop-env/.secrets'
    secrets.parent.mkdir(parents=True)
    secrets.write_text('SHOP_API_KEY="k-1"\nOTHER_KEY=no\n')
    env = session.Session(c, initialization.Runtime(), {}, tmp_path)
    assert dispatch.secret_overrides(env, 'dev') == {'SHOP_API_KEY': 'k-1'}
    assert dispatch.secret_overrides(env, 'review') == {}
    secrets.unlink()
    assert dispatch.secret_overrides(env, 'fix') == {}


def test_hooks_are_host_paths_and_none_by_default(tmp_path):
    c = load_other(tmp_path)
    assert (c.repos[0].verify_hook, c.repos[0].e2e_hook, c.repos[0].gate_hook, c.repos[0].nightly_hook) == (None,) * 4
    c = load_other(tmp_path, OTHER.replace('verify_hook = ""', 'verify_hook = "hooks/verify.sh"')
                   .replace('nightly_hook = ""', 'nightly_hook = "~/shop-hooks/nightly.sh"'))
    assert c.repos[0].verify_hook == tmp_path / 'hooks/verify.sh'
    assert c.repos[0].nightly_hook == tmp_path / 'home/shop-hooks/nightly.sh'


def test_a_gate_hook_needs_a_pattern_for_what_an_issue_may_ask_it_to_run(tmp_path):
    gated = OTHER.replace('gate_hook = ""', 'gate_hook = "hooks/gate.sh"')
    with pytest.raises(config.ConfigError, match='gate_target'):
        load_other(tmp_path, gated)
    c = load_other(tmp_path, gated.replace('gate_target = ""', "gate_target = 'tests/[a-z_]+\\.py'"))
    assert c.repos[0].gate_target == 'tests/[a-z_]+\\.py'
    with pytest.raises(config.ConfigError, match='gate_option'):
        load_other(tmp_path, OTHER.replace('gate_option = ""', 'gate_option = "(["'))
