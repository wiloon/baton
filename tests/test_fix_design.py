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
