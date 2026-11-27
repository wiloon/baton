"""A fresh start after the owner closes an issue's PR (#303, parts 2 and 3 of #155).

Baton never closes a PR, so a closed, unmerged PR of an issue is the owner rejecting that work.
Before a dev run resumes a worktree, an issue worktree whose HEAD is built on such a PR (it shares
a commit outside origin/main with the PR's head) is saved and removed, and the attempt starts
again from origin/main. Saved work lives under the local-only refs
`refs/baton/backup/issue-N/<sha>`; nothing here deletes a branch. At push time the replacement
may replace exactly the closed head on origin, through a lease (`common.push`).
"""
from pathlib import Path
import re
import subprocess

from . import names
from .common import child_env, git

SHA = re.compile(r"[0-9a-f]{40}")
# The author of backup commits: the runner host may have no git identity configured.
IDENTITY = {"GIT_AUTHOR_NAME": "baton", "GIT_AUTHOR_EMAIL": "baton@localhost",
            "GIT_COMMITTER_NAME": "baton", "GIT_COMMITTER_EMAIL": "baton@localhost"}


class Failed(Exception):
    """A lookup, fetch, save or attach failed."""


def backup_ref(issue, sha):
    return f"refs/baton/backup/issue-{issue}/{sha}"


def closed_heads(tick, issue):
    """{head SHA: PR number} of the issue's closed, unmerged PRs (same repository, base main, an
    agent branch of the issue), from every page of their history. Each head is fetched from
    refs/pull/<pr>/head, because its branch may be gone, and checked against the PR's head SHA.
    Raises Failed: a partial history is never used."""
    history = tick.pr_history(issue)
    if history is None:
        raise Failed("PR history unreadable")
    heads = {}
    for item in history:
        if (item.get("state") or "").lower() != "closed" or item.get("merged_at"):
            continue
        number, sha = item["number"], (item.get("head") or {}).get("sha")
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise Failed(f"closed PR #{number} has no head SHA")
        if (git(tick.repo_dir, "fetch", "-q", "origin", f"refs/pull/{number}/head") is None
                or git(tick.repo_dir, "rev-parse", "FETCH_HEAD") != sha):
            raise Failed(f"could not fetch the head of closed PR #{number}")
        heads[sha] = number
    return heads


def outside_main(cwd, rev):
    """The commits of REV that origin/main does not have."""
    listed = git(cwd, "rev-list", rev, "--not", "origin/main")
    if listed is None:
        raise Failed(f"cannot list the commits of {rev}")
    return set(listed.split())


def built_on(cwd, commit, heads):
    """The number of a closed PR whose head shares a commit outside origin/main with COMMIT, or
    None. A copy of the closed work behind or ahead of its head counts too."""
    mine = outside_main(cwd, commit)
    if not mine:
        return None
    for sha, number in heads.items():
        if mine & outside_main(cwd, sha):
            return number
    return None


def save(tick, issue, sha, what, pr=None):
    """Keep SHA under the issue's backup namespace and log it."""
    ref = backup_ref(issue, sha)
    if git(tick.repo_dir, "update-ref", ref, sha) is None:
        raise Failed(f"could not save {what}")
    tick.log(f"#{issue}: saved {what} {sha[:12]}{f' (closed PR #{pr})' if pr else ''} as {ref}")


def _git(cwd, *args, **env):
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          env=child_env(**IDENTITY, **env))
    if done.returncode != 0:
        raise Failed(f"git {args[0]} failed in {cwd}: {done.stderr.strip()}")
    return done.stdout.strip()


def save_uncommitted(tick, issue, wt, head):
    """Save WT's uncommitted work as two commits on HEAD: the staged changes, and on top of them
    the unstaged and untracked (not ignored) files."""
    status = git(wt, "status", "--porcelain")
    if status is None:
        raise Failed(f"cannot read the status of {Path(wt).name}")
    if not status:
        return
    staged = _git(wt, "write-tree")
    index = Path(wt, _git(wt, "rev-parse", "--git-path", "index"))
    scratch = index.with_name(f"{index.name}.baton-backup")
    try:
        scratch.write_bytes(index.read_bytes())
        _git(wt, "add", "-A", GIT_INDEX_FILE=str(scratch))
        full = _git(wt, "write-tree", GIT_INDEX_FILE=str(scratch))
    except OSError as exc:
        raise Failed(f"cannot copy the index of {wt}: {exc}") from exc
    finally:
        scratch.unlink(missing_ok=True)
    first = _git(wt, "commit-tree", staged, "-p", head, "-m", "baton backup: staged changes")
    second = _git(wt, "commit-tree", full, "-p", first, "-m", "baton backup: unstaged and untracked changes")
    save(tick, issue, second, f"uncommitted changes of {Path(wt).name} (staged in its parent)")


def save_tip(tick, issue, branch, heads=None):
    """Before BRANCH moves, keep its tip when it holds commits outside origin/main."""
    tip = git(tick.repo_dir, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    if not tip:
        return   # no such branch
    if outside_main(tick.repo_dir, tip):
        save(tick, issue, tip, f"the tip of {branch}", built_on(tick.repo_dir, tip, heads) if heads else None)


def holder(repo_dir, branch):
    """The worktree that has BRANCH checked out, or None."""
    listed = git(repo_dir, "worktree", "list", "--porcelain")
    if listed is None:
        raise Failed("cannot list worktrees")
    path = None
    for line in listed.splitlines():
        if line.startswith("worktree "):
            path = line.removeprefix("worktree ")
        elif line == f"branch refs/heads/{branch}":
            return Path(path).resolve()
    return None


def fresh_start(tick, issue):
    """Reset the issue's worktrees built on a closed PR. Returns the (branch, worktree) to use, or
    None after logging why nothing (or nothing more) was changed.

    Only `agent-N` and `cursor-N` are looked at, each by its own HEAD, detached or not: one built
    on a closed PR is saved (HEAD and uncommitted work) and removed, every other one is resumed.
    Without an active worktree afterwards, the branch `setup_worktree` moves is saved first when it
    holds work. Everything is saved before the first removal."""
    root = Path(tick.wt_root)
    scope = (root / f"{names.WORKTREE}{issue}", root / f"{names.LEGACY_WORKTREE}{issue}")
    reset = {}
    try:
        heads = {}
        for wt in scope:
            if wt.is_dir():
                head = git(wt, "rev-parse", "HEAD")
                if not head or not SHA.fullmatch(head):
                    raise Failed(f"cannot read the HEAD of {wt.name}")
                heads[wt] = head
        closed = None
        if any(outside_main(tick.repo_dir, head) for head in heads.values()):
            closed = closed_heads(tick, issue)
        for wt, head in heads.items():
            pr = built_on(tick.repo_dir, head, closed) if closed else None
            if pr:
                reset[wt] = (head, pr)
        legacy = scope[1]
        if legacy in heads and legacy not in reset:
            branch, wt = f"{names.LEGACY_BRANCH}{issue}", legacy
        else:
            branch, wt = f"{names.BRANCH}{issue}", scope[0]
        creates = wt not in heads or wt in reset
        if creates:
            held = holder(tick.repo_dir, branch)
            if held and held not in {path.resolve() for path in reset}:
                raise Failed(f"{branch} is checked out in {held}")
        for path, (head, pr) in reset.items():
            save(tick, issue, head, f"the HEAD of {path.name}", pr)
            save_uncommitted(tick, issue, path, head)
        if creates:
            save_tip(tick, issue, branch, closed)
    except Failed as exc:
        tick.log(f"#{issue}: fresh start stopped, nothing changed: {exc}; retrying next tick")
        return None
    for path, (_, pr) in reset.items():
        if git(tick.repo_dir, "worktree", "remove", "--force", str(path)) is None:
            tick.log(f"#{issue}: could not remove {path.name} (built on closed PR #{pr}); retrying next tick")
            return None
        tick.log(f"#{issue}: removed {path.name}, built on closed PR #{pr}; starting again from origin/main")
    return names.for_issue(tick.wt_root, issue)


def attach(tick, issue, wt, branch, heads=None):
    """Put WT on BRANCH at its HEAD, saving the branch's old tip first (HEADS names the closed PR
    it matches in the log). Raises Failed."""
    if git(wt, "branch", "--show-current") == branch:
        return
    save_tip(tick, issue, branch, heads)
    if git(wt, "switch", "-q", "-C", branch) is None:
        raise Failed(f"could not attach {branch} at the HEAD of {Path(wt).name}")
    tick.log(f"#{issue}: attached {branch} at the HEAD of {Path(wt).name}")


def lease(wt, remote, commit, heads):
    """The SHA the push may replace on origin: REMOTE when it is exactly a closed PR's head and not
    an ancestor of COMMIT; '' for a normal push (absent, a fast-forward, or divergent and so
    rejected)."""
    if not remote or git(wt, "merge-base", "--is-ancestor", remote, commit) is not None:
        return ""
    return remote if remote in heads else ""
