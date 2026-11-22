"""baton.pr against a fake gh: the cases tests/manual-checks.sh and
tests/completion.sh drive through fix_pr, plus the merge gate."""
import json
from io import StringIO
import os
from pathlib import Path

import pytest

from baton import pr

BODY = ("<!-- baton-task issue=242 completion=complete -->\n\nCloses #242\n\n"
        "## What changed\nDisabled buttons.\n\n## Checks to verify\nnone\n\n"
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
    assert pr.checks_none(f"## What changed\nX.\n\n## Checks to verify\n{section}\n\n## Before release\nnone") is none


def test_checks_none_needs_the_section():
    assert not pr.checks_none("## What changed\nnone\n\n## Before release\nnone")
    assert pr.checks_none("## Checks to verify:\nnone")


def test_checks_none_with_two_sections_is_false():
    assert not pr.checks_none("## Checks to verify\nnone\n\n## Notes\nX.\n\n## Checks to verify\nnone")


def test_fix_reply_block_replaces_the_section_and_leaves_the_reply(gh, dirs):
    _, ctx = dirs
    gh.set(body=BODY)
    reply = pr.split_checks(7, "## Must fix\n1. adopted — brand colours\n\n## Checks to verify\n"
                               "- /billing: Subscribe buttons use the brand colour\n\nRESULT: fixed\n", ctx)
    assert "Manual checks" not in reply and "adopted — brand colours" in reply
    assert pr.apply_checks(7, ctx) == (0, True, "PR #7: checks refreshed after the fix")
    body = gh.pr["body"]
    assert "## Checks to verify\n- /billing: Subscribe buttons use the brand colour\n" in body
    assert not pr.checks_none(body)
    assert "Disabled buttons." in body and "- [nit] tidy copy" in body and "## Before release\nnone" in body
    assert not Path(ctx, "pr-7-fix-checks.json").exists()


def test_fix_without_a_block_is_not_rechecked(gh, dirs):
    _, ctx = dirs
    gh.set(body=BODY)
    pr.split_checks(7, "## Must fix\nnone\n\nRESULT: fixed\n", ctx)
    assert pr.apply_checks(7, ctx)[:2] == (0, True)
    assert "## Checks to verify\nNot re-checked after the fix run.\n" in gh.pr["body"]
    assert not pr.checks_none(gh.pr["body"])


def test_fix_that_rechecked_none_changes_nothing(gh, dirs):
    _, ctx = dirs
    gh.set(body=BODY)
    pr.split_checks(7, "## Checks to verify\nnone\n\nRESULT: fixed\n", ctx)
    assert pr.apply_checks(7, ctx) == (0, False, "")
    assert edits(gh) == []


def test_refused_edit_keeps_the_checks_for_a_retry(gh, dirs):
    _, ctx = dirs
    gh.set(body=BODY)
    pr.split_checks(7, "## Checks to verify\n- /billing: brand colour\n\nRESULT: fixed\n", ctx)
    gh.fail("edit")
    assert pr.apply_checks(7, ctx)[0] == 1
    assert Path(ctx, "pr-7-fix-checks.json").exists()
    gh.fail("view")
    assert pr.apply_checks(7, ctx)[0] == 1


def test_gate(gh):
    gh.set(headRefName="cursor/issue-242", body=BODY.replace("## Checks to verify\nnone", "## Checks to verify\n- look"),
           commits=[], closingIssuesReferences=[])
    assert pr.gate(7)["action"] == "manual_checks"
    gh.set(body=BODY)
    assert pr.gate(7) == {"action": "merge"}
    partial = PARTIAL + "\n\n## Checks to verify\nnone"
    gh.set(headRefName="cursor/issue-419", body=partial)
    result = pr.gate(7)
    assert result["action"] == "queue_review" and "Acceptance advanced" in result["reason"]
    gh.set(body=partial + "\n\n## Acceptance advanced\n- one item\n\nThis does not close #419.")
    result = pr.gate(7)
    assert result["action"] == "queue_review" and "closing keywords" in result["reason"]
    gh.fail("view")
    assert pr.gate(7)["action"] == "manual_checks"


# --- the checks section under either name (#94) ----------------------------------------

BOTH_HEADINGS = pytest.mark.parametrize("heading", ["Manual checks", "Checks to verify"])


def with_section(heading, text):
    return BODY.replace("## Checks to verify\nnone", f"## {heading}\n{text}")


@BOTH_HEADINGS
def test_gate_reads_the_section_under_either_name(gh, heading):
    gh.set(headRefName="cursor/issue-242", commits=[], closingIssuesReferences=[], body=with_section(heading, "- look"))
    assert pr.gate(7)["action"] == "manual_checks"                    # a list hands off
    assert pr.gate(7, cleared="- look") == {"action": "merge"}          # verdicts covering every item merge
    gh.set(body=with_section(heading, "none"))
    assert pr.gate(7) == {"action": "merge"}                            # none merges


@pytest.mark.parametrize("body, cleared", [
    (BODY.replace("## Checks to verify\nnone\n\n", ""), ""),
    (with_section("Checks to verify", "- look\n\n## Checks to verify\n- listen"), "- look\n- listen"),
    (with_section("Manual checks", "- look\n\n## Checks to verify\n- listen"), "- look\n- listen"),
    (with_section("Manual checks", "- look\n\n## Manual checks\n- listen"), "- look\n- listen"),
], ids=["no section", "two sections", "one old one new", "two old"])
def test_a_body_that_is_not_well_formed_never_clears_the_gate(gh, body, cleared):
    gh.set(headRefName="cursor/issue-242", commits=[], closingIssuesReferences=[], body=body)
    assert pr.gate(7)["action"] == "manual_checks"
    assert pr.gate(7, cleared=cleared)["action"] == "manual_checks"


@BOTH_HEADINGS
def test_an_empty_section_never_clears_the_gate(gh, heading):
    gh.set(headRefName="cursor/issue-242", commits=[], closingIssuesReferences=[], body=with_section(heading, ""))
    assert pr.gate(7, cleared="")["action"] == "manual_checks"
    assert pr.gate(7)["action"] == "manual_checks"


def test_checks_none_matches_the_heading_exactly():
    assert not pr.checks_none("## Manual checks to try\nnone")
    assert not pr.checks_none("## Checks\nnone")
    assert pr.checks_none("## manual CHECKS:\nnone")


@BOTH_HEADINGS
def test_a_fix_reply_under_either_name_leaves_one_canonical_section(gh, dirs, heading):
    _, ctx = dirs
    gh.set(body=with_section(heading, "- old"))
    pr.split_checks(7, f"## Must fix\nnone\n\n## {heading}\n- new check\n\nRESULT: fixed\n", ctx)
    assert pr.apply_checks(7, ctx)[:2] == (0, True)
    body = gh.pr["body"]
    assert body.count("## Checks to verify\n- new check\n") == 1 and "Manual checks" not in body


def test_a_fix_reply_replaces_both_headings_of_the_body_with_one(gh, dirs):
    _, ctx = dirs
    gh.set(body=with_section("Manual checks", "- a").replace("## Before release", "## Checks to verify\n- b\n\n## Before release"))
    pr.split_checks(7, "## Manual checks\n- first\n\n## Checks to verify\n- last\n\nRESULT: fixed\n", ctx)
    pr.apply_checks(7, ctx)
    body = gh.pr["body"]
    assert body.count("## Checks to verify") == 1 and "Manual checks" not in body
    assert "- last" in body and "- first" not in body and "- a\n" not in body and "- b\n" not in body


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


def test_task_line_retargets_the_pr(gh, dirs, monkeypatch, capsys):
    _, ctx = dirs
    gh.set(headRefName="cursor/issue-419", body=PARTIAL)
    monkeypatch.setattr("sys.stdin", StringIO("Agreed.\nTASK: complete\nRESULT: fixed\n"))
    assert pr.main(["apply-task", "420"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "changed": True,
        "log": "PR #420: task completion partial -> complete at the fixer's request; the next review checks it",
    }
    assert "issue=419 completion=complete" in gh.pr["body"] and "\nCloses #419\n" in gh.pr["body"]
    assert os.listdir(ctx) == []   # the body file is gone


def test_apply_task_cli_keeps_exit_two_when_retargeting_fails(gh, dirs, monkeypatch, capsys):
    initial = "Closes #419\n\nThis change closes #419.\n"
    gh.set(headRefName="cursor/issue-419", body=initial)
    monkeypatch.setattr("sys.stdin", StringIO("TASK: partial\nRESULT: fixed\n"))

    assert pr.main(["apply-task", "420"]) == 2
    assert json.loads(capsys.readouterr().out) == {"changed": False, "log": ""}
    assert gh.pr["body"] == initial and edits(gh) == []


@pytest.mark.parametrize("reply", [
    "Disagree with the point.\nRESULT: fixed\n",
    'The reviewer wrote "TASK: complete" but work remains.\nRESULT: fixed\n',
    "TASK: partial\nRESULT: fixed\n",
])
def test_no_task_request_changes_nothing(gh, dirs, reply):
    _, ctx = dirs
    gh.set(headRefName="cursor/issue-419", body=PARTIAL)
    assert pr.apply_task(420, pr.task_wanted(reply), ctx) == (0, False, "")
    assert edits(gh) == []


def test_task_request_failures(gh, dirs):
    _, ctx = dirs
    gh.set(headRefName="cursor/issue-419", body=PARTIAL)
    gh.fail("edit")
    assert pr.apply_task(420, "complete", ctx)[0] == 1
    gh.fail("edit", False)
    gh.set(headRefName="main", body="No relation.")
    assert pr.apply_task(420, "complete", ctx)[0] == 2


# --- description requests ---------------------------------------------------------------

def test_body_request(gh, dirs, tmp_path):
    state, ctx = dirs
    gh.set(headRefName="cursor/issue-297", body="Refs #297\n\nOld description")
    assert pr.apply_body(335, tmp_path / "missing.md", state, ctx) == (0, False, "", False)
    request = tmp_path / "request.md"
    request.write_text("")
    assert pr.apply_body(335, request, state, ctx) == (
        2, False, "description request must be a non-empty regular file and cannot be a symlink", False)
    link = tmp_path / "link.md"
    link.symlink_to(request)
    assert pr.apply_body(335, link, state, ctx) == (
        2, False, "description request must be a non-empty regular file and cannot be a symlink", False)
    request.write_text("Closes #297\n\nCorrected description")   # live partial cannot become complete
    rc, changed, reason, task_changed = pr.apply_body(335, request, state, ctx)
    assert (rc, changed, task_changed) == (2, False, False)
    assert reason == "a body edit cannot change task completion; a fix needs a matching TASK: line"
    request.write_text("Refs #297\n\nCorrected description")
    assert pr.apply_body(335, request, state, ctx) == (0, True, "", False)
    assert gh.pr["body"].endswith("Refs #297\n\nCorrected description\n")
    assert "issue=297 completion=partial" in gh.pr["body"]
    assert os.listdir(ctx) == []


def test_body_retry_recognizes_a_write_with_a_lost_response(gh, dirs, tmp_path):
    state, ctx = dirs
    gh.set(headRefName="cursor/issue-297", body="Refs #297\n\nOld description")
    request = tmp_path / "request.md"
    request.write_text("Refs #297\n\nCorrected description")
    gh.fail("edit")
    assert pr.apply_body(335, request, state, ctx) == (
        1, False, "GitHub could not update the PR description", False)
    gh.fail("edit", False)
    assert pr.apply_body(335, request, state, ctx) == (0, True, "", False)
    assert len(edits(gh)) == 1   # the retry found the body already written


def test_body_without_task_validates_against_the_current_completion(gh, dirs, tmp_path):
    state, ctx = dirs
    gh.set(headRefName="cursor/issue-419", body=PARTIAL)
    request = tmp_path / "request.md"
    request.write_text(PARTIAL.replace("A test.", "Revised text."))
    assert pr.apply_body(420, request, state, ctx) == (0, True, "", False)
    assert pr.apply_task(420, "complete", ctx)[:2] == (0, True)
    assert pr.apply_body(420, request, state, ctx) == (
        2, False, "a body edit cannot change task completion; a fix needs a matching TASK: line", False)


def test_task_wanted_takes_the_last_full_line(gh, dirs, tmp_path):
    assert pr.task_wanted("TASK: complete\nRESULT: fixed\n") == "complete"
    assert pr.task_wanted('said "TASK: complete"\nTASK: partial\nRESULT: fixed\n') == "partial"
    assert pr.task_wanted("no task line\nRESULT: fixed\n") == ""


@pytest.mark.parametrize("text", [
    "Task completion: partial\n\n## What changed\nFixed.\n",
    "Task completion: complete\n\n## What changed\nFixed.\n",
])
def test_task_change_allows_live_or_wanted_completion_in_one_edit(gh, dirs, tmp_path, text):
    state, ctx = dirs
    gh.set(headRefName="cursor/issue-419", body=BODY.replace("242", "419"))
    request = tmp_path / "request.md"
    request.write_text(text)
    assert pr.apply_body(420, request, state, ctx, completion="partial") == (0, True, "", True)
    assert "issue=419 completion=partial" in gh.pr["body"] and "Refs #419" in gh.pr["body"]
    assert "Fixed." in gh.pr["body"] and "Closes #419" not in gh.pr["body"]
    assert len(edits(gh)) == 1


def test_body_without_task_still_requires_live_completion(gh, dirs, tmp_path):
    state, ctx = dirs
    gh.set(headRefName="cursor/issue-419", body=BODY.replace("242", "419"))
    request = tmp_path / "request.md"
    request.write_text("Task completion: partial\n\n## What changed\nFixed.\n")
    rc, changed, reason, task_changed = pr.apply_body(420, request, state, ctx)
    assert (rc, changed, task_changed) == (2, False, False)
    assert reason == "a body edit cannot change task completion; a fix needs a matching TASK: line"
    assert edits(gh) == []


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


# --- pr.gh: transient failures are retried, the rest are not (#46) ---------------------

class Scripted:
    """pr.call answering from a script of (rc, stdout, stderr); every call is kept."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, *args, input=None, limit=None):
        import subprocess
        self.calls.append(args)
        rc, out, err = self.answers.pop(0)
        return subprocess.CompletedProcess(["gh", *args], rc, out, err)


@pytest.fixture
def scripted(monkeypatch):
    pauses = []
    monkeypatch.setattr(pr, "sleep", pauses.append)
    def install(*answers):
        fake = Scripted(*answers)
        monkeypatch.setattr(pr, "call", fake)
        fake.pauses = pauses
        return fake
    return install


@pytest.mark.parametrize("stderr", ["net/http: TLS handshake timeout", "HTTP 503: Service Unavailable",
                                    "HTTP 403: You have exceeded a secondary rate limit"])
def test_a_transient_failure_is_retried_with_backoff(scripted, stderr):
    fake = scripted((1, "", stderr), (1, "", stderr), (0, "ok\n", ""))
    assert pr.gh("issue", "view", "6", "-R", "o/r").stdout == "ok\n"
    assert len(fake.calls) == 3 and fake.pauses == list(pr.BACKOFF[:2])


@pytest.mark.parametrize("stderr", ["HTTP 404: Not Found", "HTTP 401: Bad credentials", "HTTP 422: Validation Failed",
                                    "unknown flag: --nope"])
def test_a_request_error_is_not_retried(scripted, stderr):
    fake = scripted((1, "", stderr))
    assert pr.gh("issue", "edit", "6", "-R", "o/r").returncode == 1
    assert len(fake.calls) == 1 and fake.pauses == []


def test_a_pr_create_that_landed_returns_the_existing_pr(scripted):
    fake = scripted((1, "", "read: connection reset by peer"), (0, "https://github.com/o/r/pull/9\n", ""))
    result = pr.gh("pr", "create", "-R", "o/r", "--head", "docs/x", "--base", "main", "--title", "t", "--body", "b")
    assert result.returncode == 0 and result.stdout.strip().endswith("/pull/9")
    assert [c[:2] for c in fake.calls] == [("pr", "create"), ("pr", "list")]


def test_a_landed_merge_is_recognized_after_a_transient_state_read(scripted):
    fake = scripted((1, "", "HTTP 503: Service Unavailable"), (1, "", "HTTP 502: Bad Gateway"), (0, "MERGED\n", ""))
    assert pr.gh("pr", "merge", "5", "-R", "o/r", "--squash").returncode == 0
    assert [c[:2] for c in fake.calls] == [("pr", "merge"), ("pr", "view"), ("pr", "view")]


def test_a_comment_that_did_not_land_is_posted_again(scripted):
    fake = scripted((1, "", "TLS handshake timeout"), (0, json.dumps({"comments": [{"body": "older"}]}), ""), (0, "", ""))
    assert pr.gh("pr", "comment", "5", "-R", "o/r", "--body", "<!-- baton-x -->\nhi").returncode == 0
    assert [c[:2] for c in fake.calls] == [("pr", "comment"), ("pr", "view"), ("pr", "comment")]


def test_why_is_the_last_stderr_lines():
    import subprocess
    result = subprocess.CompletedProcess([], 1, "", "a\n\nb\nc\nHTTP 502: Bad Gateway\n")
    assert pr.why(result) == " (gh: b | c | HTTP 502: Bad Gateway)"
    assert pr.why(subprocess.CompletedProcess([], 1, "", "")) == ""


def test_why_redacts_github_tokens_from_stderr(gh, child_token):
    import subprocess
    ghp = "ghp_" + ("a" * 40)
    stderr = f"auth failed token={ghp} also {child_token}\nmore\ntrail\n"
    got = pr.why(subprocess.CompletedProcess([], 1, "", stderr))
    assert got == " (gh: auth failed token=[redacted] also [redacted] | more | trail)"


def test_why_redacts_before_truncating_to_the_tail(gh, monkeypatch):
    import subprocess
    pem = "-----BEGIN KEY-----\nabcdefgh\nijklmnop\n-----END KEY-----"
    monkeypatch.setenv("SOME_API_SECRET", pem)
    got = pr.why(subprocess.CompletedProcess([], 1, "", f"x\n{pem}\ntail\n"))
    assert "BEGIN KEY" not in got and "abcdefgh" not in got and "ijklmnop" not in got
    assert "tail" in got


def test_why_withholds_stderr_when_secret_lookup_raises(gh, child_token, monkeypatch):
    import subprocess
    from baton import advisor

    def boom(env):
        raise IsADirectoryError("secret env file")
    monkeypatch.setattr(advisor, "secret_values", boom)
    got = pr.why(subprocess.CompletedProcess([], 1, "", f"denied {child_token}\n"))
    assert got == " (gh: [stderr withheld: redaction failed])"
    assert pr.why(subprocess.CompletedProcess([], 1, "", "")) == ""
