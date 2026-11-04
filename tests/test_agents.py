"""baton.agents: every vendor adapter against fake CLIs replaying stream-json.

The streams follow logs recorded on a runner host (2026-10): Claude and Codex quota probes, a
Cursor dev run, a Cursor weekly-limit error. Claude/Codex quota and auth errors have
not happened there yet; those use the shapes cooldown.sh already relies on.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from baton import agents, prompts
from baton.common import Request

FAKE_CLI = f"""#!{sys.executable}
import json, os, pathlib, sys
fake = pathlib.Path(os.environ["FAKE_DIR"])
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
if args == ["login", "status"]:
    sys.exit(int(os.environ.get("FAKE_LOGIN_RC", "0")))
(fake / "calls").open("a").write(name + "\\n")
(fake / "args.json").write_text(json.dumps(args))
(fake / "env.json").write_text(json.dumps(dict(os.environ)))
plan = json.loads((fake / "plan.json").read_text())
model = args[args.index("-m") + 1] if "-m" in args else ""
plan.update(plan.get("by_model", {{}}).get(model, {{}}))
sys.stdout.write(plan.get("stream", ""))
cwd = pathlib.Path(args[args.index("-C") + 1]) if "-C" in args else pathlib.Path.cwd()
if plan.get("dirty"):
    (cwd / "stray.txt").write_text("written by a read-only run")
if plan.get("last") is not None:
    pathlib.Path(args[args.index("--output-last-message") + 1]).write_text(plan["last"])
for token in args[-1].split():
    if plan.get("base") and token.endswith("/" + plan["base"]):
        for suffix, content in plan.get("artifacts", {{}}).items():
            pathlib.Path(token + suffix).write_text(content)
sys.exit(plan.get("rc", 0))
"""


def lines(*events):
    return "".join((e if isinstance(e, str) else json.dumps(e)) + "\n" for e in events)


def claude_stream(text, subtype="success"):
    return lines({"type": "system", "subtype": "init", "model": "claude-opus-5-5", "permissionMode": "dontAsk"},
                 {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
                 {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
                     "five_hour": {"utilization": 0.3, "resetsAt": 1793027400}}}},
                 {"type": "result", "subtype": subtype, "is_error": subtype != "success", "result": text,
                  "total_cost_usd": 0.0025, "usage": {"input_tokens": 2, "output_tokens": 4}})


def cursor_stream(*texts):
    return lines({"type": "system", "subtype": "init", "apiKeySource": "login", "model": "Auto"},
                 {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "PROMPT"}]}},
                 *({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}
                   for t in texts),
                 {"type": "result", "subtype": "success", "duration_ms": 262369, "is_error": False,
                  "result": "".join(texts),
                  "usage": {"inputTokens": 86, "outputTokens": 42941, "cacheReadTokens": 5119599,
                            "cacheWriteTokens": 187325}})


THREAD = "01a10000-0000-7000-8000-000000000002"
CODEX_STREAM = lines("Reading additional input from stdin...",
                     {"type": "thread.started", "thread_id": THREAD},
                     {"type": "turn.started"},
                     {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "ok"}},
                     {"type": "turn.completed", "usage": {"input_tokens": 14984, "cached_input_tokens": 12288,
                                                          "output_tokens": 5}})


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fake claude/cursor-agent/codex and timeout(1) first on PATH; a real git worktree."""
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    for cli in ("claude", "cursor-agent", "codex"):
        (fake / "bin" / cli).write_text(FAKE_CLI)
        (fake / "bin" / cli).chmod(0o755)
    (fake / "bin" / "timeout").write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    (fake / "bin" / "timeout").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(fake))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("VIRTUAL_ENV", "/runner/.venv")
    wt = tmp_path / "wt"
    wt.mkdir()
    git = ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    (tmp_path / "logs").mkdir()

    class Env:
        root, worktree = tmp_path, wt

        def plan(self, **plan):
            (fake / "plan.json").write_text(json.dumps(plan))

        def args(self):
            return json.loads((fake / "args.json").read_text())

        def child_env(self):
            return json.loads((fake / "env.json").read_text())

        def calls(self):
            return (fake / "calls").read_text().split() if (fake / "calls").exists() else []

        def run(self, vendor, role, prompt="do it", **options):
            defaults = {"claude": dict(model="opus", budget="4", settings="/s/review-settings.json", repo="/r/app"),
                        "cursor": dict(model="auto"), "codex": dict()}[vendor]
            return agents.run(Request(vendor=vendor, role=role, prompt=prompt, cwd=wt, minutes=1,
                                      log=tmp_path / "logs" / f"run-{vendor}.jsonl", **{**defaults, **options}))

        def head(self):
            return subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True).stdout

        def dirty(self):
            return subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True).stdout

    return Env()


# --- Claude ----------------------------------------------------------------------------

def test_claude_success_flags_and_environment(env):
    env.plan(stream=claude_stream("Looks right.\n<!-- baton-review by=codex -->\nREVIEW: pass"))
    r = env.run("claude", "review", subagent_model="sonnet")
    assert (r.status, r.outcome, r.exit_code) == ("success", "REVIEW: pass", 0)
    assert "baton-review" in r.reply, "Claude's reply is raw; callers strip markers"
    args = env.args()
    assert args[:2] == ["-p", "do it"]
    for flag, value in (("--model", "opus"), ("--max-budget-usd", "4"), ("--settings", "/s/review-settings.json"),
                        ("--setting-sources", "user"), ("--permission-mode", "dontAsk"), ("--output-format", "stream-json")):
        assert args[args.index(flag) + 1] == value
    child = env.child_env()
    assert (child["BATON_AGENT"], child["BATON_AGENT_ROLE"], child["BATON_AGENT_REPO"]) == ("1", "review", "/r/app")
    assert child["BATON_AGENT_WORKTREE"] == str(env.worktree)
    assert child["BATON_AGENT_LOGS"] == str(env.root / "logs")
    assert child["CLAUDE_CODE_SUBAGENT_MODEL"] == "sonnet"
    assert "VIRTUAL_ENV" not in child, "the runner's venv must not reach the agent's tools"


def test_claude_end_line_reports_usage_and_cost(env):
    env.plan(stream=claude_stream("Done.\nREVIEW: pass"))
    r = env.run("claude", "review")
    assert "REVIEW: pass in=2 out=4 cache_read=null cache_write=null cost=0.0025" in r.summary


def test_codex_runs_without_unused_features_and_is_told_not_to_reread_agents_md(env):
    env.plan(stream=CODEX_STREAM, final="Looks right.\nREVIEW: pass")
    rollout(env)
    env.run("codex", "review", output=str(env.root / "review.md"))
    args = env.args()
    for setting in ("skills.include_instructions=false", "features.plugins=false", "features.apps=false",
                    "features.image_generation=false", "features.browser_use=false", "features.computer_use=false"):
        assert setting in args
    assert "AGENTS.md is already in your instructions" in args[-1]


def test_claude_without_a_result_line_fails(env):
    env.plan(stream=claude_stream("ok"))   # the quota probe's whole reply
    r = env.run("claude", "review")
    assert r.status == "failure" and r.outcome.startswith("RESULT: failed rc=0 (log ")
    assert r.summary.startswith("no result line")


def test_claude_budget_exceeded(env):
    env.plan(stream=claude_stream("", subtype="error_max_budget_usd"), rc=1)
    r = env.run("claude", "dev")
    assert r.outcome.startswith("RESULT: failed budget $4 exceeded")
    assert r.status == "failure"


def test_claude_quota_defers_until_the_stated_reset(env):
    env.plan(stream="Claude AI usage limit reached|You've hit your limit · resets 3pm (America/Los_Angeles)\n", rc=1)
    r = env.run("claude", "dev")
    assert (r.status, r.exit_code) == ("quota", 75)
    assert time.time() < r.cooldown_until <= time.time() + 24 * 3600


@pytest.mark.parametrize("message", ["Invalid API key · Please run /login",
                                     "Failed to authenticate: OAuth session expired and could not be refreshed"])
def test_claude_auth_defers_like_a_quota_without_a_reset(env, message):
    env.plan(stream=lines({"type": "result", "subtype": "success", "is_error": True, "result": message}), rc=1)
    r = env.run("claude", "review")
    assert (r.status, r.exit_code) == ("auth", 75)
    assert abs(r.cooldown_until - (time.time() + agents.COOLDOWN_HOURS * 3600)) < 60


def test_quota_talk_in_the_agents_own_text_is_not_a_quota(env):
    env.plan(stream=claude_stream("The usage limit reached banner needs a test.\nREVIEW: changes"))
    r = env.run("claude", "review")
    assert (r.status, r.outcome) == ("success", "REVIEW: changes")


def test_timeout_wins_over_everything(env):
    env.plan(stream="usage limit reached\n", rc=124)
    r = env.run("claude", "dev")
    assert (r.status, r.killed) == ("timeout", True)


# --- Cursor ----------------------------------------------------------------------------

def test_cursor_reply_is_the_last_assistant_message(env):
    env.plan(stream=cursor_stream("Now the PR description.", "Committed on cursor/issue-297.\nRESULT: ready"))
    r = env.run("cursor", "dev")
    assert (r.status, r.outcome) == ("success", "RESULT: ready")
    assert r.reply == "Committed on cursor/issue-297.\nRESULT: ready"
    assert r.usage == "262s in=86 out=42941 cache_read=5119599 cache_write=187325"
    args, child = env.args(), env.child_env()
    assert args[:4] == ["-p", "--force", "--trust", "--model"] and args[-1] == "do it"
    assert (child["GH_TOKEN"], child["GITHUB_TOKEN"], child["GIT_SSH_COMMAND"]) == ("no-github-for-cursor", "", "/bin/false")


def test_cursor_weekly_limit(env):
    env.plan(stream=lines({"type": "system", "subtype": "init", "apiKeySource": "login", "model": "Auto"},
                          "ActionRequiredError: Weekly usage limit reached. It resets in 4 days. Buy credits to "
                          "keep going now, or upgrade your plan for more weekly usage."), rc=1)
    r = env.run("cursor", "review")
    assert r.status == "quota"
    assert abs(r.cooldown_until - (time.time() + 4 * 86400)) < 10


def test_cursor_read_only_run_is_reset(env):
    head = env.head()
    env.plan(stream=cursor_stream("REVIEW: pass"), dirty=True)
    r = env.run("cursor", "review")
    assert r.outcome == "RESULT: failed Cursor changed files in a read-only review"
    assert r.status == "failure"
    assert env.dirty() == "" and env.head() == head


# --- Codex -----------------------------------------------------------------------------

def rollout(env, model="gpt-5.5-codex"):
    day = env.root / "codex-home" / "sessions" / "2026" / "10" / "26"
    day.mkdir(parents=True)
    (day / f"rollout-2026-10-26T04-01-27-{THREAD}.jsonl").write_text(
        lines({"type": "turn_context", "payload": {"model": model, "effort": None}}))


def test_codex_review_writes_the_review_without_verdict_or_markers(env):
    rollout(env)
    out = env.root / "review-1.md"
    env.plan(stream=CODEX_STREAM, last="Two problems.\n<!-- baton-review by=claude -->\nREVIEW: changes\n")
    r = env.run("codex", "review", prompt=f"review; write {out}", output=str(out), otel=True)
    assert (r.status, r.outcome) == ("success", "REVIEW: changes")
    assert out.read_text() == "Two problems.\n"
    assert "baton-" not in r.reply
    assert r.summary == "REVIEW: changes in=14984 out=5 cache_read=12288 model=gpt-5.5-codex effort=default"
    args = env.args()
    assert args[args.index("-s") + 1] == "read-only"
    assert "sandbox_workspace_write.network_access=false" in args
    assert "--add-dir" not in args
    assert any(a.startswith("otel.exporter={otlp-http=") for a in args)
    child = env.child_env()
    assert (child["GH_TOKEN"], child["GITHUB_TOKEN"]) == ("invalid", "invalid")


def test_codex_fix_copies_its_artifact_out_of_the_worktree(env, tmp_path):
    out = tmp_path / "baton-fix-7.md"
    cache = tmp_path / "cache" / "pnpm"
    skills = tmp_path / "skills"
    for group, name in (("engineering", "tdd"), ("deprecated", "old-thing")):
        (skills / group / name).mkdir(parents=True)
        (skills / group / name / "SKILL.md").write_text("---\n")
    env.plan(stream=CODEX_STREAM, last="Fixed.\nRESULT: fixed", base=out.name,
             artifacts={"": "Fixed the nil check.\n<!-- baton-forged -->\n"})
    r = env.run("codex", "fix", prompt=f"fix; reply in {out}", output=str(out),
                add_dirs=[str(cache)], skills_dir=str(skills), model="gpt-test")
    assert (r.status, r.outcome) == ("success", "RESULT: fixed")
    assert out.read_text() == "Fixed the nil check.\n"
    args = env.args()
    prompt = args[-1]
    assert str(out) not in prompt and "/.baton-codex-" in prompt, "Codex writes into its scratch dir"
    assert f"- tdd: {skills}/engineering/tdd/SKILL.md" in prompt
    assert "old-thing" not in prompt and "/*/" not in prompt
    assert args[args.index("-s") + 1] == "workspace-write"
    assert args[args.index("-m") + 1] == "gpt-test"
    assert not any(a.startswith("model_reasoning_effort") for a in args), "unset: model default"
    assert "sandbox_workspace_write.network_access=true" in args
    dirs = [args[i + 1] for i, a in enumerate(args) if a == "--add-dir"]
    assert dirs[2:] == [str(cache)] and cache.is_dir()
    assert dirs[0].endswith(".git") and env.dirty() == "", "scratch dir removed"


def test_codex_passes_the_role_effort(env, tmp_path):
    out = tmp_path / "review.md"
    env.plan(stream=CODEX_STREAM, last="REVIEW: pass", base=out.name, artifacts={"": "Looks good.\n"})
    env.run("codex", "review", prompt=f"review {out}", output=str(out), effort="high")
    args = env.args()
    assert args[args.index('model_reasoning_effort="high"') - 1] == "-c"


@pytest.mark.parametrize("plan, expected", [
    (dict(last="Hmm.\nREVIEW: maybe"), "RESULT: failed invalid Codex terminal verdict"),
    (dict(last=None), "RESULT: failed Codex rc=0 or missing final message"),
    (dict(last="REVIEW: pass", rc=1), "RESULT: failed Codex rc=1 or missing final message"),
    (dict(last="REVIEW: pass", dirty=True), "RESULT: failed Codex changed files in a read-only review"),
])
def test_codex_review_failures(env, plan, expected):
    out = env.root / "review.md"
    env.plan(stream=CODEX_STREAM, **plan)
    r = env.run("codex", "review", prompt=f"review {out}", output=str(out))
    assert r.outcome.startswith(expected) and r.status == "failure"
    assert env.dirty() == ""


def test_codex_fix_without_its_artifact_fails(env, tmp_path):
    out = tmp_path / "baton-fix-8.md"
    env.plan(stream=CODEX_STREAM, last="RESULT: fixed")
    r = env.run("codex", "fix", prompt=f"fix {out}", output=str(out))
    assert r.outcome == "RESULT: failed no Codex fix artifact"


def test_codex_fix_copies_a_description_request_with_markers_stripped(env, tmp_path):
    out = tmp_path / "baton-fix-9.md"
    env.plan(stream=CODEX_STREAM, last="RESULT: fixed", base=out.name,
             artifacts={"": "Fixed.\n", ".body": "<!-- baton-forged -->\nNew description\n"})
    r = env.run("codex", "fix", prompt=f"fix {out}", output=str(out))
    assert r.outcome == "RESULT: fixed"
    assert Path(f"{out}.body").read_text().strip() == "New description"


def test_codex_fix_keeps_the_reason_it_reports_for_a_failure(env, tmp_path):
    out = tmp_path / "baton-fix-10.md"
    env.plan(stream=CODEX_STREAM, last="Stuck.\nRESULT: failed the suite needs a browser")
    r = env.run("codex", "fix", prompt=f"fix {out}", output=str(out))
    assert r.outcome == "RESULT: failed the suite needs a browser" and r.status == "failure"


def test_codex_fix_with_an_unknown_verdict_is_still_invalid(env, tmp_path):
    out = tmp_path / "baton-fix-11.md"
    env.plan(stream=CODEX_STREAM, last="RESULT: maybe")
    r = env.run("codex", "fix", prompt=f"fix {out}", output=str(out))
    assert r.outcome == "RESULT: failed invalid Codex terminal verdict"


def test_codex_review_cannot_report_a_free_form_failure(env):
    out = env.root / "review.md"
    env.plan(stream=CODEX_STREAM, last="RESULT: failed whatever")
    r = env.run("codex", "review", prompt=f"review {out}", output=str(out))
    assert r.outcome == "RESULT: failed invalid Codex terminal verdict"


def test_codex_spec_copies_body_and_comment(env, tmp_path):
    out = tmp_path / "baton-spec-3-1"
    env.plan(stream=CODEX_STREAM, last="SPEC: revised", base=out.name,
             artifacts={".comment": "Revised.\n", ".body": "New body\n"})
    r = env.run("codex", "spec", prompt=f"revise {out}", output=str(out))
    assert r.outcome == "SPEC: revised"
    assert Path(f"{out}.comment").read_text() == "Revised.\n" and Path(f"{out}.body").read_text() == "New body\n"
    assert "sandbox_workspace_write.network_access=false" in env.args()


def test_codex_dev_blocked_needs_no_pr_description(env, tmp_path):
    out = tmp_path / "baton-dev-9.pr.md"
    env.plan(stream=CODEX_STREAM, last="RESULT: blocked no agreed plan")
    r = env.run("codex", "dev", prompt=f"implement; PR description: {out}", output=str(out), shots_dir="/shots")
    assert (r.status, r.outcome) == ("success", "RESULT: blocked no agreed plan")
    assert "/shots" in env.args()


def test_codex_login_required(env, monkeypatch):
    monkeypatch.setenv("FAKE_LOGIN_RC", "1")
    env.plan(stream=CODEX_STREAM, last="REVIEW: pass")
    r = env.run("codex", "review")
    assert (r.status, r.outcome) == ("auth", "RESULT: failed Codex login required; run codex login --device-auth")
    assert (r.exit_code, r.cooldown_until > time.time()) == (75, True)
    assert env.calls() == [], "the CLI never ran"


def test_codex_quota_uses_its_reset_epoch(env):
    env.plan(stream=lines({"type": "error", "message": "You have hit your usage limit", "resets_at": 1999999999}), rc=1)
    r = env.run("codex", "review")
    assert (r.status, r.cooldown_until) == ("quota", 1999999999)


@pytest.mark.parametrize("vendor, stream", [
    ("codex", lines({"type": "error", "message": "Selected model is at capacity. Please try a different model."},
                    {"type": "turn.failed", "error": {"message": "Selected model is at capacity. Please try a different model."}})),
    ("claude", lines({"type": "result", "subtype": "success", "is_error": True,
                      "result": 'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'})),
])
def test_a_full_model_is_busy_not_a_failure(env, vendor, stream):
    env.plan(stream=stream, rc=1)
    r = env.run(vendor, "spec_review")
    assert (r.status, r.exit_code) == ("busy", 75)
    assert abs(r.cooldown_until - (time.time() + agents.BUSY_MINUTES * 60)) < 10


def test_overload_talk_in_the_agents_own_text_is_not_busy(env):
    env.plan(stream=claude_stream("The server is at capacity under load tests.\nREVIEW: pass"))
    assert env.run("claude", "review").status == "success"


def test_claude_passes_the_first_other_fallback_model(env):
    env.plan(stream=claude_stream("REVIEW: pass"))
    env.run("claude", "review", fallback_models="sonnet opus")
    args = env.args()
    assert args[args.index("--fallback-model") + 1] == "sonnet"
    env.run("claude", "dev", model="sonnet", fallback_models="sonnet opus")
    args = env.args()
    assert args[args.index("--fallback-model") + 1] == "opus"
    env.run("claude", "dev", model="sonnet", fallback_models="sonnet")
    assert "--fallback-model" not in env.args()


FULL = lines({"type": "thread.started", "thread_id": THREAD}, {"type": "turn.started"},
             {"type": "error", "message": "Selected model is at capacity. Please try a different model."},
             {"type": "turn.failed", "error": {"message": "Selected model is at capacity. Please try a different model."}})


def test_codex_at_capacity_reruns_on_the_fallback_model(env):
    rollout(env, model="gpt-6-sol")
    out = env.root / "review.md"
    env.plan(stream=FULL, rc=1, by_model={"gpt-6-sol": dict(stream=CODEX_STREAM, rc=0, last="Fine.\nREVIEW: pass")})
    r = env.run("codex", "review", prompt=f"review {out}", output=str(out), fallback_models="gpt-6-sol gpt-6.1-sol")
    assert (r.status, r.outcome) == ("success", "REVIEW: pass")
    assert env.calls() == ["codex", "codex"]
    assert env.args()[env.args().index("-m") + 1] == "gpt-6-sol"
    assert "model=gpt-6-sol" in r.summary
    assert r.model == "gpt-6-sol"
    assert "at capacity" in Path(f"{r.log}.busy").read_text() and "at capacity" not in Path(r.log).read_text()


def test_codex_fallback_full_too_is_busy(env):
    env.plan(stream=FULL, rc=1)
    r = env.run("codex", "review", fallback_models="gpt-6-sol")
    assert (r.status, r.exit_code) == ("busy", 75)
    assert env.calls() == ["codex", "codex"], "one rerun, never more"


def test_codex_is_not_rerun_once_it_started_work(env):
    started = FULL.replace('{"type": "turn.started"}', '{"type": "turn.started"}\n' + json.dumps(
        {"type": "item.started", "item": {"type": "command_execution", "command": "git commit"}}))
    env.plan(stream=started, rc=1)
    r = env.run("codex", "dev", fallback_models="gpt-6-sol")
    assert r.status == "busy" and env.calls() == ["codex"]


# --- deadline, CLI, prompts -------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ('resets_at: "2033-05-18T03:33:19Z"', 1999999999),
    ("try again at 3:33 AM on May 18, 2033 (UTC)", 1999999980),
    ('{"resets_at":1999999999000}', 1999999999),
])
def test_deadline(text, expected):
    assert agents.deadline(text) == expected


def test_deadline_falls_back_to_the_cooldown():
    assert agents.deadline("no reset supplied", 5, now=1_000) == 1_000 + 5 * 3600


def test_cli_prints_json_and_exits_75_on_quota(env, capsys, monkeypatch):
    env.plan(stream="You have hit your usage limit\n", rc=1)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("the prompt"))
    rc = agents.main(["run", "--vendor", "claude", "--role", "dev", "--cwd", str(env.worktree),
                      "--log", str(env.root / "logs" / "cli.jsonl"), "--minutes", "1", "--model", "sonnet",
                      "--budget", "10", "--settings", "/s.json"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 75 and result["status"] == "quota" and result["cooldown_until"]
    assert env.args()[1] == "the prompt"


def test_render_replaces_in_order_and_drops_trailing_newlines(tmp_path):
    template = tmp_path / "t.md"
    template.write_text("Issue {{ISSUE}} in {{WORKTREE}}.\n\n")
    assert prompts.render(template, [("ISSUE", "#7 {{WORKTREE}}"), ("WORKTREE", "/wt")]) == "Issue #7 /wt in /wt."


def test_cursor_end_line_says_when_a_reconnect_lost_usage(env):
    plain = cursor_stream("RESULT: ready")
    env.plan(stream=plain)
    assert "usage=partial" not in env.run("cursor", "dev").summary
    env.plan(stream='{"type":"connection","subtype":"reconnecting"}\n' + plain)
    assert env.run("cursor", "dev").summary.endswith("usage=partial")
