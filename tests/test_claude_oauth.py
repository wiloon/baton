"""Claude's setup-token (#255): CLAUDE_CODE_OAUTH_TOKEN from `[vendors.claude] env_file` reaches every
`claude` baton starts, and nothing else. A fake `claude` on PATH records its environment."""
import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from baton import (advisor, agents, claude_cli, config, credentials, dispatch, pick, quota, quota_worker,
                   retrospective_analyst, session, settle)
from baton.config import ROUTE_ROLES
from conftest import full_text
from test_adapter_capabilities import STUB_STREAM, with_stub
from test_agents import CODEX_STREAM, FAKE_CLI, claude_stream

ROOT = Path(__file__).resolve().parents[1]
KEY = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN = "sk-ant-oat01-MARKER-255-token"
CLAUDE_TABLE = '[vendors.claude]\nadapter = "claude"\n'


def private(path, text, mode=0o600):
    path.write_text(text)
    path.chmod(mode)
    return path


def with_env_file(text, path):
    return text.replace(CLAUDE_TABLE, f'{CLAUDE_TABLE}env_file = "{path}"\n')


@pytest.fixture
def host(tmp_path, monkeypatch):
    """The real config loader and tool environment (session.create), with fake CLIs on PATH."""
    fake, bin_dir, state, logs, home = (tmp_path / n for n in ("fake", "bin", "state", "logs", "home"))
    for directory in (fake, bin_dir, state, logs, home):
        directory.mkdir()
    for cli in ("claude", "cursor-agent", "codex", "stub-agent"):
        (fake / cli).write_text(FAKE_CLI)
        (fake / cli).chmod(0o755)
    (fake / "timeout").write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    (fake / "timeout").chmod(0o755)
    for name, value in (("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}"), ("FAKE_DIR", str(fake)),
                        ("HOME", str(home)), ("CODEX_HOME", str(tmp_path / "codex-home"))):
        monkeypatch.setenv(name, value)
    env_file = private(tmp_path / "claude.env", f"{KEY}={TOKEN}\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    git = ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True)

    class Host:
        def __init__(self):
            self.root, self.fake, self.state, self.logs, self.env_file, self.wt = tmp_path, fake, state, logs, env_file, wt
            self.configure(env_file)

        def configure(self, path):
            text = with_stub(full_text())
            source = tmp_path / "config.toml"
            source.write_text(with_env_file(text, path) if path else text)
            cfg = config.load(source, home=home)
            self.config = replace(cfg, paths=replace(cfg.paths, state=state, logs=logs, self_dir=bin_dir))
            self.session = session.create(self.config, repo=self.config.repos[0])
            self.session.dirs.logs.mkdir(parents=True, exist_ok=True)
            monkeypatch.setattr(session, "resolve", lambda value=None: self.session if value is None else value)

        def plan(self, **plan):
            (fake / "plan.json").write_text(json.dumps(plan))

        def dispatch(self, vendor="claude", role="review"):
            self.plan(stream=CODEX_STREAM if vendor == "codex" else STUB_STREAM if vendor == "stub"
                      else claude_stream("fine\nREVIEW: pass"))
            return dispatch.dispatch(vendor, role, "p", "t", str(wt), settings="/s/settings.json",
                                     output=str(tmp_path / "out.md"), env=self.session)

        def child_env(self):
            return json.loads((fake / "env.json").read_text())

        def called(self):
            return (fake / "calls").read_text().split() if (fake / "calls").exists() else []

    return Host()


# --- config ------------------------------------------------------------------------------------

def test_env_file_is_a_claude_vendor_path_and_an_error_elsewhere(host, tmp_path):
    assert host.config.vendor("claude").env_file == host.env_file
    assert host.config.vendor("codex").env_file is None
    source = tmp_path / "codex.toml"
    source.write_text(full_text().replace('[vendors.codex]\nadapter = "codex"\n',
                                          '[vendors.codex]\nadapter = "codex"\nenv_file = "/x/codex.env"\n'))
    with pytest.raises(config.ConfigError, match=r"vendors.codex.env_file: unsupported by codex"):
        config.load(source, home=tmp_path)


def test_the_shipped_example_loads_without_a_claude_env_file():
    assert config.load(ROOT / "config.example.toml").vendor("claude").env_file is None
    assert '# env_file = "~/.config/baton/claude.env"' in (ROOT / "config.example.toml").read_text()


# --- dispatched runs ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ROUTE_ROLES)
def test_every_dispatched_claude_role_gets_the_token(host, role):
    host.dispatch("claude", role)
    assert host.child_env()[KEY] == TOKEN
    args = (host.fake / "args.json").read_text()
    runner_log = (host.logs / "runner.log").read_text()
    assert "start t (claude" in runner_log
    assert TOKEN not in args and TOKEN not in runner_log


@pytest.mark.parametrize("vendor", ["codex", "cursor", "stub"])
def test_other_vendors_never_see_the_token_even_from_the_runner_environment(host, monkeypatch, vendor):
    monkeypatch.setenv(KEY, TOKEN)
    host.configure(host.env_file)   # the tool environment is built from the runner's
    assert KEY not in host.session.tools   # verify hooks and CI commands run with these
    host.dispatch(vendor, "review")
    assert host.called() and KEY not in host.child_env()


def test_without_an_env_file_the_claude_child_is_unchanged(host):
    host.configure(None)
    host.dispatch("claude", "review")
    assert KEY not in host.child_env()
    assert claude_cli.oauth_env(host.config.vendor("claude")) == {}
    assert claude_cli.preflight(host.config.vendor("claude")) is None


@pytest.mark.parametrize("mode", [0o644, 0o000])
def test_a_bad_env_file_settles_claude_as_auth_without_starting_it(host, mode):
    private(host.env_file, f"{KEY}={TOKEN}\n", mode)
    if mode == 0 and os.access(host.env_file, os.R_OK):
        pytest.skip("running as root: every file is readable")
    out = host.dispatch("claude", "review")
    assert (out["rc"], out["status"]) == (75, "auth")
    assert str(host.env_file) in out["outcome"] and TOKEN not in out["outcome"]
    assert host.called() == []
    assert json.loads((host.state / "credential-claude.json").read_text())["result"] == "auth_expired"


# --- quota probe -------------------------------------------------------------------------------

def test_the_quota_probe_gets_the_token(host):
    host.plan(stream=claude_stream("ok"))
    settle.probe(str(host.state), str(host.logs), "claude", "haiku", settings=host.config.vendor("claude"))
    assert host.child_env()[KEY] == TOKEN


def test_other_vendors_probes_never_see_the_token(host, monkeypatch):
    monkeypatch.setenv(KEY, TOKEN)
    host.configure(host.env_file)
    host.plan(stream="")
    settle.probe(str(host.state), str(host.logs), "stub", "stub-1", settings=host.config.vendor("stub"))
    assert host.called() == ["stub-agent"] and KEY not in host.child_env()


def test_a_bad_env_file_records_an_expired_login_and_opens_no_probe_run(host, monkeypatch):
    host.env_file.chmod(0o644)
    runs = []
    monkeypatch.setattr(settle.telemetry, "from_env",
                        lambda: SimpleNamespace(begin=lambda *a, **k: runs.append(a), end=lambda *a: runs.append(a)))
    settle.probe(str(host.state), str(host.logs), "claude", "haiku", settings=host.config.vendor("claude"))
    assert host.called() == [] and runs == [] and not list(host.logs.glob("*quota-probe*"))
    assert json.loads((host.state / "credential-claude.json").read_text())["result"] == "auth_expired"


def test_pick_and_the_quota_worker_survive_a_bad_env_file(host, monkeypatch):
    host.env_file.chmod(0o644)
    only = (v for v in host.config.vendors if v.id == "claude")
    host.config = replace(host.config, vendors=tuple(only), quota=replace(
        host.config.quota, vendor_pool=("claude",), vendor_extra=(), caps=(("claude", 0.9),)))
    host.session.config = host.config
    assert quota_worker.run(host.config) == [{"vendor": "claude", "state": "probe_incomplete"}]
    (host.state / "quota-probes" / "claude.json").unlink()
    quota.mark_probed(str(host.state), "claude", 0)
    try:
        pick.choose(host.session, "dev", "", "pool", ["claude"])
    except (pick.Deferred, pick.Exhausted):
        pass
    assert host.called() == []


# --- retrospective analyst ---------------------------------------------------------------------

def analyst_host(host, vendor):
    from test_retrospectives import seed
    (host.fake / "claude").write_text(FAKE_ANALYST)
    (host.fake / "codex").write_text(FAKE_ANALYST)
    now = time.time()
    for v in ("claude", "codex"):
        (host.state / f"credential-{v}.json").write_text(json.dumps(dict(result="authenticated", observed_at=now)))
        (host.state / f"quota-{v}.json").write_text(json.dumps(dict(observed_at=now, windows={
            w: dict(used=0.1, observed_at=now, resets_at=now + 10000) for w in ("five_hour", "seven_day")})))
    kept = tuple(v for v in host.config.vendors if v.id == vendor)
    host.config = replace(host.config, vendors=kept,
                          environment=replace(host.config.environment, path_prefix=()),
                          retrospectives=replace(host.config.retrospectives, enabled=True),
                          quota=replace(host.config.quota, vendor_pool=(vendor,), vendor_extra=(),
                                        caps=((vendor, 0.9),)))
    seed(host.state)
    from baton import retrospectives
    root = host.state / "retrospectives"
    return lambda: retrospectives.run(host.config, host.state, root, root / "reports")


FAKE_ANALYST = FAKE_CLI.split("\n", 1)[0] + '''
import json, os, pathlib, sys
fake = pathlib.Path(__file__).parent   # the analyst environment drops FAKE_DIR
name = pathlib.Path(__file__).name
with (fake / "analyst.jsonl").open("a") as f:
    f.write(json.dumps(dict(name=name, version="--version" in sys.argv, env=dict(os.environ))) + "\\n")
if "--version" in sys.argv:
    print("2.1.100")
    sys.exit(0)
sys.stdin.read()
reply = json.dumps(dict(summary="No actionable finding.", hypotheses=[]))
if name == "codex":
    print(json.dumps(dict(type="turn_context", payload={"model": "gpt-6.1-sol"})))
    print(json.dumps(dict(type="item.completed", item={"type": "agent_message", "text": reply})))
    print(json.dumps(dict(type="turn.completed", usage={"input_tokens": 4, "output_tokens": 3})))
else:
    print(json.dumps(dict(type="result", subtype="success", result=reply, usage={"input_tokens": 4},
                          modelUsage={"claude-opus-test": {}}, total_cost_usd=0.01)))
'''


def analyst_calls(host):
    path = host.fake / "analyst.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_claude_analyst_gets_the_token_for_version_and_run(host):
    run = analyst_host(host, "claude")
    assert run()[0]["state"] == "success"
    calls = analyst_calls(host)
    assert [c["version"] for c in calls] == [True, False]
    assert all(c["env"][KEY] == TOKEN for c in calls)


def test_another_vendors_analyst_never_sees_the_token(host, monkeypatch):
    monkeypatch.setenv(KEY, TOKEN)
    run = analyst_host(host, "codex")
    run()
    calls = analyst_calls(host)
    assert calls and all(KEY not in c["env"] for c in calls)
    assert KEY not in retrospective_analyst.environment(host.config, host.config.vendor("codex"))


def test_a_bad_env_file_fails_the_analysis_without_starting_claude(host):
    host.env_file.chmod(0o644)
    run = analyst_host(host, "claude")
    assert run()[0]["state"] == "failed"
    assert analyst_calls(host) == []
    [attempt] = [json.loads(p.read_text()) for p in (host.state / "retrospectives" / "attempts").glob("*.json")]
    assert attempt["state"] == "credential_error" and attempt["rc"] != 0
    assert str(host.env_file) in attempt["failure"] and TOKEN not in json.dumps(attempt)


# --- advisor -----------------------------------------------------------------------------------

def test_the_advisor_claude_call_gets_the_token_and_redacts_it(host, monkeypatch):
    seen = []

    def process(command, prompt, workspace, env, seconds, max_bytes, lease_fd=None):
        seen.append((command, env))
        return "success", b"", 0, {}
    monkeypatch.setattr(retrospective_analyst, "bounded_process", process)
    cfg = replace(advisor.settings(host.session), timeout_seconds=5)
    with pytest.raises(advisor.BackendError):
        advisor.call_claude(cfg, host.session, "snapshot", ("extra_round",))
    [(command, env)] = seen
    assert env[KEY] == TOKEN and TOKEN not in " ".join(command)
    assert TOKEN in advisor.secret_values(host.session)   # only in the file, not os.environ
    assert TOKEN not in advisor.redact(f"excerpt {TOKEN}", advisor.secret_values(host.session))


def test_a_bad_env_file_makes_the_advisor_fall_back_without_raising(host):
    host.env_file.chmod(0o644)
    cfg = advisor.settings(host.session)
    result = advisor.attempt_backend(host.session, cfg, "claude", "", "", "snapshot", ("extra_round",),
                                     category=None, values=set(), sources={}, cut_rows=[], recordable="")
    assert not result.called and str(host.env_file) in result.failure
    assert advisor.claude_unavailable(host.session) == result.failure
    assert TOKEN not in result.failure


# --- the guard ---------------------------------------------------------------------------------

@pytest.mark.parametrize("tool", [
    {"tool_name": "Read", "tool_input": {"file_path": "HOME/.config/baton/claude.env"}},
    {"tool_name": "Bash", "tool_input": {"command": "cat ~/.config/baton/claude.env"}},
])
def test_agents_cannot_read_the_claude_env_file(tmp_path, tool):
    text = json.dumps(tool).replace("HOME", str(tmp_path))
    env = {**os.environ, "HOME": str(tmp_path), "BATON_AGENT": "1", "BATON_AGENT_ROLE": "dev",
           "BATON_AGENT_WORKTREE": str(tmp_path / "wt"), "BATON_AGENT_LOGS": str(tmp_path)}
    out = subprocess.run(["bash", str(ROOT / "bin/guard.sh")], input=text, env=env, capture_output=True, text=True)
    decision = json.loads(out.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny" and "secrets are off limits" in decision["permissionDecisionReason"]
