"""baton.merge: the merge or the handoff after a passed review, the CI repair round and the
task recovery, against a real git worktree and the fake `gh` of conftest. Ported from
tests/shots.sh, tests/verify.sh and tests/test_pr_lifecycle.py."""
import json
import os
from pathlib import Path

import pytest

from baton import after_merge, ci, merge, notify, pick, pr as pr_helpers
from conftest import sh
from test_notify import PNG, Telegram

BIN = Path(__file__).resolve().parents[1] / "bin"
PR = 335
CLEAN = "Closes #297\n\n## Checks to verify\nnone\n"
PARTIAL = "Refs #297\n\n## Acceptance advanced\n- item one\n\n## Checks to verify\nnone\n"


@pytest.fixture
def run(gh, worktree, tmp_path, monkeypatch):
    env = {"STATE": os.environ["STATE"], "CTX": os.environ["CTX"], "LOGS": os.environ["LOGS"], "GH_REPO": "owner/app",
           "OWNER": "owner", "SELF_DIR": str(BIN), "VENDOR_POOL": "claude codex", "AUTO_MERGE": "1",
           "WT_ROOT": str(tmp_path / "worktrees"), "REPO_DIR": str(worktree.repo), "CI_FIXABLE_CHECKS": "golangci-lint\ngo test",
           "ISSUE": "297"}
    gh.set(headRefName="cursor/issue-297", body=CLEAN, commits=[], closingIssuesReferences=[{"number": 297}],
           issues={"297": {"state": "OPEN"}})
    monkeypatch.setattr(pick.settle, "probe", lambda *args: None)

    class State:
        pass
    state = State()
    state.env, state.gh, state.wt, state.repo, state.events = env, gh, worktree.wt, worktree.repo, []
    state.greens = [True]
    state.repair = 0

    def event(name):
        return lambda *args, **kwargs: state.events.append(name) or state.returns.get(name, True)
    state.returns = {}
    real_gh, real_notify = pr_helpers.gh, notify.notify

    def traced_gh(*args, **kw):
        if args[:2] in (("pr", "merge"), ("issue", "edit")):
            state.events.append(" ".join(args[:2]) + (" agent-wip" if "agent-wip" in args else ""))
        return real_gh(*args, **kw)
    monkeypatch.setattr(pr_helpers, "gh", traced_gh)
    state.texts = {}
    monkeypatch.setattr(merge.notify, "notify", lambda title, text="": (state.events.append(f"notify {title}"),
                                                                       state.texts.__setitem__(title, text), real_notify(title, text)))
    shots = Path(env["CTX"], f"pr-{PR}-shots")
    shots.mkdir()
    (shots / "01-library-light.png").write_bytes(PNG)
    telegram = Telegram()
    monkeypatch.setattr(notify, "credentials", lambda: {"TELEGRAM_BOT_TOKEN": "fake-token", "TELEGRAM_CHAT_ID": "42"})
    monkeypatch.setattr(notify.urllib.request, "urlopen", telegram.urlopen)
    state.captions = lambda: [telegram.form(i)["caption"] for i in range(len(telegram.requests))
                              if telegram.method(i) == "sendPhoto"]
    monkeypatch.setattr(merge.Merger, "ci_green", lambda self, minutes: (state.events.append("ci"), state.greens.pop(0) if len(state.greens) > 1 else state.greens[0])[1])
    monkeypatch.setattr(merge.Merger, "ci_fix_round", lambda self, wt: (state.events.append("repair"), state.repair)[1])
    monkeypatch.setattr(merge.Merger, "after_merge", lambda self: state.events.append("after_merge"))
    state.log = lambda: [line.split(" ", 2)[2] for line in Path(env["LOGS"], "runner.log").read_text().splitlines()] \
        if Path(env["LOGS"], "runner.log").exists() else []
    state.labelled = lambda label: any(c[:2] == ["pr", "edit"] and label in c for c in gh.calls())
    state.merged = lambda: any(c[:2] == ["pr", "merge"] for c in gh.calls())

    def merger(**overrides):
        return merge.Merger({**env, **overrides}, merge.telemetry.from_env(env, issue=297, pr=PR), PR, "297")
    state.merger = merger
    state.go = lambda wt=None, screenshot=None, **overrides: merger(**overrides).merge_or_handoff(
        wt or str(worktree.wt), screenshot)
    return state


# --- the merge ------------------------------------------------------------------------------

def test_a_green_pr_with_no_manual_checks_merges_in_the_contract_order(run):
    run.go()
    assert run.events == ["ci", "pr merge", "notify PR #335 merged", "issue edit agent-wip", "after_merge"]
    assert not run.wt.exists() and not run.captions()
    assert any("cleaned up worktree" in line for line in run.log())
    assert not run.labelled("ready-for-human")


def test_the_merged_pr_loses_its_screenshots_and_its_task_is_recovered(run):
    shots = Path(run.env["CTX"], f"pr-{PR}-shots")
    assert shots.is_dir()
    run.go()
    assert not shots.exists()
    # Closes #297 and the issue still open: GitHub's transition is pending, so no marker yet.
    assert not Path(run.env["STATE"], f"task-merged-pr-{PR}").exists()


def test_a_ci_failure_gets_one_repair_round_then_merges_when_green(run):
    run.greens = [False, True]
    run.go()
    assert run.events[:3] == ["ci", "repair", "ci"] and run.merged()


def test_a_ci_failure_that_stays_red_goes_to_the_owner(run):
    run.greens = [False, False]
    run.go()
    assert run.events == ["ci", "repair", "ci", "notify PR #335: CI not green"] and run.labelled("ready-for-human")
    assert not run.merged()


def test_a_repair_that_fails_goes_to_the_owner_without_another_wait(run):
    run.greens, run.repair = [False], 1
    run.go()
    assert run.events == ["ci", "repair", "notify PR #335: CI not green"] and not run.merged()


def test_manual_checks_go_to_the_owner_with_screenshots(run):
    run.gh.set(body=CLEAN.replace("none", "- check the theme"))
    run.go()
    assert run.labelled("ready-for-human") and not run.merged() and len(run.captions()) == 1
    assert "notify PR #335 needs a manual check" in run.events


# --- the screenshot review's verdicts (#92) --------------------------------------------------

ONE = "The library at 390 px."
SIGNUP = "Sign up and land back on Welcome."
TWO = f"- {ONE}\n- {SIGNUP}"


def verdicts(*lines):
    return "<!-- baton-screenshot-review round=1 by=codex -->\n## Must fix\nnone\n\n## Checks\n" + "\n".join(lines)


def test_checks_all_verified_by_the_screenshot_review_merge_and_the_shots_still_reach_the_owner(run):
    run.gh.set(body=CLEAN.replace("none", TWO))
    run.go(screenshot={"checks": TWO, "comment": verdicts("1. verified", "2. verified")})
    assert run.merged() and not run.labelled("ready-for-human")
    assert len(run.captions()) == 1 and "screenshot review" in run.captions()[0]
    assert run.captions()[0].splitlines()[3:] == [f"- {ONE}", f"- {SIGNUP}"]
    assert "notify PR #335 merged" in run.events


def test_only_the_checks_left_for_the_owner_are_handed_over(run):
    run.gh.set(body=CLEAN.replace("none", TWO))
    run.go(screenshot={"checks": TWO, "comment": verdicts("1. verified", "2. owner — a real sign-up")})
    assert run.labelled("ready-for-human") and not run.merged() and len(run.captions()) == 1
    text = run.texts["PR #335 needs a manual check"]
    assert "left these checks for you" in text and SIGNUP in text and ONE not in text
    assert run.captions()[0].splitlines()[3:] == [f"- {SIGNUP}"]


@pytest.mark.parametrize("case", ["missing review", "no verdicts", "checks changed"])
def test_anything_short_of_verified_checks_goes_to_the_owner(run, case):
    run.gh.set(body=CLEAN.replace("none", TWO))
    comment = verdicts("1. verified", "2. verified")
    if case == "missing review":
        comment = None
    elif case == "no verdicts":
        comment = "<!-- baton-screenshot-review round=1 by=codex -->\n## Screenshots\n- a.png — ok"
    expected = [ONE, SIGNUP]
    if case == "checks changed":
        expected.append("A new check.")
        run.gh.set(body=CLEAN.replace("none", TWO + "\n- A new check."))
    run.go(screenshot={"checks": TWO, "comment": comment} if comment is not None else None)
    assert run.labelled("ready-for-human") and not run.merged()
    lines = [f"- {item}" for item in expected]
    handoff = run.texts["PR #335 needs a manual check"]
    assert "Checks for you" in handoff and handoff.splitlines()[1:-1] == lines
    [caption] = run.captions()
    assert caption.startswith("PR #335: Checks for you\n") and caption.splitlines()[3:] == lines


def test_checks_the_owner_wrote_into_the_issue_merge_once_verified(run):
    run.gh.set(body=CLEAN.replace("none", TWO),
               issues={"297": {"state": "OPEN", "body": "## Checks to verify\n- Sign up and land back on Welcome."}})
    run.go(screenshot={"checks": TWO, "comment": verdicts("1. verified", "2. verified")})
    assert run.merged() and not run.labelled("ready-for-human")


def test_deferred_checks_keep_only_what_the_screenshot_review_left(run):
    run.gh.set(body=CLEAN.replace("none", TWO))
    run.go(MANUAL_CHECKS="defer", screenshot={"checks": TWO, "comment": verdicts("1. verified", "2. owner")})
    assert run.merged()
    saved = json.loads(Path(run.env["STATE"], f"deferred-checks-items-pr-{PR}").read_text())
    assert saved == {"source": [ONE, SIGNUP], "left": [SIGNUP]}


def test_deferred_manual_checks_do_not_stop_the_merge(run):
    run.gh.set(body=CLEAN.replace("none", "- check the theme"))
    run.go(MANUAL_CHECKS="defer")
    assert run.merged() and not run.labelled("ready-for-human") and not run.captions()
    assert "notify PR #335 merged" in run.events and "after_merge" in run.events


def test_deferred_manual_checks_still_need_auto_merge(run):
    run.gh.set(body=CLEAN.replace("none", "- check the theme"))
    run.go(MANUAL_CHECKS="defer", AUTO_MERGE="0")
    assert run.events == ["notify PR #335 review passed"] and not run.merged() and not run.captions()


def test_a_repository_can_turn_auto_merge_off_for_itself(run):
    run.go(REPO_AUTO_MERGE="0")
    assert run.events == ["notify PR #335 review passed"] and not run.merged() and run.labelled("ready-for-human")
    run.go(REPO_AUTO_MERGE="1")
    assert run.merged()


def test_a_failed_merge_goes_to_the_owner(run):
    run.gh.fail("merge")
    run.go()
    assert run.labelled("ready-for-human") and run.events[-1] == "notify PR #335 merge failed"
    assert run.wt.exists()   # nothing is cleaned up for a PR that is still open


def test_auto_merge_off_hands_over_and_sends_screenshots_only_for_manual_checks(run):
    run.go(AUTO_MERGE="0")
    assert run.events == ["notify PR #335 review passed"] and not run.captions() and not run.merged()
    run.gh.set(body=CLEAN.replace("none", "- check"))
    run.go(AUTO_MERGE="0")
    assert len(run.captions()) == 1


# --- one section under either name, and what the owner is shown (#94) ------------------------

BOTH_HEADINGS = pytest.mark.parametrize("heading", ["Manual checks", "Checks to verify"])


def two_checks(heading):
    return f"Closes #297\n\n## {heading}\n{TWO}\n"


def remainder(run):
    return Path(run.env["STATE"], f"deferred-checks-items-pr-{PR}")


@BOTH_HEADINGS
def test_verdicts_clear_or_partly_hand_off_under_either_name(run, heading):
    run.gh.set(body=two_checks(heading))
    run.go(screenshot={"checks": TWO, "comment": verdicts("1. verified", "2. owner")})
    text = run.texts["PR #335 needs a manual check"]
    assert not run.merged() and SIGNUP in text and ONE not in text and "left these checks for you" in text
    assert run.captions()[0].splitlines()[3:] == [f"- {SIGNUP}"]
    run.go(screenshot={"checks": TWO, "comment": verdicts("1. verified", "2. verified")})
    assert run.merged()


@BOTH_HEADINGS
def test_defer_writes_only_the_left_items_under_either_name(run, heading):
    run.gh.set(body=two_checks(heading))
    run.go(MANUAL_CHECKS="defer", screenshot={"checks": TWO, "comment": verdicts("1. verified", "2. owner")})
    assert run.merged() and json.loads(remainder(run).read_text()) == {"source": [ONE, SIGNUP], "left": [SIGNUP]}


@pytest.mark.parametrize("body", [
    "Closes #297\n\n## Checks to verify\n\n",
    "Closes #297\n\n## Manual checks\nnone\n\n## Checks to verify\n- a\n",
    "Closes #297\n\n## Checks to verify\n- a\n\n## Checks to verify\n- a\n",
    "Closes #297\n",
], ids=["empty", "mixed", "duplicate", "missing"])
def test_a_section_that_lists_nothing_or_is_not_well_formed_is_never_cleared_by_screenshots(run, body):
    run.gh.set(body=body)
    run.go(screenshot={"checks": "", "comment": verdicts("1. verified")})
    assert not run.merged() and run.labelled("ready-for-human")
    run.go(MANUAL_CHECKS="defer", screenshot={"checks": "", "comment": verdicts("1. verified")})
    assert run.merged() and not remainder(run).exists()


@pytest.mark.parametrize("first, second, expected", [
    ("- Open a book.", "- Open a map.", ["Open a book.", "Open a map."]),
    ("Open a book.", "Open a map.", ["Open a book.", "Open a map."]),
    ("none", "none.", []),
    ("none\n\n🤖 Generated with Test Tool", "Open a map.", ["Open a map."]),
], ids=["lists", "plain text", "both none", "none with attribution"])
def test_a_not_well_formed_body_hands_the_owner_the_checks_of_every_section(run, first, second, expected):
    run.gh.set(body=f"Closes #297\n\n## Manual checks\n{first}\n\n## Checks to verify\n{second}\n")
    run.go()
    assert not run.merged() and run.labelled("ready-for-human")
    handoff = run.texts["PR #335 needs a manual check"]
    assert ("Checks for you" in handoff) == bool(expected)
    lines = [f"- {item}" for item in expected]
    assert handoff.splitlines()[1:-1] == lines
    assert run.captions()[0].splitlines()[3:] == lines


def test_unreadable_verdicts_are_no_verdicts(run):
    run.gh.set(body=two_checks("Checks to verify"))
    incomplete = verdicts("1. verified")   # the second check has no verdict
    run.go(screenshot={"checks": TWO, "comment": incomplete})
    text = run.texts["PR #335 needs a manual check"]
    assert not run.merged() and "Checks for you" in text and ONE in text and SIGNUP in text
    assert run.captions()[0].splitlines()[3:] == [f"- {ONE}", f"- {SIGNUP}"]
    run.go(MANUAL_CHECKS="defer", screenshot={"checks": TWO, "comment": incomplete})
    assert run.merged() and not remainder(run).exists()


def test_a_failed_merge_leaves_no_remainder(run):
    run.gh.set(body=two_checks("Checks to verify"))
    run.gh.fail("merge")
    run.go(MANUAL_CHECKS="defer", screenshot={"checks": TWO, "comment": verdicts("1. verified", "2. verified")})
    assert run.labelled("ready-for-human") and not remainder(run).exists()


def tracked_check_lines(run, monkeypatch):
    monkeypatch.setenv("MANUAL_CHECKS", "defer")
    Path(run.env["STATE"], "deferred-checks-since").write_text("2026-11-14T09:00:00+00:00\n")
    run.gh.set(mergedAt="2026-11-14T10:00:00Z")
    after_merge.deferred_checks(PR, "297")
    [tracker] = run.gh.created()
    assert tracker["title"] == "Checks to try"
    return tracker["body"].split("\n\n")[-1].splitlines()


def test_a_retry_replaces_the_remainder_of_an_earlier_attempt(run, monkeypatch):
    stale = {"source": [ONE, SIGNUP, "A new check."], "left": []}
    remainder(run).write_text(json.dumps(stale))
    run.gh.set(body=two_checks("Checks to verify") + "- A new check.\n")
    run.go(MANUAL_CHECKS="defer", screenshot={"checks": TWO, "comment": verdicts("1. verified", "2. verified")})
    assert run.merged() and not remainder(run).exists()
    assert tracked_check_lines(run, monkeypatch) == [f"- [ ] {ONE}", f"- [ ] {SIGNUP}", "- [ ] A new check."]


def test_a_check_added_between_reads_is_in_both_gate_handoff_messages(run, monkeypatch):
    initial = two_checks("Checks to verify")
    edited = initial + "- Added later.\n"
    reads = iter([initial, edited])
    monkeypatch.setattr(pr_helpers, "body", lambda pr: next(reads))
    run.go(screenshot={"checks": TWO, "comment": verdicts("1. verified", "2. verified")})
    assert not run.merged() and run.labelled("ready-for-human")
    expected = [f"- {ONE}", f"- {SIGNUP}", "- Added later."]
    handoff = run.texts["PR #335 needs a manual check"]
    assert "Checks for you" in handoff and handoff.splitlines()[1:-1] == expected
    [caption] = run.captions()
    assert caption.startswith("PR #335: Checks for you\n")
    assert caption.splitlines()[3:] == expected


def test_the_tracker_gets_the_checks_added_between_merge_and_gate_reads(run, monkeypatch):
    initial = two_checks("Checks to verify")
    edited = initial + "- Added later.\n"
    run.gh.set(body=initial)
    real_body = pr_helpers.body
    reads = []

    def read_and_edit(pr):
        body = real_body(pr)
        reads.append(body)
        if len(reads) == 1:
            run.gh.set(body=edited)
        return body

    monkeypatch.setattr(pr_helpers, "body", read_and_edit)
    run.go(MANUAL_CHECKS="defer", screenshot={"checks": TWO, "comment": verdicts("1. verified", "2. verified")})
    assert reads == [initial.rstrip("\n"), edited.rstrip("\n")]
    assert run.merged()
    assert json.loads(remainder(run).read_text()) == {"source": [ONE, SIGNUP], "left": []}
    assert tracked_check_lines(run, monkeypatch) == [f"- [ ] {ONE}", f"- [ ] {SIGNUP}", "- [ ] Added later."]


@pytest.mark.parametrize("verdict, left, listed", [
    (("1. verified", "2. owner"), [SIGNUP], "left these checks for you"),
    (None, [ONE, SIGNUP], "Checks for you"),
], ids=["partial verdicts", "no verdicts"])
def test_auto_merge_off_lists_what_is_left_in_the_handoff_and_the_caption(run, verdict, left, listed):
    run.gh.set(body=two_checks("Checks to verify"))
    run.go(AUTO_MERGE="0", screenshot={"checks": TWO, "comment": verdicts(*verdict)} if verdict else None)
    text = run.texts["PR #335 review passed"]
    assert listed in text and all(item in text for item in left)
    [caption] = run.captions()
    caption_label = "checks left for you" if verdict else "Checks for you"
    assert caption.startswith(f"PR #335: {caption_label}\n")
    assert caption.splitlines()[3:] == [f"- {item}" for item in left]
    assert (ONE in text) == (ONE in left)


def test_a_description_closing_the_wrong_issue_queues_a_metadata_review(run):
    run.gh.set(body="Refs #297\nThis PR does not close #297.\n\n## Checks to verify\nnone")
    run.go()
    assert not run.merged() and run.labelled("agent-review") and not run.labelled("ready-for-human")
    assert any("merge preflight blocked" in line for line in run.log())


def test_the_gate_reads_the_live_description_after_ci(run, monkeypatch):
    run.gh.set(body=PARTIAL, closingIssuesReferences=[])
    def unsafe_after_ci(self, minutes):
        run.gh.set(closingIssuesReferences=[{"number": 297}])
        return True
    monkeypatch.setattr(merge.Merger, "ci_green", unsafe_after_ci)
    run.go()
    assert not run.merged() and run.labelled("agent-review")


# --- the task behind the PR -----------------------------------------------------------------

def test_a_merged_partial_pr_releases_its_task_once(run):
    run.gh.set(open_prs=[], body=PARTIAL)
    sh(run.wt, "git", "push", "-q", "origin", "HEAD:main")   # the merge
    sh(run.repo, "git", "fetch", "-q", "origin")
    merger = run.merger()
    assert merger.finish_merged_task()
    assert merger.finish_merged_task()   # idempotent: the marker
    edits = [c for c in run.gh.calls() if c[:2] == ["issue", "edit"]]
    assert len(edits) == 1 and "agent-wip" in edits[0] and "ready-for-agent" in edits[0] and "297" in edits[0]
    assert not run.wt.exists() and Path(run.env["STATE"], "chain-next").exists()
    assert Path(run.env["STATE"], "last-slice-297").read_text() == f"{PR}\n"


def test_a_later_slice_owning_the_task_leaves_it_alone(run):
    run.gh.set(open_prs=[{"number": 400}], body=PARTIAL)
    assert run.merger().finish_merged_task()
    assert not [c for c in run.gh.calls() if c[:2] == ["issue", "edit"]] and run.wt.exists()


def test_an_unmerged_or_dirty_worktree_is_retained_and_the_recovery_retried(run):
    run.gh.set(open_prs=[], body=PARTIAL)
    assert not run.merger().finish_merged_task()   # its commit is not on origin/main
    sh(run.wt, "git", "push", "-q", "origin", "HEAD:main")
    sh(run.repo, "git", "fetch", "-q", "origin")
    (run.wt / "dirty.txt").write_text("x")
    assert not run.merger().finish_merged_task()
    assert any("retained unmerged or dirty worktree" in line for line in run.log())
    assert not Path(run.env["STATE"], f"task-merged-pr-{PR}").exists()


def test_a_closed_issue_loses_its_agent_labels(run):
    run.gh.set(issues={"297": {"state": "CLOSED"}})
    assert run.merger().finish_merged_task()
    edits = [c for c in run.gh.calls() if c[:2] == ["issue", "edit"]]
    assert len(edits) == 1 and "--add-label" not in edits[0]
    removed = [edits[0][i + 1] for i, arg in enumerate(edits[0]) if arg == "--remove-label"]
    assert removed == ["agent-wip", "ready-for-agent"]


def test_a_pr_without_a_task_or_a_pending_close_is_left_alone(run):
    run.gh.set(body="No relation here", headRefName="owner/x")
    assert run.merger().finish_merged_task()
    run.gh.set(body="Closes #297", headRefName="cursor/issue-297")
    assert run.merger().finish_merged_task()   # complete and still open: GitHub's transition is pending
    assert not Path(run.env["STATE"], f"task-merged-pr-{PR}").exists()


def test_the_worktree_goes_with_its_branch_and_a_detached_one_reports_failure(run):
    assert merge.remove_worktree(run.env["REPO_DIR"], run.wt)
    assert not run.wt.exists() and sh(run.repo, "git", "branch", "--list", "cursor/issue-297") == ""
    detached = run.repo.parent / "detached"
    sh(run.repo, "git", "worktree", "add", "-q", "--detach", str(detached), "origin/main")
    assert not merge.remove_worktree(run.env["REPO_DIR"], detached) and not detached.exists()


# --- the CI repair round --------------------------------------------------------------------

HEAD = "a" * 40


REAL_CI_FIX_ROUND = merge.Merger.ci_fix_round


@pytest.fixture
def repair(run, monkeypatch):
    """The real round, with baton.ci's classification on the fake gh and a stub fix run."""
    monkeypatch.setattr(merge.Merger, "ci_fix_round", REAL_CI_FIX_ROUND)
    run.fixes, run.fix_rc = [], 0

    def fix_pr(runner, pr, wt, fixer, source, feedback, head=""):
        run.fixes.append({"fixer": fixer, "source": source, "feedback": feedback, "round": runner.round})
        return run.fix_rc
    monkeypatch.setattr(merge.fix, "fix_pr", fix_pr)
    monkeypatch.setattr(ci.time, "sleep", lambda s: None)
    run.gh.set(headRefOid=HEAD, checks=[{"name": "golangci-lint", "bucket": "fail", "link": "https://github.com/owner/app/actions/runs/123"}],
               logs={"123": "golangci-lint: unused helper"})
    return run


def test_a_code_check_failure_gets_one_repair_round_per_head(repair):
    assert repair.merger().ci_fix_round(str(repair.wt)) == 0
    assert "unused helper" in repair.fixes[0]["feedback"] and repair.fixes[0]["source"] == "CI code checks"
    assert repair.fixes[0]["round"] == "ci" and Path(repair.env["STATE"], f"ci-fix-{PR}-{HEAD}").exists()
    assert repair.merger().ci_fix_round(str(repair.wt)) == 1   # the same head is not repaired twice
    assert len(repair.fixes) == 1


def test_an_image_build_failure_is_not_sent_to_a_code_fixer(repair):
    repair.gh.set(checks=[{"name": "ko image build", "bucket": "fail", "link": "https://github.com/owner/app/actions/runs/124"}])
    assert repair.merger().ci_fix_round(str(repair.wt)) == 1
    assert repair.fixes == [] and not Path(repair.env["STATE"], f"ci-fix-{PR}-{HEAD}").exists()


def test_a_repair_that_cannot_run_hands_the_head_off_without_marking_it(repair):
    for vendor in ("claude", "codex", "cursor"):
        Path(repair.env["STATE"], f"{vendor}-cooldown-until").write_text("1999999999\n")
    assert repair.merger().ci_fix_round(str(repair.wt)) == 1
    assert any("CI repair deferred; handing off this head" in line for line in repair.log())
    assert not Path(repair.env["STATE"], f"ci-fix-{PR}-{HEAD}").exists()


def test_a_repair_that_hands_the_pr_over_still_marks_the_head(repair):
    repair.fix_rc = 1
    assert repair.merger().ci_fix_round(str(repair.wt)) == 1
    assert Path(repair.env["STATE"], f"ci-fix-{PR}-{HEAD}").exists()


# --- behind branches (#122) -----------------------------------------------------------------

OLD = "a" * 40
NEW = "b" * 40
NEW2 = "c" * 40


def test_a_behind_pr_updates_waits_for_ci_and_merges_with_the_new_head(run, monkeypatch):
    monkeypatch.setattr(merge.time, "sleep", lambda s: None)
    run.gh.set(mergeStateStatus="BEHIND", headRefOid=OLD, update_heads=[NEW], after_update_status="CLEAN")
    run.greens = [True, True]  # initial gate CI, then new-head CI
    run.go()
    assert run.merged() and not run.labelled("ready-for-human")
    merge_calls = [c for c in run.gh.calls() if c[:2] == ["pr", "merge"]]
    assert merge_calls and "--match-head-commit" in merge_calls[0] and NEW in merge_calls[0]
    assert any(c[:2] == ["pr", "update-branch"] for c in run.gh.calls())
    assert run.events.count("ci") == 2


def test_a_current_or_unknown_pr_merges_without_an_update(run):
    run.gh.set(mergeStateStatus="UNKNOWN", headRefOid=OLD)
    run.go()
    assert run.merged() and not any(c[:2] == ["pr", "update-branch"] for c in run.gh.calls())
    merge_calls = [c for c in run.gh.calls() if c[:2] == ["pr", "merge"]]
    assert merge_calls and "--match-head-commit" not in merge_calls[0]


def test_a_failed_merge_that_is_now_behind_updates_and_retries(run, monkeypatch):
    monkeypatch.setattr(merge.time, "sleep", lambda s: None)
    run.gh.set(mergeStateStatus="CLEAN", headRefOid=OLD, merge_results=[1, 0],
               merge_fail_status=["BEHIND"], update_heads=[NEW], after_update_status="CLEAN")
    run.greens = [True, True]
    run.go()
    assert run.merged()
    updates = [c for c in run.gh.calls() if c[:2] == ["pr", "update-branch"]]
    merges = [c for c in run.gh.calls() if c[:2] == ["pr", "merge"]]
    assert len(updates) == 1 and len(merges) == 2 and NEW in merges[1]


def test_three_behind_updates_exhaust_without_a_fourth(run, monkeypatch):
    monkeypatch.setattr(merge.time, "sleep", lambda s: None)
    run.gh.set(mergeStateStatus="BEHIND", headRefOid=OLD,
               update_heads=[NEW, NEW2, "d" * 40], after_update_status="BEHIND",
               merge_results=[1, 1, 1], merge_fail_status=["BEHIND", "BEHIND", "BEHIND"])
    run.greens = [True, True, True, True]
    run.go()
    assert run.labelled("ready-for-human") and run.gh.pr.get("state") != "MERGED"
    assert len([c for c in run.gh.calls() if c[:2] == ["pr", "update-branch"]]) == 3
    assert len([c for c in run.gh.calls() if c[:2] == ["pr", "merge"]]) == 3
    assert "notify PR #335 merge failed" in run.events


def test_a_failed_merge_that_is_not_behind_hands_off_without_updating(run):
    run.gh.set(mergeStateStatus="DIRTY", headRefOid=OLD, merge_results=[1], merge_fail_status=["DIRTY"])
    run.go()
    assert run.labelled("ready-for-human") and run.gh.pr.get("state") != "MERGED"
    assert not any(c[:2] == ["pr", "update-branch"] for c in run.gh.calls())
    assert "notify PR #335 merge failed" in run.events


def test_behind_without_a_head_sha_hands_off_without_updating(run):
    run.gh.set(mergeStateStatus="BEHIND")  # no headRefOid
    run.go()
    assert run.labelled("ready-for-human") and run.gh.pr.get("state") != "MERGED"
    assert not any(c[:2] == ["pr", "update-branch"] for c in run.gh.calls())
    assert "headRefOid is unavailable" in run.texts["PR #335 merge failed"]


def test_an_update_branch_conflict_hands_off_with_gh_diagnostic(run, monkeypatch):
    monkeypatch.setattr(merge.time, "sleep", lambda s: None)
    (run.gh.root / "update-branch-stderr").write_text("refusing to merge unrelated histories\n")
    run.gh.fail("update-branch")
    run.gh.set(mergeStateStatus="BEHIND", headRefOid=OLD)
    run.go()
    assert not run.merged() and run.labelled("ready-for-human")
    assert "refusing to merge unrelated histories" in run.texts["PR #335 merge failed"]
    assert not any(c[:2] == ["pr", "merge"] for c in run.gh.calls())


def test_an_unchanged_head_after_update_hands_off_without_ci_or_merge(run, monkeypatch):
    monkeypatch.setattr(merge.time, "sleep", lambda s: None)
    ticks = {"n": 0}

    def mono():
        ticks["n"] += 1
        return 0 if ticks["n"] < 3 else 100

    monkeypatch.setattr(merge.time, "monotonic", mono)
    run.gh.set(mergeStateStatus="BEHIND", headRefOid=OLD)  # update succeeds but head stays OLD
    run.go()
    assert not run.merged() and run.labelled("ready-for-human")
    assert any(c[:2] == ["pr", "update-branch"] for c in run.gh.calls())
    assert not any(c[:2] == ["pr", "merge"] for c in run.gh.calls())
    assert run.events.count("ci") == 1  # only the initial gate CI


def test_red_ci_after_a_confirmed_update_hands_off_without_repair_or_merge(run, monkeypatch):
    monkeypatch.setattr(merge.time, "sleep", lambda s: None)
    run.gh.set(mergeStateStatus="BEHIND", headRefOid=OLD, update_heads=[NEW], after_update_status="CLEAN")
    run.greens = [True, False]
    run.go()
    assert not run.merged() and run.labelled("ready-for-human")
    assert "notify PR #335: CI not green" in run.events
    assert "repair" not in run.events
    assert not any(c[:2] == ["pr", "merge"] for c in run.gh.calls())


def test_checks_change_during_new_head_ci_invalidates_screenshot_clearance(run, monkeypatch):
    monkeypatch.setattr(merge.time, "sleep", lambda s: None)
    run.gh.set(body=CLEAN.replace("none", TWO), mergeStateStatus="BEHIND", headRefOid=OLD,
               update_heads=[NEW], after_update_status="CLEAN")
    run.greens = [True, True]

    def after_update_edit(*args, **kwargs):
        if args[:2] == ("pr", "update-branch"):
            run.gh.set(body=CLEAN.replace("none", TWO + "\n- A new check."), headRefOid=NEW,
                       mergeStateStatus="CLEAN")
        return real_gh(*args, **kwargs)

    real_gh = pr_helpers.gh
    monkeypatch.setattr(pr_helpers, "gh", after_update_edit)
    run.go(screenshot={"checks": TWO, "comment": verdicts("1. verified", "2. verified")})
    assert not run.merged() and run.labelled("ready-for-human")
    assert "A new check." in run.texts["PR #335 needs a manual check"]


def test_metadata_preflight_failure_after_update_queues_review(run, monkeypatch):
    monkeypatch.setattr(merge.time, "sleep", lambda s: None)
    run.gh.set(mergeStateStatus="BEHIND", headRefOid=OLD, update_heads=[NEW], after_update_status="CLEAN")
    run.greens = [True, True]
    calls = {"n": 0}

    def preflight(pr):
        calls["n"] += 1
        if any(c[:2] == ["pr", "update-branch"] for c in run.gh.calls()):
            raise ValueError("closes the wrong issue")   # only the post-update recheck fails
    monkeypatch.setattr(pr_helpers, "preflight", preflight)
    run.go()
    assert calls["n"] == 2
    assert any(c[:2] == ["pr", "update-branch"] for c in run.gh.calls())
    assert not run.merged() and not any(c[:2] == ["pr", "merge"] for c in run.gh.calls())
    assert any(c[:2] == ["pr", "edit"] and "agent-review" in c for c in run.gh.calls())
    assert "notify PR #335: metadata review queued" in run.events


def test_a_failed_merge_reread_behind_is_not_read_again_before_the_update(run, monkeypatch):
    monkeypatch.setattr(merge.time, "sleep", lambda s: None)
    reads = iter([{"mergeStateStatus": "CLEAN", "headRefOid": OLD},
                  {"mergeStateStatus": "BEHIND", "headRefOid": OLD}])
    monkeypatch.setattr(merge.Merger, "pr_merge_state",
                        lambda self: next(reads, {"mergeStateStatus": "UNKNOWN", "headRefOid": NEW}))
    run.gh.set(headRefOid=OLD, merge_results=[1, 1, 1], update_heads=[NEW])
    run.greens = [True, True]
    run.go()
    assert run.gh.pr.get("state") != "MERGED" and run.labelled("ready-for-human")
    assert len([c for c in run.gh.calls() if c[:2] == ["pr", "update-branch"]]) == 1
    assert len([c for c in run.gh.calls() if c[:2] == ["pr", "merge"]]) == 2


def test_a_failed_merge_with_unavailable_status_hands_off_without_updating(run, monkeypatch):
    reads = iter([{"mergeStateStatus": "CLEAN", "headRefOid": OLD}])
    monkeypatch.setattr(merge.Merger, "pr_merge_state", lambda self: next(reads, None))
    run.gh.set(headRefOid=OLD, merge_results=[1])
    run.go()
    assert run.gh.pr.get("state") != "MERGED" and run.labelled("ready-for-human")
    assert not any(c[:2] == ["pr", "update-branch"] for c in run.gh.calls())
    assert len([c for c in run.gh.calls() if c[:2] == ["pr", "merge"]]) == 1
    assert "notify PR #335 merge failed" in run.events


# --- owner-review paths (#131) -----------------------------------------------------------------

def test_a_pr_touching_an_owner_review_path_goes_to_the_owner_after_ci(run):
    run.gh.set(files_api=[{"filename": "bin/guard.sh"}, {"filename": "README.md"}])
    run.go(OWNER_REVIEW_PATHS="bin/guard.sh baton/advisor*.py")
    assert not run.merged() and run.labelled("ready-for-human")
    assert run.events[-1] == "notify PR #335 review and CI passed" and "ci" in run.events
    assert "bin/guard.sh" in run.texts["PR #335 review and CI passed"]
    assert "README.md" not in run.texts["PR #335 review and CI passed"]


def test_a_pr_touching_no_owner_review_path_merges(run):
    run.gh.set(files_api=[{"filename": "baton/spec.py"}])
    run.go(OWNER_REVIEW_PATHS="bin/guard.sh baton/advisor*.py")
    assert run.merged()


def test_a_rename_out_of_an_owner_review_path_counts(run):
    run.gh.set(files_api=[{"filename": "baton/limits.py", "previous_filename": "baton/quota.py"}])
    run.go(OWNER_REVIEW_PATHS="baton/quota.py")
    assert not run.merged() and "baton/quota.py" in run.texts["PR #335 review and CI passed"]


def test_an_unreadable_file_list_is_not_merged(run):
    run.gh.fail("files")
    run.go(OWNER_REVIEW_PATHS="bin/guard.sh")
    assert not run.merged() and "could not be read" in run.texts["PR #335 review and CI passed"]


def test_without_owner_review_paths_the_file_list_is_not_read(run):
    run.go()
    assert run.merged()
    assert not any(c[:1] == ["api"] and c[-3].endswith("/files") for c in run.gh.calls() if len(c) > 3)
