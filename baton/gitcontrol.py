"""What git runs on the runner's behalf: a repository's config and hooks.

The runner runs git in a worktree after an agent worked there (status, commit checks, push),
outside the agent's sandbox. A hook, or a config key that names a command (core.hooksPath,
core.fsmonitor, a filter driver), would then run with the runner's own access. So no agent run may
change them: `snapshot` records them before a run and `restore` puts back whatever changed.

Covered: the common directory's config, config.worktree, info/attributes and every file under
hooks/, and the worktree's own config.worktree. Not covered: a core.hooksPath that already points
into the checkout, where the hooks are the repository's tracked content.
"""
import os
from pathlib import Path
import shutil

from .common import git


def _roots(cwd):
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    own = git(cwd, "rev-parse", "--absolute-git-dir")
    return (Path(common), Path(own)) if common and own else None


def _watched(common, own):
    fixed = {common / "config", common / "config.worktree", common / "info" / "attributes", own / "config.worktree"}
    hooks = common / "hooks"
    found = {p for p in hooks.rglob("*") if p.is_symlink() or p.is_file()} if hooks.is_dir() else set()
    return fixed | found


def _state(path):
    """A file's link target or its bytes and mode; None when there is none."""
    try:
        if path.is_symlink():
            return ("link", os.readlink(path))
        if path.is_file():
            return ("file", path.read_bytes(), path.stat().st_mode & 0o777)
    except OSError:
        return ("unreadable",)
    return None


def snapshot(cwd):
    """The state to restore later; None when CWD is not in a git repository."""
    roots = _roots(cwd)
    if roots is None:
        return None
    return {"roots": roots, "files": {path: _state(path) for path in _watched(*roots)}}


def _put_back(path, state):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    if state is None or state[0] == "unreadable":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if state[0] == "link":
        os.symlink(state[1], path)
    else:
        path.write_bytes(state[1])
        path.chmod(state[2])


def restore(saved):
    """Put back every watched file that changed since SAVED; return their names, sorted."""
    if saved is None:
        return []
    common, own = saved["roots"]
    changed = []
    for path in sorted(set(saved["files"]) | _watched(common, own)):
        before = saved["files"].get(path)
        if _state(path) == before:
            continue
        try:
            _put_back(path, before)
        except OSError:
            pass   # still reported: the caller fails the run
        base = common if path.is_relative_to(common) else own
        changed.append(str(path.relative_to(base)))
    return changed
