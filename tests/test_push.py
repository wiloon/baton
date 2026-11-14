"""common.push: the one way an agent's work leaves the host, signing off commits for DCO (#44)."""
from conftest import sh

from baton.common import push


def commit(wt, name, message, author="Ann <ann@example.invalid>"):
    (wt / name).write_text(name + "\n")
    sh(wt, "git", "add", "-A")
    sh(wt, "git", "commit", "-q", "--author", author, "-m", message)
    return sh(wt, "git", "rev-parse", "HEAD")


def remote(worktree, ref=None):
    return sh(worktree.wt, "git", "rev-parse", f"origin/{ref or worktree.branch}")


def test_an_unsigned_commit_is_signed_off_for_its_author_before_the_push(worktree):
    base = remote(worktree)
    commit(worktree.wt, "c.txt", "fix: one")
    commit(worktree.wt, "d.txt", "fix: two")
    tree = sh(worktree.wt, "git", "rev-parse", "HEAD^{tree}")
    assert push(worktree.wt, "HEAD", signoff=True)
    assert remote(worktree) == sh(worktree.wt, "git", "rev-parse", "HEAD")
    assert sh(worktree.wt, "git", "rev-parse", "HEAD^{tree}") == tree
    logged = sh(worktree.wt, "git", "log", "--format=%an <%ae>|%(trailers:key=Signed-off-by,valueonly,separator=)",
                f"{base}..")
    assert [l for l in logged.splitlines() if l] == ["Ann <ann@example.invalid>|Ann <ann@example.invalid>"] * 2
    assert sh(worktree.wt, "git", "status", "--porcelain") == ""


def test_signed_commits_and_merges_are_left_unchanged(worktree):
    wt = worktree.wt
    commit(wt, "c.txt", "fix: signed\n\nSigned-off-by: Ann <ann@example.invalid>")
    sh(wt, "git", "checkout", "-q", "-b", "side", "HEAD~1")
    commit(wt, "d.txt", "side: signed\n\nSigned-off-by: Ann <ann@example.invalid>")
    sh(wt, "git", "checkout", "-q", worktree.branch)
    sh(wt, "git", "merge", "-q", "--no-ff", "-m", "Merge side", "side")
    head = sh(wt, "git", "rev-parse", "HEAD")
    assert push(wt, worktree.branch, signoff=True)
    assert remote(worktree) == head


def test_a_merge_above_an_unsigned_commit_keeps_its_shape_and_message(worktree):
    wt = worktree.wt
    base = remote(worktree)
    commit(wt, "c.txt", "fix: unsigned")
    sh(wt, "git", "checkout", "-q", "-b", "side", base)
    commit(wt, "d.txt", "side: unsigned")
    sh(wt, "git", "checkout", "-q", worktree.branch)
    sh(wt, "git", "merge", "-q", "--no-ff", "-m", "Merge side", "side")
    assert push(wt, worktree.branch, signoff=True)
    head = remote(worktree)
    assert sh(wt, "git", "log", "-1", "--format=%B", head) == "Merge side"
    assert len(sh(wt, "git", "log", "-1", "--format=%P", head).split()) == 2
    trailers = sh(wt, "git", "log", "--no-merges", "--format=%(trailers:key=Signed-off-by,valueonly)",
                  f"{base}..{head}")
    assert [l for l in trailers.splitlines() if l] == ["Ann <ann@example.invalid>"] * 2


def test_only_the_unpushed_commits_are_signed(worktree):
    pushed = remote(worktree)   # the fixture's own unsigned commit is already on origin
    commit(worktree.wt, "c.txt", "fix: new")
    assert push(worktree.wt, "HEAD", signoff=True)
    assert sh(worktree.wt, "git", "rev-parse", "HEAD~1") == pushed
    assert "Signed-off-by" not in sh(worktree.wt, "git", "log", "-1", "--format=%B", pushed)


def test_without_signoff_the_commits_are_pushed_as_they_are(worktree):
    head = commit(worktree.wt, "c.txt", "fix: unsigned")
    assert push(worktree.wt, "HEAD")
    assert remote(worktree) == head


def test_a_new_branch_is_pushed_with_its_upstream(worktree):
    sh(worktree.wt, "git", "checkout", "-q", "-b", "agent/issue-44")
    commit(worktree.wt, "c.txt", "feat: new")
    assert push(worktree.wt, "agent/issue-44", upstream=True, signoff=True)
    assert sh(worktree.wt, "git", "rev-parse", "--abbrev-ref", "@{u}") == "origin/agent/issue-44"
    assert "Signed-off-by: Ann <ann@example.invalid>" in sh(worktree.wt, "git", "log", "-1", "--format=%B")


def test_a_failed_push_reports_false(worktree):
    commit(worktree.wt, "c.txt", "fix: unsigned")
    sh(worktree.wt, "git", "remote", "set-url", "origin", "/nonexistent")
    assert not push(worktree.wt, "HEAD", signoff=True)
