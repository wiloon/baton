"""Stage 1 adapter capability routing (#112): dispatch, quota, probe and config for a new ID."""
import json
import os
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from baton import agents, config, dispatch, pick, quota, session, settle
from baton.common import Deferred
from conftest import full_text
from test_agents import FAKE_CLI, lines


STUB_STREAM = lines(
    {"type": "system", "subtype": "init", "model": "stub-1"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "done\nRESULT: ready"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "done\nRESULT: ready",
     "model": "stub-1"},
)

STUB_QUOTA = json.dumps({"type": "stub_quota", "seven_day": {"used": 0.25, "resets_at": 1_999_999_999}}) + "\n"


def with_stub(text):
    """Admit stub with the four role grants and matching routes."""
    text = text.replace(
        'cursor_analyst = "grok-4.7-high"\n',
        'cursor_analyst = "grok-4.7-high"\nstub_fast = "stub-1"\n',
    )
    for role in ("dev", "fix", "review", "spec_review"):
        text = text.replace(
            f"[models.routes.{role}]\n",
            f"[models.routes.{role}]\nstub = {{ model = \"stub_fast\", effort = \"low\" }}\n",
        )
    text = text.replace(
        "[vendors.cursor]\n",
        '[vendors.stub]\n'
        'adapter = "stub"\n'
        'roles = ["dev", "fixer", "reviewer", "spec_reviewer"]\n'
        'quota = "extra"\n'
        'cap = 0.9\n'
        'max_minutes = 30\n'
        'fallback_models = ["stub_fast"]\n'
        'quota_probe_model = "stub_fast"\n'
        '\n[vendors.cursor]\n',
    )
    text = text.replace('dev = ["cursor", "codex", "claude"]', 'dev = ["stub", "cursor", "codex", "claude"]')
    return text


@pytest.fixture
def stub_runner(tmp_path, monkeypatch):
    fake, bin_dir, state, logs = tmp_path / "fake", tmp_path / "bin", tmp_path / "state", tmp_path / "logs"
    home = tmp_path / "home"
    for directory in (fake, bin_dir, state, logs, home, tmp_path / "tel"):
        directory.mkdir()
    for cli in ("claude", "cursor-agent", "codex", "stub-agent"):
        (fake / cli).write_text(FAKE_CLI)
        (fake / cli).chmod(0o755)
    (fake / "timeout").write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    (fake / "timeout").chmod(0o755)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(with_stub(full_text()))
    for name, value in (("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}"), ("FAKE_DIR", str(fake)),
                        ("HOME", str(home)), ("CODEX_HOME", str(tmp_path / "codex-home"))):
        monkeypatch.setenv(name, value)
    cfg = config.load(cfg_path, home=home)
    cfg = replace(cfg, paths=replace(cfg.paths, state=state, logs=logs, self_dir=bin_dir))
    sess = session.create(cfg, repo=cfg.repos[0])
    sess.dirs.logs.mkdir(parents=True, exist_ok=True)
    sess.dirs.state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(session, "resolve", lambda value=None: sess if value is None else value)
    wt = tmp_path / "wt"
    wt.mkdir()
    git = ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True)

    class Runner:
        def __init__(self):
            self.env, self.state_dir, self.worktree = sess, state, wt
            self.root, self.config, self.logs = tmp_path, cfg, logs

        def plan(self, **plan):
            (fake / "plan.json").write_text(json.dumps(plan))

        def run(self, vendor="stub", role="dev", tag="t", prompt="p", **options):
            return dispatch.dispatch(vendor, role, prompt, tag, str(wt), env=sess, **options)

        def args(self):
            return json.loads((fake / "args.json").read_text())

        def child_env(self):
            return json.loads((fake / "env.json").read_text())

    return Runner()


def test_stub_is_registered_with_declared_capabilities():
    assert "stub" in agents.ADAPTERS
    stub = agents.ADAPTERS["stub"]
    assert stub.GITHUB_ACCESS == "runner"
    assert stub.FIX_RESUMABLE_VIA_REPLY is True
    assert stub.READONLY_RESET is True
    assert stub.IMAGES is False
    assert stub.QUOTA_WINDOWS == frozenset({"seven_day"})
    assert stub.QUOTA_ERROR


def test_config_load_admits_stub_and_rejects_unsupported_role(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(with_stub(full_text()))
    cfg = config.load(path, home=tmp_path / "home")
    assert "stub" in cfg.declared()
    assert cfg.allows("stub", "dev")
    assert cfg.allows("stub", "screenshot_reviewer") is False
    path.write_text(with_stub(full_text()).replace(
        'roles = ["dev", "fixer", "reviewer", "spec_reviewer"]',
        'roles = ["dev", "fixer", "reviewer", "spec_reviewer", "scan"]',
    ))
    with pytest.raises(config.ConfigError, match="scan"):
        config.load(path, home=tmp_path / "home")


def test_config_example_still_loads_without_stub(tmp_path):
    root = Path(__file__).resolve().parents[1]
    cfg = config.load(root / "config.example.toml", home=tmp_path / "home")
    assert set(cfg.declared()) == {"claude", "codex", "cursor"}
    assert "stub" not in cfg.declared()


def test_stub_dev_dispatch_uses_its_request_settings_and_reports_model(stub_runner):
    stub_runner.plan(stream=STUB_STREAM)
    out = stub_runner.run()
    assert (out["rc"], out["status"], out["outcome"]) == (0, "success", "RESULT: ready")
    assert out["model"] == "stub-1"
    args = stub_runner.args()
    assert args[args.index("--model") + 1] == "stub-1"
    assert args[args.index("--effort") + 1] == "low"
    assert stub_runner.child_env()["STUB_RUN_ROLE"] == "dev"
    assert out["log"].endswith("-stub.jsonl")


def test_stub_quota_error_defers_and_records_cooldown(stub_runner):
    stub_runner.plan(stream="stub quota exhausted\n", rc=1)
    out = stub_runner.run()
    assert (out["rc"], out["status"]) == (75, "quota")
    assert out["defer"].startswith("quota exhausted; cooldown until")
    assert (stub_runner.state_dir / "stub-cooldown-until").read_text().strip().isdigit()


def test_stub_auth_failure_records_credential(stub_runner, monkeypatch):
    monkeypatch.setattr(dispatch.agents, "preflight",
                        lambda vendor: "RESULT: failed Stub login required" if vendor == "stub" else None)
    out = stub_runner.run()
    assert (out["rc"], out["status"]) == (75, "auth")
    assert json.loads((stub_runner.state_dir / "credential-stub.json").read_text())["result"] == "auth_expired"


def test_stub_probe_uses_own_command_and_persists_window(stub_runner):
    stub_runner.plan(stream=STUB_QUOTA)
    state, logs = stub_runner.state_dir, stub_runner.logs
    assert settle.probe(str(state), str(logs), "stub", "stub-1") is True
    args = stub_runner.args()
    assert args[0] == "probe" and args[args.index("--model") + 1] == "stub-1"
    assert args[0] != "claude" and "exec" not in args
    snap = quota.load(state, "stub")
    assert snap["windows"]["seven_day"]["used"] == pytest.approx(0.25)
    assert quota.fresh(state, "stub", time.time(), 60)


def test_api_probe_calls_adapter_refresh_hook(stub_runner, monkeypatch):
    calls = []
    monkeypatch.setattr(agents.ADAPTERS["cursor"], "refresh_quota",
                        lambda state, now, **kw: calls.append(state) or {
                            "seven_day": {"used": 0.1, "resets_at": 1999999999}})
    settle.probe(str(stub_runner.state_dir), str(stub_runner.logs), "cursor", "")
    assert calls == [str(stub_runner.state_dir)]


def test_quota_cli_refresh_routes_through_adapter(stub_runner, monkeypatch, capsys):
    monkeypatch.setattr(agents.ADAPTERS["cursor"], "refresh_quota",
                        lambda state, now, **kw: {"seven_day": {"used": 0.5, "resets_at": 1999999999}})
    assert quota.main(["refresh", "cursor"]) == 0
    assert json.loads(capsys.readouterr().out)["seven_day"]["used"] == 0.5


def test_quota_cli_refresh_rejects_when_cursor_not_admitted(tmp_path, monkeypatch, capsys):
    text = re.sub(r"\n\[vendors\.cursor\].*?(?=\n\[quota\])", "\n", full_text(), flags=re.S)
    text = re.sub(r'\ncursor = \{[^\n]+\}\n', "\n", text)
    text = text.replace('dev = ["cursor", "codex", "claude"]', 'dev = ["codex", "claude"]')
    text = re.sub(r'\nmodel_cursor\s*=\s*"[^"]*"\s*\n', "\n", text)
    path = tmp_path / "config.toml"
    path.write_text(text)
    cfg = config.load(path, home=tmp_path / "home")
    sess = session.create(cfg, repo=cfg.repos[0])
    monkeypatch.setattr(session, "resolve", lambda value=None: sess if value is None else value)
    refreshed = []
    monkeypatch.setattr(agents.ADAPTERS["cursor"], "refresh_quota",
                        lambda *a, **k: refreshed.append(1) or {})
    assert quota.main(["refresh", "cursor"]) == 1
    assert "cursor" in capsys.readouterr().err
    assert refreshed == []


def test_raw_refresh_cursor_still_propagates_errors(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text('{"accessToken": "tok"}')
    with pytest.raises(OSError, match="boom"):
        quota.refresh_cursor(str(tmp_path), time.time(), auth=auth,
                             fetch=lambda token: (_ for _ in ()).throw(OSError("boom")))


@pytest.mark.parametrize(("current", "pinned"), [("stub", True), ("claude", False)])
def test_pick_defers_blocked_eligible_when_only_disallowed_vendor_is_free(stub_runner, monkeypatch, current, pinned):
    cfg = stub_runner.config
    cfg = replace(cfg, vendors=tuple(
        replace(v, roles=tuple(r for r in v.roles if r != "reviewer")) if v.id == "codex" else v
        for v in cfg.vendors))
    sess = session.create(cfg, repo=cfg.repos[0])
    monkeypatch.setattr(session, "resolve", lambda value=None: sess if value is None else value)
    state = stub_runner.state_dir
    for vendor in ("stub", "claude", "cursor"):
        (state / f"{vendor}-cooldown-until").write_text("1999999999\n")
    probes = []
    monkeypatch.setattr(pick.settle, "probe", lambda *a: probes.append(a[2]))
    with pytest.raises(Deferred, match="no vendor can run now"):
        pick.choose(sess, "reviewer", current, "contrib", ["stub", "claude", "codex", "cursor"], pinned=pinned)
    assert probes == []


def test_record_log_leaves_non_probe_run_vendor_state_untouched(stub_runner):
    log = stub_runner.root / "cursor.log"
    log.write_text('{"type":"result"}\n')
    quota.record_log(str(stub_runner.state_dir), "cursor", log, time.time())
    assert not (stub_runner.state_dir / "quota-cursor.json").exists()


def test_absent_followup_route_inherits_base_for_stub(tmp_path):
    text = with_stub(full_text())
    path = tmp_path / "config.toml"
    path.write_text(text)
    cfg = config.load(path, home=tmp_path / "home")
    base = cfg.models.route("review", "stub")
    follow = cfg.models.route("review", "stub", followup=True)
    assert follow == base


def test_claude_cursor_codex_configure_unchanged(stub_runner):
    env = stub_runner.env
    model, minutes, suffix, options = dispatch.configure("claude", "review", env)
    assert (model, suffix) == ("opus", "") and "budget" in options and "subagent_model" in options
    model, minutes, suffix, options = dispatch.configure("cursor", "dev", env)
    assert (model, suffix, options) == ("auto", "", {})
    model, minutes, suffix, options = dispatch.configure("codex", "review", env)
    assert suffix == "-codex" and "effort" in options and "add_dirs" in options
