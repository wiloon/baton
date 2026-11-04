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
        "## Manual checks\nnone\n\n## Before release\nnone\n\n## Follow-ups\n- [nit] tidy copy\n")


def env_ctx():
    return os.environ["CTX"]


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def run(gh, tmp_path, monkeypatch):
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

    state = Run()

    def stub(vendor, role, prompt, tag, cwd, settings="", output="", env=None, hooks=None):
        state.agent_calls.append((vendor, role, tag, output))
        state.prompts.append(prompt)
        plan = {"outcome": "RESULT: fixed", "reply": "", "commit": True, "rc": 0, "model": "", **state.plans.pop(0)}
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
            Path(env_ctx(), f"pr-{PR}-body.md").write_text(plan["request"])
        if output and plan.get("file") is not None:
            Path(output).write_text(plan["file"])
        if output and plan.get("description") is not None:
            Path(f"{output}.body").write_text(plan["description"])
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
    run.agent({"file": "## Must fix\n1. adopted — brand colours\n\n## Manual checks\n- /billing: brand colour\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 0
    assert section(run.body(), "Manual checks") == "- /billing: brand colour"
    assert section(run.body(), "What changed") == "Disabled buttons." and "- [nit] tidy copy" in run.body()
    assert "adopted — brand colours" in run.comments()[0] and "Manual checks" not in run.comments()[0]
    assert run.comments()[0].startswith("<!-- baton-fix by=claude -->\n")


def test_a_missing_recheck_is_marked_so_the_gate_hands_off(run):
    run.agent({"file": "## Must fix\nnone\n\nRESULT: fixed\n"})
    assert run.fix("codex")["rc"] == 0
    assert section(run.body(), "Manual checks") == "Not re-checked after the fix run."


def test_a_recheck_that_says_none_stays_none(run):
    run.agent({"file": "## Must fix\nnone\n\n## Manual checks\nnone\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 0 and section(run.body(), "Manual checks") == "none"


def test_a_failed_body_edit_hands_the_pr_to_the_owner(run, gh):
    gh.fail("edit")
    run.agent({"file": "## Manual checks\n- /billing: brand colour\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 1 and run.labelled()
    assert any("Manual checks not refreshed" in line for line in run.runner_log())


def cursor_plan(reply, **more):
    return {"reply": reply, **more}


def test_cursor_recheck_comes_from_its_reply(run):
    run.agent(cursor_plan("## Must fix\n1. adopted\n\n## Manual checks\n- /billing: tab colour\n\nRESULT: fixed"))
    assert run.fix("cursor")["rc"] == 0
    assert section(run.body(), "Manual checks") == "- /billing: tab colour"
    assert "Manual checks" not in run.comments()[0] and run.comments()[0].startswith("<!-- baton-fix by=cursor -->")


def test_a_description_request_github_refuses_defers_and_resumes_without_an_agent(run, gh):
    request = BODY.replace("## Manual checks\nnone", "## Manual checks\n- /billing: badge")
    run.agent(cursor_plan("## Manual checks\n- /billing: badge\n\nRESULT: fixed", request=request))
    gh.fail("edit")
    assert run.fix("cursor")["rc"] == 75
    assert len(run.agent_calls) == 1 and not run.labelled()
    gh.fail("edit", False)
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1   # resumed: no second run
    assert section(run.body(), "Manual checks") == "- /billing: badge"


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
    assert pr_helpers.apply_body(PR, str(request), state, ctx) == (1, False)
    gh.fail("edit", False)
    assert pr_helpers.apply_body(PR, str(request), state, ctx) == (0, True)


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
    ("RESULT: needs-info what?", f"PR #{PR} has questions", "needs_info"),
    ("RESULT: failed boom", f"PR #{PR} fix failed (claude)", "fix_failed"),
])
def test_needs_info_and_failure_go_to_the_owner(run, outcome, title, reason):
    run.agent({"outcome": outcome, "commit": False})
    assert run.fix()["rc"] == 1 and run.labelled()
    assert any(line.startswith(title) for line in run.runner_log())


def test_a_fix_with_no_commit_stops_the_loop(run):
    run.agent({"commit": False})
    assert run.fix()["rc"] == 1
    assert any(f"PR #{PR} needs you | The fix run made no commit; stopping the loop. {URL}" == line
               for line in run.runner_log())


def test_a_fix_that_only_rechecks_the_pr_updates_its_description_and_continues(run):
    run.agent({"commit": False, "file": "## Must fix\n1. adopted — listed the checks\n\n"
                                        "## Manual checks\n- /reader: colours\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 0 and not run.labelled()
    assert section(run.body(), "Manual checks") == "- /reader: colours"


def test_a_fix_with_no_commit_and_the_same_checks_stops_the_loop(run):
    run.agent({"commit": False, "file": "## Must fix\nnone\n\n## Manual checks\nnone\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 1 and run.labelled()
    assert any("The fix run made no commit" in line for line in run.runner_log())
    assert section(run.body(), "Manual checks") == "none"


def test_a_fix_with_no_commit_and_no_checks_stops_and_leaves_the_description(run):
    run.agent({"commit": False, "file": "## Must fix\n1. adopted — nothing to change\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 1 and run.labelled()
    assert run.body() == BODY


def test_a_cursor_fix_that_only_rechecks_the_pr_updates_its_description_and_continues(run):
    run.agent(cursor_plan("## Must fix\n1. adopted\n\n## Manual checks\n- /billing: badge\n\nRESULT: fixed", commit=False))
    assert run.fix("cursor")["rc"] == 0 and not run.labelled()
    assert section(run.body(), "Manual checks") == "- /billing: badge"


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

NEW_DESCRIPTION = "## What changed\nCorrected text.\n\n## Manual checks\nnone\n"
CHECKED = {"file": "## Must fix\n1. adopted\n\n## Manual checks\n- /billing: colour\n\nRESULT: fixed\n"}


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
    assert section(run.body(), "Manual checks") == "- /billing: colour"


def test_a_description_only_fix_needs_no_commit(run):
    run.agent({**CHECKED, "commit": False, "description": NEW_DESCRIPTION})
    assert run.fix("codex")["rc"] == 0 and not run.labelled()
    assert "Corrected text." in run.body()


def test_a_description_request_cannot_clear_manual_checks_without_a_recheck(run):
    run.agent({"commit": False, "file": "## Must fix\n1. adopted\n\nRESULT: fixed\n", "description": NEW_DESCRIPTION})
    run.fix("codex")
    assert section(run.body(), "Manual checks") == "Not re-checked after the fix run."


def test_an_invalid_description_request_keeps_the_body_and_still_pushes_the_commits(run):
    run.agent({**CHECKED, "description": "Closes #999\n\n## What changed\nOther.\n"})
    assert run.fix("codex")["rc"] == 1 and run.pushed() and run.labelled()
    assert "Disabled buttons." in run.body() and "#999" not in run.body()


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
