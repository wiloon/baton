"""A pending accept whose ADR PR was merged at a head other than the accepted one (#376): an owner
merge is reviewed again on main's text, anyone else's is handed to the owner."""
import json
import os
import re
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from conftest import sh
from test_adr_merge_replay import failing_merges
from test_adr_update import record_handoffs
from test_spec import ACCEPT, ACCEPTED, CHANGES, N, PENDING, _spec_clock, merges, reviser, wall_clock
from test_spec import loop as loop  # noqa: PLC0414

from baton import handoff, pick, session, spec
from baton.common import Deferred

ADR = "docs/adr/0001-x.md"
BRANCH = f"docs/spec-issue-{N}"
HEAD_MOVED = "spec_adr_head_moved"
BASE_TEXT = "# ADR 1\n\nstatus: proposed\nscope: all\n\nl1\nl2\nl3\nl4\nl5\n\ndecision: none\n"
PR_TEXT = BASE_TEXT.replace("decision: none", "decision: PR decision")
MAIN_TEXT = BASE_TEXT.replace("status: proposed", "status: OTHER ISSUE status").replace(
    "decision: none", "decision: OTHER ISSUE decision")
RESOLVED = MAIN_TEXT.replace("decision: OTHER", "decision: PR decision\ndecision: OTHER")


def verdict(round=1):
    return f"<!-- baton-verdict: accept by=codex -->\n## Spec review (codex), round {round}: accept\n\nall good"


def history(loop, tmp_path, *, how="merge", survives=False):
    """The enx#1235 shape, made in the owner's own clone so the runner has to fetch it: the PR is accepted
    at A, main gains another issue's edit of the same ADR file, and the owner resolves into B and merges M."""
    origin, owner = tmp_path / "origin.git", tmp_path / "owner"
    sh(tmp_path, "git", "clone", "-q", str(origin), str(owner))
    sh(origin, "git", "config", "uploadpack.allowAnySHA1InWant", "true")
    for key, value in (("user.name", "o"), ("user.email", "o@example.invalid")):
        sh(owner, "git", "config", key, value)

    def commit(text, message):
        (owner / ADR).parent.mkdir(parents=True, exist_ok=True)
        (owner / ADR).write_text(text)
        sh(owner, "git", "add", "-A")
        sh(owner, "git", "commit", "-q", "-m", message)
        return sh(owner, "git", "rev-parse", "HEAD")

    def conflicting(*command):
        assert subprocess.run(["git", *command], cwd=owner, capture_output=True).returncode != 0
        (owner / ADR).write_text(RESOLVED)
        sh(owner, "git", "add", "-A")

    commit(BASE_TEXT, "adr")
    sh(owner, "git", "push", "-q", "origin", "main")
    sh(owner, "git", "checkout", "-q", "-b", BRANCH)
    accepted = commit(PR_TEXT, "decide")
    sh(owner, "git", "push", "-q", "origin", BRANCH, f"{accepted}:refs/pull/77/head")
    sh(owner, "git", "checkout", "-q", "main")
    commit(MAIN_TEXT, "other issue")
    sh(owner, "git", "push", "-q", "origin", "main")
    sh(owner, "git", "checkout", "-q", BRANCH)
    if how == "merge":
        conflicting("merge", "main")
        sh(owner, "git", "commit", "-q", "--no-edit")
    else:
        conflicting("rebase", "main")
        sh(owner, "env", "GIT_EDITOR=true", "git", "rebase", "--continue")
    merged = sh(owner, "git", "rev-parse", "HEAD")
    sh(owner, "git", "push", "-q", "-f", "origin", BRANCH, f"{merged}:refs/pull/77/head")
    sh(owner, "git", "checkout", "-q", "main")
    sh(owner, "git", "merge", "-q", "--no-ff", "-m", "merge PR 77", BRANCH)
    commit_m = sh(owner, "git", "rev-parse", "HEAD")
    sh(owner, "git", "push", "-q", "origin", "main")
    if not survives:
        sh(owner, "git", "push", "-q", "origin", f":{BRANCH}")
    return SimpleNamespace(a=accepted, b=merged, m=commit_m, origin=origin, owner=owner)


@pytest.fixture
def moved(loop, tmp_path, monkeypatch):
    """The state after the owner merged: a pending accept for A, PR 77 MERGED at B."""
    notes = []
    monkeypatch.setattr(spec.notify, "notify", lambda title, text="": notes.append(text))

    def make(round=1, **options):
        made = history(loop, tmp_path, **options)
        made.verdict, made.notes = verdict(round), notes
        loop.gh.comments = [{"author": {"login": "owner"}, "body": f"{PENDING} head={made.a} -->\n{made.verdict}"}]
        merge_by(loop, made)
        return made
    return make


def merge_by(loop, made, who="owner"):
    gh = loop.gh
    gh.adr_pr, gh.pr_state, gh.head_oid = "", "MERGED", made.b
    gh.merged_by, gh.merge_commit, gh.pr_files = who, made.m, [ADR]


def assert_reviewed_and_accepted(loop, made, round=2):
    run, = loop.runs
    assert run["tag"] == f"issue-{N}-spec-{round}" and run["role"] == "spec_review"
    published, = loop.gh.posted(ACCEPTED)
    assert f"round {round}: accept" in published and made.b in published
    assert spec.accept_pending(loop.env, N) is None
    assert spec.verdict_plan(loop.env, N) == published
    assert "ready-for-agent" in loop.gh.labels and "ready-for-human" not in loop.gh.labels


def assert_prompt(prompt, made):
    assert made.a in prompt and made.b in prompt and "PR #77" in prompt
    assert "No ADR has been revised yet." not in prompt
    assert f"git diff {made.m}^1...{made.b} -- {ADR}" in prompt
    assert f"git diff {made.m}^1...{made.a} -- {ADR}" in prompt
    added = [line for line in prompt.splitlines() if line.startswith("+")]
    assert "+decision: PR decision" in added
    assert not [line for line in added if "OTHER ISSUE" in line]


def pending_kept(loop):
    return loop.gh.posted(PENDING) and spec.accept_pending(loop.env, N) is not None


@pytest.mark.parametrize("how", ["merge", "rebase"])
def test_enx_1235_an_adr_pr_the_owner_merged_at_a_new_head_is_reviewed_again(loop, moved, monkeypatch, how):
    made = moved(how=how)
    handoffs = record_handoffs(monkeypatch)
    loop.script = [ACCEPT]
    assert loop.run() is True
    assert handoffs == [] and merges(loop) == []
    assert_prompt(loop.runs[0]["prompt"], made)
    assert_reviewed_and_accepted(loop, made)


def test_an_edited_body_and_new_owner_feedback_do_not_stop_the_new_review(loop, moved, monkeypatch):
    made = moved()
    handoffs = record_handoffs(monkeypatch)
    clock = _spec_clock(monkeypatch)
    # The saved acceptance step was bound to the body as it was; the owner edited it since.
    handoff.prepare_retry(session.resolve(loop.env), N, "spec_pending_adr_read_failed", N,
                          {"step": "acceptance", "pr": "77", "verdict": made.verdict,
                           "issue_body": "the old body", "adr_head": made.a})
    loop.gh.comments.append({"author": {"login": "owner"}, "body": "one more thing"})
    loop.script = [ACCEPT]
    clock["now"] = 1_060
    assert loop.run() is True
    assert handoffs.count("spec_pending_adr_read_failed") <= 1 and HEAD_MOVED not in handoffs
    assert_reviewed_and_accepted(loop, made)


def test_a_saved_adr_merge_replay_reviews_the_owners_merge(loop, tmp_path, monkeypatch):
    clock = _spec_clock(monkeypatch)
    made = history(loop, tmp_path)
    made.verdict = verdict()
    loop.gh.comments = [{"author": {"login": "owner"}, "body": f"{PENDING} head={made.a} -->\n{made.verdict}"}]
    loop.gh.adr_pr, loop.gh.head_oid = "77", made.a
    failing_merges(loop)
    with pytest.raises(Deferred, match="spec_adr_merge_failed"):
        loop.run()
    before = len(merges(loop))
    merge_by(loop, made)
    loop.script = [ACCEPT]
    clock["now"] = 1_060
    assert loop.run() is True
    assert len(merges(loop)) == before
    assert not list(Path(loop.env["STATE"]).glob("handoff-*-spec_adr_merge_failed.json"))
    assert_prompt(loop.runs[0]["prompt"], made)
    assert_reviewed_and_accepted(loop, made)


@pytest.mark.parametrize("record", ["verified", "expired", "damaged"])
def test_a_saved_branch_update_reviews_the_owners_merge(loop, moved, monkeypatch, record):
    wall_clock(monkeypatch)
    made = moved()
    handoffs = record_handoffs(monkeypatch)
    update = Path(loop.env["STATE"], "adr-update-pr-77.json")
    update.write_text({
        "verified": json.dumps({"accepted": made.a, "head": "c" * 40, "count": 1}),   # B is not this head
        "expired": json.dumps({"accepted": made.a, "head": made.a, "base": "main", "count": 0,
                               "deadline": 999_000, "response": None}),
        "damaged": "{",
    }[record])
    loop.script = [ACCEPT]
    assert loop.run() is True
    assert handoffs == [] and merges(loop) == [] and not update.exists()
    assert_reviewed_and_accepted(loop, made)


def test_a_merge_at_the_runners_own_verified_update_publishes_without_a_review(loop, moved, monkeypatch):
    wall_clock(monkeypatch)
    made = moved()
    Path(loop.env["STATE"], "adr-update-pr-77.json").write_text(
        json.dumps({"accepted": made.a, "head": made.b, "count": 1}))
    assert loop.run() is True
    assert loop.runs == [] and loop.gh.posted(ACCEPTED) == [made.verdict]


@pytest.mark.parametrize("survives", [False, True])
def test_changes_in_the_last_round_go_to_the_reviser_and_a_new_pr_from_main(loop, moved, survives):
    made = moved(round=3, survives=survives)
    seen = {}
    revise = reviser(commit=True, outcome="SPEC: human")

    def revising(vendor, output, cwd):
        loop.gh.head_oid = None   # the new PR's head is the worktree's
        seen["state"] = spec.spec_state(loop.env, N)
        seen["pending"] = spec.accept_pending(loop.env, N)
        seen["head"] = spec.git(cwd, "rev-parse", "HEAD")
        return revise(vendor, output, cwd)

    loop.script = [CHANGES, revising]
    assert loop.run() is False   # the reviser asks the owner
    assert [run["role"] for run in loop.runs] == ["spec_review", "spec"]
    assert seen == {"state": ("review", 4), "pending": None, "head": made.m}
    assert spec.accept_pending(loop.env, N) is None and merges(loop) == []
    create, = [c for c in loop.gh.calls if c[:2] == ["pr", "create"]]
    assert create[create.index("--base") + 1] == "main"
    pushed = sh(made.origin, "git", "rev-parse", f"refs/heads/{BRANCH}")
    assert sh(made.origin, "git", "rev-parse", f"{pushed}^") == made.m


def test_a_reject_keeps_the_pending_accept_so_the_review_runs_again(loop, moved, monkeypatch):
    made = moved()
    handoffs = record_handoffs(monkeypatch)
    loop.script = [{**ACCEPT, "outcome": "SPEC: reject"}, ACCEPT]
    assert loop.run() is False
    assert handoffs == ["spec_rejected"] and pending_kept(loop)
    assert loop.run() is True
    assert [run["tag"] for run in loop.runs] == [f"issue-{N}-spec-2"] * 2
    assert made.b in loop.gh.posted(ACCEPTED)[0]


def test_a_deferred_reviewer_pick_leaves_the_review_to_the_next_run(loop, moved, monkeypatch):
    made = moved()
    # The accepting run's paused worktree, still holding its own commit.
    wt = Path(loop.env["WT_ROOT"], f"spec-{N}")
    sh(loop.repo, "git", "worktree", "add", "-q", "-B", BRANCH, str(wt), "origin/main")
    (wt / "stale.md").write_text("stale\n")
    sh(wt, "git", "add", "-A")
    sh(wt, "git", "commit", "-q", "-m", "stale")
    loop.path("spec-paused").write_text(f"{N}\n")
    handoffs = record_handoffs(monkeypatch)
    choose = pick.choose
    monkeypatch.setattr(pick, "choose", lambda *args, **kwargs: (_ for _ in ()).throw(Deferred("claude", "quota")))
    with pytest.raises(Deferred):
        loop.run()
    assert loop.runs == [] and handoffs == [] and pending_kept(loop)
    monkeypatch.setattr(pick, "choose", choose)

    def accepting(vendor, output, cwd):
        assert spec.git(cwd, "rev-parse", "HEAD") == made.m
        assert Path(cwd, ADR).read_text() == RESOLVED
        return ACCEPT

    loop.script = [accepting]
    assert loop.run() is True
    assert_reviewed_and_accepted(loop, made)


@pytest.mark.parametrize("who", ["someone", None])
def test_a_merge_by_anyone_else_is_handed_to_the_owner(loop, moved, monkeypatch, who):
    made = moved()
    loop.gh.merged_by = who
    handoffs = record_handoffs(monkeypatch)
    assert loop.run() is False
    assert handoffs == [HEAD_MOVED] and loop.runs == [] and merges(loop) == []
    assert pending_kept(loop) and not loop.gh.posted(ACCEPTED) and "ready-for-human" in loop.gh.labels
    text = made.notes[-1]
    assert made.a in text and made.b in text and (who or "unknown") in text


def test_the_owner_login_is_matched_without_case(loop, moved):
    made = moved()
    loop.gh.merged_by = "Owner"
    loop.script = [ACCEPT]
    assert loop.run() is True
    assert_reviewed_and_accepted(loop, made)


@pytest.mark.parametrize("broken,why", [
    ("no merge commit", "merge commit"), ("unknown merge commit", "merge commit"),
    ("no files", "files"), ("no accepted head", "accepted head"),
])
def test_an_unreadable_merged_change_is_handed_off_without_a_review(loop, moved, monkeypatch, broken, why):
    made = moved()
    if broken == "no merge commit":
        loop.gh.merge_commit = None
    elif broken == "unknown merge commit":
        loop.gh.merge_commit = "d" * 40
    elif broken == "no files":
        loop.gh.pr_files = []
    else:
        loop.gh.comments[0]["body"] = f"{PENDING} -->\n{made.verdict}"
    handoffs = record_handoffs(monkeypatch)
    assert loop.run() is False
    assert handoffs == [HEAD_MOVED] and loop.runs == [] and pending_kept(loop)
    assert why in made.notes[-1]


def test_a_surviving_branch_that_moved_past_the_merge_is_kept_and_handed_off(loop, moved, monkeypatch):
    made = moved(survives=True)
    sh(made.owner, "git", "checkout", "-q", BRANCH)
    sh(made.owner, "git", "commit", "-q", "--allow-empty", "-m", "later")
    sh(made.owner, "git", "push", "-q", "origin", BRANCH)
    later = sh(made.owner, "git", "rev-parse", "HEAD")
    handoffs = record_handoffs(monkeypatch)
    assert loop.run() is False
    assert handoffs == [HEAD_MOVED] and loop.runs == [] and pending_kept(loop)
    assert sh(made.origin, "git", "rev-parse", f"refs/heads/{BRANCH}") == later


def test_a_transient_read_of_the_merged_change_uses_the_retry_budget(loop, moved, monkeypatch):
    clock = _spec_clock(monkeypatch)
    made = moved()
    handoffs = record_handoffs(monkeypatch)
    url = sh(loop.repo, "git", "remote", "get-url", "origin")
    sh(loop.repo, "git", "remote", "set-url", "origin", "http://127.0.0.1:1/gone.git")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    with pytest.raises(Deferred, match="spec_pending_adr_read_failed"):
        loop.run()
    assert loop.runs == [] and "ready-for-human" not in loop.gh.labels
    sh(loop.repo, "git", "remote", "set-url", "origin", url)
    loop.script = [ACCEPT]
    clock["now"] = 1_060
    assert loop.run() is True
    assert set(handoffs) == {"spec_pending_adr_read_failed"}
    assert_reviewed_and_accepted(loop, made)


def test_a_very_long_diff_is_cut_and_says_how_to_read_it(loop, moved, monkeypatch):
    made = moved()
    monkeypatch.setattr(spec, "MERGED_DIFF_CAP", 40)
    loop.script = [ACCEPT]
    assert loop.run() is True
    prompt = loop.runs[0]["prompt"]
    assert re.search(r"cut at 40 characters", prompt) and f"git diff {made.m}^1...{made.b} -- {ADR}" in prompt
    assert "+decision: PR decision" not in prompt.splitlines()


def test_the_merge_failed_handoff_says_a_merge_by_the_owner_is_enough(loop, monkeypatch):
    wall_clock(monkeypatch)
    notes = []
    monkeypatch.setattr(spec.notify, "notify", lambda title, text="": notes.append(text))
    loop.gh.comments = [{"author": {"login": "owner"}, "body": f"{PENDING} head={'a' * 40} -->\n{verdict()}"}]
    loop.gh.adr_pr, loop.gh.merge_results, loop.gh.merge_fail_status = "77", [1], ["DIRTY"]
    assert loop.run() is False
    assert any("merge the ADR PR, then remove ready-for-human: that is enough, baton reviews the merged "
               "text and continues" in text for text in notes), notes


@pytest.mark.parametrize("later,author,superseded", [
    ("<!-- baton-spec-review round=2 by=codex -->\n## Spec review (codex), round 2", "owner", True),
    ("<!-- baton-spec-review round=1 by=codex -->\n## Spec review (codex), round 1", "owner", False),
    ("<!-- baton-spec-review round=2 by=codex -->\n## Spec review (codex), round 2", "stranger", False),
    (verdict(2), "owner", True),
    (verdict(2), "cursor", True),
    (verdict(2), "stranger", False),
    (verdict(1).replace("all good", "other words"), "owner", False),
])
def test_a_later_round_supersedes_a_pending_accept(later, author, superseded):
    listed = [{"author": {"login": "owner"}, "body": f"{PENDING} -->\n{verdict(1)}"},
              {"author": {"login": author}, "body": later}]
    assert (spec.pending_accept_in(listed, "owner") is None) is superseded
