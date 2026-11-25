"""Antigravity CLI (agy) as a Gemini-only vendor (#109): a fake `agy` on PATH replays stream-json.

The streams follow the CLI facts recorded on SER8 (antigravity-cli 1.5.13-1.5.15, 2026-11-20..24):
events keyed by `event` (init, step_update, result), a denied tool still exits 0 with an empty
response, and the model appears in `cli.log` as a display label when `init` has none. The
`cli.log` line shape and the usage keys are modelled on those notes, not copied from a real file.
"""
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from baton import agents, config, dispatch, quota, session, settle, vendors
from baton.common import Request
from conftest import full_text
from test_agents import lines

FAKE_AGY = f"""#!{sys.executable}
import json, os, pathlib, subprocess, sys
fake = pathlib.Path(os.environ["FAKE_DIR"])
args = sys.argv[1:]
if args == ["--version"]:
    sys.exit(0)
(fake / "calls").open("a").write("agy\\n")
(fake / "args.json").write_text(json.dumps(args))
plan = json.loads((fake / "plan.json").read_text())
if plan.get("commit"):
    pathlib.Path("committed.txt").write_text("x\\n")
    subprocess.run(["git", "add", "committed.txt"], check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "agy"], check=True)
if plan.get("dirty"):
    pathlib.Path("stray.txt").write_text("written by a read-only run")
sys.stdout.write(plan.get("stream", ""))
sys.stderr.write(plan.get("stderr", ""))
sys.exit(plan.get("rc", 0))
"""

MODEL = "gemini-4-neon-medium"
REVIEW_MODEL = "gemini-3.8-flash-high"


def stream(response="done\nRESULT: ready", *, model=MODEL, status="SUCCESS", denied=None, conversation="c1"):
    init = {"event": "init", "conversation_id": conversation}
    if model:
        init["model"] = model
    result = {"status": status, "response": response, "conversation_id": conversation,
              "usage": {"inputTokens": 100, "outputTokens": 7}}
    if denied:
        result["denied_actions"] = denied
    return lines(init, {"event": "step_update", "step": "x"}, {"event": "result", "result": result})


def with_agy(text, review=REVIEW_MODEL, dev=MODEL):
    text = text.replace('cursor_analyst = "grok-4.7-high"\n',
                        f'cursor_analyst = "grok-4.7-high"\nagy_dev = "{dev}"\nagy_review = "{review}"\n')
    for role, alias in (("dev", "agy_dev"), ("fix", "agy_dev"), ("review", "agy_review"),
                        ("spec_review", "agy_review")):
        text = text.replace(f"[models.routes.{role}]\n",
                            f"[models.routes.{role}]\nagy = {{ model = \"{alias}\" }}\n")
    return text.replace(
        "[vendors.cursor]\n",
        '[vendors.agy]\nadapter = "agy"\nroles = ["dev", "fixer", "reviewer", "spec_reviewer"]\n'
        'quota = "extra"\ncap = 0.9\nmax_minutes = 30\n\n[vendors.cursor]\n')


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return config.load(path, home=tmp_path / "home")


@pytest.fixture
def agy_runner(tmp_path, monkeypatch):
    fake, state, logs, home = tmp_path / "fake", tmp_path / "state", tmp_path / "logs", tmp_path / "home"
    for directory in (fake, state, logs, home):
        directory.mkdir()
    (fake / "agy").write_text(FAKE_AGY)
    (fake / "agy").chmod(0o755)
    (fake / "timeout").write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    (fake / "timeout").chmod(0o755)
    token = home / ".gemini/antigravity-cli/antigravity-oauth-token"
    token.parent.mkdir(parents=True)
    token.write_text("t")
    for name, value in (("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}"), ("FAKE_DIR", str(fake)),
                        ("HOME", str(home))):
        monkeypatch.setenv(name, value)
    cfg = write(tmp_path, with_agy(full_text()))
    cfg = replace(cfg, paths=replace(cfg.paths, state=state, logs=logs, self_dir=tmp_path / "bin"))
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
        env, state_dir, worktree, root, logs_dir, home_dir = sess, state, wt, tmp_path, logs, home

        def plan(self, **plan):
            (fake / "plan.json").write_text(json.dumps(plan))

        def head(self):
            return subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True,
                                  text=True).stdout.strip()

        def run(self, role="dev", **options):
            return dispatch.dispatch("agy", role, "p", "t", str(wt), env=sess, **options)

        def args(self):
            return json.loads((fake / "args.json").read_text())

        def calls(self):
            path = fake / "calls"
            return path.read_text().split() if path.exists() else []

    return Runner()


# --- registration and config -------------------------------------------------------------------

def test_agy_is_registered_with_gemini_only_review_policy():
    agy = agents.ADAPTERS["agy"]
    assert agy.SAME_VENDOR_REVIEW is True and agy.RECORDS_AUTHOR_MODEL is True
    assert agy.SAME_VENDOR_SAME_MODEL is False
    assert agy.ROUTE_FIELDS == frozenset({"model"})
    assert agy.COMMIT_MARKERS == ()
    assert agy.QUOTA_SOURCE == "none"
    assert agy.SUPPORTED_ROLES == {"dev", "fixer", "reviewer", "spec_reviewer"}
    assert agy.valid_model("gemini-4-neon-medium")
    assert not agy.valid_model("claude-opus-5-5-medium")
    assert not agy.valid_model("gpt-oss-120b-medium")
    assert not agy.valid_model("gemini-pro-orchestrator-neon")
    assert not agy.valid_model("gemini-pro-orchestrator-g1-pro")
    assert agy.distinct_models("gemini-4-neon-low", "gemini-4-neon-xhigh") is False
    assert agy.distinct_models("gemini-4-neon-high", "gemini-3.8-flash-high") is True
    assert agy.distinct_models("gemini-4-neon-high", "unknown") is False


def test_config_admits_agy_and_the_example_still_loads_without_it(tmp_path):
    cfg = write(tmp_path, with_agy(full_text()))
    assert cfg.allows("agy", "reviewer") and not cfg.allows("agy", "screenshot_reviewer")
    root = Path(__file__).resolve().parents[1]
    plain = config.load(root / "config.example.toml", home=tmp_path / "home")
    assert "agy" not in plain.declared()


@pytest.mark.parametrize("bad", ["claude-opus-5-5-high", "gpt-oss-120b-medium", "gemini-pro-orchestrator-neon"])
def test_config_rejects_a_non_gemini_route_model(tmp_path, bad):
    with pytest.raises(config.ConfigError, match="agy"):
        write(tmp_path, with_agy(full_text(), dev=bad))


def test_config_rejects_a_bad_followup_fallback_or_probe_model(tmp_path):
    text = with_agy(full_text())
    followup = text.replace("[models.routes.review_followup]\n",
                            '[models.routes.review_followup]\nagy = { model = "bad_model" }\n')
    for body in (followup,
                 text.replace('max_minutes = 30\n', 'max_minutes = 30\nfallback_models = ["bad_model"]\n', 1),
                 text.replace('max_minutes = 30\n', 'max_minutes = 30\nquota_probe_model = "bad_model"\n', 1)):
        body = body.replace('agy_review = ', 'bad_model = "claude-opus-5-5-high"\nagy_review = ')
        with pytest.raises(config.ConfigError, match="agy"):
            write(tmp_path, body)


def test_config_rejects_a_review_model_not_distinct_from_dev_or_fix(tmp_path):
    with pytest.raises(config.ConfigError, match="distinct"):
        write(tmp_path, with_agy(full_text(), review="gemini-4-neon-xhigh", dev="gemini-4-neon-low"))
    fix = with_agy(full_text()).replace('[models.routes.fix]\nagy = { model = "agy_dev" }',
                                        '[models.routes.fix]\nagy = { model = "agy_review" }')
    with pytest.raises(config.ConfigError, match="distinct"):
        write(tmp_path, fix)


# --- recorded streams through dispatch ---------------------------------------------------------

def test_dev_run_reports_the_init_model_and_builds_the_command(agy_runner):
    agy_runner.plan(stream=stream())
    out = agy_runner.run()
    assert (out["rc"], out["status"], out["outcome"], out["model"]) == (0, "success", "RESULT: ready", MODEL)
    args = agy_runner.args()
    assert args[-2] == "-p" and args[args.index("--model") + 1] == MODEL
    assert args[args.index("--mode") + 1] == "accept-edits" and "--sandbox" in args
    assert "--effort" not in args and "--dangerously-skip-permissions" not in args
    allow = json.loads(args[args.index("--settings") + 1])["permissions"]["allow"]
    assert "command(git)" in allow and "command(task ci:changed)" in allow
    assert "/tmp" in [args[i + 1] for i, a in enumerate(args) if a == "--add-dir"]
    assert out["log"].endswith("-agy.jsonl")


def test_cli_log_label_is_the_model_evidence_when_init_has_none(agy_runner):
    cli_log = agy_runner.home_dir / ".gemini/antigravity-cli/cli.log"
    cli_log.write_text(
        'I1124 10:00:00 other conversation=zzz Propagating selected model override label="Gemini 4 Neon (Low)"\n'
        'I1124 10:00:01 conversation=c1 Propagating selected model override label="Gemini 4 Neon (Medium)"\n'
        'I1124 10:00:02 conversation=c1 sir_flash_lite helper call\n')
    agy_runner.plan(stream=stream(model=""))
    out = agy_runner.run()
    assert (out["status"], out["model"]) == ("success", MODEL)


@pytest.mark.parametrize("label,model", [
    ("Gemini 4 Neon (High)", "gemini-4-neon-high"),
    ("Gemini 3.8 Flash (Low)", "gemini-3.8-flash-low"),
    ("Gemini 4 Neon", ""), ("Claude Opus 5.5 (High)", "claude-opus-5.5-high"), ("(High)", "")])
def test_label_conversion(label, model):
    from baton import agy_cli
    assert agy_cli.label_to_model(label) == model


@pytest.mark.parametrize("name,kwargs,why", [
    ("denied", dict(stream=stream("", denied=[{"tool": "command(rm)"}])), "denied"),
    ("empty", dict(stream=stream("")), "empty"),
    ("no-evidence", dict(stream=stream(model="")), "no model"),
    ("non-gemini", dict(stream=stream(model="claude-opus-5-5-high")), "non-Gemini"),
    ("mismatch", dict(stream=stream(model="gemini-4-neon-high")), "requested"),
])
def test_unproven_runs_fail_and_leave_no_commit(agy_runner, name, kwargs, why):
    start = agy_runner.head()
    agy_runner.plan(commit=True, **kwargs)
    out = agy_runner.run()
    assert out["status"] == "failure" and out["rc"] == 1
    assert out["outcome"].startswith("RESULT: failed") and why.lower() in out["outcome"].lower()
    assert out["model"] == ""
    assert agy_runner.head() == start
    assert not (agy_runner.worktree / "committed.txt").exists()


def test_read_only_run_that_changes_files_is_reset(agy_runner):
    start = agy_runner.head()
    agy_runner.plan(stream=stream("ok\nREVIEW: pass", model=REVIEW_MODEL), dirty=True)
    out = agy_runner.run("review")
    assert out["status"] == "failure" and "read-only" in out["outcome"]
    assert agy_runner.head() == start
    assert not (agy_runner.worktree / "stray.txt").exists()
    assert agy_runner.args()[agy_runner.args().index("--mode") + 1] == "plan"


def test_resource_exhausted_puts_agy_on_cooldown(agy_runner):
    agy_runner.plan(stream=lines({"event": "result", "result": {
        "status": "FAILED", "error": "RESOURCE_EXHAUSTED: quota exceeded", "response": ""}}), rc=1)
    out = agy_runner.run()
    assert (out["rc"], out["status"]) == (75, "quota")
    assert (agy_runner.state_dir / "agy-cooldown-until").read_text().strip().isdigit()


def test_untrusted_denied_action_text_is_not_a_quota_signal(agy_runner):
    agy_runner.plan(stream=stream("", denied=[{"command": "echo RESOURCE_EXHAUSTED 429"}]))
    out = agy_runner.run()
    assert out["status"] == "failure"


def test_missing_login_is_an_auth_failure(agy_runner):
    (agy_runner.home_dir / ".gemini/antigravity-cli/antigravity-oauth-token").unlink()
    out = agy_runner.run()
    assert (out["rc"], out["status"]) == (75, "auth")
    assert agy_runner.calls() == []


def test_quota_source_none_runs_no_agent_and_records_no_quota(agy_runner):
    assert settle.probe(str(agy_runner.state_dir), str(agy_runner.logs_dir), "agy", "") is True
    assert agy_runner.calls() == []
    assert not list(agy_runner.logs_dir.glob("*quota-probe-agy*"))
    snap = quota.load(agy_runner.state_dir, "agy")
    assert snap.get("probed_at") and not snap.get("windows")
    assert quota.fresh(agy_runner.state_dir, "agy", time.time(), 60)
    assert quota.credential_current(agy_runner.state_dir, "agy", time.time())


def test_usage_of_a_recorded_result(agy_runner):
    from baton import agy_cli
    row = {"event": "result", "result": {"usage": {"inputTokens": 100, "outputTokens": 7}}}
    assert agy_cli.usage_of(row) == {"input_tokens": 100, "output_tokens": 7}


# --- allowlist ---------------------------------------------------------------------------------

def test_allowlist_is_derived_from_the_ci_command():
    from baton import agy_cli
    allow = lambda role, ci, hook=None: json.loads(agy_cli.permissions_settings(role, ci, hook))["permissions"]["allow"]
    assert allow("review", "task ci && rm -rf /") == ["command(git)", "command(rg)", "command(ls)", "command(cat)"]
    got = allow("dev", "uv run --frozen pytest -q && task ci:changed BASE=x; FOO=1 bash bin/verify.sh | tee out",
                "/r/hooks/verify.sh")
    assert got == ["command(git)", "command(rg)", "command(ls)", "command(cat)", "command(python3)",
                   "command(mkdir)", "command(uv run)", "command(task ci:changed)", "command(bash bin/verify.sh)",
                   "command(tee out)", "command(/r/hooks/verify.sh)"]
    assert all("*" not in rule for rule in got)


# --- review independence and attribution ------------------------------------------------------

def test_agy_never_reviews_its_own_head_on_the_same_model(tmp_path):
    state, pr = str(tmp_path), 7
    vendors.record_author(state, pr, "agy")
    vendors.record_author_model(state, pr, "agy", MODEL)
    assert vendors.review_allowed(state, pr, "agy", "gemini-4-neon-xhigh") is False
    assert vendors.review_allowed(state, pr, "agy", REVIEW_MODEL) is True
    assert vendors.same_vendor_review_mode(state, pr, "agy", REVIEW_MODEL) == "same-vendor-different-model"
    assert vendors.review_allowed(state, pr, "agy", "") is False


def test_agy_reviews_another_vendors_pr_and_others_review_agy(tmp_path):
    state = str(tmp_path)
    vendors.record_author(state, 8, "claude")
    assert vendors.review_allowed(state, 8, "agy") is True
    vendors.record_author(state, 9, "agy")
    assert vendors.review_allowed(state, 9, "claude") is True


def test_agy_commits_are_attributed_by_record_contrib_not_a_marker():
    assert not any(marker for marker in agents.ADAPTERS["agy"].COMMIT_MARKERS)
    assert Request  # the request type the adapter reads


# --- guard.sh, Alloy, docs ---------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def test_guard_and_alloy_vendor_lists_equal_the_adapter_sets():
    import re
    expected_alloy = set(agents.ADAPTERS) - {"stub"}
    for match in re.findall(r'IsMatch\(attributes\["agent"\], "\^\(([^)]*)\)\$"\)',
                            (ROOT / "telemetry/runner.alloy").read_text()):
        assert set(match.split("|")) == expected_alloy
    expected_guard = set(agents.ADAPTERS) - {"stub", "claude"}
    found = re.findall(r"by=\(([^)]*)\)", (ROOT / "bin/guard.sh").read_text())
    assert found and all(set(m.split("|")) == expected_guard for m in found)


def test_a_dev_push_handoff_by_agy_is_a_valid_binding():
    from test_handoff import issue_push_binding
    from baton import handoff
    binding = {**issue_push_binding("1"), "vendor": "agy", "model": MODEL,
               "attempts": [["agy", MODEL, "c" * 40, "a" * 40]]}
    assert handoff._binding_valid("dev_push_failed", "1", binding) is True


def test_docs_name_agy():
    assert "agy" in (ROOT / "README.md").read_text().lower()
    assert "Antigravity" in (ROOT / "SECURITY.md").read_text()
