"""baton.merge: the merge or the handoff after a passed review, the CI repair round and the
task recovery, against a real git worktree and the fake `gh` of conftest. Ported from
tests/shots.sh, tests/verify.sh and tests/test_pr_lifecycle.py."""
import os
from pathlib import Path

import pytest

from baton import ci, merge, notify, pick, pr as pr_helpers
from conftest import sh

BIN = Path(__file__).resolve().parents[1] / "bin"
PR = 335
CLEAN = "Closes #297\n\n## Manual checks\nnone\n"
PARTIAL = "Refs #297\n\n## Acceptance advanced\n- item one\n\n## Manual checks\nnone\n"


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
    state.shots = []
    real_gh, real_notify = pr_helpers.gh, notify.notify

    def traced_gh(*args, **kw):
        if args[:2] in (("pr", "merge"), ("issue", "edit")):
            state.events.append(" ".join(args[:2]) + (" agent-wip" if "agent-wip" in args else ""))
        return real_gh(*args, **kw)
    monkeypatch.setattr(pr_helpers, "gh", traced_gh)
    monkeypatch.setattr(merge.notify, "notify", lambda title, text="": (state.events.append(f"notify {title}"), real_notify(title, text)))
    monkeypatch.setattr(merge.notify, "send_shots", lambda pr: state.shots.append(pr))
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
    state.go = lambda wt=None, **overrides: merger(**overrides).merge_or_handoff(wt or str(worktree.wt))
    return state


# --- the merge ------------------------------------------------------------------------------

def test_a_green_pr_with_no_manual_checks_merges_in_the_contract_order(run):
    run.go()
    assert run.events == ["ci", "pr merge", "notify PR #335 merged", "issue edit agent-wip", "after_merge"]
    assert not run.wt.exists() and not run.shots
    assert any("cleaned up worktree" in line for line in run.log())
    assert not run.labelled("ready-for-human")


def test_the_merged_pr_loses_its_screenshots_and_its_task_is_recovered(run):
    shots = Path(run.env["CTX"], f"pr-{PR}-shots")
    shots.mkdir()
    (shots / "01.png").write_text("x")
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
    assert run.labelled("ready-for-human") and not run.merged() and run.shots == [PR]
    assert "notify PR #335 needs a manual check" in run.events


def test_a_failed_merge_goes_to_the_owner(run):
    run.gh.fail("merge")
    run.go()
    assert run.labelled("ready-for-human") and run.events[-1] == "notify PR #335 merge failed"
    assert run.wt.exists()   # nothing is cleaned up for a PR that is still open


def test_auto_merge_off_hands_over_and_sends_screenshots_only_for_manual_checks(run):
    run.go(AUTO_MERGE="0")
    assert run.events == ["notify PR #335 review passed"] and run.shots == [] and not run.merged()
    run.gh.set(body=CLEAN.replace("none", "- check"))
    run.go(AUTO_MERGE="0")
    assert run.shots == [PR]


def test_a_description_closing_the_wrong_issue_queues_a_metadata_review(run):
    run.gh.set(body="Refs #297\nThis PR does not close #297.\n\n## Manual checks\nnone")
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
