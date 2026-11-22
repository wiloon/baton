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


# --- deferred manual checks (#48) -------------------------------------------------------

DEFERRED = "Closes #6\n\n## Checks to verify\n- Open the library at 390 px.\n- Open a book.\n\n## Before release\nnone\n"


MERGED = "2026-11-14T10:00:00Z"


@pytest.fixture
def deferred(monkeypatch, gh, tmp_path):
    monkeypatch.setenv("MANUAL_CHECKS", "defer")
    gh.set(mergedAt=MERGED)
    after_merge.defer_since()
    since = next(tmp_path.rglob("deferred-checks-since"))
    since.write_text("2026-11-14T09:00:00+00:00\n")   # defer took effect before these merges
    return since


def remainder(source, left):
    return json.dumps({"source": source, "left": left})


def test_deferred_checks_open_the_tracking_issue_when_none_is_open(gh, deferred):
    gh.set(body=DEFERRED, title="feat: covers")
    after_merge.deferred_checks(32, "6")
    [issue] = gh.created()
    assert issue["title"] == "Checks to try" and issue["label"] == "ready-for-human"
    assert issue["body"].endswith("### PR #32: feat: covers\nhttps://github.com/owner/app/pull/32\n\n"
                                  "- [ ] Open the library at 390 px.\n- [ ] Open a book.\n")


def test_deferred_checks_are_appended_to_the_open_tracking_issue_once_per_pr(gh, deferred):
    gh.set(body=DEFERRED, title="feat: covers",
           open_issues=[{"number": 41, "title": "Checks to try", "labels": ["ready-for-human"]}])
    after_merge.deferred_checks(32, "6")
    after_merge.deferred_checks(32, "6")
    comments = [c for c in gh.calls() if c[:3] == ["issue", "comment", "41"]]
    assert len(comments) == 1 and gh.created() == []
    assert comments[0][comments[0].index("--body") + 1].startswith("### PR #32: feat: covers\n")


def test_only_the_checks_the_screenshot_review_left_are_deferred(gh, deferred):
    gh.set(body=DEFERRED, title="feat: covers")
    Path(deferred.parent, "deferred-checks-items-pr-32").write_text(remainder(["Open the library at 390 px.", "Open a book."], ["Open a book."]))
    after_merge.deferred_checks(32, "6")
    [issue] = gh.created()
    assert issue["body"].endswith("\n\n- [ ] Open a book.\n") and "390 px" not in issue["body"]


def test_checks_the_screenshot_review_all_verified_add_nothing(gh, deferred):
    gh.set(body=DEFERRED, title="feat: covers")
    Path(deferred.parent, "deferred-checks-items-pr-32").write_text(remainder(["Open the library at 390 px.", "Open a book."], []))
    after_merge.deferred_checks(32, "6")
    assert gh.created() == [] and not [c for c in gh.calls() if c[:2] == ["issue", "comment"]]


def body_with(*sections):
    return "Closes #6\n\n" + "".join(f"## {heading}\n{text}\n\n" for heading, text in sections) + "## Before release\nnone\n"


@pytest.mark.parametrize("heading", ["Manual checks", "Checks to verify"])
def test_the_tracker_section_holds_the_left_items_under_either_name(gh, deferred, heading):
    gh.set(body=body_with((heading, "- Open the library at 390 px.\n- Open a book.")), title="feat: covers")
    Path(deferred.parent, "deferred-checks-items-pr-32").write_text(
        remainder(["Open the library at 390 px.", "Open a book."], ["Open a book."]))
    after_merge.deferred_checks(32, "6")
    [issue] = gh.created()
    assert issue["body"].endswith("\n\n- [ ] Open a book.\n") and "390 px" not in issue["body"]


@pytest.mark.parametrize("heading", ["Manual checks", "Checks to verify"])
def test_all_verified_marks_the_pr_done_under_either_name(gh, deferred, heading):
    gh.set(body=body_with((heading, "- Open a book.")), title="feat: covers")
    Path(deferred.parent, "deferred-checks-items-pr-32").write_text(remainder(["Open a book."], []))
    after_merge.deferred_checks(32, "6")
    assert gh.created() == [] and Path(deferred.parent, "deferred-checks-pr-32").exists()


def test_a_remainder_computed_from_other_checks_is_not_trusted(gh, deferred):
    gh.set(body=body_with(("Checks to verify", "- Open a book.\n- Added later.")), title="feat: covers")
    Path(deferred.parent, "deferred-checks-items-pr-32").write_text(remainder(["Open a book."], []))
    after_merge.deferred_checks(32, "6")
    [issue] = gh.created()
    assert "- [ ] Open a book.\n- [ ] Added later.\n" in issue["body"]


@pytest.mark.parametrize("saved", [None, "not json", json.dumps({"source": "x"})], ids=["absent", "unreadable", "malformed"])
def test_without_a_usable_remainder_every_current_check_is_sent(gh, deferred, saved):
    gh.set(body=body_with(("Checks to verify", "- Open a book.\n- Open a map.")), title="feat: covers")
    if saved is not None:
        Path(deferred.parent, "deferred-checks-items-pr-32").write_text(saved)
    after_merge.deferred_checks(32, "6")
    [issue] = gh.created()
    assert "- [ ] Open a book.\n- [ ] Open a map.\n" in issue["body"]


@pytest.mark.parametrize("left", ["", {}, [1]], ids=["string", "object", "non-string item"])
def test_a_malformed_remainder_with_matching_source_sends_the_current_check(gh, deferred, left):
    gh.set(body=body_with(("Checks to verify", "- Open the map")))
    Path(deferred.parent, "deferred-checks-items-pr-32").write_text(remainder(["Open the map"], left))
    after_merge.deferred_checks(32, "6")
    [issue] = gh.created()
    assert issue["body"].endswith("- [ ] Open the map\n")


@pytest.mark.parametrize("first, second, expected", [
    ("- Open a book.", "- Open a map.", ["Open a book.", "Open a map."]),
    ("Open a book.", "Open a map.", ["Open a book.", "Open a map."]),
    ("none", "none.", []),
    ("none\n\n🤖 Generated with Test Tool", "Open a map.", ["Open a map."]),
], ids=["lists", "plain text", "both none", "none with attribution"])
def test_deferred_recovery_handles_each_checks_block(gh, deferred, first, second, expected):
    gh.set(body=body_with(("Manual checks", first), ("Checks to verify", second)), title="feat: covers")
    after_merge.deferred_checks(32, "6")
    if expected:
        [issue] = gh.created()
        assert issue["body"].split("\n\n")[-1].splitlines() == [f"- [ ] {item}" for item in expected]
    else:
        assert gh.created() == []
    assert Path(deferred.parent, "deferred-checks-pr-32").exists()


def test_an_empty_section_with_no_remainder_is_done_without_a_tracker_entry(gh, deferred):
    gh.set(body=body_with(("Checks to verify", "")), title="feat: covers")
    after_merge.deferred_checks(32, "6")
    assert gh.created() == [] and Path(deferred.parent, "deferred-checks-pr-32").exists()


def test_an_open_legacy_tracker_keeps_receiving_checks(gh, deferred):
    gh.set(body=DEFERRED, title="feat: covers",
           open_issues=[{"number": 41, "title": "Manual checks to try", "labels": ["ready-for-human"]}])
    after_merge.deferred_checks(32, "6")
    assert gh.created() == [] and [c for c in gh.calls() if c[:3] == ["issue", "comment", "41"]]


def test_no_checks_and_the_gate_setting_add_nothing(gh, monkeypatch):
    gh.set(body=DEFERRED, title="t")
    after_merge.deferred_checks(32, "6")   # MANUAL_CHECKS unset: "gate"
    monkeypatch.setenv("MANUAL_CHECKS", "defer")
    gh.set(body="Closes #6\n\n## Checks to verify\nnone\n")
    after_merge.deferred_checks(33, "6")
    assert gh.created() == [] and not [c for c in gh.calls() if c[:2] == ["issue", "comment"]]


def test_two_prs_merging_in_one_tick_share_one_tracking_issue(gh, deferred):
    """The second PR must find the tracker the first just opened, though search does not see it yet (#52)."""
    gh.set(body=DEFERRED, title="feat: covers")
    after_merge.deferred_checks(32, "6")
    gh.set(title="feat: reader")
    after_merge.deferred_checks(33, "6")
    [issue] = gh.created()
    assert "### PR #32: feat: covers\n" in issue["body"]
    [comment] = [c for c in gh.calls() if c[:3] == ["issue", "comment", "900"]]
    assert comment[comment.index("--body") + 1].startswith("### PR #33: feat: reader\n")


def test_prs_merged_before_defer_took_effect_add_nothing(gh, deferred):
    deferred.write_text("2026-11-14T11:00:00+00:00\n")   # turned on after PR #20 merged
    gh.set(body=DEFERRED, title="feat: old")
    after_merge.deferred_checks(20, "6")
    gh.set(mergedAt="2026-11-14T12:00:00Z")
    after_merge.deferred_checks(20, "6")   # once decided, never filed later
    assert gh.created() == [] and not [c for c in gh.calls() if c[:2] == ["issue", "comment"]]


def test_defer_since_is_written_once_and_cleared_when_defer_is_off(gh, monkeypatch, tmp_path):
    monkeypatch.setenv("MANUAL_CHECKS", "defer")
    first = after_merge.defer_since()
    assert first is not None and after_merge.defer_since() == first
    monkeypatch.setenv("MANUAL_CHECKS", "gate")
    assert after_merge.defer_since() is None and not list(tmp_path.rglob("deferred-checks-since"))


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


def test_two_sites_on_one_pr_log_which_filing_hit_the_rejection(gh, deferred, linear, tmp_path):
    gh.set_labels()
    gh.fail("label-create")
    gh.set(body="## Before release\n- FAIL one — detail\n" + DEFERRED, title="feat: covers")
    after_merge.owner_actions(32, "")
    after_merge.deferred_checks(32, "6")
    log = log_lines(tmp_path)
    assert any(l.startswith("PR #32: owner actions: issue create missing label") for l in log)
    assert any(l.startswith("PR #32: deferred checks: issue create missing label") for l in log)


def test_deferred_checks_drops_the_marker_only_on_confirmed_missing_label(gh, deferred, tmp_path):
    gh.set_labels()   # ready-for-human absent
    gh.fail("label-create")
    gh.set(body=DEFERRED, title="feat: covers")
    after_merge.deferred_checks(32, "6")
    assert gh.created() == []
    marker = Path(tmp_path, "state", "deferred-checks-pr-32")
    assert not marker.exists()
    raw = Path(tmp_path, "logs", "runner.log").read_text()
    assert "could not add label: 'ready-for-human' not found" in raw
    lost_at = raw.index("deferred checks lost")
    assert "(gh:" in raw[lost_at:lost_at + 120]
    assert raw.index("(gh:", lost_at) < raw.index("Open the library", lost_at)
    assert sum(1 for c in gh.calls() if c[:2] == ["issue", "create"]) == 2   # labelled, one retry
    assert ("PR #32: deferred checks: label create 'ready-for-human' failed "
            "(gh: failed to create label 'ready-for-human')") in log_lines(tmp_path)
    gh.fail("label-create", on=False)   # label creation becomes possible; the label is still absent
    after_merge.deferred_checks(32, "6")
    assert "ready-for-human" in gh.labels()
    [issue] = gh.created()
    assert issue["label"] == "ready-for-human"
    after_merge.deferred_checks(32, "6")
    assert len(gh.created()) == 1


def test_deferred_checks_keeps_the_marker_on_ambiguous_create_failure(gh, deferred, tmp_path):
    gh.set(body=DEFERRED, title="feat: covers")
    gh.fail("issue-create")
    gh.stderr("issue-create", "HTTP 500: boom\n")
    after_merge.deferred_checks(32, "6")
    assert gh.created() == []
    assert Path(tmp_path, "state", "deferred-checks-pr-32").exists()
    assert not any(c[:2] == ["label", "create"] for c in gh.calls())
    raw = Path(tmp_path, "logs", "runner.log").read_text()
    lost_at = raw.index("deferred checks lost")
    assert "(gh: HTTP 500: boom)" in raw[lost_at:]
    assert raw.index("(gh:", lost_at) < raw.index("Open the library", lost_at)


def test_deferred_checks_keeps_the_marker_when_commenting_the_tracker_fails(gh, deferred, tmp_path):
    gh.set(body=DEFERRED, title="feat: covers",
           open_issues=[{"number": 41, "title": "Checks to try", "labels": ["ready-for-human"]}])
    gh.fail("issue-comment")
    gh.stderr("issue-comment", "HTTP 403: forbidden\n")
    after_merge.deferred_checks(32, "6")
    assert Path(tmp_path, "state", "deferred-checks-pr-32").exists()
    assert gh.created() == []
    [line] = [l for l in log_lines(tmp_path) if "deferred checks lost" in l]
    assert "(gh: HTTP 403: forbidden)" in line
    assert line.index("(gh:") < line.index("Open the library")


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
