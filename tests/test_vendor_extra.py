"""Vendors besides the pool (quota.vendor_extra): Cursor runs only when it is listed, and preflight
checks only the CLIs of the vendors that may run."""
import os
from pathlib import Path
import stat

import pytest

from conftest import FULL
from baton import bootstrap, config, pick, session

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, text):
    path = tmp_path / 'config.toml'
    path.write_text(text)
    return path


def baseline(**replace):
    text = (ROOT / 'config.example.toml').read_text()
    for old, new in replace.items():
        text = text.replace(old, new)
    return text


def test_the_baseline_runs_no_vendor_outside_the_pool(tmp_path):
    c = config.load(ROOT / 'config.example.toml', home=tmp_path)
    assert c.quota.vendor_extra == ()
    assert pick.all_vendors(session.create(c, {})) == list(c.quota.vendor_pool)


def test_a_listed_cursor_is_a_candidate_after_the_pool(tmp_path):
    c = config.load(FULL, home=tmp_path)   # lists cursor
    assert pick.all_vendors(session.create(c, {})) == [*c.quota.vendor_pool, 'cursor']


@pytest.mark.parametrize('extra', ['["claude"]', '["cursor", "cursor"]', '["gemini"]', '"cursor"'])
def test_only_cursor_may_be_an_extra_vendor(tmp_path, extra):
    with pytest.raises(config.ConfigError, match='vendor_extra'):
        config.load(write(tmp_path, baseline(**{'vendor_extra = []': f'vendor_extra = {extra}'})), home=tmp_path)


def fake_tools(tmp_path):
    """gh, claude, codex and cursor-agent that record their name and succeed."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name in ('gh', 'claude', 'codex', 'cursor-agent'):
        tool = bin_dir / name
        tool.write_text(f'#!/bin/sh\necho {name} >> "{tmp_path}/called"\n')
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    return bin_dir


@pytest.mark.parametrize('pool,extra,expected', [
    ('["claude"]', '[]', ['gh', 'claude']),
    ('["codex"]', '[]', ['gh', 'codex', 'codex']),
    ('["claude", "codex"]', '["cursor"]', ['gh', 'claude', 'codex', 'codex', 'cursor-agent']),
])
def test_preflight_checks_the_vendors_that_may_run(tmp_path, monkeypatch, pool, extra, expected):
    bin_dir = fake_tools(tmp_path)
    text = baseline(**{'vendor_pool = ["claude", "codex"]': f'vendor_pool = {pool}',
                       'vendor_extra = []': f'vendor_extra = {extra}'})
    text = text.replace('path_prefix = ["~/.local/share/mise/shims", "~/.local/bin"]', f'path_prefix = ["{bin_dir}"]')
    source = write(tmp_path, text.replace('~/', f'{tmp_path}/h/'))
    monkeypatch.setenv('PATH', f'{bin_dir}:{os.defpath}')
    assert bootstrap.main(['--config', str(source), '--preflight']) == 0
    assert (tmp_path / 'called').read_text().split() == expected
