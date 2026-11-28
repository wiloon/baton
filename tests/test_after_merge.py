"""baton.after_merge and .md against a fake gh: side bugs filed once, nits on the
PR's own code never filed (P-2), owner actions in Linear once per issue (P-3).
Ported from tests/follow-ups.sh and tests/owner-actions.sh."""
import json
import re
import io
from pathlib import Path
import urllib.error

import pytest

from baton import after_merge, md

REVIEW = "<!-- baton-review round=1 by=codex -->\n"


def unlabelled(line):
    """A runner.log line without its time and its [owner/name] label, which every line carries."""
    _, _, label, message = line.split(" ", 3)
    assert re.fullmatch(r"\[[^/]+/[^/]+\]", label), line
    return message


def comments(*bodies, login="owner"):
    return [{"author": {"login": login}, "body": body} for body in bodies]


def log_lines(tmp_path):
    path = tmp_path / "logs" / "runner.log"
    return [unlabelled(line) for line in path.read_text().splitlines()
            if line[:1].isdigit()] if path.exists() else []


# --- md --------------------------------------------------------------------------------

@pytest.mark.parametrize("text, found", [
    ("## Checks to verify\n- look\n## Next\nx", "- look\n"),
    ("## Checks to verify\r\nnone\r\n", ""),
    ("## Checks to verify\n- None.\n", ""),
    ("## Checks to verify\n\n", ""),
    ("## Other\n- look", ""),
    ("## Checks to verify:\n- look", ""),   # the heading must match exactly
])
def test_section(text, found):
    assert md.section("Checks to verify", text) == found


def test_items():
    text = ("## Follow-ups\n- [bug] Toast twice — Claimer.tsx:40\n  more detail\n"
            "* [nit] tidy copy\n1. plain\n## Nits\n-\tlast\n\n")
    assert md.items(text) == [("bug", "Toast twice — Claimer.tsx:40\n  more detail"),
                              ("nit", "tidy copy"), ("nit", "plain"), ("nit", "last\n")]
    assert md.items("none\n") == []


def test_bug_key():
    assert md.bug_key("Same-tab invite waits until reload.") == "same tab invite waits until reload"


# --- follow-ups ------------------------------------------------------------------------

def test_a_bug_listed_twice_is_filed_once(gh, tmp_path):
    gh.set(title="fix(web): keep a newer invite code",
           body="## Follow-ups\n- [bug] Same-tab invite waits until reload — ReferralClaimer.tsx:24 ignores the event",
           comments=comments(REVIEW + "## Must fix\nnone\n## Follow-ups\n"
                             "- [bug] same-tab invite waits until reload. — ReferralClaimer.tsx:24, already in the PR\n"
                             "- [bug] Claim toast shows twice — ReferralClaimer.tsx:40")
           + comments(REVIEW + "## Follow-ups\n- [bug] ignored: not the owner's comment", login="someone"))
    after_merge.follow_ups(1, "2")
    created = gh.created()
    assert [c["title"] for c in created] == ["Same-tab invite waits until reload", "Claim toast shows twice"]
    assert {c["label"] for c in created} == {"needs-triage"}
    assert created[0]["body"].endswith("\n\nFound while working on PR #1 (issue #2), outside its scope.")
    assert "PR #1: filed a side bug | https://github.com/owner/app/issues/900" in log_lines(tmp_path)


def test_a_bug_with_an_open_issue_is_not_filed_again(gh, tmp_path):
    gh.set(body="", open_issues=["Claim toast shows twice"],
           comments=comments(REVIEW + "## Follow-ups\n- [bug] Same-tab invite waits until reload — x\n"
                                      "- [bug] Claim toast shows twice — ReferralClaimer.tsx:40"))
    after_merge.follow_ups(1, "2")
    assert [c["title"] for c in gh.created()] == ["Same-tab invite waits until reload"]
    assert "PR #1: side bug already filed: Claim toast shows twice" in log_lines(tmp_path)


def test_runs_once_per_pr(gh):
    gh.set(body="## Follow-ups\n- [bug] One — x", comments=[])
    after_merge.follow_ups(1, "2")
    after_merge.follow_ups(1, "2")
    assert len(gh.created()) == 1
    assert Path(after_merge.os.environ["STATE"], "follow-ups-pr-1").exists()


DIFF = ("diff --git a/api/billing_startup_test.go b/api/billing_startup_test.go\n"
        "--- a/api/billing_startup_test.go\n+++ b/api/billing_startup_test.go\n"
        "@@ -90,3 +95,20 @@ func x() {\n+added\n")


def test_nits_on_the_prs_own_code_are_not_filed(gh, tmp_path):
    gh.set(title="t", body="", diff=DIFF, comments=comments(
        REVIEW + "## Must fix\nnone\n## Nits\n"
        "- `api/billing_startup_test.go:109`: stray blank line in the PR's own helper.\n"
        "- `billing_startup_test.go:15-20`: repeated sequence, but outside the PR's hunks.\n"
        "- `api/config/config.go:757`: misplaced line the PR didn't touch.\n"
        "## Follow-ups\n- [nit] api/billing_startup_test.go:100 another own-code nit"))
    after_merge.follow_ups(1, "2")
    [issue] = gh.created()
    assert issue["title"] == "Follow-ups from #1: t" and issue["label"] == "follow-up"
    assert issue["body"] == (
        "<!-- baton-author: vendor=codex model=unknown -->\n"
        "Non-blocking items PR #1 (issue #2) found in code it left untouched. "
        "None changes behaviour the PR shipped; pick any subset or close this.\n\n## Review nits\n\n"
        "- `billing_startup_test.go:15-20`: repeated sequence, but outside the PR's hunks.\n"
        "- `api/config/config.go:757`: misplaced line the PR didn't touch.")
    assert "PR #1: own-code nit not filed: api/billing_startup_test.go:100 another own-code nit" in log_lines(tmp_path)


def test_a_nit_range_running_past_the_prs_hunk_is_filed(gh):
    gh.set(title="t", body="## Follow-ups\n- [nit] extension/E2E_TESTING.md:100-470 older text is stale",
           diff="+++ b/extension/E2E_TESTING.md\n@@ -76,6 +76,43 @@\n", comments=[])
    after_merge.follow_ups(1, "2")
    assert gh.created()[0]["body"].endswith("- extension/E2E_TESTING.md:100-470 older text is stale")


def test_a_nit_citing_code_outside_the_prs_hunks_as_well_is_filed(gh):
    gh.set(title="t", body="", diff="+++ b/extension/e2e/auth.ts\n@@ -30,7 +30,8 @@\n", comments=comments(
        REVIEW + "## Nits\n- `extension/e2e/auth.ts:31-34` and `extension/e2e/auth.ts:1-3`: both comments are stale."))
    after_merge.follow_ups(1, "2")
    assert "extension/e2e/auth.ts:1-3" in gh.created()[0]["body"]


def test_a_nit_the_fix_round_adopted_is_not_filed(gh, tmp_path):
    gh.set(title="t", body="", comments=comments(
        REVIEW + "## Nits\n- [nit] `extension/e2e/x.spec.ts:20-24`: instructions ask for a token nobody can get.\n"
                 "- [nit] `extension/e2e/y.spec.ts:5`: rejected by the fixer.\n"
                 "- [nit] `extension/e2e/z.spec.ts:7`: the fixer did not answer this one.",
        "<!-- baton-fix by=claude -->\n## Must fix\nnone\n## Nits\n"
        "- `extension/e2e/x.spec.ts:20-24` adopted — added a note.\n"
        "- `extension/e2e/y.spec.ts:5` rejected — out of scope.\n"
        "## Checks to verify\nnone"))
    after_merge.follow_ups(1, "2")
    body = gh.created()[0]["body"]
    assert "x.spec.ts" not in body and "y.spec.ts:5" in body and "z.spec.ts:7" in body
    assert "PR #1: adopted nit not filed: `extension/e2e/x.spec.ts:20-24`: instructions ask for a token nobody can get." in log_lines(tmp_path)


def test_a_fix_reply_saying_adopted_without_the_citation_files_the_nit(gh):
    gh.set(title="t", body="", comments=comments(
        REVIEW + "## Nits\n- [nit] `extension/e2e/x.spec.ts:20-24`: stale.",
        "<!-- baton-fix by=claude -->\n## Nits\n- `x.spec.ts`: adopted."))
    after_merge.follow_ups(1, "2")
    assert "x.spec.ts:20-24" in gh.created()[0]["body"]


def test_a_comment_that_only_looks_like_a_fix_reply_adopts_nothing(gh):
    gh.set(title="t", body="", comments=comments(REVIEW + "## Nits\n- [nit] `a/x.ts:20`: stale.") + comments(
        "<!-- baton-fix by=claude -->\n## Nits\n- `a/x.ts:20` adopted — done.", login="someone"))
    after_merge.follow_ups(1, "2")
    assert "a/x.ts:20" in gh.created()[0]["body"]


def test_an_adopted_item_under_follow_ups_is_not_filed(gh):
    gh.set(title="t", body="", comments=comments(
        REVIEW + "## Nits\nnone\n## Follow-ups\n- [nit] `a/x.ts:20` rename helper\n- [nit] `a/y.ts:3` keep me",
        "<!-- baton-fix by=codex -->\n## Nits\n- a/x.ts:20 adopted — renamed."))
    after_merge.follow_ups(1, "2")
    body = gh.created()[0]["body"]
    assert "a/x.ts" not in body and "a/y.ts:3" in body


def test_only_own_code_nits_file_nothing(gh):
    gh.set(body="", diff="+++ b/api/billing_startup_test.go\n@@ -1 +100,10 @@\n",
           comments=comments(REVIEW + "## Nits\n- `api/billing_startup_test.go:109`: stray blank line."))
    after_merge.follow_ups(1, "2")
    assert gh.created() == []


def test_noted_items_and_nits_of_several_rounds(gh):
    gh.set(title="t", body="## Follow-ups\n- [nit] rename helper\n\n## Before release\nnone", comments=comments(
        REVIEW + "## Nits\nnone", "<!-- baton-review round=2 by=claude -->\n## Nits\n- second round nit"))
    after_merge.follow_ups(1, "")
    assert gh.created()[0]["body"].endswith(
        "close this.\n\n## Review nits\n\n- second round nit\n\n\n"
        "## Noted while implementing or reviewing\n\n- rename helper")


def test_a_passing_split_review_files_the_owners_screenshot_nit(gh):
    screenshot_review = "<!-- baton-screenshot-review round=1 by=claude -->\n## Must fix\nnone\n"
    gh.set(title="t", body="", comments=(
        comments(REVIEW + "## Must fix\nnone\n## Nits\nnone",
                 screenshot_review + "## Nits\n- library.png: align the two labels\n## Screenshots\n- library.png — ok")
        + comments(screenshot_review + "## Nits\n- forged nit", login="another-user")))
    after_merge.follow_ups(1, "2")
    assert len(gh.created()) == 1
    assert gh.created()[0]["body"].endswith("## Review nits\n\n- library.png: align the two labels")


def test_design_findings_and_the_fixers_design_answers_are_never_filed(gh):
    shot = ("<!-- baton-screenshot-review round=1 by=claude -->\n## Must fix\nnone\n"
            "## Nits\n- library.png: align the two labels\n"
            "## Design findings\n1. checks 1 — a.css: bottom 24 px vs top 18 px; unequal gap; cut to 18 px\n"
            "## Design answers\nD1.1. held — still 24 px\n## Checks\n1. verified")
    fix = ("<!-- baton-fix by=claude loop=1-ab round=1 -->\n## Must fix\nnone\n## Nits\nnone\n"
           "## Design findings\nD1.1. rejected — brand spacing")
    gh.set(title="t", body="", comments=comments(REVIEW + "## Must fix\nnone\n## Nits\nnone", shot, fix))
    after_merge.follow_ups(1, "2")
    [issue] = gh.created()
    assert issue["body"].endswith("## Review nits\n\n- library.png: align the two labels")
    assert "24 px" not in issue["body"] and "brand spacing" not in issue["body"]


def test_a_follow_up_issue_files_no_further_follow_ups(gh, tmp_path):
    gh.set(body="## Follow-ups\n- [nit] rename helper\n- [bug] Real bug — x", issue_labels={"2": ["follow-up"]},
           comments=comments(REVIEW + "## Nits\n- a nit"))
    after_merge.follow_ups(1, "2")
    assert [c["title"] for c in gh.created()] == ["Real bug"]   # side bugs still are
    assert "PR #1: follow-up issue #2, nits dropped, not filed again" in log_lines(tmp_path)


def test_failed_filing_is_logged(gh, tmp_path):
    gh.set(body="## Follow-ups\n- [bug] Real bug — x\n- a nit", comments=[])
    gh.fail("issue-create")
    after_merge.follow_ups(1, "")
    assert log_lines(tmp_path) == ["PR #1: filing a side bug failed: Real bug",
                                   "PR #1: filing the follow-up issue failed"]


# --- owner actions -------------------------------------------------------------------

@pytest.fixture
def linear(monkeypatch):
    titles = []

    def create(title, description):
        titles.append(title)
        return None if title.startswith("FAIL") else "https://linear.app/t/WIL-1"
    monkeypatch.setattr(after_merge, "linear_create", create)
    return titles


def test_each_owner_action_is_filed_once_per_issue(gh, linear):
    gh.set(body="## Before release\n- Stripe webhook event list — subscribe the live endpoint to the new events\n"
                "- Cutover backup and drain — back up app.db before the cutover")
    after_merge.owner_actions(340, "297")
    assert linear == ["Stripe webhook event list", "Cutover backup and drain"]
    # The next slice of the same issue lists the same items, one spelled a little
    # differently, plus one new item.
    gh.set(body="## Before release\n- Stripe webhook event list. — subscribe the live endpoint to the new events\n"
                "- Cutover backup and drain — back up app.db before the cutover\n"
                "- Keep ADR-066 — leave Managed Payments off")
    after_merge.owner_actions(342, "297")
    assert linear[2:] == ["Keep ADR-066"]
    # Another issue with the same item title gets its own task.
    after_merge.owner_actions(400, "398")
    assert len(linear) == 6
    # A slice that only repeats filed items opens no fallback GitHub issue.
    assert gh.created() == []
    comment = [c for c in gh.calls() if c[:2] == ["pr", "comment"]][0]
    assert comment[comment.index("--body") + 1] == (
        "<!-- baton-owner-actions -->\nBefore the next release (Linear):\n"
        "- [Stripe webhook event list](https://linear.app/t/WIL-1)\n"
        "- [Cutover backup and drain](https://linear.app/t/WIL-1)\n")


def test_owner_actions_run_once_per_pr_and_skip_none(gh, linear, tmp_path):
    gh.set(body="## Before release\nnone")
    after_merge.owner_actions(1, "2")
    assert not Path(tmp_path, "state", "owner-actions-pr-1").exists()   # retried next tick
    gh.set(body="## Before release\n- One thing")
    after_merge.owner_actions(1, "2")
    after_merge.owner_actions(1, "2")
    assert linear == ["One thing"]


def test_linear_failure_falls_back_to_github(gh, linear, tmp_path):
    gh.set(body="## Before release\n- FAIL one — detail\n- Works")
    after_merge.owner_actions(5, "")
    [issue] = gh.created()
    assert issue["label"] == "ready-for-human"
    assert issue["title"] == "Before the next release: owner actions from #5"
    assert issue["body"] == ("Filing these in Linear failed, so they are here instead (principle P-3). "
                             "From PR #5.\n\n- FAIL one — detail\n")
    assert Path(tmp_path, "state", "owner-actions-issue-none-5").read_text() == "fail one\nworks\n"
    assert "PR #5: owner actions (Linear failed) | https://github.com/owner/app/issues/900" in log_lines(tmp_path)


def test_prose_owner_actions_are_handed_over_whole(gh, linear):
    gh.set(body="## Before release\nRotate the webhook secret on the host.")
    after_merge.owner_actions(5, "7")
    assert linear == []
    assert gh.created()[0]["body"].endswith("From PR #5 (issue #7).\n\nRotate the webhook secret on the host.")


def test_linear_create_without_a_key(gh, monkeypatch, tmp_path):
    monkeypatch.setenv("LINEAR_ENV", str(tmp_path / "missing.env"))
    assert after_merge.linear_create("t", "d") is None
    (tmp_path / "linear.env").write_text("OTHER=1\n")
    (tmp_path / "linear.env").chmod(0o600)
    monkeypatch.setenv("LINEAR_ENV", str(tmp_path / "linear.env"))
    assert after_merge.linear_create("t", "d") is None


# --- why Linear failed ---------------------------------------------------------------

def test_linear_create_logs_a_missing_key(gh, tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_ENV", str(tmp_path / "missing.env"))
    assert after_merge.linear_create("t", "d") is None
    assert any("Linear: cannot read the key" in line for line in log_lines(tmp_path))


def linear_key(tmp_path, monkeypatch):
    (tmp_path / "linear.env").write_text("LINEAR_API_KEY=lin_secret\n")
    (tmp_path / "linear.env").chmod(0o600)
    monkeypatch.setenv("LINEAR_ENV", str(tmp_path / "linear.env"))


def test_linear_create_logs_an_http_error_without_the_key(gh, tmp_path, monkeypatch):
    linear_key(tmp_path, monkeypatch)

    def refuse(request, timeout):
        raise urllib.error.HTTPError("https://api.linear.app", 400, "Bad Request", {},
                                     io.BytesIO(b'{"errors":[{"message":"Argument invalid"}]}'))
    monkeypatch.setattr(after_merge.urllib.request, "urlopen", refuse)
    assert after_merge.linear_create("Keep ADR-066", "d") is None
    log = "\n".join(log_lines(tmp_path))
    assert "Linear: creating 'Keep ADR-066' failed: HTTP 400" in log and "Argument invalid" in log
    assert "lin_secret" not in log


def test_linear_create_logs_graphql_errors_and_network_errors(gh, tmp_path, monkeypatch):
    linear_key(tmp_path, monkeypatch)

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False
    monkeypatch.setattr(after_merge.urllib.request, "urlopen",
                        lambda request, timeout: Response(b'{"errors":[{"message":"Entity not found: labelIds"}]}'))
    assert after_merge.linear_create("t", "d") is None
    assert any("Entity not found: labelIds" in line for line in log_lines(tmp_path))

    def down(request, timeout):
        raise urllib.error.URLError("certificate verify failed")
    monkeypatch.setattr(after_merge.urllib.request, "urlopen", down)
    assert after_merge.linear_create("t", "d") is None
    assert any("certificate verify failed" in line for line in log_lines(tmp_path))


# --- side bugs are filed when the PR opens -----------------------------------------------

BUG_BODY = "## Follow-ups\n- [bug] My Documents omits a saved document — page.tsx:88 never invalidates\n- [nit] tidy"


def test_a_side_bug_is_filed_at_pr_open_and_its_number_posted(gh):
    gh.set(body=BUG_BODY, comments=[])
    after_merge.file_pr_bugs("5", "7")
    assert [(c["title"], c["label"]) for c in gh.created()] == [("My Documents omits a saved document", "needs-triage")]
    posted = gh.pr["comments"][-1]["body"]
    assert posted.startswith("<!-- baton-filed-bugs -->")
    assert "#900 My Documents omits a saved document" in posted


def test_filing_at_pr_open_runs_once_and_the_merge_does_not_file_again(gh):
    gh.set(body=BUG_BODY, comments=[], title="t")
    after_merge.file_pr_bugs("5", "7")
    after_merge.file_pr_bugs("5", "7")
    assert len(gh.created()) == 1
    gh.set(open_issues=[])   # the bug issue was closed before the merge: still not filed again
    after_merge.follow_ups("5", "7")
    assert [c["title"] for c in gh.created()].count("My Documents omits a saved document") == 1


def test_a_bug_already_open_is_not_filed_or_posted(gh):
    gh.set(body=BUG_BODY, comments=[], open_issues=["My Documents omits a saved document"])
    after_merge.file_pr_bugs("5", "7")
    assert gh.created() == [] and not gh.pr["comments"]


# --- a bug the PR already names by number (a fix round wrote it) is not filed again ------

NUMBERED_BODY = ("## Follow-ups\n- [bug] #650 — fixed by #656 (merged). A side panel card keeps a "
                 "stale miss — SidePanel.tsx:900\n- [bug] #651: popover spills past the edge")


def test_a_bug_the_pr_names_by_number_is_not_filed_after_the_merge(gh, tmp_path):
    gh.set(title="t", body=NUMBERED_BODY, comments=comments(
        REVIEW + "## Follow-ups\n- [bug] #652 Toast shows twice — Claimer.tsx:40"))
    after_merge.follow_ups("5", "7")
    assert gh.created() == []
    assert "PR #5: side bug already filed: #650" in log_lines(tmp_path)


def test_a_bug_the_pr_names_by_number_is_not_filed_when_the_pr_opens(gh):
    gh.set(body=NUMBERED_BODY, comments=[])
    after_merge.file_pr_bugs("5", "7")
    assert gh.created() == [] and not gh.pr["comments"]


# --- missing-label recovery (#168) -------------------------------------------------------

def test_follow_ups_creates_a_missing_label_then_files_labelled(gh, tmp_path):
    gh.set_labels("needs-triage")   # follow-up absent
    gh.set(title="t", body="## Follow-ups\n- [nit] rename helper", comments=[])
    after_merge.follow_ups(1, "")
    [issue] = gh.created()
    assert issue["label"] == "follow-up" and issue["labels"] == ["follow-up"]
    assert "follow-up" in gh.labels()
    assert ("PR #1: follow-ups: issue create missing label "
            "(gh: could not add label: 'follow-up' not found)") in log_lines(tmp_path)
    assert [c[:2] for c in gh.calls() if c[:2] == ["label", "create"]] == [["label", "create"]]
    assert sum(1 for c in gh.calls() if c[:2] == ["issue", "create"]) == 2


def test_follow_ups_falls_back_to_unlabelled_when_label_create_fails(gh, tmp_path):
    gh.set_labels("needs-triage")
    gh.fail("label-create")
    gh.set(title="t", body="## Follow-ups\n- [nit] rename helper", comments=[])
    after_merge.follow_ups(1, "")
    [issue] = gh.created()
    assert issue["labels"] == [] and issue["label"] == ""
    log = log_lines(tmp_path)
    assert ("PR #1: follow-ups: issue create missing label "
            "(gh: could not add label: 'follow-up' not found)") in log
    assert ("PR #1: follow-ups: label create 'follow-up' failed "
            "(gh: failed to create label 'follow-up')") in log
    assert any("without label follow-up" in line and "issues/900" in line for line in log)
    assert any("apply follow-up before work starts" in line for line in log)
    assert sum(1 for c in gh.calls() if c[:2] == ["issue", "create"]) == 3   # labelled, retry, unlabelled


def test_readme_documents_follow_up_and_needs_triage():
    text = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    rows = {line.split("|")[1].strip(): line for line in text.splitlines() if line.startswith("| `")}
    assert "files no further nits" in rows["`follow-up`"]
    assert "side bug" in rows["`needs-triage`"]
    loop = text[text.index("for l in"):text.index("done")]
    assert "follow-up" in loop


def test_filing_failure_redacts_tokens_in_runner_log(gh, tmp_path, child_token):
    ghp = "ghp_" + ("b" * 40)
    gh.set(title="t", body="## Follow-ups\n- [nit] rename helper", comments=[])
    gh.fail("issue-create")
    gh.stderr("issue-create", f"denied using {ghp} and {child_token}\n")
    after_merge.follow_ups(1, "")
    log = Path(tmp_path, "logs", "runner.log").read_text()
    assert ghp not in log and child_token not in log
    assert log_lines(tmp_path) == [
        "PR #1: filing the follow-up issue failed (gh: denied using [redacted] and [redacted])"]


def test_file_pr_bugs_falls_back_to_unlabelled_and_posts_the_number(gh, tmp_path):
    gh.set_labels()   # needs-triage absent
    gh.fail("label-create")
    gh.set(body=BUG_BODY, comments=[])
    after_merge.file_pr_bugs("5", "7")
    [issue] = gh.created()
    assert issue["labels"] == [] and issue["title"] == "My Documents omits a saved document"
    assert "#900 My Documents omits a saved document" in gh.pr["comments"][-1]["body"]
    log = log_lines(tmp_path)
    assert ("PR #5: side bug: issue create missing label "
            "(gh: could not add label: 'needs-triage' not found)") in log
    assert ("PR #5: side bug: label create 'needs-triage' failed "
            "(gh: failed to create label 'needs-triage')") in log
    assert ("PR #5: filed a side bug without label needs-triage — apply it | "
            "https://github.com/owner/app/issues/900") in log
    assert sum(1 for c in gh.calls() if c[:2] == ["issue", "create"]) == 3
    gh.set(open_issues=[], title="t")
    after_merge.follow_ups("5", "7")
    assert [c["title"] for c in gh.created()].count("My Documents omits a saved document") == 1


def test_owner_actions_falls_back_to_unlabelled_when_label_stays_missing(gh, linear, tmp_path):
    gh.set_labels()
    gh.fail("label-create")
    gh.set(body="## Before release\n- FAIL one — detail")
    after_merge.owner_actions(5, "")
    [issue] = gh.created()
    assert issue["labels"] == [] and issue["title"].startswith("Before the next release")
    log = log_lines(tmp_path)
    assert ("PR #5: owner actions: issue create missing label "
            "(gh: could not add label: 'ready-for-human' not found)") in log
    assert ("PR #5: owner actions (Linear failed) without label ready-for-human — apply it | "
            "https://github.com/owner/app/issues/900") in log
    assert sum(1 for c in gh.calls() if c[:2] == ["issue", "create"]) == 3


def test_owner_actions_terminal_failure_logs_why_before_the_list(gh, linear, tmp_path):
    gh.set(body="## Before release\n- FAIL one — detail")
    gh.fail("issue-create")
    gh.stderr("issue-create", "HTTP 403: forbidden\n")
    after_merge.owner_actions(5, "")
    assert gh.created() == []
    [line] = [l for l in log_lines(tmp_path) if "owner actions lost" in l]
    assert "(gh: HTTP 403: forbidden)" in line
    assert line.index("(gh:") < line.index("FAIL one")


def test_side_bug_terminal_failure_logs_why(gh, tmp_path):
    gh.set(body="## Follow-ups\n- [bug] Real bug — x", comments=[])
    gh.fail("issue-create")
    gh.stderr("issue-create", "HTTP 403: forbidden\n")
    after_merge.follow_ups(1, "")
    assert "PR #1: filing a side bug failed: Real bug (gh: HTTP 403: forbidden)" in log_lines(tmp_path)


def test_follow_ups_label_create_already_exists_then_labelled_retry_succeeds(gh, tmp_path, monkeypatch):
    """A concurrent create leaves the label in place; label create returns already-exists."""
    meta = {"color": "d73a4a", "description": "runner follow-ups"}
    gh.set_labels("needs-triage")
    gh.set(title="t", body="## Follow-ups\n- [nit] rename helper", comments=[])
    from baton import pr as pr_mod
    inner = pr_mod.call

    def call(*args, input=None, limit=None):
        result = inner(*args, input=input, limit=limit)
        if (args[:2] == ("issue", "create") and result.returncode != 0
                and "follow-up" not in gh.labels()):
            inventory = gh.labels()
            inventory["follow-up"] = dict(meta)
            (gh.root / "labels.json").write_text(json.dumps(inventory))
        return result

    monkeypatch.setattr(pr_mod, "call", call)
    after_merge.follow_ups(1, "")
    [issue] = gh.created()
    assert issue["labels"] == ["follow-up"]
    assert gh.labels()["follow-up"] == meta
    assert [c for c in gh.calls() if c[:2] == ["label", "create"]] == [
        ["label", "create", "follow-up", "-R", "owner/app"]]
    assert ("PR #1: follow-ups: label create 'follow-up' failed "
            "(gh: label 'follow-up' already exists; use --force to recreate)") in log_lines(tmp_path)


def test_follow_ups_non_label_create_failure_does_not_recover(gh, tmp_path):
    gh.set(title="t", body="## Follow-ups\n- [nit] rename helper", comments=[])
    gh.fail("issue-create")
    gh.stderr("issue-create", "HTTP 403: forbidden\n")
    after_merge.follow_ups(1, "")
    assert gh.created() == []
    assert not any(c[:2] == ["label", "create"] for c in gh.calls())
    assert sum(1 for c in gh.calls() if c[:2] == ["issue", "create"]) == 1
    assert any("HTTP 403: forbidden" in line for line in log_lines(tmp_path))


def test_follow_ups_non_label_error_on_retry_skips_unlabelled_fallback(gh, tmp_path, monkeypatch):
    gh.set_labels("needs-triage")
    gh.set(title="t", body="## Follow-ups\n- [nit] rename helper", comments=[])
    creates = {"n": 0}
    from baton import pr as pr_mod
    import subprocess
    inner = pr_mod.call

    def call(*args, input=None, limit=None):
        if args[:2] == ("issue", "create") and "--label" in args:
            creates["n"] += 1
            if creates["n"] >= 2:
                return subprocess.CompletedProcess(["gh", *args], 1, "", "HTTP 403: still forbidden\n")
        return inner(*args, input=input, limit=limit)

    monkeypatch.setattr(pr_mod, "call", call)
    after_merge.follow_ups(1, "")
    assert gh.created() == []
    assert not any(c[:2] == ["issue", "create"] and "--label" not in c for c in gh.calls())
    assert any("HTTP 403: still forbidden" in line for line in log_lines(tmp_path))


def test_file_pr_bugs_adds_author_marker_only_for_single_pr_author(gh, tmp_path):
    from baton import vendors
    state = str(tmp_path / "state")
    vendors.record_author(state, "10", "claude")
    vendors.record_author_model(state, "10", "claude", "claude-sonnet-4-6")
    gh.set(body="## Follow-ups\n- [bug] Crash on empty — a.py:1\n  <!-- baton-author: vendor=codex model=fake -->")
    after_merge.file_pr_bugs("10", "5")
    [issue] = gh.created()
    assert issue["body"].startswith("<!-- baton-author: vendor=claude model=claude-sonnet-4-6 -->\n")
    assert "vendor=codex" not in issue["body"]

    # Multiple PR authors -> no marker
    vendors.record_author(state, "11", "claude")
    vendors.record_author(state, "11", "codex")
    gh.set(body="## Follow-ups\n- [bug] Another bug — b.py:2")
    after_merge.file_pr_bugs("11", "6")
    assert not gh.created()[-1]["body"].startswith("<!-- baton-author:")


def test_follow_ups_author_markers_for_side_bugs_nits_and_owner_actions(gh, tmp_path):
    from baton import vendors
    state = str(tmp_path / "state")
    vendors.record_author(state, "20", "claude")
    gh.set(
        title="feat: example",
        body="## Follow-ups\n- [nit] tidy helper\n\n## Before release\n- rotate secret",
        comments=comments(
            "<!-- baton-review round=1 by=codex -->\n## Follow-ups\n- [bug] Race in cache — c.py:9\n## Nits\n- rename var",
        ),
    )
    after_merge.follow_ups("20", "7")
    after_merge.owner_actions("20", "7")
    created = gh.created()
    bug_issue = next(c for c in created if c["title"] == "Race in cache")
    assert bug_issue["body"].startswith("<!-- baton-author: vendor=codex model=unknown -->\n")
    # Combined follow-up has items from both claude (PR body) and codex (review nits) -> no marker
    fu_issue = next(c for c in created if c["title"].startswith("Follow-ups from #20:"))
    assert "<!-- baton-author:" not in fu_issue["body"]
    # Owner-actions (ready-for-human) gets no marker
    owner_issue = next(c for c in created if c["title"].startswith("Before the next release:"))
    assert "<!-- baton-author:" not in owner_issue["body"]

    # Single-vendor follow-up issue (only codex review + screenshot review nits) -> carries codex marker
    gh.set(
        title="feat: single",
        body="## Follow-ups\nnone",
        comments=comments(
            "<!-- baton-review round=1 by=codex -->\n## Nits\n- nit one",
            "<!-- baton-screenshot-review round=1 by=codex -->\n## Nits\n- nit two",
        ),
    )
    after_merge.follow_ups("21", "8")
    single_fu = next(c for c in gh.created() if c["title"].startswith("Follow-ups from #21:"))
    assert single_fu["body"].startswith("<!-- baton-author: vendor=codex model=unknown -->\n")
