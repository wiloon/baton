"""baton.fix: fix runs against a real git worktree with a bare origin, the fake `gh`
of conftest, and a stub for the agent run. Ported from tests/manual-checks.sh,
tests/completion.sh and the fix half of tests/verify.sh."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from baton import fix

BIN = Path(__file__).resolve().parents[1] / "bin"
PR = 420
URL = f"https://github.com/owner/app/pull/{PR}"
BODY = ("<!-- baton-task issue=419 completion=partial -->\n\nRefs #419\n\n## What changed\nDisabled buttons.\n\n"
        "## Checks to verify\nnone\n\n## Before release\nnone\n\n## Follow-ups\n- [nit] tidy copy\n")


def env_ctx():
    return os.environ["CTX"]


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def run(gh, tmp_path, monkeypatch):
    # A parent verification can export these. This fixture uses the configured repository.
    for name in ("VERIFY_HOOK", "GATE_HOOK", "CI_COMMAND"):
        monkeypatch.delenv(name, raising=False)
    origin, wt, selfdir = tmp_path / "origin.git", tmp_path / "wt", tmp_path / "self"
    sh(tmp_path, "git", "init", "-q", "--bare", str(origin))
    sh(tmp_path, "git", "clone", "-q", str(origin), str(wt))
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid")):
        sh(wt, "git", "config", key, value)
    sh(wt, "git", "checkout", "-q", "-b", "cursor/issue-1")
    (wt / "a.txt").write_text("a\n")
    sh(wt, "git", "add", "-A")
    sh(wt, "git", "commit", "-q", "-m", "base")
    sh(wt, "git", "push", "-q", "-u", "origin", "cursor/issue-1")
    selfdir.mkdir()
    for path in BIN.iterdir():
        if path.is_file():
            (selfdir / path.name).symlink_to(path)
    (selfdir / "verify.sh").unlink()
    gh.set(body=BODY, headRefName="cursor/issue-419")
    for name, value in (("SELF_DIR", str(selfdir)), ("MAX_VERIFY_FIXES", "2"), ("ISSUE", "419"), ("ROUND", "0")):
        monkeypatch.setenv(name, value)

    class Run:
        root, worktree, bin = tmp_path, wt, selfdir
        fake_gh = gh
        plans, agent_calls = [], []

        def verify_script(self, *results):
            """verify.sh: each run takes the next result, "pass" or the failure text."""
            (tmp_path / "verify-results.json").write_text(json.dumps(list(results)))
            script = selfdir / "verify.sh"
            script.write_text(f'#!/usr/bin/env python3\nimport json, pathlib, sys\n'
                              f'p = pathlib.Path("{tmp_path}/verify-results.json")\nr = json.loads(p.read_text())\n'
                              f'x = r.pop(0) if len(r) > 1 else r[0]\np.write_text(json.dumps(r))\n'
                              f'if x == "pass":\n    print("Verification passed")\nelse:\n    print(x, file=sys.stderr); sys.exit(1)\n')
            script.chmod(0o755)

        def verify_gate_script(self, line):
            """verify.sh that records E2E_GATE and prints LINE."""
            script = selfdir / "verify.sh"
            script.write_text(f'#!/bin/sh\nprintf "%s" "$E2E_GATE" > "{tmp_path}/gate-env"\necho "Verification passed"\necho "{line}"\n')
            script.chmod(0o755)

        def gate_ran(self):
            return (tmp_path / "gate-env").exists()

        def gate_env(self):
            return (tmp_path / "gate-env").read_text()

        def agent(self, *plans):
            """The next agent runs: {outcome, reply, commit, file, rc, model}."""
            self.plans[:] = list(plans)

        def fix(self, fixer="claude", feedback="feedback", head="", source="reviewer"):
            args = type("A", (), {"pr": PR, "wt": str(wt), "fixer": fixer, "source": source, "head": head})
            return fix.run("pr", args, os.environ, feedback)

        def verify(self, vendor="claude"):
            args = type("A", (), {"wt": str(wt), "base": "origin/main", "vendor": vendor, "kind": "pr", "number": PR})
            return fix.run("verify", args, os.environ)

        def runner_log(self):
            path = tmp_path / "logs" / "runner.log"
            return [line.split(" ", 3)[3] for line in path.read_text().splitlines()] if path.exists() else []   # after time and [owner/name]

        def labelled(self):
            return any(c[:2] == ["pr", "edit"] and "ready-for-human" in c for c in gh.calls())

        def comments(self):
            return [c[c.index("--body") + 1] for c in gh.calls() if c[:2] == ["pr", "comment"]]

        def body(self):
            return gh.pr["body"]

        @property
        def pushed_new(self):
            return not self.pushed()

        def pushed(self):
            return sh(wt, "git", "rev-parse", "HEAD") == sh(origin, "git", "rev-parse", "cursor/issue-1")

        def watch_pushes(self):
            hook = wt / ".git" / "hooks" / "pre-push"
            hook.write_text('#!/bin/sh\nprintf "push\\n" >> push-attempts.log\n')
            hook.chmod(0o755)

        def push_attempts(self):
            log = wt / "push-attempts.log"
            return log.read_text().splitlines() if log.exists() else []

        def remote_head(self):
            return sh(origin, "git", "rev-parse", "cursor/issue-1")

    state = Run()

    def stub(vendor, role, prompt, tag, cwd, settings="", output="", env=None, hooks=None):
        state.agent_calls.append((vendor, role, tag, output))
        state.prompts.append(prompt)
        plan = {"outcome": "RESULT: fixed", "reply": "", "commit": True, "rc": 0, "model": "",
                "request_directory": False, **state.plans.pop(0)}
        if plan["rc"] == 75:
            return {"rc": 75, "status": "quota", "outcome": "", "reply": None, "model": "", "defer": "quota exhausted"}
        if plan["commit"]:
            name = f"f{len(state.agent_calls)}.txt"
            Path(cwd, name).write_text("x\n")
            sh(cwd, "git", "add", "-A")
            sh(cwd, "git", "commit", "-q", "-m", name)
        if plan.get("stray"):
            Path(cwd, "stray.txt").write_text("x\n")
        if plan.get("request"):
            request = Path(env_ctx(), f"pr-{PR}-body.md")
            if plan["request_directory"]:
                request.mkdir()
            else:
                request.write_text(plan["request"])
        if output and plan.get("file") is not None:
            Path(output).write_text(plan["file"])
        if output and plan.get("description") is not None:
            request = Path(f"{output}.body")
            if plan["request_directory"]:
                request.mkdir()
            else:
                request.write_text(plan["description"])
        return {"rc": 0, "status": "success", "outcome": plan["outcome"], "reply": plan["reply"], "model": plan["model"],
                "defer": ""}
    state.agent_calls = []
    state.prompts = []
    monkeypatch.setattr(fix.dispatch, "dispatch", stub)
    state.verify_script("pass")
    yield state
    Path(f"/tmp/baton-fix-{PR}.md").unlink(missing_ok=True)
    Path(f"/tmp/baton-fix-{PR}.md.body").unlink(missing_ok=True)


def section(body, heading):
    out, on = [], False
    for line in body.split("\n"):
        if line.startswith("## "):
            on = line == f"## {heading}"
        elif on:
            out.append(line)
    return "\n".join(out).strip("\n")


# --- Manual checks (tests/manual-checks.sh) -------------------------------------------------

def test_a_recheck_replaces_the_section_and_the_posted_reply_drops_it(run):
    run.agent({"file": "## Must fix\n1. adopted — brand colours\n\n## Checks to verify\n- /billing: brand colour\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 0
    assert section(run.body(), "Checks to verify") == "- /billing: brand colour"
    assert section(run.body(), "What changed") == "Disabled buttons." and "- [nit] tidy copy" in run.body()
    assert "adopted — brand colours" in run.comments()[0] and "Checks to verify" not in run.comments()[0]
    assert run.comments()[0].startswith("<!-- baton-fix by=claude -->\n")


def test_a_missing_recheck_is_marked_so_the_gate_hands_off(run):
    run.agent({"file": "## Must fix\nnone\n\nRESULT: fixed\n"})
    assert run.fix("codex")["rc"] == 0
    assert section(run.body(), "Checks to verify") == "Not re-checked after the fix run."


def test_a_recheck_that_says_none_stays_none(run):
    run.agent({"file": "## Must fix\nnone\n\n## Checks to verify\nnone\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 0 and section(run.body(), "Checks to verify") == "none"


def test_a_failed_checks_refresh_defers_under_the_category1_budget(run, gh, monkeypatch):
    from baton import handoff
    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    gh.fail("edit")
    run.agent({"file": "## Checks to verify\n- /billing: brand colour\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 75 and not run.labelled()
    assert any("Checks not refreshed" in line or "handoff-retry fix_checks_refresh_failed" in line
               for line in run.runner_log())


def test_a_checks_refresh_that_keeps_failing_replays_twice_then_hands_off_once(run, gh, monkeypatch):
    clock = _due(monkeypatch)
    gh.fail("edit-before-write")
    run.agent({"file": "## Checks to verify\n- /billing: brand colour\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 75
    assert run.fix()["rc"] == 75 and len(run.agent_calls) == 1   # no early attempt, no fixer rerun
    clock["now"] = 1_060
    assert run.fix()["rc"] == 75 and len(run.agent_calls) == 1   # first replay fails: 300 s next
    clock["now"] = 1_200
    assert run.fix()["rc"] == 75
    clock["now"] = 1_360
    assert run.fix()["rc"] == 1 and len(run.agent_calls) == 1   # second replay fails: one handoff
    assert len([c for c in gh.calls() if c[:2] == ["pr", "edit"] and "ready-for-human" in c]) == 1


def test_a_failed_checks_refresh_recovers_without_running_the_fixer_again(run, gh, monkeypatch):
    clock = _due(monkeypatch)
    gh.fail("edit")
    run.agent({"file": "## Checks to verify\n- /billing: brand colour\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 75
    gh.fail("edit", False)
    clock["now"] = 1_060
    assert run.fix()["rc"] == 0 and len(run.agent_calls) == 1 and not run.labelled()
    assert section(run.body(), "Checks to verify") == "- /billing: brand colour"


def test_a_fix_push_replays_only_the_push_then_refreshes_the_checks(run, gh, monkeypatch):
    from baton import fix as fix_module
    from baton.common import PushResult
    clock = _due(monkeypatch)
    results = [PushResult(False, True, "connection reset"), PushResult(False, True, "connection reset"),
               PushResult(True)]
    pushes = []
    monkeypatch.setattr(fix_module, "push_result", lambda *a, **k: pushes.append(1) or results[len(pushes) - 1])
    run.agent({"file": "## Checks to verify\n- /billing: brand colour\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 75 and len(pushes) == 1
    assert run.fix()["rc"] == 75 and len(pushes) == 1   # no early attempt
    clock["now"] = 1_060
    assert run.fix()["rc"] == 75 and len(pushes) == 2
    clock["now"] = 1_360
    assert run.fix()["rc"] == 0 and len(pushes) == 3 and len(run.agent_calls) == 1
    assert section(run.body(), "Checks to verify") == "- /billing: brand colour"


def test_a_fix_push_that_keeps_failing_hands_off_once(run, gh, monkeypatch):
    from baton import fix as fix_module
    from baton.common import PushResult
    clock = _due(monkeypatch)
    monkeypatch.setattr(fix_module, "push_result", lambda *a, **k: PushResult(False, True, "connection reset"))
    run.agent({"file": "## Checks to verify\nnone\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 75
    clock["now"] = 1_060
    assert run.fix()["rc"] == 75
    clock["now"] = 1_360
    assert run.fix()["rc"] == 1 and len(run.agent_calls) == 1
    assert len([c for c in gh.calls() if c[:2] == ["pr", "edit"] and "ready-for-human" in c]) == 1


def test_a_deferred_cursor_checks_refresh_keeps_its_deferral(run, gh, monkeypatch):
    _due(monkeypatch)
    run.agent(cursor_plan("## Checks to verify\n- /billing: tab colour\n\nRESULT: fixed"))
    gh.fail("edit")
    assert run.fix("cursor")["rc"] == 75 and not run.labelled()


def cursor_plan(reply, **more):
    return {"reply": reply, **more}


def test_cursor_recheck_comes_from_its_reply(run):
    run.agent(cursor_plan("## Must fix\n1. adopted\n\n## Checks to verify\n- /billing: tab colour\n\nRESULT: fixed"))
    assert run.fix("cursor")["rc"] == 0
    assert section(run.body(), "Checks to verify") == "- /billing: tab colour"
    assert "Checks to verify" not in run.comments()[0] and run.comments()[0].startswith("<!-- baton-fix by=cursor -->")


def test_a_description_request_github_refuses_defers_and_resumes_without_an_agent(run, gh, monkeypatch):
    from baton import handoff
    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    request = BODY.replace("## Checks to verify\nnone", "## Checks to verify\n- /billing: badge")
    run.agent(cursor_plan("## Checks to verify\n- /billing: badge\n\nRESULT: fixed", request=request))
    gh.fail("edit")
    assert run.fix("cursor")["rc"] == 75
    assert len(run.agent_calls) == 1 and not run.labelled()
    clock["now"] = 1_060   # first later attempt is due
    gh.fail("edit", False)
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1   # resumed: no second run
    assert section(run.body(), "Checks to verify") == "- /billing: badge"


def test_a_description_only_cursor_fix_pushes_nothing_and_keeps_the_evidence(run, gh):
    request = BODY.replace("Disabled buttons.", "Corrected description")
    run.agent(cursor_plan("Description correction requested.\nRESULT: fixed", request=request, commit=False))
    shots = run.root / "ctx" / f"pr-{PR}-shots"
    assert run.fix("cursor")["rc"] == 0
    assert "Corrected description" in run.body() and not run.labelled() and not run.pushed_new
    assert shots.is_dir()


def test_a_crash_before_the_fix_completes_resumes_from_the_saved_request(run, gh, monkeypatch):
    request = BODY.replace("Disabled buttons.", "Corrected description")
    run.agent(cursor_plan("RESULT: fixed", request=request, commit=False))
    (run.root / "ctx" / f"pr-{PR}-shots").mkdir()
    record_author = fix.vendors.record_author
    monkeypatch.setattr(fix.vendors, "record_author", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        run.fix("cursor")
    assert "Corrected description" in run.body()
    monkeypatch.setattr(fix.vendors, "record_author", record_author)
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1   # replayed, not rerun


def test_a_description_write_whose_response_was_lost_counts_as_a_change(run, gh):
    from baton import pr as pr_helpers
    request = run.root / "request.md"
    request.write_text("Refs #297\n\nCorrected description")
    gh.set(headRefName="cursor/issue-297", body="Refs #297\n\nOld description")
    gh.fail("edit")
    state, ctx = str(run.root / "state"), str(run.root / "ctx")
    assert pr_helpers.apply_body(PR, str(request), state, ctx) == (
        1, False, "GitHub could not update the PR description", False, True)
    gh.fail("edit", False)
    assert pr_helpers.apply_body(PR, str(request), state, ctx) == (0, True, "", False, False)


# --- task completion (tests/completion.sh) ---------------------------------------------------

def test_a_fixer_that_agrees_changes_the_task_relation_without_a_commit(run, gh):
    run.agent({"commit": False, "reply": "Agreed.\nTASK: complete\nRESULT: fixed"})
    assert run.fix()["rc"] == 0
    assert "completion=complete" in run.body() and "Closes #419" in run.body()
    assert not run.labelled()


@pytest.mark.parametrize("reply", ["Disagree.\nRESULT: fixed", "TASK: partial\nRESULT: fixed",
                                   'The reviewer wrote "TASK: complete" but work remains.\nRESULT: fixed'])
def test_without_a_real_change_the_owner_takes_over(run, reply):
    run.agent({"commit": False, "reply": reply})
    assert run.fix()["rc"] == 1 and run.labelled() and "completion=partial" in run.body()
    assert not any("task completion change failed" in line for line in run.runner_log())


# --- the gate ladder -----------------------------------------------------------------------------

@pytest.mark.parametrize("outcome, title, reason", [
    ("RESULT: needs-info what?", f"PR #{PR} has questions", "fix_needs_info"),
    ("RESULT: failed boom", f"PR #{PR} fix failed (claude)", "fix_run_failed"),
])
def test_needs_info_and_failure_go_to_the_owner(run, outcome, title, reason, monkeypatch):
    seen = []
    from baton import fix as fix_mod
    original = fix_mod.Runner.handoff

    def record(self, pr, code, ttl, text):
        seen.append(code)
        return original(self, pr, code, ttl, text)

    monkeypatch.setattr(fix_mod.Runner, "handoff", record)
    run.agent({"outcome": outcome, "commit": False})
    assert run.fix()["rc"] == 1 and run.labelled()
    assert any(line.startswith(title) for line in run.runner_log())
    assert seen == [reason]


def test_a_fix_with_no_commit_stops_the_loop(run):
    run.agent({"commit": False})
    assert run.fix()["rc"] == 1
    assert any(f"PR #{PR} needs you | The fix run made no commit; stopping the loop. {URL}" == line
               for line in run.runner_log())


def test_a_fix_that_only_rechecks_the_pr_updates_its_description_and_continues(run):
    run.agent({"commit": False, "file": "## Must fix\n1. adopted — listed the checks\n\n"
                                        "## Checks to verify\n- /reader: colours\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 0 and not run.labelled()
    assert section(run.body(), "Checks to verify") == "- /reader: colours"


def test_a_fix_with_no_commit_and_the_same_checks_stops_the_loop(run):
    run.agent({"commit": False, "file": "## Must fix\nnone\n\n## Checks to verify\nnone\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 1 and run.labelled()
    assert any("The fix run made no commit" in line for line in run.runner_log())
    assert section(run.body(), "Checks to verify") == "none"


def test_a_fix_with_no_commit_and_no_checks_stops_and_leaves_the_description(run):
    run.agent({"commit": False, "file": "## Must fix\n1. adopted — nothing to change\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 1 and run.labelled()
    assert run.body() == BODY


def test_a_cursor_fix_that_only_rechecks_the_pr_updates_its_description_and_continues(run):
    run.agent(cursor_plan("## Must fix\n1. adopted\n\n## Checks to verify\n- /billing: badge\n\nRESULT: fixed", commit=False))
    assert run.fix("cursor")["rc"] == 0 and not run.labelled()
    assert section(run.body(), "Checks to verify") == "- /billing: badge"


def test_a_cursor_fix_with_no_commit_and_no_checks_stops_and_leaves_the_description(run):
    run.agent(cursor_plan("## Must fix\n1. adopted\n\nRESULT: fixed", commit=False))
    assert run.fix("cursor")["rc"] == 1 and run.labelled()
    assert run.body() == BODY


def test_a_verified_fix_is_recorded_pushed_and_its_checks_refreshed(run):
    run.agent({"model": "gpt-6.1-sol"})
    assert run.fix("codex")["rc"] == 0 and run.pushed()
    state = run.root / "state"
    assert (state / f"pr-{PR}-authors").read_text() == "codex\n"
    assert (state / f"pr-{PR}-author-models").read_text() == "codex gpt-6.1-sol\n"
    assert (state / f"pr-{PR}-contrib").read_text().startswith("codex ")


@pytest.mark.parametrize("fixer", ["codex", "cursor"])
def test_the_fixer_sees_the_current_origin_main(run, fixer):
    other = run.root / "other"
    sh(run.root, "git", "clone", "-q", str(run.root / "origin.git"), str(other))
    sh(other, "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "main")
    sh(other, "git", "push", "-q", "origin", "HEAD:main")
    run.agent({})
    run.fix(fixer)
    assert sh(run.worktree, "git", "rev-parse", "origin/main") == sh(other, "git", "rev-parse", "HEAD")


def test_a_failed_push_hands_off(run):
    run.agent({})
    sh(run.worktree, "git", "remote", "set-url", "origin", "/nonexistent")
    assert run.fix()["rc"] == 1 and run.labelled()
    assert any(f"PR #{PR}: pushing fix failed" in line for line in run.runner_log())


def test_a_partial_fix_is_pushed_unverified_for_the_owner(run):
    run.agent({"outcome": "RESULT: failed ran out of time"})
    assert run.fix()["rc"] == 1 and run.pushed() and run.labelled()


def test_a_codex_failure_is_handed_off_before_its_reply_is_posted(run):
    run.agent({"outcome": "RESULT: failed no plan", "commit": False, "file": "something\n"})
    assert run.fix("codex")["rc"] == 1 and run.comments() == []


def test_a_codex_failure_still_pushes_the_commits_it_made(run):
    run.agent({"outcome": "RESULT: failed could not finish"})
    assert run.fix("codex")["rc"] == 1 and run.pushed() and run.labelled()
    assert run.comments() == []
    assert (run.root / "state" / f"pr-{PR}-contrib").read_text().startswith("codex ")


# --- a description request from a fixer without GitHub access ---------------------------------

NEW_DESCRIPTION = "## What changed\nCorrected text.\n\n## Checks to verify\nnone\n"
CHECKED = {"file": "## Must fix\n1. adopted\n\n## Checks to verify\n- /billing: colour\n\nRESULT: fixed\n"}


def test_a_claude_fixer_can_submit_a_description_only_replacement(run):
    # #75: Claude gets the same snapshot + replacement channel as Codex; no commit is progress.
    head = sh(run.worktree, "git", "rev-parse", "HEAD")
    remote = sh(run.root / "origin.git", "git", "rev-parse", "cursor/issue-1")
    run.agent({"commit": False, "file": "## Must fix\n1. adopted — listed the commit\n\n"
                                        "## Checks to verify\nnone\n\nRESULT: fixed\n",
               "description": NEW_DESCRIPTION})
    assert run.fix()["rc"] == 0 and not run.labelled()
    path = Path(env_ctx(), f"pr-{PR}-description.md")
    assert str(path) in run.prompts[0]
    assert path.read_text().rstrip("\n") == BODY.rstrip("\n")
    assert "Corrected text." in run.body() and "Disabled buttons." not in run.body()
    assert "<!-- baton-task issue=419 completion=partial -->" in run.body() and "Refs #419" in run.body()
    assert section(run.body(), "Checks to verify") == "none"
    assert sh(run.worktree, "git", "rev-parse", "HEAD") == head
    assert sh(run.root / "origin.git", "git", "rev-parse", "cursor/issue-1") == remote == head


def test_a_codex_fixer_can_read_the_current_description(run):
    run.agent(dict(CHECKED))
    run.fix("codex")
    path = Path(env_ctx(), f"pr-{PR}-description.md")
    assert str(path) in run.prompts[0]
    assert path.read_text().rstrip("\n") == BODY.rstrip("\n")


def test_a_description_request_is_applied_with_the_runners_relation_and_the_fixers_recheck(run):
    run.agent({**CHECKED, "description": NEW_DESCRIPTION})
    assert run.fix("codex")["rc"] == 0 and run.pushed()
    assert "Corrected text." in run.body() and "Disabled buttons." not in run.body()
    assert "<!-- baton-task issue=419 completion=partial -->" in run.body() and "Refs #419" in run.body()
    assert section(run.body(), "Checks to verify") == "- /billing: colour"


def test_a_description_only_fix_needs_no_commit(run):
    run.agent({**CHECKED, "commit": False, "description": NEW_DESCRIPTION})
    assert run.fix("codex")["rc"] == 0 and not run.labelled()
    assert "Corrected text." in run.body()


def test_a_description_request_cannot_clear_manual_checks_without_a_recheck(run):
    run.agent({"commit": False, "file": "## Must fix\n1. adopted\n\nRESULT: fixed\n", "description": NEW_DESCRIPTION})
    run.fix("codex")
    assert section(run.body(), "Checks to verify") == "Not re-checked after the fix run."


def test_an_invalid_description_request_keeps_the_body_and_still_pushes_the_commits(run):
    run.agent({**CHECKED, "description": "Closes #999\n\n## What changed\nOther.\n"})
    assert run.fix("codex")["rc"] == 0 and run.pushed() and not run.labelled()
    assert "Disabled buttons." in run.body() and "#999" not in run.body()
    assert any("description request not applied" in line for line in run.runner_log())
    assert any("a task PR cannot close an unrelated issue" in c for c in run.comments())


def _codes(monkeypatch):
    seen = []
    original = fix.Runner.handoff

    def watch(self, pr, code, title, text, **kwargs):
        seen.append(code)
        return original(self, pr, code, title, text, **kwargs)

    monkeypatch.setattr(fix.Runner, "handoff", watch)
    return seen


@pytest.mark.parametrize("rc", [1, 2])
@pytest.mark.parametrize("fixer", ["claude", "cursor"])
def test_a_description_failure_is_discarded_not_handed_off(run, monkeypatch, fixer, rc):
    """#141: a rejected or failed description request never hands off by itself. The Claude/Codex
    path continues (its checks refresh counts as a change); Cursor's no-commit run stops as fix_no_commit."""
    if fixer == "cursor" and rc == 1:
        pytest.skip("a GitHub failure defers the Cursor path before any handoff")
    seen = _codes(monkeypatch)
    monkeypatch.setattr(fix.pr_helpers, "apply_body", lambda *a, **k: (rc, False, "something", False, False))
    run.agent(cursor_plan("Adopted.\n\nRESULT: fixed", commit=False) if fixer == "cursor" else {**CHECKED, "commit": False})
    run.fix(fixer)
    assert seen == ([] if fixer == "claude" else ["fix_no_commit"])


@pytest.mark.parametrize("rc, code", [(1, "fix_task_write_failed"), (2, "fix_task_invalid"), (3, "fix_task_invalid")])
def test_a_task_change_failure_is_classified_by_its_result(run, monkeypatch, rc, code):
    seen = _codes(monkeypatch)
    monkeypatch.setattr(fix.pr_helpers, "apply_task", lambda *a, **k: (rc, False, "", False))
    run.agent({"reply": "TASK: partial\nRESULT: fixed", "file": "## Must fix\nnone\n\nRESULT: fixed\n", "commit": False})
    run.fix("claude")
    assert seen == [code]


def test_a_cursor_task_change_that_is_unusable_is_invalid(run, monkeypatch):
    seen = _codes(monkeypatch)
    monkeypatch.setattr(fix.pr_helpers, "apply_task", lambda *a, **k: (2, False, "", False))
    monkeypatch.setattr(fix.pr_helpers, "apply_body", lambda *a, **k: (0, False, "", False, False))
    run.agent(cursor_plan("TASK: partial\nRESULT: fixed", commit=False))
    run.fix("cursor")
    assert seen == ["fix_task_invalid"]


COMPLETE = ("<!-- baton-task issue=419 completion=complete -->\n\nCloses #419\n\n## What changed\nDisabled buttons.\n\n"
            "## Checks to verify\nnone\n\n## Before release\nnone\n\n## Follow-ups\n- [nit] tidy copy\n")


@pytest.mark.parametrize("fixer", ["cursor", "codex"])
def test_retargeting_a_copied_body_preserves_checks_without_a_recheck(run, gh, fixer):
    initial = COMPLETE.replace("## Checks to verify\nnone", "## Checks to verify\n- /billing: badge")
    gh.set(body=initial)
    reply = "TASK: partial\nRESULT: fixed\n"
    if fixer == "cursor":
        run.agent(cursor_plan(reply, request=initial, commit=False))
    else:
        run.agent({"reply": reply, "file": reply, "description": initial, "commit": False})

    assert run.fix(fixer)["rc"] == 0 and not run.labelled() and not run.pushed_new
    assert run.body() == initial.replace("completion=complete", "completion=partial").replace("Closes #419", "Refs #419")


@pytest.mark.parametrize("fixer", ["cursor", "codex"])
@pytest.mark.parametrize("body_says", ["partial", "complete"])
def test_task_partial_accepts_a_matching_or_copied_body_and_pushes(run, gh, fixer, body_says):
    # Acceptance 1: live complete + TASK: partial; body may already say partial or still say complete.
    gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    gh.fail("relation-after-body-edit")
    text = ("Task completion: partial\n\n## What changed\nFixed all items.\n\n## Checks to verify\nnone\n"
            if body_says == "partial" else
            "Task completion: complete\n\n## What changed\nFixed all items.\n\n## Checks to verify\nnone\n")
    if fixer == "cursor":
        run.agent(cursor_plan("Adopted.\n\n## Checks to verify\nnone\n\nTASK: partial\nRESULT: fixed",
                              request=text))
    else:
        run.agent({**CHECKED, "description": text, "reply": "Adopted.\nTASK: partial\nRESULT: fixed\n"})
    assert run.fix(fixer)["rc"] == 0 and run.pushed() and not run.labelled()
    assert "completion=partial" in run.body() and "Refs #419" in run.body()
    assert "Fixed all items." in run.body() and "Closes #419" not in run.body()


@pytest.mark.parametrize("fixer", ["cursor", "codex"])
@pytest.mark.parametrize("directory", [True, False], ids=["directory", "no-relation"])
def test_a_rejected_request_reports_the_exact_reason(run, gh, fixer, directory):
    reply = "TASK: partial\nRESULT: fixed"
    if directory:
        reason = "description request must be a non-empty regular file and cannot be a symlink"
        gh.set(body=COMPLETE)
    else:
        reason = "cannot determine the PR's issue and task completion"
        gh.set(body="Original description.", headRefName="feature/no-issue")
    plan = cursor_plan(reply, request=BODY) if fixer == "cursor" else {**CHECKED, "reply": reply, "description": BODY}
    run.agent({**plan, "request_directory": directory})
    assert run.fix(fixer)["rc"] == 0 and run.pushed() and not run.labelled()
    assert any(f"description request not applied ({fixer})" in line and reason in line for line in run.runner_log())
    assert f"Discarded the PR description request from {fixer}.\n\n{reason}" in [
        comment.split("\n", 1)[-1] for comment in run.comments()
    ]
    if directory:
        assert "completion=partial" in run.body() and "Refs #419" in run.body()


@pytest.mark.parametrize("fixer", ["cursor", "codex"])
@pytest.mark.parametrize("reply,description", [
    ("RESULT: fixed\n", "Task completion: partial\n\n## What changed\nNope.\n"),
    ("TASK: complete\nRESULT: fixed\n", "Task completion: partial\n\n## What changed\nNope.\n"),
    ("RESULT: fixed\n", "Task completion: partial\nTask completion: complete\n\n## What changed\nNope.\n"),
])
def test_a_rejected_body_never_reaches_the_pr(run, gh, fixer, reply, description):
    # Acceptance 2: no TASK / TASK confirms live / conflicting fields — replacement stays off the PR.
    gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    if fixer == "cursor":
        run.agent(cursor_plan(reply, request=description))
    else:
        run.agent({**CHECKED, "description": description, "reply": reply})
    out = run.fix(fixer)
    assert out["rc"] == 0 and not run.labelled() and run.pushed()
    assert "Disabled buttons." in run.body() and "Nope." not in run.body()
    assert "completion=complete" in run.body() and "Closes #419" in run.body()
    assert any("description request not applied" in line for line in run.runner_log())


@pytest.mark.parametrize("fixer", ["cursor", "codex"])
def test_a_rejected_body_with_task_partial_still_pushes_and_retargets(run, gh, monkeypatch, fixer):
    # Acceptance 3: rejected description, valid TASK: partial → push, Refs, shared handoff with reason.
    gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    bad = "Task completion: partial\nTask completion: complete\n\n## What changed\nNope.\n"
    if fixer == "cursor":
        run.agent(cursor_plan("## Checks to verify\nnone\n\nTASK: partial\nRESULT: fixed", request=bad))
    else:
        run.agent({**CHECKED, "description": bad, "reply": "TASK: partial\nRESULT: fixed\n"})
    label_heads = []
    call_gh = fix.pr_helpers.gh

    def observe_label(*args, **kwargs):
        if args[:2] == ("pr", "edit") and "--add-label" in args and "ready-for-human" in args:
            label_heads.append(run.remote_head())
        return call_gh(*args, **kwargs)

    monkeypatch.setattr(fix.pr_helpers, "gh", observe_label)
    run.watch_pushes()
    assert run.fix(fixer)["rc"] == 0 and run.pushed() and not run.labelled()
    assert label_heads == []
    assert run.push_attempts() == ["push"]
    assert "Nope." not in run.body() and "Disabled buttons." in run.body()
    assert "completion=partial" in run.body() and "Refs #419" in run.body()
    assert any("description request not applied" in line and "conflicting" in line
               for line in run.runner_log())


@pytest.mark.parametrize("fixer", ["cursor", "codex"])
def test_a_rejected_body_does_not_hide_a_failed_task_retarget(run, gh, fixer):
    # Live body has a closing keyword in prose, so retarget to partial fails; the rejected
    # body must not swallow that failure.
    gh.set(body=COMPLETE.replace("Disabled buttons.", "Disabled buttons. Fixes #419 too."),
           headRefName="cursor/issue-419")
    bad = "Task completion: partial\nTask completion: complete\n\n## What changed\nNope.\n"
    if fixer == "cursor":
        run.agent(cursor_plan("## Checks to verify\nnone\n\nTASK: partial\nRESULT: fixed", request=bad))
    else:
        run.agent({**CHECKED, "description": bad, "reply": "TASK: partial\nRESULT: fixed\n"})
    run.fix(fixer)
    assert any("task completion change failed" in line for line in run.runner_log())
    assert "completion=complete" in run.body()


@pytest.mark.parametrize("fixer", ["cursor", "codex"])
def test_a_rejected_body_with_no_commit_hands_off_without_pushing(run, gh, fixer):
    gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    bad = "Task completion: partial\n\n## What changed\nNope.\n"   # no TASK line
    if fixer == "cursor":
        run.agent(cursor_plan("RESULT: fixed", request=bad, commit=False))
    else:
        run.agent({"commit": False, "description": bad, "reply": "RESULT: fixed\n", "file": "RESULT: fixed\n"})
    run.watch_pushes()
    assert run.fix(fixer)["rc"] == 1 and run.labelled()
    assert run.push_attempts() == []
    assert "Nope." not in run.body() and "Closes #419" in run.body()
    assert any("description request not applied" in line for line in run.runner_log())


@pytest.mark.parametrize("fixer", ["cursor", "codex"])
@pytest.mark.parametrize("completion", ["partial", "complete"])
def test_an_identical_canonical_body_pushes_without_a_description_write(run, gh, fixer, completion):
    live = BODY if completion == "partial" else COMPLETE
    gh.set(body=live, headRefName="cursor/issue-419")
    reply = "## Checks to verify\nnone\n\nRESULT: fixed"
    if fixer == "cursor":
        run.agent(cursor_plan(reply, request=live))
    else:
        run.agent({**CHECKED, "file": reply + "\n", "description": live, "reply": reply + "\n"})
    before = run.body()
    edits = [c for c in gh.calls() if c[:2] == ["pr", "edit"] and "--body-file" in c]
    assert run.fix(fixer)["rc"] == 0 and run.pushed() and not run.labelled()
    assert run.body() == before
    assert [c for c in gh.calls() if c[:2] == ["pr", "edit"] and "--body-file" in c] == edits
    assert not any("description request not applied" in line for line in run.runner_log())
    assert section(run.body(), "Checks to verify") == "none"


@pytest.mark.parametrize("fixer", ["cursor", "codex"])
def test_an_identical_body_with_no_commit_hands_off(run, gh, fixer):
    gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    if fixer == "cursor":
        run.agent(cursor_plan("RESULT: fixed", request=COMPLETE, commit=False))
    else:
        run.agent({**CHECKED, "commit": False, "description": COMPLETE, "reply": "RESULT: fixed\n",
                   "file": "RESULT: fixed\n"})
    run.watch_pushes()
    assert run.fix(fixer)["rc"] == 1 and run.labelled()
    assert run.push_attempts() == []
    assert not any("description request not applied" in line for line in run.runner_log())


@pytest.mark.parametrize("fixer", ["cursor", "codex"])
@pytest.mark.parametrize("progress", ["task", "checks"])
def test_a_rejected_body_with_other_progress_continues_without_push(run, gh, fixer, progress):
    gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    bad = "Task completion: partial\nTask completion: complete\n\n## What changed\nNope.\n"
    if progress == "task":
        reply = "TASK: partial\nRESULT: fixed"
        file = "## Must fix\n1. adopted\n\nRESULT: fixed\n"
    else:
        reply = "## Checks to verify\n- /billing: badge\n\nRESULT: fixed"
        file = "## Must fix\n1. adopted\n\n## Checks to verify\n- /billing: badge\n\nRESULT: fixed\n"
    if fixer == "cursor":
        run.agent(cursor_plan(reply, request=bad, commit=False))
    else:
        run.agent({**CHECKED, "commit": False, "description": bad, "reply": reply + "\n", "file": file})
    run.watch_pushes()
    assert run.fix(fixer)["rc"] == 0 and not run.labelled()
    assert run.push_attempts() == []
    assert "Nope." not in run.body()
    if progress == "task":
        assert "completion=partial" in run.body() and "Refs #419" in run.body()
    else:
        assert section(run.body(), "Checks to verify") == "- /billing: badge"
    assert any("description request not applied" in line for line in run.runner_log())


@pytest.mark.parametrize("fixer", ["claude", "codex"])
def test_a_github_description_failure_still_pushes_for_claude_and_codex(run, gh, fixer):
    # Only the description edit fails; matching `none` checks need no body write after push.
    request = BODY.replace("Disabled buttons.", "Corrected text.")
    gh.fail("edit-before-write")
    run.agent({"file": "## Must fix\n1. adopted\n\n## Checks to verify\nnone\n\nRESULT: fixed\n",
               "description": request})
    assert run.fix(fixer)["rc"] == 0 and run.pushed() and not run.labelled()
    assert "Disabled buttons." in run.body() and "Corrected text." not in run.body()
    assert any("GitHub could not update the PR description" in line for line in run.runner_log())
    assert any("GitHub could not update the PR description" in c for c in run.comments())


def test_a_rejected_description_comment_failure_still_returns_zero(run, gh):
    run.agent({**CHECKED, "description": "Closes #999\n\n## What changed\nOther.\n"})
    gh.fail("comment")
    assert run.fix("codex")["rc"] == 0 and run.pushed() and not run.labelled()
    assert "Disabled buttons." in run.body() and "#999" not in run.body()
    assert any("description request not applied" in line for line in run.runner_log())


def test_cursor_verify_failure_beats_a_description_handoff(run, gh):
    gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    bad = "Task completion: partial\n\n## What changed\nNope.\n"   # no TASK: → rejected body
    run.verify_script("lint broke")
    # Repair also fails so verification hands off before the description handoff.
    run.agent(cursor_plan("RESULT: fixed", request=bad),
              {"commit": False, "outcome": "RESULT: failed stuck"})
    assert run.fix("cursor")["rc"] == 1 and run.labelled() and not run.pushed()
    assert any("<!-- baton-verify-failed -->" in c for c in run.comments())
    assert "description request not applied" not in "\n".join(run.runner_log())


def _due(monkeypatch, now=1_000):
    from baton import handoff
    clock = {"now": now}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    return clock


def test_cursor_defers_body_and_task_then_resumes_without_an_agent(run, gh, monkeypatch):
    # Acceptance 4: matching partial body + TASK on live complete; GitHub edit fails, then resumes.
    clock = _due(monkeypatch)
    gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    text = "Task completion: partial\n\n## What changed\nFixed all items.\n\n## Checks to verify\nnone\n"
    run.agent(cursor_plan("## Checks to verify\nnone\n\nTASK: partial\nRESULT: fixed", request=text))
    gh.fail("edit-before-write")
    assert run.fix("cursor")["rc"] == 75 and len(run.agent_calls) == 1 and not run.labelled()
    assert run.body() == COMPLETE
    clock["now"] = 1_060
    gh.fail("edit-before-write", False)
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1 and run.pushed()
    assert "completion=partial" in run.body() and "Refs #419" in run.body()
    assert "Fixed all items." in run.body()


@pytest.mark.parametrize("failure", ["edit", "edit-before-write"])
def test_cursor_resumes_a_task_only_edit_after_github_failure(run, gh, failure, monkeypatch):
    clock = _due(monkeypatch)
    initial = COMPLETE.replace("## Checks to verify\nnone", "## Checks to verify\n- /billing: badge")
    gh.set(body=initial, headRefName="cursor/issue-419")
    run.agent(cursor_plan("Agreed.\nTASK: partial\nRESULT: fixed", commit=False))
    gh.fail(failure)
    assert run.fix("cursor")["rc"] == 75 and len(run.agent_calls) == 1 and not run.labelled()
    if failure == "edit-before-write":
        assert run.body() == initial
    clock["now"] = 1_060
    gh.fail(failure, False)
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1 and not run.pushed_new
    assert "completion=partial" in run.body() and "Refs #419" in run.body()
    assert section(run.body(), "Checks to verify") == "- /billing: badge"


def test_cursor_retries_a_failed_read_then_hands_off_an_unchanged_request(run, gh, monkeypatch):
    clock = _due(monkeypatch)
    run.agent(cursor_plan("RESULT: fixed", request=BODY, commit=False))
    gh.fail("view")
    assert run.fix("cursor")["rc"] == 75 and not run.labelled()
    clock["now"] = 1_060
    gh.fail("view", False)
    assert run.fix("cursor")["rc"] == 1 and len(run.agent_calls) == 1 and run.labelled()
    assert run.body() == BODY
    assert any("made no commit" in line for line in run.runner_log())


@pytest.mark.parametrize("request_kind", ["replacement", "task-only", "rejected"])
def test_a_deferred_task_change_preserves_the_owners_issue_correction(run, gh, request_kind, monkeypatch):
    clock = _due(monkeypatch)
    gh.set(body=COMPLETE, headRefName="owner-branch")
    request = {
        "replacement": COMPLETE.replace("Disabled buttons.", "Revised description."),
        "task-only": None,
        "rejected": COMPLETE.replace("issue=419", "issue=999"),
    }[request_kind]
    reply = "## Checks to verify\nnone\n\nTASK: partial\nRESULT: fixed"
    run.agent(cursor_plan(reply, request=request, commit=False))
    gh.fail("edit-before-write")
    assert run.fix("cursor")["rc"] == 75

    corrected = COMPLETE.replace("419", "420").replace("## Checks to verify\nnone", "## Checks to verify\n- Owner check")
    gh.set(body=corrected)
    clock["now"] = 1_060
    gh.fail("edit-before-write", False)
    assert run.fix("cursor")["rc"] == 1 and run.labelled()
    assert run.body() == corrected
    assert len(run.agent_calls) == 1


@pytest.mark.parametrize("outcome", ["RESULT: failed boom", "RESULT: needs-info", "RESULT: fixed"])
def test_cursor_does_not_resume_a_run_interrupted_before_checks_are_saved(run, gh, monkeypatch, outcome):
    from baton import pr as pr_helpers
    request = BODY.replace("Disabled buttons.", "Stale description.")
    first = cursor_plan(f"## Checks to verify\n- /old: badge\n\n{outcome}", request=request, commit=False)
    run.agent({**first, "outcome": outcome},
              cursor_plan("## Checks to verify\n- /new: badge\n\nRESULT: fixed", commit=False))
    with monkeypatch.context() as patch:
        def interrupt(*args):
            raise KeyboardInterrupt
        patch.setattr(pr_helpers, "split_checks", interrupt)
        with pytest.raises(KeyboardInterrupt):
            run.fix("cursor")
    assert run.body() == BODY
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 2
    assert section(run.body(), "Checks to verify") == "- /new: badge"
    assert "Stale description." not in run.body()


def test_cursor_resumes_task_after_a_rejected_body_when_github_fails(run, gh, monkeypatch):
    clock = _due(monkeypatch)
    gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    bad = "Task completion: partial\nTask completion: complete\n\n## What changed\nNope.\n"
    run.agent(cursor_plan("TASK: partial\nRESULT: fixed", request=bad, commit=False))
    gh.fail("edit")
    # Body is invalid (no edit); task edit is attempted and GitHub refuses → defer.
    assert run.fix("cursor")["rc"] == 75 and len(run.agent_calls) == 1
    clock["now"] = 1_060
    gh.fail("edit", False)
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1 and not run.labelled()
    assert "Nope." not in run.body() and "completion=partial" in run.body() and "Refs #419" in run.body()
    assert any("description request not applied" in line for line in run.runner_log())


def test_cursor_lost_body_response_still_counts_on_resume(run, gh, monkeypatch):
    # Lost success response: fake gh writes then fails; retry sees the landed body vs baseline.
    clock = _due(monkeypatch)
    gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    text = COMPLETE.replace("Disabled buttons.", "Fixed all items.")
    run.agent(cursor_plan("## Checks to verify\nnone\n\nTASK: partial\nRESULT: fixed",
                          request=text, commit=False))
    gh.fail("edit")
    assert run.fix("cursor")["rc"] == 75
    assert "Fixed all items." in run.body() and "completion=partial" in run.body()
    # Keep the edit failure active. The retry can finish using the already-landed body.
    clock["now"] = 1_060
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1
    assert len([c for c in gh.calls() if c[:2] == ["pr", "edit"] and "--body-file" in c]) == 1


def test_a_description_request_from_an_earlier_run_is_not_applied(run):
    Path(f"/tmp/baton-fix-{PR}.md.body").write_text(NEW_DESCRIPTION)
    run.agent(dict(CHECKED))
    assert run.fix("codex")["rc"] == 0
    assert "Disabled buttons." in run.body()


def test_a_failed_fix_does_not_apply_its_description_request(run):
    run.agent({"outcome": "RESULT: needs-info", "commit": False, "file": "1. which?\n", "description": NEW_DESCRIPTION})
    assert run.fix("codex")["rc"] == 1
    assert "Disabled buttons." in run.body()


def test_a_quota_pause_defers_with_the_vendor_and_reason(run):
    run.agent({"rc": 75})
    out = run.fix()
    assert (out["rc"], out["defer_vendor"], out["defer"]) == (75, "claude", "quota exhausted")
    assert not run.labelled()


# --- verification with repair runs (tests/verify.sh) --------------------------------------------

def test_a_failing_check_gets_a_repair_run_by_the_same_vendor(run):
    run.verify_script("lint broke", "pass")
    run.agent({"commit": True})
    (run.worktree / "b.txt").write_text("b\n")
    sh(run.worktree, "git", "add", "-A")
    sh(run.worktree, "git", "commit", "-q", "-m", "feature")
    out = run.verify()
    assert (out["rc"], out["rounds"]) == (0, 1)
    assert run.agent_calls[-1][:3] == ("claude", "fix", f"pr-{PR}-verify-fix-1")


def test_a_repair_run_gets_the_failure_in_its_prompt_and_writes_its_reply_under_tmp(run):
    # #38: Claude's shell could not read the ctx dir, and guard.sh denies writes there.
    run.verify_script("e2e timeout in {{REPLY_FILE}} spec", "pass")
    run.agent({"commit": True})
    assert run.verify()["rc"] == 0
    prompt, output = run.prompts[-1], run.agent_calls[-1][3]
    assert "<failure>\ne2e timeout in {{REPLY_FILE}} spec\n</failure>" in prompt   # data stays data
    assert output.startswith("/tmp/baton-") and output.endswith(f"-pr-{PR}-verify-fix.md")
    assert f"to {output} with the Write tool" in prompt


def test_a_long_failure_reaches_the_prompt_as_its_tail():
    text = "head\n" + "x" * fix.FAILURE_PROMPT_CAP + "\nthe error"
    excerpt = fix.failure_excerpt(text)
    assert excerpt.endswith("\nthe error") and "head" not in excerpt and "see the failure file" in excerpt
    assert fix.failure_excerpt("short") == "short"


def gate_body(section="e2e/a.spec.ts --repeat-each=15"):
    return {"419": {"body": f"Goal\n\n## E2E gate\n{section}\n"}}


def test_the_issue_gate_reaches_verification_and_its_result_is_posted(run):
    run.verify_gate_script("E2E gate passed: e2e/a.spec.ts --repeat-each=15 (15 passed)")
    run.fake_gh.set(issues=gate_body(), body=BODY.replace("completion=partial", "completion=complete"))
    out = run.verify()
    assert out["rc"] == 0
    assert run.gate_env() == "e2e/a.spec.ts --repeat-each=15"
    assert any(c[:2] == ["pr", "comment"] and "E2E gate passed" in c[c.index("--body") + 1]
               and "<!-- baton-e2e-gate -->" in c[c.index("--body") + 1] for c in run.fake_gh.calls())


def test_a_partial_slice_does_not_run_the_issues_gate(run):
    run.verify_gate_script("")
    run.fake_gh.set(issues=gate_body())
    assert run.verify()["rc"] == 0
    assert run.gate_env() == ""


def test_no_gate_means_no_gate_and_no_comment(run):
    run.verify_gate_script("")
    run.fake_gh.set(issues={"419": {"body": "Goal"}})
    assert run.verify()["rc"] == 0
    assert run.gate_env() == ""
    assert not any(c[:2] == ["pr", "comment"] for c in run.fake_gh.calls())


def test_an_invalid_gate_stops_without_a_repair_run(run):
    run.verify_gate_script("")
    run.fake_gh.set(issues=gate_body("e2e/a.spec.ts; rm -rf /"), body=BODY.replace("completion=partial", "completion=complete"))
    out = run.verify()
    assert out["rc"] == 1 and "E2E gate" in out["failure"]
    assert run.agent_calls == [] and not run.gate_ran()


def test_verification_gets_the_repositorys_hooks_and_ci_command(run):
    script = run.bin / "verify.sh"
    script.write_text(f'#!/bin/sh\nprintf "%s|%s|%s|%s" "$VERIFY_HOOK" "$GATE_HOOK" "$CI_COMMAND" "$2" > "{run.root}/hook-env"\n'
                      'echo "Verification passed"\n')
    script.chmod(0o755)
    assert run.verify()["rc"] == 0
    verify_hook, gate_hook, ci, base = (run.root / "hook-env").read_text().split("|")
    assert verify_hook.endswith("/hooks/verify.sh") and gate_hook.endswith("/hooks/gate.sh")
    assert ci == f"task ci:changed BASE={base}"


def test_verification_gives_up_after_the_configured_repairs(run):
    run.verify_script("still broken")
    run.agent({"commit": True}, {"commit": True})
    out = run.verify()
    assert (out["rc"], out["rounds"]) == (1, 2) and "still broken" in out["failure"]
    assert len(run.agent_calls) == 2
    assert "verify failed (attempt 2 of 2)" in " ".join(run.runner_log())


def test_a_repair_with_no_commit_is_checked_again_and_passes(run):
    run.verify_script("node 24.22.2 missing", "pass")
    run.agent({"commit": False})
    out = run.verify()
    assert (out["rc"], out["rounds"]) == (0, 1)
    assert len(run.agent_calls) == 1


def test_repairs_with_no_commit_stay_bounded_and_end_with_the_newest_failure(run):
    run.verify_script("first failure", "second failure", "third failure")
    run.agent({"commit": False}, {"commit": False})
    out = run.verify()
    assert (out["rc"], out["rounds"]) == (1, 2)
    assert out["failure"] == "third failure"
    assert len(run.agent_calls) == 2


def test_only_a_repair_with_a_new_commit_is_recorded_as_a_ci_fix(run, monkeypatch):
    events = []
    monkeypatch.setattr(fix.telemetry.Telemetry, "task", lambda self, kind, *args: events.append(kind))
    run.verify_script("first", "second", "pass")
    run.agent({"commit": False}, {"commit": True})
    assert run.verify()["rc"] == 0
    assert events.count("ci_fix") == 1


def test_a_repair_that_reports_failure_ends_verification(run):
    run.verify_script("broken")
    run.agent({"commit": False, "outcome": "RESULT: failed no idea"})
    out = run.verify()
    assert (out["rc"], out["rounds"], out["failure"]) == (1, 1, "broken")
    assert len(run.agent_calls) == 1


def test_a_repair_that_leaves_a_dirty_tree_ends_verification(run):
    run.verify_script("broken", "pass")
    run.agent({"commit": False, "stray": True})
    out = run.verify()
    assert (out["rc"], out["rounds"], out["failure"]) == (1, 1, "broken")
    assert len(run.agent_calls) == 1


def test_verification_refuses_a_dirty_worktree(run):
    (run.worktree / "stray.txt").write_text("x")
    out = run.verify()
    assert out["rc"] == 1 and out["failure"].startswith("Verification refused") and run.agent_calls == []


def test_a_fix_that_fails_verification_comments_and_hands_off(run):
    run.verify_script("tests failed")
    run.agent({}, {"commit": False, "outcome": "RESULT: failed stuck"})
    assert run.fix()["rc"] == 1 and run.labelled() and not run.pushed()
    comment = next(c for c in run.comments() if c.startswith("<!-- baton-verify-failed -->"))
    assert "after 1 of 2 fix rounds" in comment and "tests failed" in comment


def test_a_verified_repair_makes_the_author_model_unknown(run):
    run.verify_script("lint broke", "pass")
    run.agent({"model": "gpt-6.1-sol"}, {})
    assert run.fix("codex")["rc"] == 0
    assert (run.root / "state" / f"pr-{PR}-author-models").read_text() == "codex unknown\n"


# --- screenshot review (#50) ----------------------------------------------------------------

@pytest.mark.parametrize("fixer", ["claude", "codex"])
def test_with_screenshot_review_the_fixer_reshoots_into_a_cleared_dir(run, monkeypatch, fixer):
    monkeypatch.setenv("SCREENSHOT_REVIEW", "1")
    shots = Path(env_ctx(), f"pr-{PR}-shots")
    shots.mkdir(parents=True, exist_ok=True)
    (shots / "01-stale-390.png").write_bytes(b"x")
    run.agent(dict(CHECKED))
    run.fix(fixer)
    assert str(shots) in run.prompts[0] and "screenshot" in run.prompts[0].lower()
    assert not (shots / "01-stale-390.png").exists() and shots.is_dir()


def test_without_screenshot_review_the_fix_prompt_names_no_screenshots_dir(run):
    run.agent(dict(CHECKED))
    run.fix("claude")
    assert f"pr-{PR}-shots" not in run.prompts[0]
