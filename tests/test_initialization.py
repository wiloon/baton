import re
from pathlib import Path

from conftest import FULL
from baton.config import load
from baton.initialization import Runtime, cache_directories, child_environment, prepare, skills_directory

ROOT = Path(__file__).resolve().parents[1]


def test_minimal_service_environment_and_no_secret_reads(tmp_path):
    c = load(ROOT/'config.example.toml', home=tmp_path)
    env = child_environment(c, {})
    assert env['PATH'].startswith(str(tmp_path/'.local/share/mise/shims'))
    assert env['GOCACHE'] == str(tmp_path/'.cache/go-build')
    assert env['ENABLE_CLAUDEAI_MCP_SERVERS'] == 'false'
    assert env['COREPACK_ENABLE_DOWNLOAD_PROMPT'] == '0'
    assert 'APP_SECRET_KEY' not in env and not list(tmp_path.iterdir())


def test_ambient_scheduler_variables_do_not_leak_to_tools(tmp_path):
    c = load(FULL, home=tmp_path)
    env = child_environment(c, {'PATH': f'{FULL.parent}/.venv/bin:/usr/bin', 'MODEL_CODEX_DEV': 'wrong',
        'QUOTA_CAP_CLAUDE': '0', 'PREFER_DEV': 'wrong', 'BATON_CHAIN': '5', 'GH_TOKEN': 'credential',
        'PYTHONPATH': 'wrong', 'VIRTUAL_ENV': 'wrong', 'APP_SECRET_KEY': 'old-secret'})
    assert all(k not in env for k in ('MODEL_CODEX_DEV','QUOTA_CAP_CLAUDE','PREFER_DEV','BATON_CHAIN',
                                     'PYTHONPATH','VIRTUAL_ENV','APP_SECRET_KEY'))
    assert env['GH_TOKEN'] == 'credential' and f'{FULL.parent}/.venv/bin' not in env['PATH']


def test_runtime_is_distinct_per_invocation():
    first, second = Runtime(), Runtime()
    first.chain = 2
    first.issue = '7'
    assert second.chain == 0 and second.issue == ''


def test_skills_discovery_and_runtime_lock_directory(tmp_path):
    c = load(ROOT/'config.example.toml', home=tmp_path)
    assert skills_directory(tmp_path) is None
    for version in ('1.9.0', '1.10.0'):
        (tmp_path/version/'skills').mkdir(parents=True)
    assert skills_directory(tmp_path) == tmp_path/'1.10.0/skills'
    assert cache_directories(c, {}, 1000)[-1] == Path('/tmp/pnpm-store-operation-locks-1000')
    assert cache_directories(c, {'XDG_RUNTIME_DIR':'/run/user/1000'},1000)[-1] == Path('/run/user/1000/pnpm-store-operation-locks-1000')


def test_prepare_is_explicit_and_repeatable(tmp_path):
    c = load(ROOT/'config.example.toml', home=tmp_path)
    assert not c.paths.state.exists()
    prepare(c)
    prepare(c)
    assert c.paths.ctx.is_dir() and c.environment.gotmpdir.is_dir()


def test_prompt_retention_keeps_recent_files_and_screenshot_directories(tmp_path):
    from dataclasses import replace
    import os
    from baton.initialization import expire_context
    config = load(ROOT/'config.example.toml', home=tmp_path)
    config = replace(config, paths=replace(config.paths, ctx=tmp_path))
    ctx = tmp_path/'acme/app'   # each repository's own ctx
    ctx.mkdir(parents=True)
    old = ctx/'old-prompt.md'
    recent = ctx/'recent-prompt.md'
    shots = ctx/'pr-1-shots'
    old.write_text('old')
    recent.write_text('recent')
    shots.mkdir()
    now = 1800000000
    os.utime(old,(now-15*86400,now-15*86400))
    os.utime(recent,(now,now))
    os.utime(shots,(now-15*86400,now-15*86400))
    expire_context(config,now)
    assert not old.exists() and recent.exists() and shots.is_dir()


def test_empty_host_settings_leave_the_tools_their_own(tmp_path):
    text = (ROOT/'config.example.toml').read_text()
    for key in ('ssl_cert_file', 'node_extra_ca_certs', 'goproxy'):
        text = re.sub(rf'(?m)^{key} = .*$', f'{key} = ""', text)
    (tmp_path/'config.toml').write_text(text)
    c = load(tmp_path/'config.toml', home=tmp_path)
    env = child_environment(c, {'SSL_CERT_FILE': '/etc/ssl/ambient.pem'})
    assert env['SSL_CERT_FILE'] == '/etc/ssl/ambient.pem'
    assert all(k not in env for k in ('GIT_SSL_CAINFO', 'NODE_EXTRA_CA_CERTS', 'GOPROXY'))


def test_a_host_ca_bundle_reaches_git_too(tmp_path):
    text = (ROOT/'config.example.toml').read_text()
    text = re.sub(r'(?m)^ssl_cert_file = .*$', 'ssl_cert_file = "~/ca.crt"', text)
    text = re.sub(r'(?m)^goproxy = .*$', 'goproxy = "https://proxy.example/,direct"', text)
    (tmp_path/'config.toml').write_text(text)
    env = child_environment(load(tmp_path/'config.toml', home=tmp_path), {})
    assert env['SSL_CERT_FILE'] == env['GIT_SSL_CAINFO'] == str(tmp_path/'ca.crt')
    assert env['GOPROXY'] == 'https://proxy.example/,direct'
