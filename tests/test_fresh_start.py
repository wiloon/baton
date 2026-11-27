"""A fresh start after the owner closes an issue's PR, and publishing its replacement (#303).

Everything runs through Tick.run on the devrun fixture: a real clone, a real bare origin (which
also holds refs/pull/N/head, as GitHub does), the fake `gh` and a stubbed agent."""
from pathlib import Path
import subprocess

import pytest

from baton import common, dev, handoff, session
from conftest import sh
from test_tick import (DONE, ctx, devrun, env, implements, issue, label_event, label_history,  # noqa: F401
                       rest_pr, set_branch_history)

BRANCH, LEGACY = "agent/issue-300", "cursor/issue-300"


def origin(devrun):
    return Path(devrun.repo).parent / "origin.git"


def wt_of(devrun, branch=BRANCH):
    return Path(devrun.env["WT_ROOT"]) / ("cursor-300" if branch == LEGACY else "agent-300")


def commit(cwd, name, text, message):
    Path(cwd, name).write_text(text)
    sh(cwd, "git", "add", "-A")
    sh(cwd, "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", message)
    return sh(cwd, "git", "rev-parse", "HEAD")


def remote(devrun, branch=BRANCH):
    found = sh(origin(devrun), "git", "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}")
    return found or None


def backups(devrun):
    listed = sh(devrun.repo, "git", "for-each-ref", "--format=%(objectname)", "refs/baton/backup/issue-300/")
    return set(listed.split())


def contains(cwd, ref, sha):
    return subprocess.run(["git", "-C", str(cwd), "merge-base", "--is-ancestor", sha, ref]).returncode == 0


def closed_pr(devrun, number=146, branch=BRANCH, first_page=()):
    """Issue 300's PR NUMBER on BRANCH, closed by the owner: its head is in BRANCH's worktree, on
    origin's branch and in origin's refs/pull/NUMBER/head. Returns the closed head."""
    wt = wt_of(devrun, branch)
    sh(devrun.repo, "git", "worktree", "add", "-q", "-b", branch, str(wt), "origin/main")
    head = commit(wt, "old.txt", "old\n", "closed work")
    sh(wt, "git", "push", "-q", "origin", branch)
    sh(origin(devrun), "git", "update-ref", f"refs/pull/{number}/head", head)
    pages = [list(first_page), [rest_pr(number, branch, sha=head)]] if first_page else [[rest_pr(number, branch, sha=head)]]
    set_branch_history(devrun, branch, pages)
    set_branch_history(devrun, LEGACY if branch == BRANCH else BRANCH, [[]])
    return head


def advance_main(devrun):
    commit(devrun.repo, "main.txt", "main moved\n", "main moves on")
    sh(devrun.repo, "git", "push", "-q", "origin", "main")
    return sh(devrun.repo, "git", "rev-parse", "HEAD")


def dirty(wt):
    """Staged, unstaged and untracked changes in WT."""
    Path(wt, "staged.txt").write_text("staged\n")
    sh(wt, "git", "add", "staged.txt")
    Path(wt, "a.txt").write_text("unstaged\n")
    Path(wt, "untracked.txt").write_text("untracked\n")


def assert_dirty_saved(devrun):
    """The uncommitted work of a removed worktree is readable from a backup ref."""
    repo = devrun.repo
    saved = [sha for sha in backups(devrun)
             if sh(repo, "git", "log", "-1", "--format=%s", sha) == "baton backup: unstaged and untracked changes"]
    assert len(saved) == 1, backups(devrun)
    assert sh(repo, "git", "show", f"{saved[0]}:untracked.txt") == "untracked"
    assert sh(repo, "git", "show", f"{saved[0]}:a.txt") == "unstaged"
    assert sh(repo, "git", "show", f"{saved[0]}^:staged.txt") == "staged"
    assert sh(repo, "git", "show", f"{saved[0]}^:a.txt") == "a"


def unrelated(devrun, name="unrelated.txt"):
    """A commit outside origin/main that shares nothing with the closed PR; origin has its objects."""
    scratch = Path(devrun.env["WT_ROOT"]) / "scratch"
    sh(devrun.repo, "git", "worktree", "add", "-q", "--detach", str(scratch), "origin/main")
    sha = commit(scratch, name, "other\n", "unrelated work")
    sh(scratch, "git", "push", "-q", "origin", f"{sha}:refs/scratch/{name}")
    sh(devrun.repo, "git", "worktree", "remove", "--force", str(scratch))
    return sha


def queue(devrun, mode="released"):
    """Issue 300 reaches dev.run through Tick.run: still agent-wip (released by the tick), WIP
    already removed, or forced by hand. Returns the tick's argv."""
    devrun.gh.answers["issue view"] = {"title": "A title", "body": "A body", "comments": [], "state": "OPEN",
                                       "labels": [{"name": "ready-for-agent"}], "author": {"login": "owner"}}
    label_history(devrun, 300, [label_event(1, "owner")])
    devrun.gh.answers["pr list"] = lambda args: "" if "--jq" in args else []
    if mode == "forced":
        return ("300",)
    devrun.gh.answers["issue list"] = lambda args: (
        ([{"number": 300}] if mode == "wip" else []) if "agent-wip" in args
        else [] if "claude-found" in args else [issue(300)])
    return ()


def starts(devrun):
    """The agent records where it starts (HEAD and status), then implements."""
    seen = []

    def run(cwd, output):
        seen.append((sh(cwd, "git", "rev-parse", "HEAD"), sh(cwd, "git", "status", "--porcelain"), cwd))
        return implements()(cwd, output)
    devrun.script = [run]
    return seen


def pushed_tip(devrun, branch=BRANCH):
    sh(devrun.repo, "git", "fetch", "-q", "origin")
    return remote(devrun, branch)


@pytest.fixture
def untouched(devrun):
    """The fixture's cursor-297 worktree belongs to another issue: it must not change."""
    other = Path(devrun.env["WT_ROOT"]) / "cursor-297"
    before = sh(other, "git", "rev-parse", "HEAD")
    yield
    assert sh(other, "git", "rev-parse", "HEAD") == before and other.is_dir()


# --- Part 2: the fresh start ------------------------------------------------------------------------------------------
def test_a_worktree_on_a_closed_pr_restarts_from_main_and_the_replacement_is_published(devrun, untouched):
    head = closed_pr(devrun)
    dirty(wt_of(devrun))
    main = advance_main(devrun)
    seen = starts(devrun)

    assert devrun.make(*queue(devrun)).run() == 0, devrun.log()

    assert seen and seen[0][0] == main and seen[0][1] == ""
    assert devrun.gh.ran("pr", "create")
    tip = pushed_tip(devrun)
    assert tip == sh(wt_of(devrun), "git", "rev-parse", "HEAD") and not contains(origin(devrun), tip, head)
    assert head in backups(devrun) and f"closed PR #146" in devrun.log()
    assert_dirty_saved(devrun)
    assert not (wt_of(devrun) / "untracked.txt").exists()


def fork_page():
    """A full first page of entries that are not the issue's PRs (another repository)."""
    return [rest_pr(1000 + n, BRANCH, repo="fork/app") for n in range(100)]


def test_a_closed_head_on_the_second_history_page_still_resets_the_worktree(devrun):
    head = closed_pr(devrun, first_page=fork_page())
    main = advance_main(devrun)
    seen = starts(devrun)
    assert devrun.make(*queue(devrun)).run() == 0, devrun.log()
    assert seen[0][0] == main and head in backups(devrun)


@pytest.mark.parametrize("mode", ["wip", "released"])
@pytest.mark.parametrize("page", ["failed", "malformed"])
def test_an_unreadable_second_page_dispatches_no_agent_on_the_worktree(devrun, mode, page):
    head = closed_pr(devrun, first_page=fork_page())
    key = f"api repos/owner/app/pulls?state=all&head=owner:{BRANCH}&per_page=100"
    if page == "failed":
        set_branch_history(devrun, BRANCH, [], fail=True)
    else:
        devrun.gh.answers[key] = [fork_page(), [{"bad": "entry"}]]
    seen = starts(devrun)
    devrun.make(*queue(devrun, mode)).run()
    assert not seen and not devrun.runs
    assert sh(wt_of(devrun), "git", "rev-parse", "HEAD") == head and not backups(devrun)


@pytest.mark.parametrize("mode", ["wip", "forced"])
def test_the_fresh_start_also_runs_for_a_released_or_forced_issue(devrun, mode):
    head = closed_pr(devrun)
    main = advance_main(devrun)
    seen = starts(devrun)
    assert devrun.make(*queue(devrun, mode)).run() == 0, devrun.log()
    assert seen[0][0] == main and head in backups(devrun) and devrun.gh.ran("pr", "create")


def test_the_legacy_pair_restarts_on_the_agent_branch(devrun):
    head = closed_pr(devrun, branch=LEGACY)
    main = advance_main(devrun)
    seen = starts(devrun)
    assert devrun.make(*queue(devrun)).run() == 0, devrun.log()
    assert seen[0][0] == main and seen[0][2] == str(wt_of(devrun))
    assert not wt_of(devrun, LEGACY).exists() and head in backups(devrun)
    create, = devrun.gh.ran("pr", "create")
    assert create[create.index("--head") + 1] == BRANCH
    assert remote(devrun, LEGACY) == head   # the closed PR's own branch is not touched


def test_a_closed_pr_whose_branch_was_deleted_still_resets(devrun):
    head = closed_pr(devrun)
    sh(origin(devrun), "git", "update-ref", "-d", f"refs/heads/{BRANCH}")
    main = advance_main(devrun)
    seen = starts(devrun)
    assert devrun.make(*queue(devrun)).run() == 0, devrun.log()
    assert seen[0][0] == main and head in backups(devrun)
    assert not contains(origin(devrun), pushed_tip(devrun), head)


class Crash(Exception):
    pass


@pytest.mark.parametrize("stop", ["deferred", "crashed"])
def test_a_stopped_replacement_resumes_on_the_next_run_without_a_reset(devrun, stop):
    closed_pr(devrun)
    main = advance_main(devrun)
    seen = []

    def deferred(cwd, output):
        seen.append(sh(cwd, "git", "rev-parse", "HEAD"))
        commit(cwd, "partial.txt", "partial\n", "replacement, part one")
        if stop == "crashed":
            raise Crash("the runner died mid-run")
        return {**DONE, "rc": 75, "defer": "quota codex=blocked"}

    def resumed(cwd, output):
        seen.append(sh(cwd, "git", "rev-parse", "HEAD"))
        return implements()(cwd, output)
    devrun.script = [deferred, resumed]
    argv = queue(devrun)
    try:
        devrun.make(*argv).run()
    except Crash:
        assert stop == "crashed"
    replacement = sh(wt_of(devrun), "git", "rev-parse", "HEAD")
    saved = backups(devrun)
    devrun.make(*argv).run()
    assert seen == [main, replacement], devrun.log()
    assert backups(devrun) == saved and devrun.log().count("removed agent-300") == 1
    assert devrun.gh.ran("pr", "create")


def test_unrelated_local_work_is_resumed(devrun):
    closed_pr(devrun)
    wt = wt_of(devrun)
    sh(wt, "git", "reset", "-q", "--hard", "origin/main")
    mine = commit(wt, "mine.txt", "mine\n", "unrelated local work")
    seen = starts(devrun)
    assert devrun.make(*queue(devrun)).run() == 0, devrun.log()
    assert seen[0][0] == mine and mine not in backups(devrun)
    assert "removed agent-300" not in devrun.log()


@pytest.mark.parametrize("failure", ["lookup", "fetch", "save"])
def test_a_failed_lookup_fetch_or_save_changes_nothing_and_dispatches_no_agent(devrun, failure):
    head = closed_pr(devrun)
    dirty(wt_of(devrun))
    if failure == "lookup":
        set_branch_history(devrun, BRANCH, [], fail=True)
    elif failure == "fetch":
        sh(origin(devrun), "git", "update-ref", "-d", "refs/pull/146/head")
    else:   # a ref below the backup ref's name: the backup ref cannot be created
        sh(devrun.repo, "git", "update-ref", f"refs/baton/backup/issue-300/{head}/other", head)
    status = sh(wt_of(devrun), "git", "status", "--porcelain")
    devrun.make(*queue(devrun)).run()
    assert not devrun.runs
    assert sh(wt_of(devrun), "git", "rev-parse", "HEAD") == head
    assert sh(wt_of(devrun), "git", "status", "--porcelain") == status
    assert "fresh start stopped, nothing changed" in devrun.log()


def test_a_detached_head_on_the_closed_pr_restarts_and_saves_both_tips(devrun, untouched):
    head = closed_pr(devrun)
    wt = wt_of(devrun)
    sh(wt, "git", "switch", "-q", "--detach")
    newer = unrelated(devrun)
    sh(devrun.repo, "git", "branch", "-f", BRANCH, newer)
    dirty(wt)
    main = advance_main(devrun)
    seen = starts(devrun)
    assert devrun.make(*queue(devrun)).run() == 0, devrun.log()
    assert seen[0][0] == main
    assert {head, newer} <= backups(devrun)
    assert_dirty_saved(devrun)


def test_a_detached_head_on_other_work_resumes_and_the_branch_is_reattached(devrun, untouched):
    head = closed_pr(devrun)
    wt = wt_of(devrun)
    other = unrelated(devrun)
    sh(wt, "git", "switch", "-q", "--detach", other)     # the branch stays on the closed PR
    Path(wt, "wip.txt").write_text("in progress\n")
    seen = starts(devrun)
    assert devrun.make(*queue(devrun)).run() == 0, devrun.log()
    assert seen[0][0] == other and "wip.txt" in seen[0][1]
    assert head in backups(devrun) and "the tip of agent/issue-300" in devrun.log()
    assert "(closed PR #146)" in devrun.log()
    final = sh(wt, "git", "rev-parse", "HEAD")
    assert sh(wt, "git", "branch", "--show-current") == BRANCH
    assert pushed_tip(devrun) == final and contains(wt, final, other) and not contains(wt, final, head)


# --- Part 3: publishing the replacement -------------------------------------------------------------------------------
def test_a_remote_moved_to_an_ancestor_after_closure_is_fast_forwarded(devrun):
    closed_pr(devrun)
    moved = unrelated(devrun)
    sh(origin(devrun), "git", "update-ref", f"refs/heads/{BRANCH}", moved)
    wt = wt_of(devrun)
    sh(wt, "git", "reset", "-q", "--hard", moved)
    seen = starts(devrun)
    assert devrun.make(*queue(devrun)).run() == 0, devrun.log()
    assert seen[0][0] == moved and devrun.gh.ran("pr", "create")
    assert pushed_tip(devrun) == sh(wt, "git", "rev-parse", "HEAD")


def test_a_transient_lease_push_defers_and_the_due_retry_uses_the_saved_lease(devrun, monkeypatch):
    head = closed_pr(devrun)
    advance_main(devrun)
    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    pushes, real = [], common.push

    def flaky(wt, ref, **kw):
        pushes.append(kw.get("lease"))
        if len(pushes) == 1:
            return common.PushResult(False, transient=True, detail="connection refused")
        return real(wt, ref, **kw)
    monkeypatch.setattr(dev, "push", flaky)
    starts(devrun)
    argv = queue(devrun)
    devrun.make(*argv).run()
    saved = handoff.load_retry(session.resolve(devrun.env), "300", handoff.DEV_PUSH)
    assert saved["continuation"]["lease"] == head and remote(devrun) == head
    clock["now"] = 1_060
    devrun.make(*argv).run()
    assert pushes == [head, head], devrun.log()
    assert pushed_tip(devrun) == sh(wt_of(devrun), "git", "rev-parse", "HEAD") and devrun.gh.ran("pr", "create")


def handed_off(devrun):
    return [c for c in devrun.gh.ran("issue", "edit", "300") if "ready-for-human" in c]


def test_a_divergent_remote_is_left_alone_and_handed_off(devrun):
    closed_pr(devrun)
    divergent = unrelated(devrun, "divergent.txt")
    sh(origin(devrun), "git", "update-ref", f"refs/heads/{BRANCH}", divergent)
    advance_main(devrun)
    starts(devrun)
    devrun.make(*queue(devrun)).run()
    assert remote(devrun) == divergent and handed_off(devrun) and not devrun.gh.ran("pr", "create")


def test_a_lost_lease_is_rejected_and_handed_off(devrun, monkeypatch):
    closed_pr(devrun)
    advance_main(devrun)
    changed = unrelated(devrun, "changed.txt")
    real = common.push

    def racing(wt, ref, **kw):   # the remote moves between the read and the push
        sh(origin(devrun), "git", "update-ref", f"refs/heads/{BRANCH}", changed)
        return real(wt, ref, **kw)
    monkeypatch.setattr(dev, "push", racing)
    starts(devrun)
    devrun.make(*queue(devrun)).run()
    assert remote(devrun) == changed and handed_off(devrun) and not devrun.gh.ran("pr", "create")


def test_a_replacement_that_merges_the_closed_work_back_is_not_pushed(devrun):
    head = closed_pr(devrun)
    advance_main(devrun)

    def merges_back(cwd, output):
        result = implements()(cwd, output)
        sh(cwd, "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "merge", "-q", "--no-edit", head)
        return result
    devrun.script = [merges_back]
    devrun.make(*queue(devrun)).run()
    assert devrun.runs and remote(devrun) == head
    assert handed_off(devrun) and not devrun.gh.ran("pr", "create")
    assert "closed PR #146" in devrun.log()


def test_the_push_never_forces_without_a_lease(devrun, monkeypatch):
    calls = []
    real = session.run_process

    def spy(cmd, *a, **k):
        if "push" in cmd:
            calls.append(cmd)
        return real(cmd, *a, **k)
    monkeypatch.setattr(session, "run_process", spy)
    closed_pr(devrun)
    advance_main(devrun)
    starts(devrun)
    assert devrun.make(*queue(devrun)).run() == 0, devrun.log()
    push, = calls
    assert "--force" not in push and f"--force-with-lease=refs/heads/{BRANCH}:" in " ".join(push)
