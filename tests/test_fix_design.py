"""Design findings in the fix run (#96): the loop id on the fix comment and the rejection-only fix,
through fix.run (the production entry the loop's fixers use) with the agent double of tests/test_fix.py."""
from unittest import mock

import pytest

from baton import fix
from test_fix import PR, URL, fix_worktree, run   # noqa: F401  (fixtures)

REJECTED = "## Must fix\nnone\n\n## Design findings\nD1.1. rejected — 24 px is the brand spacing\n\nRESULT: fixed\n"


@pytest.fixture
def design(monkeypatch):
    monkeypatch.setenv("ROUND", "2")
    with mock.patch.multiple(fix.Runner, loop_id="1700-abc", design_ids=["D1.1"], design_only=True):
        yield


def no_commit_handoff(run):
    return any("fix_no_commit" in line for line in run.runner_log())


def test_the_fix_comment_carries_the_loop_id_and_round(run, design):
    run.agent({"file": REJECTED, "commit": False})
    run.fix()
    assert run.comments()[0].startswith("<!-- baton-fix by=claude loop=1700-abc round=2 -->\n")


def test_a_fix_comment_outside_a_loop_keeps_the_plain_marker(run):
    run.agent({"file": "## Must fix\n1. adopted — x\n\nRESULT: fixed\n"})
    assert run.fix()["rc"] == 0
    assert run.comments()[0].startswith("<!-- baton-fix by=claude -->\n")


def test_design_only_feedback_answered_all_rejected_needs_no_commit(run, design):
    run.agent({"file": REJECTED, "commit": False})
    assert run.fix()["rc"] == 0
    assert not no_commit_handoff(run) and not run.labelled()


@pytest.mark.parametrize("reply", [
    "## Design findings\nnone\n\nRESULT: fixed\n",                                       # unanswered
    "## Design findings\nD1.1. adopted — made it 18 px\n\nRESULT: fixed\n",               # adopted with no change
    "## Design findings\nD1.1. rejected — a\nD1.1. rejected — a\n\nRESULT: fixed\n",      # answered twice
])
def test_anything_less_than_all_rejected_still_hands_off_no_commit(run, design, reply):
    run.agent({"file": reply, "commit": False})
    assert run.fix()["rc"] == 1
    assert no_commit_handoff(run)


def test_a_must_fix_item_in_the_feedback_still_hands_off_no_commit(run, monkeypatch):
    monkeypatch.setenv("ROUND", "2")
    with mock.patch.multiple(fix.Runner, loop_id="1700-abc", design_ids=["D1.1"], design_only=False):
        run.agent({"file": REJECTED, "commit": False})
        assert run.fix()["rc"] == 1
    assert no_commit_handoff(run)


# --- cursor_fix (the reply-protocol path) ---
from test_fix import BODY, _flaky_edits, cursor_plan   # noqa: E402

CURSOR_REJECTED = "## Design findings\nD1.1. rejected — 24 px is the brand spacing\n\nRESULT: fixed\n"


def test_a_cursor_fix_comment_carries_the_loop_id_and_round(run, design):
    run.agent(cursor_plan(CURSOR_REJECTED, commit=False))
    run.fix("cursor")
    assert run.comments()[0].startswith("<!-- baton-fix by=cursor loop=1700-abc round=2 -->\n")


def test_a_cursor_rejection_only_fix_needs_no_commit(run, design):
    run.agent(cursor_plan(CURSOR_REJECTED, commit=False))
    assert run.fix("cursor")["rc"] == 0
    assert not no_commit_handoff(run) and not run.labelled()


@pytest.mark.parametrize("reply", [
    "## Design findings\nnone\n\nRESULT: fixed\n",
    "## Design findings\nD1.1. adopted — made it 18 px\n\nRESULT: fixed\n",
])
def test_a_cursor_fix_with_less_than_all_rejected_hands_off_no_commit(run, design, reply):
    run.agent(cursor_plan(reply, commit=False))
    assert run.fix("cursor")["rc"] == 1
    assert no_commit_handoff(run)


def test_a_cursor_reply_over_the_cap_keeps_the_design_section_whole(run, design, monkeypatch):
    monkeypatch.setattr(fix, "REPLY_CAP", 300)
    run.agent(cursor_plan("x" * 1000 + "\n\n" + CURSOR_REJECTED, commit=False))
    assert run.fix("cursor")["rc"] == 0
    posted = run.comments()[0]
    assert "D1.1. rejected — 24 px is the brand spacing" in posted and len(posted.split("\n", 1)[1]) <= 300
    assert "x" * 1000 not in posted


def test_a_design_section_over_the_cap_posts_nothing_and_hands_off(run, design, monkeypatch):
    monkeypatch.setattr(fix, "REPLY_CAP", 100)
    reply = "## Design findings\nD1.1. rejected — " + "y" * 400 + "\n\nRESULT: fixed\n"
    run.agent(cursor_plan(reply, commit=False))
    assert run.fix("cursor")["rc"] == 1
    assert run.comments() == [] and not no_commit_handoff(run)
    assert any("design answers exceed the reply cap" in line for line in run.runner_log())


@pytest.mark.parametrize("extra", [0, 1, 2, 3])
def test_capped_reply_never_exceeds_the_cap_when_the_section_nearly_fills_it(monkeypatch, extra):
    monkeypatch.setattr(fix, "REPLY_CAP", 100)
    head = "## Design findings\nD1.1. rejected — "
    section = head + "z" * (100 - extra - len(head)) + "\n"
    out = fix.Runner.capped_reply(None, "x" * 500 + "\n\n" + section)
    assert out is not None and len(out) <= 100 and section.rstrip("\n") in out
    assert not out.startswith("\n")


def deferred_then_resumed_without_change(run, monkeypatch, *, drop_saved=False):
    """First pass defers the description (rc 75); the resumed call runs no agent and its description
    update succeeds with no change."""
    clock, _ = _flaky_edits(monkeypatch)
    request = BODY.replace("Disabled buttons.", "Corrected description")
    run.agent(cursor_plan(CURSOR_REJECTED, request=request, commit=False))
    assert run.fix("cursor")["rc"] == 75
    if drop_saved:
        for saved in run.root.glob("state/*-fix-design-*"):
            saved.unlink()
    clock["now"] = 1_060
    monkeypatch.setattr(fix.Runner, "write_description", lambda self, *a, **k: (0, False, "", False, False))
    rc = run.fix("cursor")["rc"]
    assert len(run.agent_calls) == 1
    return rc


def test_a_resume_after_a_deferred_description_keeps_the_rejection_only_decision(run, design, monkeypatch):
    assert deferred_then_resumed_without_change(run, monkeypatch) == 0
    assert not no_commit_handoff(run)


def test_a_resume_without_the_saved_answers_hands_off_no_commit(run, design, monkeypatch):
    assert deferred_then_resumed_without_change(run, monkeypatch, drop_saved=True) == 1
    assert no_commit_handoff(run)
