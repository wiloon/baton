"""baton.fix: fix runs against a real git worktree with a bare origin, the fake `gh`
of conftest, and a stub for the agent run. Ported from tests/manual-checks.sh,
tests/completion.sh and the fix half of tests/verify.sh."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

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
def fix_worktree(gh, tmp_path, monkeypatch):
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
    gh.set(body=BODY, headRefName="cursor/issue-419", headRefOid="a" * 40, state="OPEN", labels=[])
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

        def fix(self, fixer="claude", feedback="feedback", head="", source="reviewer", owner_triggered=False):
            args = type("A", (), {"pr": PR, "wt": str(wt), "fixer": fixer, "source": source, "head": head,
                                  "owner_triggered": owner_triggered})
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
    state.dispatch_agent = stub
    state.verify_script("pass")
    yield state
    Path(f"/tmp/baton-fix-{PR}.md").unlink(missing_ok=True)
    Path(f"/tmp/baton-fix-{PR}.md.body").unlink(missing_ok=True)


@pytest.fixture
def run(fix_worktree, monkeypatch):
    monkeypatch.setattr(fix.dispatch, "dispatch", fix_worktree.dispatch_agent)
    return fix_worktree


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


def test_a_lost_checks_refresh_response_is_recognized_without_a_second_write(run, gh):
    """A checks edit that landed before its answer was lost counts as success (#151)."""
    gh.fail("edit")
    run.agent({"file": "## Checks to verify\n- /billing: brand colour\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 0 and not run.labelled() and run.pushed()
    assert section(run.body(), "Checks to verify") == "- /billing: brand colour"
    assert len([c for c in gh.calls() if c[:2] == ["pr", "edit"] and "--body-file" in c]) == 1


def test_a_permanent_checks_refresh_failure_hands_off_without_a_deferral(run, gh):
    gh.fail("edit-before-write")
    run.agent({"file": "## Checks to verify\n- /billing: brand colour\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 1 and run.labelled() and run.pushed()
    assert section(run.body(), "Checks to verify") == "none"
    assert any("Checks not refreshed" in line for line in run.runner_log())


def test_a_transient_checks_refresh_defers_then_recovers_without_a_second_worker(run, monkeypatch):
    """fix_checks_refresh_failed: no early replay, one worker, body updated after 60s (#151)."""
    import subprocess
    from baton import handoff, session

    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    monkeypatch.setattr(fix.pr_helpers, "sleep", lambda *_: None)
    call, edits = fix.pr_helpers.call, []

    def flaky(*args, input=None, limit=None):
        if args[:2] == ("pr", "edit") and "--body-file" in args:
            edits.append(clock["now"])
            if clock["now"] < 1_060:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 503 unavailable")
        return call(*args, input=input, limit=limit)

    monkeypatch.setattr(fix.pr_helpers, "call", flaky)
    run.agent({"file": "## Checks to verify\n- /billing: brand colour\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 75
    assert len(run.agent_calls) == 1 and not run.labelled() and run.pushed()
    assert section(run.body(), "Checks to verify") == "none"
    env = session.resolve()
    saved = handoff.load_retry(env, "419", "fix_checks_refresh_failed", str(PR))
    assert saved["attempts"] == 0
    assert "- /billing: brand colour" in saved["continuation"]["intended"]
    clock["now"] = 1_059
    before = len(edits)
    assert run.fix()["rc"] == 75
    assert len(edits) == before and len(run.agent_calls) == 1
    clock["now"] = 1_060
    assert run.fix()["rc"] == 0
    assert len(run.agent_calls) == 1 and section(run.body(), "Checks to verify") == "- /billing: brand colour"
    assert 1_000 in edits and 1_060 in edits
    assert handoff.load_retry(env, "419", "fix_checks_refresh_failed", str(PR)) is None


def test_an_exhausted_checks_refresh_hands_off_exactly_once(run, monkeypatch):
    import subprocess
    from baton import handoff

    clock = {"now": 1_000}
    notes = []
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    monkeypatch.setattr(fix.pr_helpers, "sleep", lambda *_: None)
    monkeypatch.setattr(handoff.notify, "notify", lambda title, text="": notes.append((title, text)))
    call = fix.pr_helpers.call

    def always_fail(*args, input=None, limit=None):
        if args[:2] == ("pr", "edit") and "--body-file" in args:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 503 unavailable")
        return call(*args, input=input, limit=limit)

    monkeypatch.setattr(fix.pr_helpers, "call", always_fail)
    run.agent({"file": "## Checks to verify\n- /billing: brand colour\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 75
    clock["now"] = 1_060
    assert run.fix()["rc"] == 75
    clock["now"] = 1_360
    assert run.fix()["rc"] == 1 and run.labelled()
    assert sum("Checks not refreshed" in title for title, _ in notes) == 1
    assert len(run.agent_calls) == 1
    assert not list(Path(run.root / "state").glob("handoff-*-*fix_checks_refresh_failed.json"))


def cursor_plan(reply, **more):
    return {"reply": reply, **more}


def test_cursor_recheck_comes_from_its_reply(run):
    run.agent(cursor_plan("## Must fix\n1. adopted\n\n## Checks to verify\n- /billing: tab colour\n\nRESULT: fixed"))
    assert run.fix("cursor")["rc"] == 0
    assert section(run.body(), "Checks to verify") == "- /billing: tab colour"
    assert "Checks to verify" not in run.comments()[0] and run.comments()[0].startswith("<!-- baton-fix by=cursor -->")


def test_a_description_request_github_refuses_defers_and_resumes_without_an_agent(run, monkeypatch):
    clock, _ = _flaky_edits(monkeypatch)
    request = BODY.replace("## Checks to verify\nnone", "## Checks to verify\n- /billing: badge")
    run.agent(cursor_plan("## Checks to verify\n- /billing: badge\n\nRESULT: fixed", request=request))
    assert run.fix("cursor")["rc"] == 75
    assert len(run.agent_calls) == 1 and not run.labelled()
    clock["now"] = 1_060
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
    result = run.fix("cursor")
    assert result["rc"] == 0 and len(run.agent_calls) == 1, run.runner_log()   # replayed, not rerun


def test_a_description_write_whose_response_was_lost_counts_as_a_change(run, gh):
    from baton import pr as pr_helpers
    request = run.root / "request.md"
    request.write_text("Refs #297\n\nCorrected description")
    gh.set(headRefName="cursor/issue-297", body="Refs #297\n\nOld description")
    gh.fail("edit")
    state, ctx = str(run.root / "state"), str(run.root / "ctx")
    # The fake gh writes then fails: look-before-decide recognizes the landed body (#151).
    assert pr_helpers.apply_body(PR, str(request), state, ctx) == (0, True, "", False)


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
    assert (f"PR #{PR} needs you | The fix run made no commit; stopping the loop. {URL} "
            "Reason fix_no_commit (category 2).") in run.runner_log()


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


def reject_pushes(run):
    hook = run.root / "origin.git" / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho rejected by policy >&2\nexit 1\n")
    hook.chmod(0o755)


def test_a_failed_push_hands_off(run):
    """A real `git push` the remote refuses is permanent: one worker, one handoff, no retry."""
    reject_pushes(run)
    run.agent({})
    assert run.fix()["rc"] == 1 and run.labelled() and len(run.agent_calls) == 1
    assert any(f"PR #{PR}: pushing fix failed" in line for line in run.runner_log())


def test_a_partial_fix_whose_push_fails_permanently_hands_off_with_the_workers_outcome(run, monkeypatch):
    from baton import handoff

    sent = []
    monkeypatch.setattr(handoff.notify, "notify", lambda title, text="": sent.append((title, text)))
    reject_pushes(run)
    run.agent({"outcome": "RESULT: failed ran out of time"})
    assert run.fix()["rc"] == 1 and len(sent) == 1
    assert "ran out of time" in sent[0][1] and "pushing fix failed" in sent[0][1]


def test_an_unreadable_remote_ref_that_looks_transient_is_retried_not_handed_off(run, monkeypatch):
    from baton import handoff

    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    real = handoff.observe_remote

    def flaky(wt, branch):
        return (None, True) if clock["now"] < 1_060 else real(wt, branch)

    monkeypatch.setattr(handoff, "observe_remote", flaky)
    run.agent({})
    assert run.fix()["rc"] == 75 and not run.labelled() and len(run.agent_calls) == 1
    clock["now"] = 1_060
    assert run.fix()["rc"] == 0 and run.pushed() and not run.labelled()
    assert len(run.agent_calls) == 1


@pytest.mark.parametrize("interrupted", [False, True])
def test_a_recovered_remote_read_allows_the_second_due_push_to_succeed(run, monkeypatch, interrupted):
    from baton import common, handoff, session

    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    observe_remote = handoff.observe_remote
    monkeypatch.setattr(handoff, "observe_remote", lambda wt, branch: (
        (None, True) if clock["now"] == 1_000 else observe_remote(wt, branch)))
    pushes = []

    def flaky(*args, **kwargs):
        pushes.append(clock["now"])
        saved = handoff.load_retry(session.resolve(), "419", "fix_push_failed", str(PR))
        assert saved["continuation"]["remote"] == run.remote_head()
        if clock["now"] == 1_060:
            return common.PushResult(False, transient=True, detail="connection refused")
        return common.push(*args, **kwargs)

    monkeypatch.setattr(fix, "push", flaky)
    run.agent({})
    assert run.fix()["rc"] == 75 and not pushes
    clock["now"] = 1_060
    if interrupted:
        retry_path = handoff.retry_path(session.resolve(), "419", "fix_push_failed", str(PR))
        atomic_json = handoff.atomic_json

        def interrupt(path, data):
            if path == retry_path and data["continuation"]["remote"] is not None:
                raise InterruptedError("stopped while recording the remote baseline")
            atomic_json(path, data)

        with monkeypatch.context() as patch:
            patch.setattr(handoff, "atomic_json", interrupt)
            with pytest.raises(InterruptedError):
                run.fix()
    else:
        assert run.fix()["rc"] == 75
    assert not run.labelled()
    clock["now"] = 1_359
    first_push = [] if interrupted else [1_060]
    assert run.fix()["rc"] == 75 and pushes == first_push
    clock["now"] = 1_360
    assert run.fix()["rc"] == 0 and run.pushed() and not run.labelled()
    assert pushes == [*first_push, 1_360] and len(run.agent_calls) == 1


def test_an_unreadable_remote_ref_that_is_permanent_hands_off(run, monkeypatch):
    from baton import handoff

    monkeypatch.setattr(handoff, "observe_remote", lambda wt, branch: (None, False))
    run.agent({})
    assert run.fix()["rc"] == 1 and run.labelled() and not run.pushed()


@pytest.mark.parametrize("initially_transient", [False, True])
def test_a_permanently_unreadable_partial_push_ref_hands_off_with_the_worker_outcome(
        run, monkeypatch, initially_transient):
    from baton import handoff

    sent = []
    clock = {"now": 1_000}
    monkeypatch.setattr(handoff.notify, "notify", lambda title, text="": sent.append((title, text)))
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    monkeypatch.setattr(handoff, "observe_remote", lambda wt, branch: (
        None, initially_transient and clock["now"] == 1_000))
    monkeypatch.setattr(fix, "push", lambda *a, **k: pytest.fail("pushed without a remote baseline"))
    run.agent({"outcome": "RESULT: failed ran out of time"})
    if initially_transient:
        assert run.fix()["rc"] == 75 and not sent
        clock["now"] = 1_060
    assert run.fix()["rc"] == 1 and run.labelled()
    [(title, message)] = sent
    assert "pushing fix failed" in title
    assert "could not observe the remote ref" in message and "RESULT: failed ran out of time" in message
    assert len(run.agent_calls) == 1 and not run.pushed()


def test_a_missing_fix_push_record_hands_off_instead_of_running_the_worker_again(run, monkeypatch):
    from baton import common, handoff, session

    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    pushes = []

    def refused(*a, **k):
        pushes.append(1)
        return common.PushResult(False, transient=True, detail="refused")

    monkeypatch.setattr(fix, "push", refused)
    run.agent({}, {})
    assert run.fix()["rc"] == 75
    context = session.resolve()
    for path in (handoff.retry_path(context, "419", "fix_push_failed", str(PR)),
                 handoff.waiting_path(context, "419", "fix_push_failed", str(PR))):
        path.unlink(missing_ok=True)
    clock["now"] = 1_060
    assert run.fix()["rc"] == 1 and run.labelled()
    assert len(run.agent_calls) == 1 and len(pushes) == 1


def test_a_cursor_run_with_no_commit_posts_one_discard_comment(run, monkeypatch):
    run.fake_gh.set(body="Original description.", headRefName="feature/no-issue")
    run.agent(cursor_plan("Adopted.\n\nRESULT: fixed", request=BODY, commit=False))
    run.fix("cursor")
    assert sum("baton-description-discarded" in c for c in run.comments()) == 1


def test_a_transient_fix_push_defers_then_recovers_without_a_second_worker(run, monkeypatch):
    """fix_push_failed: no early replay, one worker, landed after 60s (#151)."""
    from baton import common, handoff, session

    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    pushes, real = [], common.push

    def flaky(wt, ref, *, upstream=False, signoff=False):
        pushes.append(clock["now"])
        if clock["now"] < 1_060:
            return common.PushResult(False, transient=True, detail="connection refused")
        return real(wt, ref, upstream=upstream, signoff=signoff)

    monkeypatch.setattr(fix, "push", flaky)
    run.agent({})
    assert run.fix()["rc"] == 75
    assert len(run.agent_calls) == 1 and not run.labelled() and not run.pushed()
    env = session.resolve()
    saved = handoff.load_retry(env, "419", "fix_push_failed", str(PR))
    assert saved["attempts"] == 0 and re.fullmatch(r"[0-9a-f]{40}", saved["continuation"]["commit"])
    clock["now"] = 1_059
    before = len(pushes)
    assert run.fix()["rc"] == 75
    assert len(pushes) == before and len(run.agent_calls) == 1
    clock["now"] = 1_060
    assert run.fix()["rc"] == 0
    assert len(run.agent_calls) == 1 and run.pushed()
    assert 1_000 in pushes and 1_060 in pushes
    assert handoff.load_retry(env, "419", "fix_push_failed", str(PR)) is None


def test_a_lost_fix_push_response_is_recognized_without_a_second_write(run, monkeypatch):
    """A push that landed before its answer was lost counts as success (#151)."""
    from baton import common

    pushes, real = [], common.push

    def write_then_fail(wt, ref, *, upstream=False, signoff=False):
        pushes.append(ref)
        result = real(wt, ref, upstream=upstream, signoff=signoff)
        assert result.ok
        return common.PushResult(False, transient=True, detail="connection reset")

    monkeypatch.setattr(fix, "push", write_then_fail)
    run.agent({})
    assert run.fix()["rc"] == 0 and run.pushed() and len(pushes) == 1


def test_an_exhausted_fix_push_hands_off_exactly_once(run, monkeypatch):
    from baton import common, handoff, session

    sent = []
    monkeypatch.setattr(handoff.notify, "notify", lambda title, text="": sent.append((title, text)))

    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    monkeypatch.setattr(fix, "push",
                        lambda *a, **k: common.PushResult(False, transient=True, detail="connection refused"))
    run.agent({})
    assert run.fix()["rc"] == 75
    for now in (1_060, 1_360):
        clock["now"] = now
        assert run.fix()["rc"] == (1 if now == 1_360 else 75)
    assert run.labelled() and len(run.agent_calls) == 1
    edits = [c for c in run.fake_gh.calls() if c[:2] == ["pr", "edit"] and "ready-for-human" in c]
    assert len(edits) == 1 and len(sent) == 1
    assert handoff.load_retry(session.resolve(), "419", "fix_push_failed", str(PR)) is None


def test_a_recovered_partial_push_keeps_the_original_handoff(run, monkeypatch):
    """After a partial push recovers, finish with fix_run_failed — not a second worker (#151)."""
    from baton import common, handoff

    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    pushes, real = [], common.push

    def flaky(wt, ref, *, upstream=False, signoff=False):
        pushes.append(clock["now"])
        if clock["now"] < 1_060:
            return common.PushResult(False, transient=True, detail="connection refused")
        return real(wt, ref, upstream=upstream, signoff=signoff)

    monkeypatch.setattr(fix, "push", flaky)
    run.agent({"outcome": "RESULT: failed ran out of time"})
    assert run.fix()["rc"] == 75 and not run.labelled()
    clock["now"] = 1_060
    assert run.fix()["rc"] == 1 and run.pushed() and run.labelled()
    assert len(run.agent_calls) == 1
    assert any("fix failed" in line for line in run.runner_log())


def test_an_exhausted_partial_push_hands_off_once_with_the_workers_outcome(run, monkeypatch):
    from baton import common, handoff

    sent = []
    monkeypatch.setattr(handoff.notify, "notify", lambda title, text="": sent.append((title, text)))
    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    monkeypatch.setattr(fix, "push", lambda *a, **k: common.PushResult(False, transient=True, detail="refused"))
    run.agent({"outcome": "RESULT: failed ran out of time"})
    assert run.fix()["rc"] == 75
    for now in (1_060, 1_360):
        clock["now"] = now
        run.fix()
    assert len(sent) == 1 and "ran out of time" in sent[0][1]


@pytest.mark.parametrize("damage", ["invalid JSON", "missing remote", "invalid description status"])
def test_a_damaged_fix_push_record_hands_off_without_replay(run, monkeypatch, damage):
    from baton import common, handoff, session

    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    pushes = []

    def refused(*args, **kwargs):
        pushes.append(clock["now"])
        return common.PushResult(False, transient=True, detail="connection refused")

    monkeypatch.setattr(fix, "push", refused)
    run.agent({})
    assert run.fix()["rc"] == 75
    env = session.resolve()
    path = handoff.retry_path(env, "419", "fix_push_failed", str(PR))
    if damage == "invalid JSON":
        path.write_text("{not-json")
    else:
        for record in (path, handoff.waiting_path(env, "419", "fix_push_failed", str(PR))):
            data = json.loads(record.read_text())
            if damage == "missing remote":
                del data["continuation"]["remote"]
            else:
                data["continuation"]["description_rc"] = -1
            record.write_text(json.dumps(data))
    clock["now"] = 1_060
    assert run.fix()["rc"] == 1 and run.labelled() and len(run.agent_calls) == 1
    assert pushes == [1_000] and not run.comments()


def test_a_remote_change_during_fix_push_wait_hands_off(run, monkeypatch):
    from baton import common, handoff

    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    monkeypatch.setattr(fix, "push",
                        lambda *a, **k: common.PushResult(False, transient=True, detail="connection refused"))
    run.agent({})
    assert run.fix()["rc"] == 75
    # A different commit lands on the remote while we wait.
    other = run.root / "other"
    sh(run.root, "git", "clone", "-q", str(run.root / "origin.git"), str(other))
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid")):
        sh(other, "git", "config", key, value)
    sh(other, "git", "checkout", "-q", "cursor/issue-1")
    (other / "remote-change.txt").write_text("x\n")
    sh(other, "git", "add", "-A")
    sh(other, "git", "commit", "-q", "-m", "remote")
    sh(other, "git", "push", "-q", "origin", "HEAD:cursor/issue-1")
    clock["now"] = 1_060
    assert run.fix()["rc"] == 1 and run.labelled() and not run.pushed()
    assert len(run.agent_calls) == 1


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
    monkeypatch.setattr(fix.Runner, "handoff", lambda self, pr, code, title, text: (seen.append(code), original(self, pr, code, title, text))[1])
    return seen


@pytest.mark.parametrize("rc", [1, 2])
@pytest.mark.parametrize("fixer", ["claude", "cursor"])
def test_a_description_failure_is_discarded_not_handed_off(run, monkeypatch, fixer, rc):
    """#141: a rejected or failed description request never hands off by itself. The Claude/Codex
    path continues (its checks refresh counts as a change); Cursor's no-commit run stops as fix_no_commit."""
    if fixer == "cursor" and rc == 1:
        pytest.skip("a GitHub failure defers the Cursor path before any handoff")
    seen = _codes(monkeypatch)
    monkeypatch.setattr(fix.pr_helpers, "apply_body", lambda *a, **k: (rc, False, "something", False))
    run.agent(cursor_plan("Adopted.\n\nRESULT: fixed", commit=False) if fixer == "cursor" else {**CHECKED, "commit": False})
    run.fix(fixer)
    assert seen == ([] if fixer == "claude" else ["fix_no_commit"])


@pytest.mark.parametrize("rc, code", [(1, "fix_task_write_failed"), (2, "fix_task_invalid"), (3, "fix_task_invalid")])
def test_a_task_change_failure_is_classified_by_its_result(run, monkeypatch, rc, code):
    seen = _codes(monkeypatch)

    def fake_task(self, pr, url, **kwargs):
        if rc == 1:
            return self.handoff(pr, code, f"PR #{pr}: task completion change failed", url), False, ""
        return rc, False, ""

    monkeypatch.setattr(fix.Runner, "write_task", fake_task)
    run.agent({"reply": "TASK: partial\nRESULT: fixed", "file": "## Must fix\nnone\n\nRESULT: fixed\n", "commit": False})
    run.fix("claude")
    assert seen == [code]


def test_a_cursor_task_change_that_is_unusable_is_invalid(run, monkeypatch):
    seen = _codes(monkeypatch)
    monkeypatch.setattr(fix.Runner, "write_task", lambda *a, **k: (2, False, ""))
    monkeypatch.setattr(fix.pr_helpers, "apply_body", lambda *a, **k: (0, False, "", False))
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


def test_cursor_defers_body_and_task_then_resumes_without_an_agent(run, monkeypatch):
    # Acceptance 4: matching partial body + TASK on live complete; GitHub edit fails, then resumes.
    clock, _ = _flaky_edits(monkeypatch)
    run.fake_gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    text = "Task completion: partial\n\n## What changed\nFixed all items.\n\n## Checks to verify\nnone\n"
    run.agent(cursor_plan("## Checks to verify\nnone\n\nTASK: partial\nRESULT: fixed", request=text))
    assert run.fix("cursor")["rc"] == 75 and len(run.agent_calls) == 1 and not run.labelled()
    assert run.body() == COMPLETE
    clock["now"] = 1_060
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1 and run.pushed()
    assert "completion=partial" in run.body() and "Refs #419" in run.body()
    assert "Fixed all items." in run.body()


def test_a_lost_task_write_response_is_recognized_without_a_second_write(run, gh):
    """A TASK: edit that landed before its answer was lost counts as success (#151)."""
    initial = COMPLETE.replace("## Checks to verify\nnone", "## Checks to verify\n- /billing: badge")
    gh.set(body=initial, headRefName="cursor/issue-419")
    run.agent(cursor_plan("Agreed.\nTASK: partial\nRESULT: fixed", commit=False))
    gh.fail("edit")
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1 and not run.labelled()
    assert "completion=partial" in run.body() and "Refs #419" in run.body()
    assert section(run.body(), "Checks to verify") == "- /billing: badge"
    assert len([c for c in gh.calls() if c[:2] == ["pr", "edit"] and "--body-file" in c]) == 1


def test_a_transient_task_write_defers_then_recovers_without_a_second_worker(run, monkeypatch):
    """fix_task_write_failed: no early replay, one worker, body updated after 60s (#151)."""
    import subprocess
    from baton import handoff, session

    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    monkeypatch.setattr(fix.pr_helpers, "sleep", lambda *_: None)
    call, edits = fix.pr_helpers.call, []
    initial = COMPLETE.replace("## Checks to verify\nnone", "## Checks to verify\n- /billing: badge")
    run.fake_gh.set(body=initial, headRefName="cursor/issue-419")

    def flaky(*args, input=None, limit=None):
        if args[:2] == ("pr", "edit") and "--body-file" in args:
            edits.append(clock["now"])
            if clock["now"] < 1_060:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 503 unavailable")
        return call(*args, input=input, limit=limit)

    monkeypatch.setattr(fix.pr_helpers, "call", flaky)
    run.agent(cursor_plan("Agreed.\nTASK: partial\nRESULT: fixed", commit=False))
    assert run.fix("cursor")["rc"] == 75 and len(run.agent_calls) == 1 and not run.labelled()
    env = session.resolve()
    saved = handoff.load_retry(env, "419", "fix_task_write_failed", str(PR))
    assert saved["attempts"] == 0 and saved["continuation"]["want"] == "partial"
    clock["now"] = 1_059
    before = len(edits)
    assert run.fix("cursor")["rc"] == 75
    assert len(edits) == before and len(run.agent_calls) == 1
    clock["now"] = 1_060
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1
    assert "completion=partial" in run.body() and section(run.body(), "Checks to verify") == "- /billing: badge"
    assert 1_000 in edits and 1_060 in edits
    assert handoff.load_retry(env, "419", "fix_task_write_failed", str(PR)) is None


def test_a_permanent_task_write_failure_hands_off_without_a_deferral(run, gh):
    initial = COMPLETE.replace("## Checks to verify\nnone", "## Checks to verify\n- /billing: badge")
    gh.set(body=initial, headRefName="cursor/issue-419")
    run.agent(cursor_plan("Agreed.\nTASK: partial\nRESULT: fixed", commit=False))
    gh.fail("edit-before-write")
    assert run.fix("cursor")["rc"] == 1 and run.labelled() and len(run.agent_calls) == 1
    assert run.body() == initial
    assert any("task completion change failed" in line for line in run.runner_log())


def test_cursor_discards_a_request_it_cannot_read_the_body_for_then_hands_off_no_commit(run, gh):
    # Section 5: nothing was captured to replay, so the request is discarded and the
    # no-commit fix hands off under its own reason, not a description reason.
    run.agent(cursor_plan("RESULT: fixed", request=BODY, commit=False))
    gh.fail("view")
    assert run.fix("cursor")["rc"] == 1 and len(run.agent_calls) == 1 and run.labelled()
    gh.fail("view", False)
    assert run.body() == BODY
    assert any("made no commit" in line for line in run.runner_log())


@pytest.mark.parametrize("request_kind", ["task-only", "rejected"])
def test_a_deferred_task_change_preserves_the_owners_issue_correction(run, monkeypatch, request_kind):
    """An issue change during a category-1 task wait hands off (#151)."""
    import subprocess
    from baton import handoff

    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    monkeypatch.setattr(fix.pr_helpers, "sleep", lambda *_: None)
    call = fix.pr_helpers.call
    run.fake_gh.set(body=COMPLETE, headRefName="owner-branch")
    request = None if request_kind == "task-only" else COMPLETE.replace("issue=419", "issue=999")
    reply = "## Checks to verify\nnone\n\nTASK: partial\nRESULT: fixed"
    run.agent(cursor_plan(reply, request=request, commit=False))

    def flaky(*args, input=None, limit=None):
        if args[:2] == ("pr", "edit") and "--body-file" in args and clock["now"] < 1_060:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 503 unavailable")
        return call(*args, input=input, limit=limit)

    monkeypatch.setattr(fix.pr_helpers, "call", flaky)
    assert run.fix("cursor")["rc"] == 75

    corrected = COMPLETE.replace("419", "420").replace("## Checks to verify\nnone", "## Checks to verify\n- Owner check")
    run.fake_gh.set(body=corrected)
    clock["now"] = 1_060
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


def test_cursor_applies_task_after_a_rejected_body_when_the_write_lands(run, gh):
    """Invalid description is discarded; a lost TASK: response still counts as success (#151)."""
    gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    bad = "Task completion: partial\nTask completion: complete\n\n## What changed\nNope.\n"
    run.agent(cursor_plan("TASK: partial\nRESULT: fixed", request=bad, commit=False))
    gh.fail("edit")
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1 and not run.labelled()
    assert "Nope." not in run.body() and "completion=partial" in run.body() and "Refs #419" in run.body()
    assert any("description request not applied" in line for line in run.runner_log())


def test_cursor_lost_body_response_still_counts_on_resume(run, gh):
    # Lost success response: fake gh writes then fails; look-before-decide finishes without a wait.
    gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    text = COMPLETE.replace("Disabled buttons.", "Fixed all items.")
    run.agent(cursor_plan("## Checks to verify\nnone\n\nTASK: partial\nRESULT: fixed",
                          request=text, commit=False))
    gh.fail("edit")
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1 and not run.labelled()
    assert "Fixed all items." in run.body() and "completion=partial" in run.body()
    assert len([c for c in gh.calls() if c[:2] == ["pr", "edit"] and "--body-file" in c]) == 1


def test_a_transient_description_edit_defers_then_recovers_without_a_second_worker(run, monkeypatch):
    """Cursor description edits wait for the shared 60s budget, then recover without a second worker (#151)."""
    from baton import handoff, session

    clock, edits = _flaky_edits(monkeypatch)
    request = BODY.replace("Disabled buttons.", "Corrected description")
    run.agent(cursor_plan("## Checks to verify\nnone\n\nRESULT: fixed", request=request, commit=False))
    assert run.fix("cursor")["rc"] == 75 and len(run.agent_calls) == 1 and not run.labelled()
    saved = handoff.load_retry(session.resolve(), "419", "fix_description_write_failed", str(PR))
    assert saved["attempts"] == 0 and saved["due"] == 1_060
    clock["now"] = 1_059
    before = len(edits)
    assert run.fix("cursor")["rc"] == 75
    assert len(edits) == before and len(run.agent_calls) == 1
    clock["now"] = 1_060
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1
    assert "Corrected description" in run.body() and "Disabled buttons." not in run.body()
    assert not run.labelled()
    assert not list((run.root / "state").glob("handoff-*-*fix_description_write_failed.json"))


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
    assert "verify failed (repair 2 of 2 in this call)" in " ".join(run.runner_log())


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


def test_a_rejected_push_with_an_unreadable_lookup_hands_off_without_replay(run, monkeypatch):
    from baton import common, session

    pushes = []
    real = session.run_process

    def lookup(cmd, **kwargs):
        if "ls-remote" in cmd and pushes:
            return subprocess.CompletedProcess(cmd, 1, "", "connection refused")
        return real(cmd, **kwargs)

    def reject(*args, **kwargs):
        pushes.append(1)
        return common.PushResult(False, detail="pre-receive hook declined")

    monkeypatch.setattr(session, "run_process", lookup)
    monkeypatch.setattr(fix, "push", reject)
    run.agent({})
    assert run.fix()["rc"] == 1
    assert pushes == [1] and run.labelled()
    assert sum("handoff fix_push_failed" in line for line in run.runner_log()) == 1


@pytest.mark.parametrize("outcome", ["RESULT: failed timed out", "RESULT: needs-info a question"])
@pytest.mark.parametrize("recover", [True, False])
def test_cursor_partial_commits_use_the_saved_push(run, monkeypatch, outcome, recover):
    from baton import common, handoff

    now, pushes = [1_000], []
    monkeypatch.setattr(handoff, "clock", lambda: now[0])

    def push(*args, **kwargs):
        pushes.append(now[0])
        if recover and now[0] == 1_060:
            return common.push(*args, **kwargs)
        return common.PushResult(False, transient=recover, detail="push failed")

    notices = []
    monkeypatch.setattr(handoff.notify, "notify", lambda title, text: notices.append(text))
    monkeypatch.setattr(fix, "push", push)
    run.agent({"outcome": outcome})
    if recover:
        assert run.fix("cursor")["rc"] == 75 and not run.labelled()
        now[0] = 1_060
    assert run.fix("cursor")["rc"] == 1 and run.labelled()
    assert run.pushed() is recover
    assert pushes == ([1_000, 1_060] if recover else [1_000])
    assert len(run.agent_calls) == len(notices) == 1 and outcome in notices[0]


@pytest.mark.parametrize("partial", [False, True])
def test_a_restart_during_a_landed_fix_continuation_skips_the_worker_and_push(run, monkeypatch, partial):
    from baton import common

    pushes = []

    def push(*args, **kwargs):
        pushes.append(1)
        return common.push(*args, **kwargs)

    monkeypatch.setattr(fix, "push", push)
    run.agent({"outcome": "RESULT: failed ran out of time"} if partial else {})

    def interrupt(*args, **kwargs):
        raise InterruptedError("stopped during continuation")

    with monkeypatch.context() as patch:
        if partial:
            patch.setattr(fix.Runner, "handoff", interrupt)
        else:
            patch.setattr(fix.Runner, "refresh_checks", interrupt)
        with pytest.raises(InterruptedError, match="during continuation"):
            run.fix()
    assert run.pushed() and not run.labelled()
    assert run.fix()["rc"] == int(partial)
    assert len(run.agent_calls) == len(pushes) == 1
    assert run.labelled() is partial


@pytest.mark.parametrize("change", ["feedback", "paused", "closed", "issue closed", "landed plus feedback"])
def test_a_waiting_review_push_revalidates_owner_inputs_before_replay_or_continuation(run, monkeypatch, change):
    from baton import common, handoff, session

    now, pushes = [1_000], []
    monkeypatch.setattr(handoff, "clock", lambda: now[0])

    def fail(*args, **kwargs):
        pushes.append(now[0])
        return common.PushResult(False, transient=True, detail="connection refused")

    monkeypatch.setattr(fix, "push", fail)
    monkeypatch.setenv("ROUND", "1")
    run.agent({})
    assert run.fix()["rc"] == 75
    if change in ("feedback", "landed plus feedback"):
        run.fake_gh.set(comments=[{"author": {"login": "owner"}, "body": "Please stop"}])
    if change == "landed plus feedback":
        assert common.push(run.worktree, "HEAD")
    if change == "paused":
        run.fake_gh.set(labels=[{"name": "ready-for-human"}])
    if change == "closed":
        run.fake_gh.set(state="CLOSED")
    if change == "issue closed":
        run.fake_gh.set(issues={"419": {"state": "CLOSED"}})
    now[0] = 1_060
    if change == "paused":
        assert run.fix()["rc"] == 75 and pushes == [1_000] and not run.labelled()
        saved = handoff.load_retry(session.resolve(), "419", handoff.FIX_PUSH, str(PR))
        assert saved["attempts"] == 0
        run.fake_gh.set(labels=[])
        monkeypatch.setattr(fix, "push", common.push)
        assert run.fix()["rc"] == 0 and run.pushed() and len(run.agent_calls) == 1
        return
    assert run.fix()["rc"] != 75
    assert pushes == [1_000] and len(run.agent_calls) == 1
    assert handoff.load_retry(session.resolve(), "419", handoff.FIX_PUSH, str(PR)) is None
    assert run.labelled() is (change in ("feedback", "landed plus feedback"))


def test_restart_after_refreshing_checks_preserves_the_fixers_check_section(run, monkeypatch):
    from baton import handoff

    run.agent({"file": "## Must fix\nnone\n\n## Checks to verify\n- View the updated button\n\nRESULT: fixed\n"})
    restore = handoff.restore_retry

    def interrupt(env, episode, reason, item, pending):
        if pending.get("phase") == "finished":
            raise InterruptedError("stopped after refreshing checks")
        return restore(env, episode, reason, item, pending)

    with monkeypatch.context() as patch:
        patch.setattr(handoff, "restore_retry", interrupt)
        with pytest.raises(InterruptedError, match="after refreshing checks"):
            run.fix()
    assert section(run.body(), "Checks to verify") == "- View the updated button"
    assert run.fix()["rc"] == 0 and len(run.agent_calls) == 1
    assert section(run.body(), "Checks to verify") == "- View the updated button"


# --- live worker retries (#125) --------------------------------------------------------------

@pytest.fixture
def advised_fix(run, monkeypatch):
    from baton import advisor
    from baton.config import Advisor
    monkeypatch.setenv(advisor.KEY, "sk-" + "f" * 24)
    monkeypatch.setattr(advisor, "settings", lambda env: Advisor(env_file=None, mode="live"))
    run.fake_gh.set(state="OPEN", baseRefName="main", headRefOid=run.remote_head(), comments=[], labels=[], reviews=[])
    run.advice, run.advice_calls = [], []

    def call(cfg, key, snapshot, actions, **_):
        run.advice_calls.append((snapshot, actions))
        return run.advice.pop(0), {}
    monkeypatch.setattr(advisor, "call_deepseek", call)
    return run


def test_a_live_retry_step_re_runs_a_failed_fix(advised_fix):
    advised_fix.advice = [{"action": "retry_step", "reason": "transient worker failure",
                           "evidence": ["Decision point: fix_run_failed"]}]
    advised_fix.agent({"outcome": "RESULT: failed boom", "commit": False},
                      {"outcome": "RESULT: fixed", "commit": True, "reply": "RESULT: fixed"})
    assert advised_fix.fix()["rc"] == 0 and not advised_fix.labelled()
    assert len(advised_fix.agent_calls) == 2
    assert len(advised_fix.advice_calls) == 1
    assert "fix_run_failed" in advised_fix.advice_calls[0][0]


def test_a_live_alternate_does_not_rerun_the_failed_fixer(advised_fix):
    advised_fix.advice = [{"action": "retry_step_other_vendor", "reason": "try another fixer",
                           "evidence": ["Decision point: fix_run_failed"]}]
    advised_fix.agent({"outcome": "RESULT: failed boom", "commit": True},
                      {"outcome": "RESULT: fixed", "commit": True, "reply": "RESULT: fixed"})
    assert advised_fix.fix("claude")["rc"] == 0
    assert [call[0] for call in advised_fix.agent_calls] == ["claude", "codex"]
    assert set((advised_fix.root / "state" / f"pr-{PR}-authors").read_text().split()) == {"claude", "codex"}


@pytest.mark.parametrize(("fixer", "action", "next_fixer"), [
    ("claude", "retry_step", "claude"),
    ("codex", "retry_step_other_vendor", "claude"),
])
def test_a_due_partial_push_resumes_live_recovery(advised_fix, monkeypatch, fixer, action, next_fixer):
    from baton import common, handoff

    now, pushes = [1_000], []
    monkeypatch.setattr(handoff, "clock", lambda: now[0])

    def flaky_push(wt, ref, **kwargs):
        pushes.append(now[0])
        if now[0] < 1_060:
            return common.PushResult(False, transient=True, detail="connection refused")
        result = common.push(wt, ref, **kwargs)
        advised_fix.fake_gh.set(headRefOid=advised_fix.remote_head())
        return result

    monkeypatch.setattr(fix, "push", flaky_push)
    advised_fix.advice = [{"action": action, "reason": "transient worker failure",
                           "evidence": ["Decision point: fix_run_failed"]}]
    advised_fix.agent({"outcome": "RESULT: failed boom"}, {})
    assert advised_fix.fix(fixer, feedback="Keep the original findings")["rc"] == 75
    assert pushes == [1_000] and not advised_fix.advice_calls

    now[0] = 1_060
    result = advised_fix.fix(fixer, feedback="")
    assert result["rc"] == 0 and advised_fix.pushed() and not advised_fix.labelled()
    assert result["authors"] == list(dict.fromkeys([fixer, next_fixer]))
    assert pushes == [1_000, 1_060, 1_060]
    assert [call[0] for call in advised_fix.agent_calls] == [fixer, next_fixer]
    assert len(advised_fix.advice_calls) == 1
    assert "Keep the original findings" in advised_fix.prompts[1]


@pytest.mark.parametrize("interrupted", [False, True])
def test_a_landed_cursor_partial_push_runs_live_recovery(advised_fix, monkeypatch, interrupted):
    from baton import common, handoff

    pushes = []

    def push(wt, ref, **kwargs):
        pushes.append(ref)
        result = common.push(wt, ref, **kwargs)
        advised_fix.fake_gh.set(headRefOid=advised_fix.remote_head())
        return result

    monkeypatch.setattr(fix, "push", push)
    advised_fix.advice = [{"action": "retry_step", "reason": "transient worker failure",
                           "evidence": ["Decision point: fix_run_failed"]}]
    advised_fix.agent(cursor_plan("RESULT: failed boom", outcome="RESULT: failed boom"),
                      cursor_plan("RESULT: fixed"))
    if interrupted:
        restore = handoff.restore_retry

        def stop_after_landing(env, episode, reason, item, pending):
            restore(env, episode, reason, item, pending)
            if pending.get("phase") == "landed":
                raise InterruptedError("stopped after landing")

        with monkeypatch.context() as patch:
            patch.setattr(handoff, "restore_retry", stop_after_landing)
            with pytest.raises(InterruptedError, match="stopped after landing"):
                advised_fix.fix("cursor")
        assert advised_fix.pushed() and len(pushes) == 1 and not advised_fix.advice_calls

    assert advised_fix.fix("cursor")["rc"] == 0 and advised_fix.pushed() and not advised_fix.labelled()
    assert [call[0] for call in advised_fix.agent_calls] == ["cursor", "cursor"]
    assert len(pushes) == 2 and len(advised_fix.advice_calls) == 1
    advised_fix.agent(cursor_plan("RESULT: fixed"))
    assert advised_fix.fix("cursor", feedback="Next review round")["rc"] == 0
    assert len(advised_fix.agent_calls) == 3 and not advised_fix.labelled()


def test_alternate_fixer_uses_the_cursor_adapter(advised_fix, monkeypatch):
    monkeypatch.setenv("VENDOR_POOL", "claude")
    advised_fix.advice = [{"action": "retry_step_other_vendor", "reason": "try Cursor",
                           "evidence": ["Decision point: fix_run_failed"]}]
    advised_fix.agent({"outcome": "RESULT: failed boom", "commit": False},
                      cursor_plan("RESULT: fixed", commit=True))
    result = advised_fix.fix("claude")
    assert result["rc"] == 0 and result["fixer"] == "cursor"
    assert [call[0] for call in advised_fix.agent_calls] == ["claude", "cursor"]
    assert "cursor-fix" in advised_fix.agent_calls[1][2]


def test_a_verification_retry_after_deferral_uses_the_saved_budget(advised_fix, monkeypatch):
    from baton.common import Deferred

    advised_fix.verify_script("lint broke", "lint broke", "lint broke", "pass")
    advised_fix.advice = [{"action": "retry_step", "reason": "flaky local check",
                           "evidence": ["Decision point: fix_verify_failed"]}]
    advised_fix.agent({"commit": True}, {"commit": True}, {"commit": True})
    verify = fix.Runner.verify

    def defer_retry(self, *args, **kwargs):
        if kwargs.get("repairs") == 0 and len(advised_fix.agent_calls) == 3 and not defer_retry.used:
            defer_retry.used = True
            raise Deferred("claude", "short block")
        return verify(self, *args, **kwargs)

    defer_retry.used = False
    monkeypatch.setattr(fix.Runner, "verify", defer_retry)
    assert advised_fix.fix()["rc"] == 75
    assert advised_fix.fix()["rc"] == 0
    assert len(advised_fix.agent_calls) == 3
    assert len(advised_fix.advice_calls) == 1


def test_a_landed_task_write_keeps_the_granted_verification_budget(advised_fix, monkeypatch):
    from baton.common import Deferred

    advised_fix.fake_gh.set(body=COMPLETE)
    advised_fix.verify_script("lint broke", "lint broke", "lint broke", "pass")
    advised_fix.advice = [{"action": "retry_step", "reason": "transient verification",
                           "evidence": ["Decision point: fix_verify_failed"]}]
    advised_fix.agent(_TASK_REPLY, {"commit": True}, {"commit": True})
    verify = fix.Runner.verify
    budgets = []

    def defer_once(self, *args, **kwargs):
        budgets.append(kwargs.get("repairs"))
        if len(budgets) == 2:
            raise Deferred("claude", "short block")
        return verify(self, *args, **kwargs)

    monkeypatch.setattr(fix.Runner, "verify", defer_once)
    assert advised_fix.fix()["rc"] == 75
    assert "completion=partial" in advised_fix.body() and advised_fix.pushed_new
    # The review loop resumes pending metadata without fetching the worker's original prompt.
    assert advised_fix.fix(feedback="")["rc"] == 0 and advised_fix.pushed()
    assert budgets == [None, 0, 0]
    assert len(advised_fix.agent_calls) == 3


def _retry_then_block_in_verify(advised_fix, monkeypatch):
    """A granted retry commits; the verification after it is cut short by a short block."""
    from baton.common import Deferred

    advised_fix.advice = [{"action": "retry_step", "reason": "transient worker failure",
                           "evidence": ["Decision point: fix_run_failed"]}]
    advised_fix.agent({"outcome": "RESULT: failed boom", "commit": False},
                      {"outcome": "RESULT: fixed", "commit": True, "reply": "RESULT: fixed"})
    verify = fix.Runner.verify
    calls = []

    def verify_once_blocked(self, *args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise Deferred("claude", "short block")
        return verify(self, *args, **kwargs)

    monkeypatch.setattr(fix.Runner, "verify", verify_once_blocked)
    return calls


def test_a_deferral_after_the_retry_committed_continues_without_a_replay(advised_fix, monkeypatch):
    _retry_then_block_in_verify(advised_fix, monkeypatch)
    assert advised_fix.fix()["rc"] == 75
    assert advised_fix.fix()["rc"] == 0 and not advised_fix.labelled()
    assert len(advised_fix.agent_calls) == 2 and len(advised_fix.advice_calls) == 1


def test_a_restart_after_the_retry_was_dispatched_hands_off_instead_of_dispatching_again(advised_fix, monkeypatch):
    calls = _retry_then_block_in_verify(advised_fix, monkeypatch)
    monkeypatch.setattr(fix.Runner, "verify", lambda self, *a, **k: calls.append(a) or (_ for _ in ()).throw(
        RuntimeError("killed")))
    with pytest.raises(RuntimeError):
        advised_fix.fix()
    reasons = []
    handoff = fix.Runner.handoff
    monkeypatch.setattr(fix.Runner, "handoff", lambda self, pr, reason, *a, **k: reasons.append(reason) or handoff(self, pr, reason, *a, **k))
    assert advised_fix.fix()["rc"] == 1 and advised_fix.labelled()
    assert reasons == ["fix_run_failed"]
    assert len(advised_fix.agent_calls) == 2 and len(advised_fix.advice_calls) == 1


def test_a_restart_after_a_verify_retry_dispatched_hands_off_with_the_saved_reason(advised_fix, monkeypatch):
    advised_fix.verify_script("lint broke")
    advised_fix.advice = [{"action": "retry_step", "reason": "flaky local check",
                           "evidence": ["Decision point: fix_verify_failed"]}]
    advised_fix.agent({"commit": True}, {"commit": True}, {"commit": True})
    verify = fix.Runner.verify

    def die_in_retry(self, *args, **kwargs):
        if kwargs.get("repairs") == 0:
            raise RuntimeError("killed")
        return verify(self, *args, **kwargs)

    monkeypatch.setattr(fix.Runner, "verify", die_in_retry)
    with pytest.raises(RuntimeError):
        advised_fix.fix()
    monkeypatch.setattr(fix.Runner, "verify", verify)
    reasons = []
    handoff = fix.Runner.handoff
    monkeypatch.setattr(fix.Runner, "handoff", lambda self, pr, reason, *a, **k: reasons.append(reason) or handoff(self, pr, reason, *a, **k))
    assert advised_fix.fix()["rc"] == 1 and advised_fix.labelled()
    assert reasons == ["fix_verify_failed"]


def test_a_verification_retry_checks_the_base_it_failed_on(advised_fix, monkeypatch):
    from baton.common import git
    from baton.common import Deferred

    advised_fix.verify_script("lint broke", "lint broke", "lint broke", "pass")
    advised_fix.advice = [{"action": "retry_step", "reason": "flaky local check",
                           "evidence": ["Decision point: fix_verify_failed"]}]
    advised_fix.agent({"commit": True}, {"commit": True}, {"commit": True})
    verify, bases = fix.Runner.verify, []

    def spy(self, wt, base, *args, **kwargs):
        bases.append(base)
        if kwargs.get("repairs") == 0 and not spy.used:
            spy.used = True
            raise Deferred("claude", "short block")
        return verify(self, wt, base, *args, **kwargs)

    spy.used = False
    monkeypatch.setattr(fix.Runner, "verify", spy)
    git(advised_fix.worktree, "update-ref", "refs/remotes/origin/main", "HEAD")
    original = git(advised_fix.worktree, "rev-parse", "origin/main")
    assert advised_fix.fix()["rc"] == 75
    tree = git(advised_fix.worktree, "rev-parse", "origin/main^{tree}")
    tip = git(advised_fix.worktree, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit-tree", tree,
              "-p", "origin/main", "-m", "main moved")
    git(advised_fix.worktree, "update-ref", "refs/remotes/origin/main", tip)
    assert advised_fix.fix()["rc"] == 0
    assert bases[1:] == [original, original]   # both the deferred and the resumed retry


@pytest.mark.parametrize("reason", ["fix_no_commit", "fix_task_invalid"])
def test_cursor_retry_replaces_the_saved_request(advised_fix, monkeypatch, reason):
    advised_fix.advice = [{"action": "retry_step", "reason": "correct the request",
                           "evidence": [f"Decision point: {reason}"]}]
    if reason == "fix_task_invalid":
        calls = iter([(2, False, "invalid task relation"), (0, False, "")])
        monkeypatch.setattr(fix.Runner, "write_task", lambda *a, **k: next(calls))
        attempt = cursor_plan("TASK: partial\nRESULT: fixed", commit=False)
    else:
        attempt = cursor_plan("RESULT: fixed", request=BODY, commit=False)
    advised_fix.agent(attempt, cursor_plan("RESULT: fixed", commit=True))
    assert advised_fix.fix("cursor")["rc"] == 0
    assert [call[0] for call in advised_fix.agent_calls] == ["cursor", "cursor"]
    if reason == "fix_task_invalid":
        assert "Validation error: invalid task relation" in next(
            (p.read_text() for p in (advised_fix.root / "ctx").glob(f"pr-{PR}-cursor-fix-*.md")
             if "Validation error" in p.read_text()), "")


@pytest.mark.parametrize("enabled", [True, False])
def test_observe_records_a_recommendation_without_rerunning(advised_fix, monkeypatch, enabled):
    from baton import advisor
    from baton.config import Advisor

    monkeypatch.setattr(advisor, "settings", lambda env: Advisor(env_file=None, mode="observe"))
    if not enabled:
        from dataclasses import replace

        from baton import session
        configured = session.resolve(os.environ)
        opted_out = replace(configured, repo=replace(configured.repo, advisor=False))
        args = type("Args", (), {"pr": PR, "wt": str(advised_fix.worktree), "fixer": "claude",
                                  "source": "reviewer", "head": ""})
    advised_fix.advice = [{"action": "retry_step", "reason": "transient",
                           "evidence": ["Decision point: fix_run_failed"]}]
    advised_fix.agent({"outcome": "RESULT: failed boom", "commit": False})
    result = advised_fix.fix() if enabled else fix.run("pr", args, opted_out, "feedback")
    assert result["rc"] == 1 and advised_fix.labelled()
    assert len(advised_fix.agent_calls) == 1
    assert len(advised_fix.advice_calls) == int(enabled)


@pytest.mark.parametrize("failure", ["unreadable PR", "changed PR head", "provider error", "invalid reply"])
def test_fix_retry_requires_fresh_pr_state_and_valid_advice(advised_fix, monkeypatch, failure):
    from baton import advisor

    if failure == "unreadable PR":
        monkeypatch.setattr(fix.pr_helpers, "view", lambda *a, **k: None)
    advised_fix.advice = [{"action": "retry_step", "reason": "transient",
                           "evidence": ["Decision point: fix_run_failed"]}]
    if failure == "changed PR head":
        original = advisor.call_deepseek

        def new_head(*args, **kwargs):
            advised_fix.fake_gh.set(headRefOid="b" * 40)
            return original(*args, **kwargs)

        monkeypatch.setattr(advisor, "call_deepseek", new_head)
    if failure in ("provider error", "invalid reply"):
        def invalid(*args, **kwargs):
            advised_fix.advice_calls.append((args[2], args[3]))
            if failure == "provider error":
                raise OSError("provider unavailable")
            return {"action": "retry_step", "reason": None, "evidence": "bad"}, {}
        monkeypatch.setattr(advisor, "call_deepseek", invalid)
    advised_fix.agent({"outcome": "RESULT: failed boom", "commit": False})
    assert advised_fix.fix()["rc"] == 1 and advised_fix.labelled()
    assert len(advised_fix.agent_calls) == len(advised_fix.advice_calls) == 1


def test_a_live_verify_retry_rechecks_within_the_same_budget(advised_fix):
    from baton import recovery
    advised_fix.verify_script("lint broke", "lint broke", "lint broke", "pass")
    advised_fix.advice = [{"action": "retry_step", "reason": "flaky local check",
                           "evidence": ["Decision point: fix_verify_failed"]}]
    advised_fix.agent({"commit": True, "reply": "RESULT: fixed"},
                      {"outcome": "RESULT: fixed", "commit": True},
                      {"outcome": "RESULT: fixed", "commit": True})
    assert advised_fix.fix()["rc"] == 0 and not advised_fix.labelled()
    assert len(advised_fix.advice_calls) == 1
    assert "fix_verify_failed" in advised_fix.advice_calls[0][0]
    assert "repairs 2 of 2" in advised_fix.advice_calls[0][0]
    assert advised_fix.advice_calls[0][1] == ("handoff", "retry_step")


def test_a_live_verify_retry_still_red_hands_off_without_a_fresh_budget(advised_fix):
    advised_fix.verify_script("lint broke")
    advised_fix.advice = [{"action": "retry_step", "reason": "flaky local check",
                           "evidence": ["Decision point: fix_verify_failed"]}]
    advised_fix.agent({"commit": True, "reply": "RESULT: fixed"},
                      {"outcome": "RESULT: fixed", "commit": True},
                      {"outcome": "RESULT: fixed", "commit": True})
    assert advised_fix.fix()["rc"] == 1 and advised_fix.labelled()
    assert len(advised_fix.agent_calls) == 3 and len(advised_fix.advice_calls) == 1


def test_a_live_handoff_advice_at_fix_verify_hands_off(advised_fix):
    advised_fix.verify_script("lint broke")
    advised_fix.advice = [{"action": "handoff", "reason": "needs a human",
                           "evidence": ["Decision point: fix_verify_failed"]}]
    advised_fix.agent({"commit": True, "reply": "RESULT: fixed"},
                      {"outcome": "RESULT: fixed", "commit": True},
                      {"outcome": "RESULT: fixed", "commit": True})
    assert advised_fix.fix()["rc"] == 1 and advised_fix.labelled()
    assert len(advised_fix.agent_calls) == 3 and len(advised_fix.advice_calls) == 1


def test_verify_still_red_with_zero_remaining_rechecks_once(run):
    """Point 12: remaining budget 0 means one recheck only, no repair agent; rounds keep prior count."""
    run.verify_script("lint broke")
    from baton import hooks as telemetry, session
    env = session.resolve(os.environ)
    runner = fix.Runner(env, pr=PR, hooks=telemetry.from_env(env, pr=PR))
    rc, failure, rounds = runner.verify(str(run.worktree), "origin/main", "claude", "pr", PR,
                                        repairs=0, repairs_before=2)
    assert (rc, failure, rounds) == (1, "lint broke", 2)
    assert run.agent_calls == []


@pytest.mark.parametrize("fixer", ["claude", "codex"])
def test_a_checks_only_reply_posts_no_fix_comment(run, fixer):
    run.agent({"file": "## Checks to verify\nnone\n\nRESULT: fixed\n"})
    assert run.fix(fixer)["rc"] == 0
    assert run.comments() == []


# --- deferred TASK: writes keep the rest of the fix (#151) ---
def _flaky_edits(monkeypatch, fail_until=1_060):
    """A GitHub whose body edits answer 503 until the clock reaches FAIL_UNTIL; returns (clock, edits)."""
    import subprocess
    from baton import handoff

    clock, edits = {"now": 1_000}, []
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    monkeypatch.setattr(fix.pr_helpers, "sleep", lambda *_: None)
    call = fix.pr_helpers.call

    def flaky(*args, input=None, limit=None):
        if args[:2] == ("pr", "edit") and "--body-file" in args:
            edits.append(clock["now"])
            if clock["now"] < fail_until:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 503 unavailable")
        return call(*args, input=input, limit=limit)

    monkeypatch.setattr(fix.pr_helpers, "call", flaky)
    return clock, edits


_TASK_REPLY = {"reply": "TASK: partial\nRESULT: fixed", "file": "## Must fix\nnone\n\nRESULT: fixed\n"}


def test_a_deferred_task_write_resumes_verify_and_push_without_a_second_worker(run, monkeypatch):
    clock, _ = _flaky_edits(monkeypatch)
    run.verify_gate_script("")
    run.fake_gh.set(body=COMPLETE)
    run.agent(_TASK_REPLY)
    assert run.fix("claude")["rc"] == 75
    assert run.pushed_new and not run.gate_ran() and len(run.agent_calls) == 1
    clock["now"] = 1_060
    assert run.fix("claude")["rc"] == 0
    assert run.gate_ran()
    assert len(run.agent_calls) == 1 and run.pushed() and not run.labelled()
    assert "completion=partial" in run.body()


def test_a_restart_after_the_task_edit_continues_without_another_edit_or_worker(run, monkeypatch):
    clock, edits = _flaky_edits(monkeypatch)
    run.fake_gh.set(body=COMPLETE)
    run.agent({**_TASK_REPLY, "file": "## Checks to verify\nnone\n\nRESULT: fixed\n"})
    run.verify_gate_script("")
    assert run.fix()["rc"] == 75
    clock["now"] = 1_060

    def interrupted(*args, **kwargs):
        raise InterruptedError("runner stopped before verification")

    with monkeypatch.context() as stopped:
        stopped.setattr(fix.Runner, "verify", interrupted)
        with pytest.raises(InterruptedError):
            run.fix()
    assert "completion=partial" in run.body()
    assert run.pushed_new and not run.gate_ran()
    edit_count = len(edits)

    assert run.fix()["rc"] == 0
    assert run.gate_ran() and run.pushed()
    assert len(edits) == edit_count and len(run.agent_calls) == 1


@pytest.mark.parametrize("damage", ["missing", "empty", "wrong_type"])
def test_a_damaged_task_continuation_hands_off_before_reconciling(run, monkeypatch, damage):
    clock, edits = _flaky_edits(monkeypatch)
    run.fake_gh.set(body=COMPLETE)
    run.agent(_TASK_REPLY)
    assert run.fix()["rc"] == 75
    paths = list((run.root / "state").glob("handoff-*-fix_task_write_failed.json"))
    assert len(paths) == 2
    for path in paths:
        record = json.loads(path.read_text())
        resume = record["continuation"]["resume"]
        if damage == "missing":
            del record["continuation"]["resume"]
        elif damage == "empty":
            record["continuation"]["resume"] = {}
        else:
            resume["description_changed"] = "false"
        path.write_text(json.dumps(record))
    clock["now"] = 1_060
    calls_before = len(run.fake_gh.calls())
    edit_count = len(edits)
    assert run.fix()["rc"] == 1 and run.labelled()
    assert len(edits) == edit_count and len(run.agent_calls) == 1
    assert run.body() == COMPLETE
    assert not any("state,labels,headRefOid,body" in call for call in run.fake_gh.calls()[calls_before:])


def test_a_deferred_task_write_still_refreshes_the_fixers_checks(run, monkeypatch):
    clock, _ = _flaky_edits(monkeypatch)
    run.fake_gh.set(body=COMPLETE)
    run.agent({"commit": False, "reply": "TASK: partial\nRESULT: fixed",
               "file": "## Checks to verify\n- /billing: brand colour\n\nRESULT: fixed\n"})
    assert run.fix("claude")["rc"] == 75
    assert section(run.body(), "Checks to verify") == "none"
    clock["now"] = 1_060
    assert run.fix("claude")["rc"] == 0 and len(run.agent_calls) == 1
    assert "completion=partial" in run.body()
    assert section(run.body(), "Checks to verify") == "- /billing: brand colour"
    assert not run.labelled()


@pytest.mark.parametrize("fixer", ["claude", "cursor"])
def test_a_deferred_task_write_posts_the_discard_comment_once(run, monkeypatch, fixer):
    clock, _ = _flaky_edits(monkeypatch)
    run.fake_gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    bad = "Task completion: partial\nTask completion: complete\n\n## What changed\nNope.\n"
    if fixer == "cursor":
        run.agent(cursor_plan("TASK: partial\nRESULT: fixed", request=bad, commit=False))
    else:
        run.agent({**_TASK_REPLY, "description": bad, "commit": False})
    assert run.fix(fixer)["rc"] == 75
    assert not [c for c in run.comments() if "Discarded the PR description request" in c]
    clock["now"] = 1_060
    assert run.fix(fixer)["rc"] == 0 and len(run.agent_calls) == 1
    assert len([c for c in run.comments() if "Discarded the PR description request" in c]) == 1
    assert "Nope." not in run.body() and "completion=partial" in run.body()


def test_a_deferred_cursor_task_write_still_pushes_its_commit(run, monkeypatch):
    clock, _ = _flaky_edits(monkeypatch)
    run.fake_gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    run.agent(cursor_plan("TASK: partial\nRESULT: fixed"))
    assert run.fix("cursor")["rc"] == 75 and run.pushed_new
    (run.root / "state" / f"cursor-fix-task-{PR}").unlink()
    (run.root / "state" / f"cursor-fix-base-{PR}").unlink()
    clock["now"] = 1_060
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1
    assert run.pushed() and "completion=partial" in run.body()


@pytest.mark.parametrize("fixer", ["claude", "cursor"])
def test_a_discard_survives_deferred_task_and_checks_writes(run, monkeypatch, fixer):
    import subprocess

    clock, _ = _flaky_edits(monkeypatch)
    call = fix.pr_helpers.call

    def defer_checks(*args, input=None, limit=None):
        if args[:2] == ("pr", "edit") and "--body-file" in args and "/billing" in input:
            if clock["now"] < 1_120:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 503 unavailable")
        return call(*args, input=input, limit=limit)

    monkeypatch.setattr(fix.pr_helpers, "call", defer_checks)
    run.fake_gh.set(body=COMPLETE, headRefName="cursor/issue-419")
    bad = "Task completion: partial\nTask completion: complete\n\n## What changed\nNope.\n"
    checks = "## Checks to verify\n- /billing: brand colour\n"
    if fixer == "cursor":
        run.agent(cursor_plan(f"TASK: partial\n{checks}\nRESULT: fixed", request=bad, commit=False))
    else:
        run.agent({**_TASK_REPLY, "file": checks, "description": bad, "commit": False})
    assert run.fix(fixer)["rc"] == 75
    assert run.body() == COMPLETE
    clock["now"] = 1_060
    assert run.fix(fixer)["rc"] == 75
    assert "completion=partial" in run.body()
    assert section(run.body(), "Checks to verify") == "none"
    assert not any("baton-description-discarded" in c for c in run.comments())

    clock["now"] = 1_120
    assert run.fix(fixer)["rc"] == 0
    assert section(run.body(), "Checks to verify") == "- /billing: brand colour"
    assert sum("baton-description-discarded" in c for c in run.comments()) == 1
    assert len(run.agent_calls) == 1
    assert not list((run.root / "state").glob("handoff-*-fix_*_failed.json"))
    assert not (run.root / "state" / f"cursor-fix-task-{PR}").exists()


@pytest.mark.parametrize("operation", ["task", "checks"])
def test_a_malformed_pr_head_response_hands_off_without_saving_or_editing(run, monkeypatch, operation):
    import subprocess

    _, edits = _flaky_edits(monkeypatch)
    call = fix.pr_helpers.call

    def malformed_head(*args, input=None, limit=None):
        if args[:2] == ("pr", "view") and args[-2:] == ("--json", "headRefOid"):
            return subprocess.CompletedProcess(args, 0, "invalid JSON", "")
        return call(*args, input=input, limit=limit)

    monkeypatch.setattr(fix.pr_helpers, "call", malformed_head)
    run.fake_gh.set(body=COMPLETE)
    if operation == "task":
        run.agent(_TASK_REPLY)
    else:
        run.agent({"commit": False, "file": "## Checks to verify\n- /billing: brand colour\n"})
    assert run.fix()["rc"] == 1 and run.labelled()
    assert edits == [] and run.body() == COMPLETE
    assert not any(f"handoff-retry fix_{operation}_" in line for line in run.runner_log())


def test_an_exhausted_task_write_hands_off_exactly_once(run, monkeypatch):
    from baton import handoff

    clock, edits = _flaky_edits(monkeypatch, fail_until=10 ** 9)
    notes = []
    monkeypatch.setattr(handoff.notify, "notify", lambda title, text="": notes.append((title, text)))
    run.fake_gh.set(body=COMPLETE)
    run.agent(_TASK_REPLY)
    assert run.fix("claude")["rc"] == 75
    clock["now"] = 1_060
    assert run.fix("claude")["rc"] == 75
    clock["now"] = 1_360
    assert run.fix("claude")["rc"] == 1 and run.labelled()
    assert sum("task completion change failed" in title for title, _ in notes) == 1
    assert sorted(set(edits)) == [1_000, 1_060, 1_360] and len(run.agent_calls) == 1   # one write per attempt, with gh's own repeats
    assert not list(Path(run.root / "state").glob("handoff-*-*fix_task_write_failed.json"))


def test_a_pr_closed_during_the_last_attempt_clears_without_a_handoff(run, monkeypatch):
    clock, _ = _flaky_edits(monkeypatch, fail_until=10 ** 9)
    run.fake_gh.set(body=COMPLETE)
    run.agent({"commit": False, "reply": "RESULT: fixed",
               "file": "## Checks to verify\n- /billing: brand colour\n\nRESULT: fixed\n"})
    assert run.fix("claude")["rc"] == 75
    clock["now"] = 1_060
    assert run.fix("claude")["rc"] == 75
    clock["now"] = 1_360
    call = fix.pr_helpers.call

    def closing(*args, input=None, limit=None):
        done = call(*args, input=input, limit=limit)
        if args[:2] == ("pr", "edit") and "--body-file" in args:
            run.fake_gh.set(state="MERGED")
        return done

    monkeypatch.setattr(fix.pr_helpers, "call", closing)
    run.fix("claude")
    assert not run.labelled() and len(run.agent_calls) == 1
    assert not list(Path(run.root / "state").glob("handoff-*-*fix_checks_refresh_failed.json"))


def test_a_landed_checks_refresh_counts_as_progress_when_the_body_cannot_be_read_after(run, monkeypatch):
    run.fake_gh.set(body=COMPLETE)
    run.agent({"commit": False, "reply": "RESULT: fixed",
               "file": "## Checks to verify\n- /billing: brand colour\n\nRESULT: fixed\n"})
    body = fix.pr_helpers.body

    def body_after_edit(pr):
        return None if section(run.body(), "Checks to verify") != "none" else body(pr)

    monkeypatch.setattr(fix.pr_helpers, "body", body_after_edit)
    assert run.fix("claude")["rc"] == 0 and not run.labelled()
    assert not any("made no commit" in line for line in run.runner_log())


def test_an_unreadable_pr_head_hands_off_before_a_task_write_is_saved(run, gh):
    gh.set(body=COMPLETE, headRefOid="")
    run.agent(_TASK_REPLY)
    assert run.fix("claude")["rc"] == 1 and run.labelled()
    assert "completion=partial" not in run.body()


def test_a_deferred_replacement_description_preserves_the_owners_issue_correction(run, monkeypatch):
    clock, _ = _flaky_edits(monkeypatch)
    run.fake_gh.set(body=COMPLETE, headRefName="owner-branch")
    request = COMPLETE.replace("Disabled buttons.", "Revised description.")
    run.agent(cursor_plan("## Checks to verify\nnone\n\nTASK: partial\nRESULT: fixed", request=request, commit=False))
    assert run.fix("cursor")["rc"] == 75
    corrected = COMPLETE.replace("419", "420").replace("## Checks to verify\nnone", "## Checks to verify\n- Owner check")
    run.fake_gh.set(body=corrected)
    clock["now"] = 1_060
    # Section 5: a changed body during the wait discards the description; the TASK: write then
    # sees the owner's issue correction and hands off.
    assert run.fix("cursor")["rc"] == 1 and run.labelled()
    assert run.body() == corrected and len(run.agent_calls) == 1


def test_an_exhausted_cursor_description_edit_discards_once_without_a_description_handoff(run, monkeypatch):
    clock, edits = _flaky_edits(monkeypatch, fail_until=10_000)
    request = BODY.replace("Disabled buttons.", "Corrected description")
    run.agent(cursor_plan("## Checks to verify\nnone\n\nRESULT: fixed", request=request))
    assert run.fix("cursor")["rc"] == 75
    clock["now"] = 1_060
    assert run.fix("cursor")["rc"] == 75
    clock["now"] = 1_360
    assert run.fix("cursor")["rc"] == 0 and len(run.agent_calls) == 1
    assert {1_000, 1_060, 1_360} == set(edits) and edits.count(1_360) == edits.count(1_000)
    assert "Corrected description" not in run.body() and not run.labelled()
    assert sum("baton-description-discarded" in c for c in run.comments()) == 1


def test_claude_discards_a_description_at_once_when_the_edit_is_confirmed_absent(run, monkeypatch):
    clock, edits = _flaky_edits(monkeypatch, fail_until=10_000)
    run.agent({"commit": False, "file": "## Must fix\n1. adopted\n\n## Checks to verify\nnone\n\nRESULT: fixed\n",
               "description": BODY.replace("Disabled buttons.", "Corrected description")})
    # The discarded description made no progress, so the no-commit fix takes fix_no_commit.
    assert run.fix("claude")["rc"] == 1 and len(run.agent_calls) == 1
    assert any("made no commit" in line for line in run.runner_log())
    # Each repeat follows a lookup that confirmed the body unchanged (section 3).
    assert len(edits) == len(fix.pr_helpers.BACKOFF) + 1 and "Corrected description" not in run.body()
    assert sum("baton-description-discarded" in c for c in run.comments()) == 1


def _body_view_fails(monkeypatch, *, landed=False, views_failing=10_000):
    """Edits answer 503 (after landing, if LANDED); the first VIEWS_FAILING body views error."""
    import subprocess
    from baton import handoff

    clock, edits, views = {"now": 1_000}, [], {"n": 0}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    monkeypatch.setattr(fix.pr_helpers, "sleep", lambda *_: None)
    call = fix.pr_helpers.call

    def wrapped(*args, input=None, limit=None):
        if args[:2] == ("pr", "edit") and "--body-file" in args:
            edits.append(clock["now"])
            if landed:
                call(*args, input=input, limit=limit)
            return subprocess.CompletedProcess(args, 1, "", "HTTP 503 unavailable")
        if args[:2] == ("pr", "view") and edits and "body" in " ".join(args):
            views["n"] += 1
            if views["n"] <= views_failing:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 404 unreadable")
        return call(*args, input=input, limit=limit)

    monkeypatch.setattr(fix.pr_helpers, "call", wrapped)
    return clock, edits


_CLAUDE_DESCRIPTION = {"commit": False, "description": BODY.replace("Disabled buttons.", "Corrected description"),
                       "file": "## Must fix\n1. adopted\n\n## Checks to verify\nnone\n\nRESULT: fixed\n"}


def test_claude_does_not_resend_a_lost_description_edit_that_landed(run, monkeypatch):
    _, edits = _body_view_fails(monkeypatch, landed=True, views_failing=0)
    run.agent(_CLAUDE_DESCRIPTION)
    assert run.fix("claude")["rc"] == 0
    assert edits == [1_000] and "Corrected description" in run.body()
    assert not any("baton-description-discarded" in c for c in run.comments())
    assert not any("made no commit" in line for line in run.runner_log())


def test_an_unreadable_description_lookup_discards_with_could_not_confirm(run, monkeypatch):
    _, edits = _body_view_fails(monkeypatch, views_failing=2)
    run.agent(_CLAUDE_DESCRIPTION)
    run.fix("claude")
    assert edits == [1_000]
    discards = [c for c in run.comments() if "baton-description-discarded" in c]
    assert len(discards) == 1 and "could not confirm" in discards[0]


def test_an_unreadable_lookup_that_later_finds_the_edit_landed_counts_it(run, monkeypatch):
    _, edits = _body_view_fails(monkeypatch, landed=True, views_failing=2)
    run.agent(_CLAUDE_DESCRIPTION)
    assert run.fix("claude")["rc"] == 0
    assert edits == [1_000] and "Corrected description" in run.body()
    assert not any("baton-description-discarded" in c for c in run.comments())
    assert not any("made no commit" in line for line in run.runner_log())


def test_a_consumed_cursor_description_attempt_is_not_repeated_after_an_interrupted_tick(run, monkeypatch):
    clock, edits = _flaky_edits(monkeypatch, fail_until=10_000)
    run.agent(cursor_plan("## Checks to verify\nnone\n\nRESULT: fixed",
                          request=BODY.replace("Disabled buttons.", "Corrected description")))
    assert run.fix("cursor")["rc"] == 75
    first = len(edits)
    clock["now"] = 1_060
    call = fix.pr_helpers.call

    def boom(*args, input=None, limit=None):
        if args[:2] == ("pr", "view"):
            raise RuntimeError("tick killed")
        return call(*args, input=input, limit=limit)

    monkeypatch.setattr(fix.pr_helpers, "call", boom)
    with pytest.raises(RuntimeError):
        run.fix("cursor")
    monkeypatch.setattr(fix.pr_helpers, "call", call)
    assert run.fix("cursor")["rc"] == 75
    assert len(edits) == first   # the consumed attempt made no edit and was not repeated
    clock["now"] = 1_360
    assert run.fix("cursor")["rc"] == 0   # attempt two was the last: it discards
    assert edits.count(1_360) == first and len(edits) == 2 * first

# --- stage 2 fix capability routing (#113) -------------------------------------------------

def test_adapters_declare_fix_capabilities():
    from baton import agents
    expected = {
        "claude": ("fix-prompt.md", "settings", False),
        "codex": ("codex-fix-prompt.md", "denied", False),
        "cursor": ("cursor-fix-prompt.md", "runner", True),
        "stub": ("stub-fix-prompt.md", "runner", True),
    }
    for vendor, (template, access, reply) in expected.items():
        adapter = agents.ADAPTERS[vendor]
        assert adapter.FIX_TEMPLATE == template
        assert adapter.GITHUB_ACCESS == access
        assert adapter.FIX_RESUMABLE_VIA_REPLY is reply


def _admit_stub_fixer(run, monkeypatch):
    from dataclasses import replace
    from baton import config, session
    from conftest import full_text
    from test_adapter_capabilities import with_stub

    path = Path(run.root, "stub-fix.toml")
    path.write_text(with_stub(full_text()))
    cfg = config.load(path, home=run.root / "home")
    cfg = replace(cfg, paths=replace(cfg.paths, self_dir=run.bin,
                                     state=Path(os.environ["STATE"]),
                                     logs=Path(os.environ["LOGS"]),
                                     ctx=Path(os.environ["CTX"])))
    sess = session.create(cfg, repo=cfg.repos[0])
    sess = replace(sess, dirs=replace(
        sess.dirs, state=Path(os.environ["STATE"]), logs=Path(os.environ["LOGS"]),
        ctx=Path(os.environ["CTX"])))

    def resolve(value=None):
        if isinstance(value, session.Session):
            return value
        return sess

    monkeypatch.setattr(session, "resolve", resolve)
    return sess


@pytest.mark.parametrize("fixer,template_line", [
    ("claude", f"4. Write one reply to `/tmp/baton-fix-{PR}.md` with the Write tool; do not post it."
               " The runner posts it. Include no `<!-- baton-` markers."),
    ("codex", f"4. Write one reply to `/tmp/baton-fix-{PR}.md`; do not post it."
              " The runner posts it. Include no `<!-- baton-` markers."),
    ("cursor", "5. The runner posts your final message on the PR."
               " Put the checks section above the `TASK:`/`RESULT:` lines."),
    ("stub", "5. The runner posts your final message on the PR."
             " Put the checks section above the `TASK:`/`RESULT:` lines."),
])
def test_fix_templates_come_from_the_adapter(run, monkeypatch, fixer, template_line):
    _admit_stub_fixer(run, monkeypatch)
    reply = "## Must fix\nnone\n\n## Checks to verify\nnone\n\nRESULT: fixed\n"
    run.agent(cursor_plan(reply) if fixer in ("cursor", "stub") else {"file": reply})
    assert run.fix(fixer)["rc"] == 0
    prompt = run.prompts[0]
    assert template_line in prompt.splitlines()
    if fixer in ("cursor", "stub"):
        assert f"pr-{PR}-body.md" in prompt
        assert not run.agent_calls[0][3]
    else:
        assert run.agent_calls[0][3] == f"/tmp/baton-fix-{PR}.md"
        assert f"pr-{PR}-description.md" in prompt
        assert f"/tmp/baton-fix-{PR}.md.body" in prompt
    assert run.comments()[0].startswith(f"<!-- baton-fix by={fixer} -->")


@pytest.mark.parametrize("fixer,ref,prefix", [("cursor", "cursor/issue-1", "Cursor "), ("claude", "HEAD", "")])
def test_verification_resume_uses_the_fixer_protocol_and_clears_cursor_state(
    advised_fix, monkeypatch, gh, fixer, ref, prefix,
):
    from baton import recovery, session
    from baton.common import Deferred

    run = advised_fix
    state = Path(os.environ["STATE"])
    cursor_base = state / f"cursor-fix-base-{PR}"
    cursor_task = state / f"cursor-fix-task-{PR}"
    reply = "## Checks to verify\nnone\n\nRESULT: fixed"
    if fixer == "claude":
        request = BODY.replace("## Checks to verify\nnone", "## Checks to verify\n- /billing: cursor badge")
        run.agent(cursor_plan(reply, request=request))
        clock, _ = _flaky_edits(monkeypatch, fail_until=1_001)
        assert run.fix("cursor")["rc"] == 75
        assert cursor_base.exists() and cursor_task.exists()
        clock["now"] = 1_001  # GitHub is back; Cursor's retry is still inside its budget

    run.verify_script("lint broke", "lint broke", "lint broke", "pass")
    run.advice = [{"action": "retry_step", "reason": "transient verification",
                   "evidence": ["Decision point: fix_verify_failed"]}]
    run.agent(cursor_plan(reply) if fixer == "cursor" else {"file": reply},
              {"commit": True}, {"commit": True})
    verify = fix.Runner.verify

    def defer_once(self, *args, **kwargs):
        if kwargs.get("repairs") == 0 and not defer_once.used:
            defer_once.used = True
            raise Deferred(fixer, "short block")
        return verify(self, *args, **kwargs)

    defer_once.used = False
    monkeypatch.setattr(fix.Runner, "verify", defer_once)
    assert run.fix(fixer)["rc"] == 75
    saved = recovery.load(session.resolve(os.environ), "419", PR)
    assert saved["reason"] == "fix_verify_failed"
    assert cursor_base.exists()

    pushes = []
    push = fix.Runner.push_with_retry

    def capture_push(self, pr, wt, push_ref, **kwargs):
        pushes.append((push_ref, kwargs["who"], kwargs["fixer"], kwargs["clear_cursor"]))
        return push(self, pr, wt, push_ref, **kwargs)

    monkeypatch.setattr(fix.Runner, "push_with_retry", capture_push)
    calls = list(run.agent_calls)
    assert run.fix(fixer)["rc"] == 0
    assert pushes == [(ref, prefix, fixer, fixer == "cursor")] and run.pushed()
    assert not cursor_base.exists() and not cursor_task.exists()
    assert run.agent_calls == calls


def test_stub_reply_fix_discards_a_pending_cursor_request(run, monkeypatch, gh):
    _admit_stub_fixer(run, monkeypatch)
    request = BODY.replace("Disabled buttons.", "Cursor's superseded description.")
    run.agent(cursor_plan("## Checks to verify\n- /billing: cursor badge\n\nRESULT: fixed", request=request))
    clock, _ = _flaky_edits(monkeypatch, fail_until=1_001)
    assert run.fix("cursor")["rc"] == 75
    clock["now"] = 1_001  # GitHub is back, but Cursor's retry (due +60s) is not yet due

    run.agent(cursor_plan("## Checks to verify\nnone\n\nRESULT: fixed"))
    assert run.fix("stub")["rc"] == 0
    assert [call[0] for call in run.agent_calls] == ["cursor", "stub"]
    assert section(run.body(), "What changed") == "Disabled buttons."
    assert section(run.body(), "Checks to verify") == "none"
    assert not (Path(env_ctx()) / f"pr-{PR}-body.md").exists()
    assert not (Path(os.environ["STATE"]) / f"cursor-fix-task-{PR}").exists()

    feedback = "Round two: explain when the button becomes available."
    run.agent(cursor_plan("## Checks to verify\nnone\n\nRESULT: fixed"))
    assert run.fix("cursor", feedback=feedback)["rc"] == 0
    assert [call[0] for call in run.agent_calls] == ["cursor", "stub", "cursor"]
    feedback_file = re.search(rf"`([^`]+/pr-{PR}-cursor-fix-[^`]+\.md)`", run.prompts[-1])
    assert feedback_file and Path(feedback_file[1]).read_text().startswith(feedback + "\n")


def test_stub_fix_posts_its_marker_and_retries_a_description_update_without_a_cli_run(fix_worktree, monkeypatch, gh):
    run = fix_worktree
    _admit_stub_fixer(run, monkeypatch)
    replacement = "Buttons stay disabled until billing details are saved."
    request = BODY.replace("Disabled buttons.", replacement)
    reply = "Updated the description.\n\n## Checks to verify\nnone\n\nRESULT: fixed"
    calls = run.root / "stub-fix-calls"
    cli = run.root / "stubs" / "stub-agent"
    cli.write_text(f'''#!{sys.executable}
import json, re, sys
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("stub-agent 1")
    sys.exit(0)
with Path({str(calls)!r}).open("a") as log:
    log.write("stub-agent\\n")
destination = re.search(r"PR-description artifact `([^`]+)`", sys.argv[-1])[1]
Path(destination).write_text({request!r})
print(json.dumps({{"type": "result", "subtype": "success", "model": "stub-1", "result": {reply!r}}}))
''')
    cli.chmod(0o755)
    (cli.parent / "timeout").unlink()

    clock, _ = _flaky_edits(monkeypatch)
    assert run.fix("stub")["rc"] == 75
    assert replacement not in run.body()
    assert calls.read_text().splitlines() == ["stub-agent"]
    assert run.comments()[0].startswith("<!-- baton-fix by=stub -->")

    clock["now"] = 1_060
    assert run.fix("stub")["rc"] == 0
    assert section(run.body(), "What changed") == replacement
    assert calls.read_text().splitlines() == ["stub-agent"]
