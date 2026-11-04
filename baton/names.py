"""Names of an issue's agent branch and worktree, whichever vendor implements it.

New work uses `agent/issue-N` in `<wt_root>/agent-N`. The `cursor/issue-N` and `cursor-N`
names predate Claude and Codex in the pool; they are still read, and an issue whose legacy
worktree is still on disk keeps both legacy names until cleanup removes it."""
import re
from pathlib import Path

BRANCH = "agent/issue-"
LEGACY_BRANCH = "cursor/issue-"
WORKTREE = "agent-"
LEGACY_WORKTREE = "cursor-"
BRANCH_RE = re.compile(r"(?:agent|cursor)/issue-(\d+)")


def issue_of(branch):
    """The issue number of an agent branch, or None for any other branch."""
    match = BRANCH_RE.fullmatch(branch or "")
    return match[1] if match else None


def is_agent_prefix(branch):
    return (branch or "").startswith(("agent/", "cursor/"))


def worktree_for(root, branch):
    """The worktree that holds an agent branch."""
    issue = issue_of(branch)
    prefix = LEGACY_WORKTREE if branch.startswith(LEGACY_BRANCH) else WORKTREE
    return Path(root) / f"{prefix}{issue}"


def for_issue(root, issue):
    """(branch, worktree) for an issue: the legacy pair while its legacy worktree exists."""
    legacy = Path(root) / f"{LEGACY_WORKTREE}{issue}"
    if legacy.is_dir():
        return f"{LEGACY_BRANCH}{issue}", legacy
    return f"{BRANCH}{issue}", Path(root) / f"{WORKTREE}{issue}"


def branches(issue):
    """Every agent branch name an issue's PR may be on."""
    return (f"{BRANCH}{issue}", f"{LEGACY_BRANCH}{issue}")


def worktrees(root):
    """(issue, path) of every agent worktree on disk, either naming."""
    found = []
    for prefix in (WORKTREE, LEGACY_WORKTREE):
        for path in sorted(Path(root).glob(f"{prefix}*/")):
            issue = path.name.removeprefix(prefix)
            if issue.isdecimal():
                found.append((issue, path))
    return found
