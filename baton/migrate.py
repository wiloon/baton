"""One-off move of a single-repository runner's files into one repository's directories (#327).

    python -m baton.migrate OWNER/NAME --clone OLD_CLONE

Before #327 the runner watched one repository: its checkpoints and PR records sat in the state
directory beside the machine's quota and cooldowns, its worktrees in wt_root, its ctx and run logs
at the top of theirs, and its clone wherever the deploy put it. This moves all of that into
OWNER/NAME's RepoDirs, under the runner's lock:

  state/<per-repo file>   -> state/repos/OWNER/NAME/   (saved worktree and ctx paths rewritten)
  wt_root/<worktree>      -> wt_root/OWNER/NAME/       (then `git worktree repair`)
  ctx/<file>, logs/<file> -> ctx/OWNER/NAME/, logs/OWNER/NAME/
  OLD_CLONE               -> clones/OWNER/NAME
  logs/runner.log         its lines get the [OWNER/NAME] label the runner now writes

What stays global is GLOBAL_STATE. Running it again finds nothing to move. The runner refuses to
start while `pending` finds single-repository files, so nothing is read from the old places.
"""
import argparse
import fcntl
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .config import ConfigError, load, repo_dirs

# The machine's, not a repository's: the lock and the tick's turn, vendor cooldowns and run counts,
# quota and credential snapshots, telemetry/retrospectives, vendor leases, and old deploy backups.
GLOBAL_STATE = re.compile(r"lock|last-repo|chain-next|telemetry|retrospectives|vendor-leases|quota-locks|quota-probes|deploy-backups|repos|"
                          r"(?:[a-z]+-)?cooldown-until|(?:[a-z]+-)?runs-\d{4}-\d\d-\d\d|"
                          r"(?:quota|credential)-[a-z]+\.json")
LABELLED = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[[^\]\s]+/[^\]\s]+\] ")


def _legacy_state(config):
    special = {config.paths.logs, config.paths.ctx}
    try:
        entries = sorted(config.paths.state.iterdir())
    except OSError:
        return []
    return [p for p in entries if p not in special and not GLOBAL_STATE.fullmatch(p.name)
            and not p.name.endswith(".tmp")]


def _repo_roots(config, root):
    """Owner directories of configured repositories and repositories retained on disk.

    Removing a repository from the configuration leaves its isolated files in place. Its
    state/repos owner directory still identifies the layout, so it must not be migrated again.
    """
    owners = {repo.gh_repo.split("/")[0] for repo in config.repos}
    saved = config.paths.state / "repos"
    if saved.is_dir():
        owners.update(path.name for path in saved.iterdir() if path.is_dir())
    return {root / owner for owner in owners}


def _legacy_worktrees(config):
    try:
        entries = sorted(config.paths.wt_root.iterdir())
    except OSError:
        return []
    owners = _repo_roots(config, config.paths.wt_root)
    return [p for p in entries if p.is_dir() and p not in owners]


def _legacy_files(config, root, keep=()):
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    owners = _repo_roots(config, root)
    return [p for p in entries if p not in owners and p.name not in keep and p not in (config.paths.logs, config.paths.ctx)]


def pending(config):
    """A short description of single-repository files still in the old places, or ''."""
    found = [*_legacy_state(config), *_legacy_worktrees(config), *_legacy_files(config, config.paths.ctx)]
    return f"{len(found)} entries such as {found[0]}" if found else ""


def _move(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise SystemExit(f"migrate: {target} already exists; not overwriting it")
    shutil.move(str(source), str(target))


def _rewrite(path, old_new):
    """Saved absolute paths (a checkpoint's wt=, an opening's "wt") follow the move."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return
    changed = text
    for old, new in old_new:
        changed = changed.replace(old, new)
    if changed != text:
        path.write_text(changed)


def migrate(config, gh_repo, old_clone, log=print):
    repo = config.repo(gh_repo)
    dirs = repo_dirs(config.paths, repo)
    old_wt, old_ctx = str(config.paths.wt_root), str(config.paths.ctx)
    rewrites = ((old_wt + "/", str(dirs.worktrees) + "/"), (old_ctx + "/", str(dirs.ctx) + "/"))
    moved = 0
    for path in _legacy_state(config):
        target = dirs.state / path.name
        _move(path, target)
        for file in [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file()]:
            _rewrite(file, rewrites)
        moved += 1
    for path in _legacy_files(config, config.paths.ctx):
        _move(path, dirs.ctx / path.name)
        moved += 1
    for path in _legacy_files(config, config.paths.logs, keep=("runner.log",)):
        _move(path, dirs.logs / path.name)
        moved += 1
    worktrees = []
    for path in _legacy_worktrees(config):
        _move(path, dirs.worktrees / path.name)
        worktrees.append(dirs.worktrees / path.name)
        moved += 1
    if old_clone and Path(old_clone).exists() and Path(old_clone) != dirs.clone:
        _move(Path(old_clone), dirs.clone)
        moved += 1
    if worktrees:
        repaired = subprocess.run(["git", "-C", str(dirs.clone), "worktree", "repair", *map(str, worktrees)],
                                  capture_output=True, text=True)
        if repaired.returncode:
            raise SystemExit(f"migrate: git worktree repair failed: {repaired.stderr.strip()}")
    runner_log = config.paths.logs / "runner.log"
    # Only with the move: later unlabelled lines are runner-wide ("busy", "no work in any repository").
    if moved and runner_log.is_file():
        label = f"[{repo.gh_repo}] "
        lines = runner_log.read_text(errors="surrogateescape").splitlines(keepends=True)
        out = [line if LABELLED.match(line) or len(line) < 20 or line[19:20] != " "
               else line[:20] + label + line[20:] for line in lines]
        if out != lines:
            runner_log.write_text("".join(out), errors="surrogateescape")
    log(f"migrate: moved {moved} entries into {repo.gh_repo}'s directories")
    return moved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", metavar="OWNER/NAME")
    parser.add_argument("--clone", default="", help="Where the single-repository clone is (moved to clones/OWNER/NAME)")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.toml")
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
        config.repo(args.repo)
    except ConfigError as error:
        print(f"Invalid configuration: {error}", file=sys.stderr)
        return 2
    config.paths.state.mkdir(parents=True, exist_ok=True)
    with open(config.paths.state / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("migrate: a tick holds the lock; try again when it ends", file=sys.stderr)
            return 75
        migrate(config, args.repo, args.clone)
    return 0


if __name__ == "__main__":
    sys.exit(main())
