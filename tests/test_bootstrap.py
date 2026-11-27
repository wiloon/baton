"""Native typed entry tests: no legacy fixture adaptation or real agent calls."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import sys

import pytest
from baton import bootstrap, hooks, quota, session, vendors, tick
from baton.config import load
from conftest import full_text
from test_repos import TWO

ROOT = Path(__file__).resolve().parents[1]


def test_invalid_configuration_fails_before_initialization(tmp_path, monkeypatch, capsys):
    source = tmp_path/'config.toml'
    source.write_text('[scheduling]\nauto_merge = "private-value"\n')
    monkeypatch.setattr(bootstrap, 'prepare', lambda _: pytest.fail('initialized invalid config'))
    assert bootstrap.main(['--config',str(source)]) == 2
    assert 'private-value' not in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [source]


def test_validate_and_scan_listing_do_not_touch_state(tmp_path, monkeypatch, capsys):
    source = tmp_path/'config.toml'
    source.write_text(full_text().replace('~/',str(tmp_path/'absent')+'/'))
    monkeypatch.setattr(bootstrap,'prepare',lambda _:pytest.fail('unexpected prepare'))
    assert bootstrap.main(['--config',str(source),'--validate']) == 0
    assert bootstrap.main(['--config',str(source),'scan','list']) == 0
    assert 'api/billing' in capsys.readouterr().out
    assert not (tmp_path/'absent').exists()


def test_explicit_session_scopes_vendor_helpers(tmp_path):
    config = load(ROOT/'config.example.toml',home=tmp_path)
    caps = tuple((vid, 0.42 if vid == 'claude' else cap) for vid, cap in config.quota.caps)
    config = replace(config, quota=replace(config.quota, caps=caps), preferences=(('dev', ('codex',)),))
    explicit = session.create(config,{})
    config.paths.state.mkdir(parents=True)
    quota.record(config.paths.state, 'claude', {'seven_day': {'used': .5, 'resets_at': 2000}}, 1000)
    @session.scoped
    def invoke(env=None):
        chosen, rc = vendors.pick(config.paths.state, ['claude', 'codex'], 'dev',
                                  ['claude', 'codex'], now=1000)
        assert rc == 0 and chosen['vendor'] == 'codex'
        assert 'cap 42%' in chosen['reason']
        assert vendors.preference('dev') == (['codex'],'dev')
    invoke(explicit)


def test_chain_preserves_config_and_bound_and_closes_lock(tmp_path, monkeypatch):
    config = load(ROOT/'config.example.toml',home=tmp_path)
    config = replace(config,paths=replace(config.paths,state=tmp_path))
    explicit = session.create(config,{})
    invocation = tick.Tick(explicit)
    invocation.state.mkdir(parents=True,exist_ok=True)
    (invocation.state/'chain-next').touch()
    invocation.lock_file = (tmp_path/'lock').open('w')
    calls=[]
    invocation.exec=lambda executable,argv:calls.append((executable,argv))
    monkeypatch.setattr(invocation,'log',lambda _:None)
    invocation.chain_next()
    assert invocation.lock_file.closed
    assert calls[0][1][-4:] == ['--config',str(config.source),'--chain','1']
    assert explicit.runtime.chain == 1


@pytest.fixture
def walked(tmp_path, monkeypatch):
    """bootstrap.main over two configured repositories, each tick's item a merged partial slice."""
    source = tmp_path/'config.toml'
    source.write_text(TWO.replace('~/', f'{tmp_path}/home/'))
    seen = SimpleNamespace(source=source, worked=[], events=[], execs=[], flag=True)
    def work(self):
        seen.worked.append((self.gh_repo, bool(self.manual)))
        if not seen.flag:
            return None
        (self.state/'chain-next').touch()
        return self.after_item(80)
    monkeypatch.setattr(tick.Tick, 'work', work)
    monkeypatch.setattr(tick.Tick, 'scan_fallback', lambda self: None)
    monkeypatch.setattr(hooks.Telemetry, 'tick', lambda self, manual=False: seen.events.append(bool(manual)))
    monkeypatch.setattr(tick.os, 'execv', lambda executable, argv: seen.execs.append(argv))
    seen.run = lambda *argv: bootstrap.main(['--config', str(source), *argv])
    seen.flags = lambda: list((tmp_path/'home').rglob('chain-next'))
    return seen


def test_a_serve_child_ticks_only_its_repository_and_exits_80_instead_of_chaining(walked):
    assert walked.run('--repo', 'acme/shop', '--serve-child') == 80
    assert walked.worked == [('acme/shop', False)]
    assert walked.events == [False]
    assert not walked.execs and not walked.flags()


def test_a_serve_child_without_a_merged_slice_exits_0(walked):
    walked.flag = False
    assert walked.run('--repo', 'acme/shop', '--serve-child') == 0
    assert walked.worked == [('acme/shop', False)]


def test_an_assigned_serve_child_that_runs_no_item_exits_81(walked):
    walked.flag = False
    assert walked.run('--repo', 'acme/shop', '--serve-child', '--issue', '12') == 81
    assert walked.worked == [('acme/shop', False)]
    assert walked.events == [False]


def test_a_no_new_issue_child_that_runs_no_item_exits_81(walked):
    walked.flag = False
    assert walked.run('--repo', 'acme/shop', '--serve-child', '--no-new-issue') == 81
    assert walked.worked == [('acme/shop', False)]


def test_an_assigned_serve_child_that_runs_an_item_keeps_scheduled_semantics(walked):
    assert walked.run('--repo', 'acme/shop', '--serve-child', '--issue', '9') == 80
    assert walked.worked == [('acme/shop', False)]
    assert walked.events == [False]


def test_a_serve_child_honours_paused(walked):
    walked.source.write_text(walked.source.read_text().replace('paused = false', 'paused = true'))
    assert walked.run('--repo', 'acme/shop', '--serve-child') == 0
    assert not walked.worked


def test_the_timer_path_still_chains_by_re_exec(walked):
    assert walked.run('--repo', 'acme/shop') == 0
    assert walked.worked == [('acme/app', False)]
    assert walked.execs and walked.execs[0][-2:] == ['--chain', '1']


def test_service_style_minimal_environment_ignores_legacy_overrides(tmp_path):
    done = subprocess.run([sys.executable,'-m','baton','--config',str(ROOT/'config.example.toml'),'--validate'],cwd=ROOT,
                          env={'HOME':str(tmp_path),'PATH':os.defpath,'PAUSED':'1','VENDOR_POOL':'invalid'},
                          capture_output=True,text=True)
    assert done.returncode == 0 and done.stdout == 'Configuration valid\n'
    assert not list(tmp_path.iterdir())


def test_without_a_config_the_runner_says_how_to_make_one(tmp_path):
    missing = tmp_path/'config.toml'
    done = subprocess.run([sys.executable,'-m','baton','--config',str(missing),'--validate'],cwd=ROOT,
                          capture_output=True,text=True)
    assert done.returncode == 2
    assert done.stderr == f'Invalid configuration: {missing}: no such file; copy config.example.toml to it and edit it\n'
