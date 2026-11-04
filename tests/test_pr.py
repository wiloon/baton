"""baton.pr against a fake gh: the cases tests/manual-checks.sh and
tests/completion.sh drive through fix_pr, plus the merge gate."""
import json
import os
from pathlib import Path

import pytest

from baton import pr

BODY = ("<!-- baton-task issue=242 completion=complete -->\n\nCloses #242\n\n"
        "## What changed\nDisabled buttons.\n\n## Manual checks\nnone\n\n"
        "## Before release\nnone\n\n## Follow-ups\n- [nit] tidy copy")
PARTIAL = ("<!-- baton-task issue=419 completion=partial -->\n\nRefs #419\n\n"
           "This PR leaves the parent issue open.\n\n## What changed\nA test.")


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "state", tmp_path / "ctx"


def edits(gh):
    return [c for c in gh.calls() if c[:2] == ["pr", "edit"]]


# --- Manual checks: gate, split, refresh (#155) ---------------------------------------

@pytest.mark.parametrize("section, none", [
    ("none", True), ("None.", True), ("- none", True), ("  * none", True), ("none\r", True),
    ("none. No colour changes.", False), ("Not re-checked after the fix run.", False),
    ("- /billing: brand colour", False), ("", False), ("none!", False),
    # Only a section that says exactly none (#265): a check beside it goes to the owner.
    ("- /billing: brand colour\nnone", False), ("none\n\n- /billing: brand colour", False),
    ("none\nnone", False),
    # Tool attribution lines are not part of the answer.
    ("none\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n\n"
     "Co-Authored-By: Claude <noreply@anthropic.com>", True),
])
def test_checks_none(section, none):
    assert pr.checks_none(f"## What changed\nX.\n\n## Manual checks\n{section}\n\n## Before release\nnone") is none


def test_checks_none_needs_the_section():
    assert not pr.checks_none("## What changed\nnone\n\n## Before release\nnone")
    assert pr.checks_none("## Manual checks:\nnone")


def test_checks_none_with_two_sections_is_false():
    assert not pr.checks_none("## Manual checks\nnone\n\n## Notes\nX.\n\n## Manual checks\nnone")


def test_fix_reply_block_replaces_the_section_and_leaves_the_reply(gh, dirs):
    _, ctx = dirs
    gh.set(body=BODY)
    reply = pr.split_checks(7, "## Must fix\n1. adopted — brand colours\n\n## Manual checks\n"
                               "- /billing: Subscribe buttons use the brand colour\n\nRESULT: fixed\n", ctx)
    assert "Manual checks" not in reply and "adopted — brand colours" in reply
    assert pr.apply_checks(7, ctx) == (0, True, "PR #7: Manual checks refreshed after the fix")
    body = gh.pr["body"]
    assert "## Manual checks\n- /billing: Subscribe buttons use the brand colour\n" in body
    assert not pr.checks_none(body)
    assert "Disabled buttons." in body and "- [nit] tidy copy" in body and "## Before release\nnone" in body
    assert not Path(ctx, "pr-7-fix-checks.json").exists()


def test_fix_without_a_block_is_not_rechecked(gh, dirs):
    _, ctx = dirs
    gh.set(body=BODY)
    pr.split_checks(7, "## Must fix\nnone\n\nRESULT: fixed\n", ctx)
    assert pr.apply_checks(7, ctx)[:2] == (0, True)
    assert "## Manual checks\nNot re-checked after the fix run.\n" in gh.pr["body"]
    assert not pr.checks_none(gh.pr["body"])


def test_fix_that_rechecked_none_changes_nothing(gh, dirs):
    _, ctx = dirs
    gh.set(body=BODY)
    pr.split_checks(7, "## Manual checks\nnone\n\nRESULT: fixed\n", ctx)
    assert pr.apply_checks(7, ctx) == (0, False, "")
    assert edits(gh) == []


def test_refused_edit_keeps_the_checks_for_a_retry(gh, dirs):
    _, ctx = dirs
    gh.set(body=BODY)
    pr.split_checks(7, "## Manual checks\n- /billing: brand colour\n\nRESULT: fixed\n", ctx)
    gh.fail("edit")
    assert pr.apply_checks(7, ctx)[0] == 1
    assert Path(ctx, "pr-7-fix-checks.json").exists()
    gh.fail("view")
    assert pr.apply_checks(7, ctx)[0] == 1


def test_gate(gh):
    gh.set(headRefName="cursor/issue-242", body=BODY.replace("## Manual checks\nnone", "## Manual checks\n- look"),
           commits=[], closingIssuesReferences=[])
    assert pr.gate(7) == {"action": "manual_checks"}
    gh.set(body=BODY)
    assert pr.gate(7) == {"action": "merge"}
    partial = PARTIAL + "\n\n## Manual checks\nnone"
    gh.set(headRefName="cursor/issue-419", body=partial)
    result = pr.gate(7)
    assert result["action"] == "queue_review" and "Acceptance advanced" in result["reason"]
    gh.set(body=partial + "\n\n## Acceptance advanced\n- one item\n\nThis does not close #419.")
    result = pr.gate(7)
    assert result["action"] == "queue_review" and "closing keywords" in result["reason"]
    gh.fail("view")
    assert pr.gate(7) == {"action": "manual_checks"}


# --- task relation ----------------------------------------------------------------------

@pytest.mark.parametrize("branch", ["agent/issue-297", "cursor/issue-297"])
def test_details_and_relation(gh, monkeypatch, branch):
    gh.set(headRefName=branch, author={"login": "acme-owner"}, isCrossRepository=False,
           state="OPEN", isDraft=False, body="Refs #297")
    assert pr.details(335, "acme-owner", r"^cursor$") == {
        "branch": branch, "route": "cursor", "issue": "297", "completion": "partial"}
    assert pr.relation(335) == {"issue": "297", "completion": "partial"}
    gh.fail("view")
    with pytest.raises(ValueError):
        pr.details(335, "acme-owner", r"^cursor$")


def test_task_line_retargets_the_pr(gh, dirs):
    _, ctx = dirs
    gh.set(headRefName="cursor/issue-419", body=PARTIAL)
    rc, changed, log = pr.apply_task(420, "Agreed: #420 delivers every acceptance item.\nTASK: complete\nRESULT: fixed\n", ctx)
    assert (rc, changed) == (0, True)
    assert log == "PR #420: task completion partial -> complete at the fixer's request; the next review checks it"
    assert "issue=419 completion=complete" in gh.pr["body"] and "\nCloses #419\n" in gh.pr["body"]
    assert os.listdir(ctx) == []   # the body file is gone


@pytest.mark.parametrize("reply", [
    "Disagree with the point.\nRESULT: fixed\n",
    'The reviewer wrote "TASK: complete" but work remains.\nRESULT: fixed\n',
    "TASK: partial\nRESULT: fixed\n",
])
def test_no_task_request_changes_nothing(gh, dirs, reply):
    _, ctx = dirs
    gh.set(headRefName="cursor/issue-419", body=PARTIAL)
    assert pr.apply_task(420, reply, ctx) == (0, False, "")
    assert edits(gh) == []


def test_task_request_failures(gh, dirs):
    _, ctx = dirs
    gh.set(headRefName="cursor/issue-419", body=PARTIAL)
    gh.fail("edit")
    assert pr.apply_task(420, "TASK: complete\n", ctx)[0] == 1
    gh.fail("edit", False)
    gh.set(headRefName="main", body="No relation.")
    assert pr.apply_task(420, "TASK: complete\n", ctx)[0] == 2


# --- description requests ---------------------------------------------------------------

def test_body_request(gh, dirs, tmp_path):
    state, ctx = dirs
    gh.set(headRefName="cursor/issue-297", body="Refs #297\n\nOld description")
    assert pr.apply_body(335, tmp_path / "missing.md", state, ctx) == (0, False)
    request = tmp_path / "request.md"
    request.write_text("")
    assert pr.apply_body(335, request, state, ctx) == (2, False)
    link = tmp_path / "link.md"
    link.symlink_to(request)
    assert pr.apply_body(335, link, state, ctx) == (2, False)
    request.write_text("Closes #297\n\nCorrected description")   # partial cannot close
    assert pr.apply_body(335, request, state, ctx) == (2, False)
    request.write_text("Refs #297\n\nCorrected description")
    assert pr.apply_body(335, request, state, ctx) == (0, True)
    assert gh.pr["body"].endswith("Refs #297\n\nCorrected description\n")
    assert "issue=297 completion=partial" in gh.pr["body"]
    assert os.listdir(ctx) == []


def test_body_retry_recognizes_a_write_with_a_lost_response(gh, dirs, tmp_path):
    state, ctx = dirs
    gh.set(headRefName="cursor/issue-297", body="Refs #297\n\nOld description")
    request = tmp_path / "request.md"
    request.write_text("Refs #297\n\nCorrected description")
    gh.fail("edit")
    assert pr.apply_body(335, request, state, ctx) == (1, False)
    gh.fail("edit", False)
    assert pr.apply_body(335, request, state, ctx) == (0, True)
    assert len(edits(gh)) == 1   # the retry found the body already written


def test_cli_prints_json_and_keeps_exit_codes(gh, dirs, monkeypatch, capsys):
    state, ctx = dirs
    monkeypatch.setenv("STATE", str(state))
    monkeypatch.setenv("CTX", str(ctx))
    gh.set(headRefName="cursor/issue-297", body="Refs #297", commits=[], closingIssuesReferences=[])
    assert pr.main(["relation", "335"]) == 0
    assert json.loads(capsys.readouterr().out) == {"issue": "297", "completion": "partial"}
    assert pr.main(["preflight", "335"]) == 1
    assert "Acceptance advanced" in capsys.readouterr().err
    assert pr.main(["checks-none", "335"]) == 1
    assert pr.main(["apply-body", "335", str(ctx / "none.md")]) == 0
    assert json.loads(capsys.readouterr().out) == {"changed": False}
