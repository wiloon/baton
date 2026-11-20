"""Vendors besides the pool (quota.vendor_extra): Cursor runs only when it is listed, and preflight
checks only the CLIs of the vendors that may run."""
import os
import re
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


def without_cursor(text):
    text = re.sub(r"\n\[vendors\.cursor\].*?(?=\n\[)", "\n", text, flags=re.S)
    text = re.sub(r'\ncursor = \{[^\n]+\}\n', "\n", text)
    text = text.replace('dev = ["cursor", "codex", "claude"]', 'dev = ["codex", "claude"]')
    text = text.replace('model_cursor = "cursor_analyst"\n', "")
    return text


def pool_subset(text, pool, extra=()):
    keep = set(pool) | set(extra)
    for vendor in ('claude', 'codex', 'cursor'):
        if vendor not in keep:
            text = re.sub(rf"\n\[vendors\.{vendor}\].*?(?=\n\[)", "\n", text, flags=re.S)
            text = re.sub(rf'(?m)^{vendor} = \{{[^\n]+\}}\n', "", text)
            text = re.sub(rf'\nmodel_{vendor}\s*=\s*"[^"]*"\s*\n', "\n", text)
    fmt = lambda items: '[' + ', '.join(f'"{v}"' for v in items) + ']'
    order = [v for v in ('cursor', 'codex', 'claude') if v in keep]
    if order:
        text = re.sub(r'(?m)^dev = \[.*\]$', f'dev = {fmt(order)}', text, count=1)
    reviewers = [v for v in ('claude', 'codex') if v in keep]
    if reviewers:
        text = re.sub(r'(?m)^reviewer = \[.*\]$', f'reviewer = {fmt(reviewers)}', text, count=1)
    return text


def test_the_baseline_runs_no_vendor_outside_the_pool(tmp_path):
    source = write(tmp_path, without_cursor((ROOT / 'config.example.toml').read_text()))
    c = config.load(source, home=tmp_path)
    assert c.quota.vendor_extra == ()
    assert pick.all_vendors(session.create(c, {})) == list(c.quota.vendor_pool)


def test_a_listed_cursor_is_a_candidate_after_the_pool(tmp_path):
    c = config.load(FULL, home=tmp_path)   # lists cursor
    assert pick.all_vendors(session.create(c, {})) == [*c.quota.vendor_pool, 'cursor']


@pytest.mark.parametrize('mutate,match', [
    (lambda t: t.replace('[quota]\n', '[quota]\nvendor_extra = ["cursor", "cursor"]\n', 1), 'vendor_extra'),
    (lambda t: t + '\n[vendors.gemini]\nadapter = "gemini"\nroles = ["dev"]\nquota = "pool"\ncap = 0.5\nmax_minutes = 10\n', 'adapter'),
    (lambda t: t.replace('[quota]\n', '[quota]\nvendor_extra = "cursor"\n', 1), 'vendor_extra'),
])
def test_invalid_quota_lane_config_is_rejected(tmp_path, mutate, match):
    with pytest.raises(config.ConfigError, match=match):
        config.load(write(tmp_path, mutate((ROOT / 'config.example.toml').read_text())), home=tmp_path)


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
    (['claude'], [], ['gh', 'claude']),
    (['codex'], [], ['gh', 'codex', 'codex']),
    (['claude', 'codex'], ['cursor'], ['gh', 'claude', 'codex', 'codex', 'cursor-agent']),
])
def test_preflight_checks_the_vendors_that_may_run(tmp_path, monkeypatch, pool, extra, expected):
    bin_dir = fake_tools(tmp_path)
    text = pool_subset((ROOT / 'config.example.toml').read_text(), pool, extra)
    text = text.replace('path_prefix = ["~/.local/share/mise/shims", "~/.local/bin"]', f'path_prefix = ["{bin_dir}"]')
    source = write(tmp_path, text.replace('~/', f'{tmp_path}/h/'))
    monkeypatch.setenv('PATH', f'{bin_dir}:{os.defpath}')
    assert bootstrap.main(['--config', str(source), '--preflight']) == 0
    assert (tmp_path / 'called').read_text().split() == expected