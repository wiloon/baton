"""Per-vendor five_hour_max override (#148)."""
import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from baton import config, quota, session, vendor_policy, vendors
from conftest import full_text

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_793_000_000
TEXT = full_text()


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def with_overrides(text, *, codex=None, cursor=None):
    if codex is not None:
        text = text.replace(
            '[vendors.codex]\nadapter = "codex"\n',
            f'[vendors.codex]\nadapter = "codex"\nfive_hour_max = {codex}\n',
            1,
        )
    if cursor is not None:
        text = text.replace(
            '[vendors.cursor]\nadapter = "cursor"\n',
            f'[vendors.cursor]\nadapter = "cursor"\nfive_hour_max = {cursor}\n',
            1,
        )
    return text


def test_example_and_explicit_overrides_validate(tmp_path):
    example = write(tmp_path, TEXT)
    assert subprocess.run(
        [sys.executable, "-m", "baton", "--config", str(example), "--validate"],
        cwd=ROOT, capture_output=True, text=True,
    ).returncode == 0
    path = write(tmp_path, with_overrides(TEXT, codex=1.0, cursor=1.0))
    done = subprocess.run(
        [sys.executable, "-m", "baton", "--config", str(path), "--validate"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stderr
    c = config.load(path, home=tmp_path)
    assert c.vendor("codex").five_hour_max == 1.0
    assert c.vendor("cursor").five_hour_max == 1.0
    assert c.vendor("claude").five_hour_max is None
    assert c.quota.five_hour_max_of("codex") == 1.0
    assert c.quota.five_hour_max_of("claude") == 0.9
    assert c.quota.five_hour_max_of("cursor") == 1.0


@pytest.mark.parametrize("value", ["0", "1.01", "inf", "nan", "true"])
def test_invalid_five_hour_max_is_rejected(tmp_path, value):
    path = write(tmp_path, with_overrides(TEXT, codex=value))
    done = subprocess.run(
        [sys.executable, "-m", "baton", "--config", str(path), "--validate"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert done.returncode == 2
    assert "vendors.codex.five_hour_max" in done.stderr


def snap(state, vendor, *, week=0.1, five=None):
    windows = {"seven_day": {"used": week, "resets_at": NOW + 302400, "observed_at": NOW}}
    if five is not None:
        windows["five_hour"] = {"used": five, "resets_at": NOW + 3600, "observed_at": NOW}
    Path(state, f"quota-{vendor}.json").write_text(json.dumps({
        "observed_at": NOW, "windows": windows}))


def load_override_config(tmp_path, *, codex=1.0, cursor=1.0):
    return config.load(write(tmp_path, with_overrides(TEXT, codex=codex, cursor=cursor)),
                       home=tmp_path)


def test_choose_uses_per_vendor_five_hour_max(tmp_path):
    settings = load_override_config(tmp_path).quota
    state = tmp_path / "state"
    state.mkdir()
    snap(state, "codex", week=0.2, five=0.95)
    snap(state, "claude", week=0.2, five=0.90)
    chosen, summary = vendor_policy.choose(
        state, settings, ("claude", "codex"), ["codex", "claude"], NOW)
    assert chosen == "codex"
    assert "claude=blocked(5h 90% >= 90%)" in summary
    assert "codex=" in summary and "blocked" not in summary.split("codex=")[1].split()[0]
    snap(state, "codex", week=0.2, five=1.0)
    chosen, summary = vendor_policy.choose(
        state, settings, ("claude", "codex"), ["codex", "claude"], NOW)
    assert chosen is None
    assert "codex=blocked(5h 100% >= 100%)" in summary
    global_only = config.load(write(tmp_path, TEXT), home=tmp_path).quota
    snap(state, "codex", week=0.2, five=0.95)
    snap(state, "claude", week=0.2, five=0.10)
    chosen, summary = vendor_policy.choose(
        state, global_only, ("claude", "codex"), ["codex", "claude"], NOW)
    assert chosen == "claude"
    assert "codex=blocked(5h 95% >= 90%)" in summary


def test_cursor_weekly_only_ignores_five_hour_override(tmp_path):
    settings = load_override_config(tmp_path).quota
    state = tmp_path / "state"
    state.mkdir()
    snap(state, "cursor", week=0.50)
    chosen, summary = vendor_policy.choose(state, settings, (), ["cursor"], NOW)
    assert chosen == "cursor"
    snap(state, "cursor", week=0.95)
    chosen, summary = vendor_policy.choose(state, settings, (), ["cursor"], NOW)
    assert chosen is None
    assert "cursor=blocked(week 95% >= cap 95%)" in summary


def test_quota_pick_cli_uses_per_vendor_limits(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    state.mkdir()
    snap(state, "codex", week=0.2, five=0.95)
    snap(state, "claude", week=0.2, five=0.90)
    cfg = load_override_config(tmp_path)
    cfg = replace(cfg, paths=replace(cfg.paths, state=state))
    env = session.create(cfg, repo=cfg.repos[0])

    def resolve(value=None):
        return value if isinstance(value, session.Session) else env

    monkeypatch.setattr(session, "resolve", resolve)
    monkeypatch.setattr(quota.time, "time", lambda: NOW)
    assert quota.main(["pick", "codex", "claude"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "codex"
    assert "claude=blocked(5h 90% >= 90%)" in out[1]


def test_strict_availability_uses_effective_five_hour_max(tmp_path):
    settings = load_override_config(tmp_path).quota
    state = tmp_path / "state"
    state.mkdir()
    snap(state, "codex", week=0.2, five=0.95)
    quota.credential(state, "codex", "authenticated", NOW)
    avail = vendor_policy.availability(state, settings, "codex", NOW, strict=True)
    assert avail.reason == ""
    snap(state, "codex", week=0.2, five=1.0)
    avail = vendor_policy.availability(state, settings, "codex", NOW, strict=True)
    assert avail.reason == "quota_exhausted"
    assert avail.horizon == vendor_policy.SHORT
    assert "5h 100% >= 100%" in avail.detail


def test_telemetry_quota_metrics_use_effective_five_hour_max(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "telemetry_five_hour", ROOT / "bin" / "telemetry.py")
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)
    state = tmp_path / "state"
    state.mkdir()
    path = write(tmp_path, with_overrides(TEXT, codex=1.0, cursor=1.0))
    snap(state, "codex", week=0.2, five=0.95)
    snap(state, "claude", week=0.2, five=0.90)
    snap(state, "cursor", week=0.4)
    values = {}
    effective = t._runner_config(path)
    t.quota_metrics(state, effective,
                    lambda name, value, labels: values.__setitem__((name, labels), value), NOW)

    def value(name, vendor, window=None):
        labels = '{agent="' + vendor + '"' + (',window="' + window + '"' if window else '') + '}'
        return values[(name, labels)]

    assert value("quota_gate", "codex") == 1
    assert value("quota_runner_cap_ratio", "codex", "five_hour") == 1.0
    assert value("quota_runner_headroom_ratio", "codex", "five_hour") == pytest.approx(0.05)
    assert value("quota_gate", "claude") == 2
    assert value("quota_runner_cap_ratio", "claude", "five_hour") == 0.9
    assert value("quota_runner_headroom_ratio", "claude", "five_hour") == 0
    assert value("quota_window_state", "cursor", "five_hour") == 4


def test_spec_revision_waits_for_codex_at_effective_cap(tmp_path, monkeypatch):
    cfg = load_override_config(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    cfg = replace(cfg, paths=replace(cfg.paths, state=state))
    env = session.create(cfg, repo=cfg.repos[0])

    def resolve(value=None):
        return value if isinstance(value, session.Session) else env

    monkeypatch.setattr(session, "resolve", resolve)
    snap(state, "codex", week=0.2, five=0.95)
    snap(state, "claude", week=0.2, five=0.10)
    result, rc = vendors.pick(state, ["claude", "codex"], "reviser", ["claude", "codex"],
                              exclude=["claude"], by="contrib", now=NOW,
                              other_model="claude-sonnet-4-6")
    assert (result["vendor"], rc) == ("codex", 0)
    details = vendors.selection_details(state, ["claude", "codex"], ["claude", "codex"],
                                        "codex", "reviser", now=NOW)
    assert "codex[" in details and "gate=open" in details
    snap(state, "codex", week=0.2, five=1.0)
    result, rc = vendors.pick(state, ["claude", "codex"], "reviser", ["claude", "codex"],
                              exclude=["claude"], by="contrib", now=NOW,
                              other_model="claude-sonnet-4-6")
    assert (result["vendor"], rc) == ("", 75)
    assert "waiting for codex" in result["reason"]
    assert "5h 100% >= 100%" in result["reason"]
    details = vendors.selection_details(state, ["claude", "codex"], ["claude", "codex"],
                                        "", "reviser", now=NOW)
    assert "5h 100% >= 100%" in details
