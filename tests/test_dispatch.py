"""baton.dispatch and baton.hooks: one run from start to settled, with the telemetry
record around it, against fake CLIs and a fake bin/telemetry.py. Ported from the telemetry
and dispatch sections of tests/roles.sh and tests/cooldown.sh."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from baton import dispatch, hooks
from conftest import in_flight
from test_agents import CODEX_STREAM, FAKE_CLI, claude_stream, lines

FAKE_TELEMETRY = f"""#!{sys.executable}
import json, os, pathlib, sys
root = pathlib.Path(os.environ["FAKE_TELEMETRY"])
args = sys.argv[3:]
with open(root / "calls", "a") as f:
    f.write(json.dumps(args) + "\\n")
if (root / "fails").exists():
    sys.exit(1)
action = args[2]
if action == "begin":
    print("12345678-1234-1234-1234-123456789abc 1234567890abcdef1234567890abcdef 1234567890abcdef pipe-1")
elif action == "task":
    print("pipe-1")
elif action == "must-fix":
    print(2)
elif action == "fix-answers":
    print("3 1")
elif action == "ci-result":
    print("pass")
"""


@pytest.fixture
def runner(tmp_path, monkeypatch):
    fake, bin_dir, state, logs = tmp_path / "fake", tmp_path / "bin", tmp_path / "state", tmp_path / "logs"
    for directory in (fake, bin_dir, state, logs, tmp_path / "tel"):
        directory.mkdir()
    for cli in ("claude", "cursor-agent", "codex"):
        (fake / cli).write_text(FAKE_CLI)
        (fake / cli).chmod(0o755)
    (fake / "timeout").write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    (fake / "timeout").chmod(0o755)
    (bin_dir / "telemetry.py").write_text(FAKE_TELEMETRY)
    (bin_dir / "telemetry.py").chmod(0o755)
    for name, value in (("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}"), ("FAKE_DIR", str(fake)),
                        ("FAKE_TELEMETRY", str(tmp_path / "tel")), ("HOME", str(tmp_path / "home")),
                        ("CODEX_HOME", str(tmp_path / "codex-home")), ("STATE", str(state)),
                        ("LOGS", str(logs)), ("GH_REPO", "owner/app")):
        monkeypatch.setenv(name, value)
    wt = tmp_path / "wt"
    wt.mkdir()
    git = ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True)

    class Runner:
        env = {"STATE": str(state), "LOGS": str(logs), "SELF_DIR": str(bin_dir), "GH_REPO": "owner/app",
               "MAX_MINUTES": "1", "CURSOR_MAX_MINUTES": "1", "MODEL_REVIEW": "opus", "BUDGET_REVIEW": "4",
               "MODEL_CURSOR": "auto", "MODEL_CODEX_REVIEW": "gpt-test", "MODEL_SPEC": "opus", "BUDGET_SPEC": "4",
               "COOLDOWN_HOURS": "5", "ISSUE": "142", "PR": "168", "ROUND": "0",
               "BATON_TELEMETRY_ITEM": str(tmp_path / "item.json")}
        root, state_dir, worktree = tmp_path, state, wt

        def plan(self, **plan):
            (fake / "plan.json").write_text(json.dumps(plan))

        def enable_telemetry(self):
            (bin_dir / "telemetry-enabled").touch()

        def run(self, vendor="claude", role="review", tag="t", prompt="p", **options):
            return dispatch.dispatch(vendor, role, prompt, tag, str(wt), env={**self.env, **options.pop("env", {})},
                                     settings="/s/settings.json", **options)

        def runner_log(self):
            path = logs / "runner.log"
            return [line.split(" ", 3)[3] for line in path.read_text().splitlines()] if path.exists() else []   # after time and [owner/name]

        def child_env(self):
            return json.loads((fake / "env.json").read_text())

        def calls(self):
            path = tmp_path / "tel" / "calls"
            return [json.loads(line)[2:] for line in path.read_text().splitlines()] if path.exists() else []

        def fail_telemetry(self):
            (tmp_path / "tel" / "fails").touch()

    return Runner()


# --- dispatch ----------------------------------------------------------------------------

def test_a_run_is_counted_logged_and_settled(runner):
    runner.plan(stream=claude_stream("fine\nREVIEW: pass"))
    out = runner.run()
    assert (out["rc"], out["status"], out["outcome"], out["defer"]) == (0, "success", "REVIEW: pass", "")
    assert "fine" in out["reply"]
    assert (runner.state_dir / f"runs-{__import__('time').strftime('%F')}").read_text() == "1\n"
    log = runner.runner_log()
    assert log[0].startswith("start t (claude review, opus) in ") and log[1].startswith("end t rc=0 ")
    assert log[2] == "t: vendor=claude outcome=success rc=0"
    assert json.loads((runner.state_dir / "credential-claude.json").read_text())["result"] == "authenticated"


def test_a_vendor_in_cooldown_defers_without_running(runner):
    (runner.state_dir / "claude-cooldown-until").write_text("1999999999\n")
    out = runner.run()
    assert (out["rc"], out["status"], out["outcome"], out["reply"]) == (75, "deferred", "", None)
    assert out["defer"] == "cooldown until epoch 1999999999"
    assert not (runner.root / "fake" / "calls").exists() and runner.runner_log() == []
    assert not list(runner.state_dir.glob("runs-*"))


def test_an_unknown_vendor_fails_before_anything_else(runner, capsys):
    out = runner.run("gemini")
    assert (out["rc"], out["status"], out["outcome"]) == (1, "", "RESULT: failed unknown vendor gemini")
    assert "unknown vendor gemini" in capsys.readouterr().err and not list(runner.state_dir.glob("*runs-*"))


def test_a_quota_error_rests_the_vendor_and_defers(runner):
    runner.plan(stream="You have hit your usage limit\n", rc=1)
    out = runner.run()
    assert (out["rc"], out["status"]) == (75, "quota") and out["defer"].startswith("quota exhausted; cooldown until")
    assert (runner.state_dir / "claude-cooldown-until").read_text().strip().isdigit()
    assert runner.run()["status"] == "deferred"


def test_a_failed_login_before_the_run_is_an_auth_deferral(runner, monkeypatch):
    monkeypatch.setenv("FAKE_LOGIN_RC", "1")
    out = runner.run("codex", "review", env={"CODEX_MAX_MINUTES": "1"})
    assert (out["rc"], out["status"]) == (75, "auth") and out["defer"].startswith("authentication failed")
    assert json.loads((runner.state_dir / "credential-codex.json").read_text())["result"] == "auth_expired"


# --- telemetry around a run ------------------------------------------------------------------

def test_telemetry_is_off_without_the_enabled_file(runner):
    runner.plan(stream=claude_stream("REVIEW: pass"))
    runner.run()
    assert runner.calls() == [] and "TRACEPARENT" not in runner.child_env()


def test_codex_gets_the_run_context_in_its_child_only(runner):
    runner.enable_telemetry()
    day = runner.root / "codex-home" / "sessions" / "2026" / "10" / "26"
    day.mkdir(parents=True)
    (day / "rollout-2026-10-26T04-01-27-01a10000-0000-7000-8000-000000000002.jsonl").write_text(
        lines({"type": "turn_context", "payload": {"model": "gpt-5.5-codex", "effort": None}}))
    runner.plan(stream=CODEX_STREAM, last="ok\nREVIEW: pass\n")
    out = runner.run("codex", "review", env={"CODEX_MAX_MINUTES": "1"}, output=str(runner.root / "review.md"))
    assert out["status"] == "success"
    child = runner.child_env()
    assert child["TRACEPARENT"] == "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
    assert child["OTEL_RESOURCE_ATTRIBUTES"] == ("baton.run_id=12345678-1234-1234-1234-123456789abc,baton.role=review,"
                                                 "baton.issue=142,baton.pr=168,baton.stage=review,baton.pipeline_id=pipe-1")
    assert child["OTEL_METRIC_EXPORT_INTERVAL"] == "1000"
    assert not {"TRACEPARENT", "OTEL_RESOURCE_ATTRIBUTES"} & set(os.environ)
    begin, end = runner.calls()[0], runner.calls()[-1]
    assert begin[:2] == ["begin", "codex"] and begin[2] == "review" and begin[-2:] == ["--repo", "owner/app"]
    assert end[0] == "end" and end[2:3] == ["success"]


def test_claude_exports_metrics_only_to_the_loopback_collector(runner):
    runner.enable_telemetry()
    runner.plan(stream=claude_stream("REVIEW: pass"))
    runner.run()
    child = runner.child_env()
    assert (child["CLAUDE_CODE_ENABLE_TELEMETRY"], child["OTEL_METRICS_EXPORTER"], child["OTEL_LOGS_EXPORTER"],
            child["OTEL_TRACES_EXPORTER"]) == ("1", "otlp", "none", "none")
    assert child["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"] == "http://127.0.0.1:4320/v1/metrics"
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in os.environ


@pytest.mark.parametrize("caller", ["worker", "picker", "standalone"])
def test_claude_quota_probe_alias_matches_cli_and_telemetry(runner, monkeypatch, caller):
    from dataclasses import replace
    from types import SimpleNamespace
    from baton import pick, quota_worker, session, settle
    from baton.config import load

    env = session.resolve(runner.env)
    path = runner.root / "routes.toml"
    path.write_text(env.config.source.read_text().replace('claude_probe = "haiku"',
                                                         'claude_probe = "claude-haiku-4-5"'))
    loaded = load(path, home=runner.root / 'home')
    vendors = tuple(v for v in loaded.vendors if v.id == 'claude')
    quota = replace(loaded.quota, vendor_pool=('claude',), vendor_extra=(),
                    caps=(('claude', loaded.cap('claude')),))
    paths = replace(loaded.paths, state=runner.state_dir, logs=runner.root / 'logs')
    env.config = replace(loaded, quota=quota, vendors=vendors, paths=paths)
    resolve = session.resolve
    monkeypatch.setattr(session, "resolve", lambda value=None: resolve(env if value is None else value))
    runner.plan(stream=claude_stream("ok"))
    requests = []

    def begin(vendor, role, minutes, *, requested_model):
        requests.append((vendor, role, requested_model))

    monkeypatch.setattr(settle.telemetry, "from_env", lambda: SimpleNamespace(begin=begin, end=lambda *args: None))
    if caller == "worker":
        quota_worker.run(env.config)
    elif caller == "picker":
        assert pick.choose(env, "dev", "", "pool", ["claude"]) == "claude"
    else:
        settle.main(["probe", "claude"])
    args = json.loads((runner.root / "fake" / "args.json").read_text())
    assert args[args.index("--model") + 1] == "claude-haiku-4-5"
    assert requests == [("claude", "probe", "claude-haiku-4-5")]


def test_codex_adapter_does_not_support_scan_role():
    from baton import codex_cli
    assert "scan" not in codex_cli.SUPPORTED_ROLES


@pytest.mark.parametrize("role", ["spec_review", "spec", "review", "screenshot_review", "fix"])
def test_short_read_mostly_claude_runs_write_five_minute_cache(runner, role):
    runner.plan(stream=claude_stream("fine\nREVIEW: pass"))
    runner.run(role=role)
    assert runner.child_env()["FORCE_PROMPT_CACHING_5M"] == "1"


def test_a_dev_run_that_waits_on_long_tests_keeps_the_longer_cache(runner):
    runner.plan(stream=claude_stream("fine\nRESULT: ready"))
    runner.run(role="dev", env={"MODEL_DEV": "sonnet", "BUDGET_DEV": "1"})
    assert "FORCE_PROMPT_CACHING_5M" not in runner.child_env()


def test_other_vendors_never_get_the_claude_cache_switch(runner):
    runner.plan(stream=CODEX_STREAM)
    runner.run(vendor="codex", role="review")
    assert "FORCE_PROMPT_CACHING_5M" not in runner.child_env()


def test_a_failing_telemetry_never_changes_the_result(runner):
    runner.enable_telemetry()
    runner.fail_telemetry()
    runner.plan(stream=claude_stream("REVIEW: pass"))
    out = runner.run()
    assert (out["rc"], out["outcome"]) == (0, "REVIEW: pass")
    assert "TRACEPARENT" not in runner.child_env() and "CLAUDE_CODE_ENABLE_TELEMETRY" not in runner.child_env()


# --- the hooks ---------------------------------------------------------------------------------

def make_hooks(runner, **context):
    runner.enable_telemetry()
    return hooks.Telemetry(runner.env["SELF_DIR"], runner.env["STATE"], "owner/app", runner.env["BATON_TELEMETRY_ITEM"],
                           **context)


def test_a_task_event_keeps_the_pipeline_for_the_next_one(runner):
    telemetry = make_hooks(runner, issue=142, pr=168)
    telemetry.task("handoff", "handoff", "--reason", "fix_run_failed")
    telemetry.task("merged", "merged")
    first, second = runner.calls()
    assert first[:9] == ["task", "handoff", "--repo", "owner/app", "--issue", "142", "--pr", "168", "--key"]
    assert "--pipeline" not in first and second[second.index("--pipeline") + 1] == "pipe-1"


@pytest.mark.parametrize("outcome, verdict", [("REVIEW: pass", "pass"), ("REVIEW: changes x", "changes"),
                                              ("SPEC: reject", "reject")])
def test_a_verdict_carries_only_codes_and_a_count(runner, outcome, verdict):
    telemetry = make_hooks(runner, pr=168)
    kind = "spec_review" if outcome.startswith("SPEC") else "review"
    telemetry.verdict(kind, "codex", "a" * 40, 2, outcome, "secret review text")
    call = runner.calls()[-1]
    assert call[call.index("--verdict") + 1] == verdict and call[call.index("--key") + 1] == f"{kind}:2:{'a' * 40}"
    assert ("--must-fix" in call) == (kind == "review")
    assert "secret review text" not in " ".join(call)


def test_no_verdict_line_records_nothing(runner):
    make_hooks(runner).verdict("review", "codex", "a" * 40, 1, "RESULT: failed", "")
    assert runner.calls() == []


def test_a_fix_event_needs_the_last_run(runner):
    telemetry = make_hooks(runner)
    telemetry.fix("claude", 1, "answers")
    assert runner.calls() == []
    telemetry._remember(last_run_id="run-7")
    telemetry.fix("claude", 1, "answers")
    call = runner.calls()[-1]
    assert call[call.index("--adopted") + 1:call.index("--adopted") + 2] == ["3"] and "run-7" in call


def test_ci_result_is_unknown_when_the_head_moved(runner, tmp_path, monkeypatch):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "gh").write_text('#!/bin/sh\ncase "$*" in *headRefOid*) printf %040d 2;; *) echo "[]";; esac\n')
    (stubs / "timeout").write_text('#!/bin/sh\nshift\nexec "$@"\n')
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_REPO", "owner/app")
    make_hooks(runner, pr=168).ci(168, 0, "%040d" % 1)
    call = runner.calls()[-1]
    assert call[call.index("--ci-result") + 1] == "unknown"


def test_a_run_in_flight_does_not_defer_another_run_of_its_vendor(runner):
    runner.plan(stream=claude_stream("fine\nREVIEW: pass"))
    with in_flight(runner.state_dir, 'claude'):
        out = runner.run()
    assert (out['rc'], out['outcome'], out['defer']) == (0, 'REVIEW: pass', '')


def test_a_run_in_flight_does_not_stop_a_probe_and_a_probe_does_not_stop_a_run(runner, monkeypatch):
    from baton import settle
    runner.plan(stream=claude_stream("fine\nREVIEW: pass"))
    real_run, probes, during_probe = dispatch.agents.run, [], []

    def probe_request(state, logs, vendor, model, settings=None):
        probes.append(vendor)
        monkeypatch.setattr(dispatch.agents, "run", real_run)
        during_probe.append(runner.run())

    def run_and_probe(*args):
        monkeypatch.setattr(settle, "_probe", probe_request)
        probes.append(settle.probe(str(runner.state_dir), str(runner.root / "logs"), "claude", "m",
                                   needed=lambda: True))
        return real_run(*args)

    monkeypatch.setattr(dispatch.agents, "run", run_and_probe)
    out = runner.run()
    assert probes == ["claude", True] and out["outcome"] == "REVIEW: pass"
    assert [(o["outcome"], o["defer"]) for o in during_probe] == [("REVIEW: pass", "")]


def test_a_claude_screenshot_review_gets_its_own_route_and_budget(runner):
    runner.plan(stream=claude_stream("fine\nREVIEW: pass"))
    out = runner.run(role="screenshot_review", env={"BUDGET_SCREENSHOT_REVIEW": "2.5"})
    args = json.loads((runner.root / "fake" / "args.json").read_text())
    assert out["outcome"] == "REVIEW: pass"
    assert args[args.index("--max-budget-usd") + 1] == "2.5"
    assert args[args.index("--model") + 1] == "opus" and args[args.index("--effort") + 1] == "low"
    assert args[args.index("--settings") + 1] == "/s/settings.json"
    assert runner.child_env()["BATON_AGENT_ROLE"] == "screenshot_review"


def test_a_codex_screenshot_review_runs_terra_in_a_read_only_sandbox(runner, monkeypatch):
    monkeypatch.setattr(dispatch.agents, "preflight", lambda *_: "")
    runner.plan(stream=CODEX_STREAM, last="all fine\nREVIEW: changes\n")
    out = runner.run("codex", "screenshot_review", output=str(runner.root / "shots.md"))
    args = json.loads((runner.root / "fake" / "args.json").read_text())
    assert args[args.index("-m") + 1] == "gpt-6.1-terra" and args[args.index("-s") + 1] == "read-only"
    assert 'model_reasoning_effort="low"' in args
    assert out["outcome"] == "REVIEW: changes" and (runner.root / "shots.md").read_text().strip() == "all fine"


def test_a_codex_screenshot_review_without_its_artifact_fails(runner, monkeypatch):
    monkeypatch.setattr(dispatch.agents, "preflight", lambda *_: "")
    runner.plan(stream=CODEX_STREAM, last="REVIEW: pass\n")
    out = runner.run("codex", "screenshot_review", output=str(runner.root / "shots.md"))
    assert out["outcome"].startswith("RESULT: failed") and "no Codex review artifact" in out["outcome"]


# --- git control files (config, hooks) ---------------------------------------------------

TAMPER = f"""#!{sys.executable}
import json, os, pathlib, subprocess, sys
fake = pathlib.Path(os.environ["FAKE_DIR"])
plan = json.loads((fake / "tamper.json").read_text())
git_dir = pathlib.Path.cwd() / ".git"
for name, content in plan.get("write", {{}}).items():
    path = git_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)
for name in plan.get("remove", []):
    (git_dir / name).unlink()
os.execv(str(fake / "claude-real"), [str(fake / "claude-real"), *sys.argv[1:]])
"""


def tamper(runner, **plan):
    fake = runner.root / "fake"
    (fake / "claude").rename(fake / "claude-real")
    (fake / "claude").write_text(TAMPER)
    (fake / "claude").chmod(0o755)
    (fake / "tamper.json").write_text(json.dumps(plan))


def test_a_run_that_adds_a_hook_fails_and_the_hook_is_removed(runner):
    runner.plan(stream=claude_stream("fine\nREVIEW: pass"))
    tamper(runner, write={"hooks/pre-push": "#!/bin/sh\ntouch /tmp/ran\n"})
    out = runner.run()
    assert (out["rc"], out["status"]) == (1, "failure")
    assert "git configuration or hooks" in out["outcome"] and out["reply"] == ""
    assert not (runner.worktree / ".git" / "hooks" / "pre-push").exists()
    assert any("changed git control files; restored: hooks/pre-push" in line for line in runner.runner_log())


def test_a_run_that_edits_git_config_fails_and_the_config_is_restored(runner):
    config = runner.worktree / ".git" / "config"
    before = config.read_bytes()
    runner.plan(stream=claude_stream("fine\nREVIEW: pass"))
    tamper(runner, write={"config": before.decode() + "[core]\n\tfsmonitor = /tmp/x\n"})
    out = runner.run()
    assert out["status"] == "failure" and config.read_bytes() == before
    assert config.stat().st_mode & 0o777 != 0o755   # the mode is restored too


def test_a_changed_or_removed_existing_hook_is_put_back(runner):
    hook = runner.worktree / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    runner.plan(stream=claude_stream("fine\nREVIEW: pass"))
    tamper(runner, remove=["hooks/pre-commit"], write={"info/attributes": "* filter=x\n"})
    out = runner.run()
    assert out["status"] == "failure"
    assert hook.read_text() == "#!/bin/sh\nexit 0\n" and hook.stat().st_mode & 0o777 == 0o755
    assert not (runner.worktree / ".git" / "info" / "attributes").exists()


def test_a_run_that_leaves_git_control_files_alone_is_unaffected(runner):
    runner.plan(stream=claude_stream("fine\nREVIEW: pass"))
    tamper(runner)
    out = runner.run()
    assert (out["rc"], out["status"], out["outcome"]) == (0, "success", "REVIEW: pass")
    assert not any("git control files" in line for line in runner.runner_log())


def test_a_deferred_run_keeps_its_status_but_the_files_are_still_restored(runner):
    runner.plan(stream="You've hit your limit \u00b7 resets 3am\n", rc=1)
    tamper(runner, write={"hooks/post-checkout": "#!/bin/sh\n"})
    out = runner.run()
    assert out["status"] == "quota" and out["rc"] == 75
    assert not (runner.worktree / ".git" / "hooks" / "post-checkout").exists()


# --- route tiers (#237) --------------------------------------------------------------------

LIGHT_DEV = '\n[models.routes.dev_light]\nclaude = { model = "claude_probe", effort = "low" }\n'
HEAVY_FIX = '\n[models.routes.fix_heavy]\nclaude = { model = "claude_strong", effort = "high" }\n'


def tiered(runner, tables):
    """The runner's session, its config's routes extended by `tables`."""
    from dataclasses import replace
    from baton import config, session
    sess = session.resolve(runner.env)
    path = runner.root / "tiered.toml"
    path.write_text(sess.config.source.read_text() + tables)
    sess.config = replace(sess.config, models=config.load(path, home=runner.root / "home").models)
    return sess


def requested(runner, sess, role):
    runner.plan(stream=claude_stream("ok"))
    dispatch.dispatch("claude", role, "p", "t", str(runner.worktree), settings="/s/settings.json", env=sess)
    args = json.loads((runner.root / "fake" / "args.json").read_text())
    return args[args.index("--model") + 1], args[args.index("--effort") + 1]


@pytest.mark.parametrize("tables, model", [(LIGHT_DEV, ("haiku", "low")), ("", ("sonnet", "medium"))])
def test_a_stored_light_tier_runs_the_dev_step_on_its_route(runner, tables, model):
    from baton import tiers
    sess = tiered(runner, tables)
    tiers.write(sess, "142", {"dev": "light"})
    assert requested(runner, sess, "dev") == model


def test_a_follow_up_run_ignores_the_stored_tier(runner):
    from baton import tiers
    sess = tiered(runner, HEAVY_FIX)
    tiers.write(sess, "142", {"fix": "heavy"})
    assert requested(runner, sess, "fix") == ("opus", "high")
    assert requested(runner, sess.with_runtime(fix_followup=True), "fix") == ("sonnet", "medium")
    ends = [line for line in runner.runner_log() if line.startswith("end t ")]
    assert ends[-1].endswith(" tier=heavy")   # the step's tier, whichever entry ran


def test_the_end_line_and_the_run_record_name_the_tier(runner):
    from baton import tiers
    runner.enable_telemetry()
    sess = tiered(runner, LIGHT_DEV)
    tiers.write(sess, "142", {"dev": "light"})
    requested(runner, sess, "dev")
    assert runner.runner_log()[1].startswith("end t rc=0 ") and runner.runner_log()[1].endswith(" tier=light")
    begin = runner.calls()[0]
    assert begin[begin.index("--tier") + 1] == "light"


def test_an_adapter_error_still_names_the_tier(runner, monkeypatch):
    def boom(*args):
        raise RuntimeError("boom")
    monkeypatch.setattr(dispatch.agents, "run", boom)
    runner.run()
    assert "end t adapter rc=1, no result tier=standard" in runner.runner_log()


# --- maintenance drain (#337) -----------------------------------------------------------------

def test_maintenance_deferral_does_not_lend_a_slot():
    from baton.common import Deferred, MAINTENANCE_PREFIX
    assert Deferred("codex", f"{MAINTENANCE_PREFIX}reboot").lends_slot is False
    assert Deferred("codex", "vendor capacity reserved").lends_slot is True
    assert Deferred("codex", "quota exhausted").lends_slot is True
    assert Deferred("codex", f"{MAINTENANCE_PREFIX}x", lends_slot=True).lends_slot is True


def test_dispatch_defers_for_maintenance_without_starting_or_counting(runner):
    import json, time
    from baton.common import MAINTENANCE_PREFIX
    (runner.state_dir / "maintenance").write_text(json.dumps({"expires": time.time() + 600, "reason": "reboot", "id": "t"}))
    out = runner.run()
    assert (out["rc"], out["status"], out["defer"]) == (75, "deferred", f"{MAINTENANCE_PREFIX}reboot")
    assert not (runner.root / "fake" / "calls").exists()
    assert not list(runner.state_dir.glob("runs-*")) and not list(runner.state_dir.glob("*cooldown*"))
    assert runner.runner_log() == []


def test_dispatch_continues_a_step_during_maintenance(runner):
    import json, time
    (runner.state_dir / "maintenance").write_text(json.dumps({"expires": time.time() + 600, "reason": "reboot", "id": "t"}))
    runner.plan(stream=claude_stream("fine\nREVIEW: pass"))
    out = runner.run(continues_step=True)
    assert (out["rc"], out["status"], out["outcome"]) == (0, "success", "REVIEW: pass")
    assert (runner.state_dir / f"runs-{__import__('time').strftime('%F')}").read_text() == "1\n"


def test_an_expired_maintenance_flag_does_not_defer_dispatch(runner):
    import json, time
    (runner.state_dir / "maintenance").write_text(json.dumps({"expires": time.time() - 10, "reason": "old", "id": "t"}))
    runner.plan(stream=claude_stream("fine\nREVIEW: pass"))
    out = runner.run()
    assert (out["rc"], out["status"]) == (0, "success")
