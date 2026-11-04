"""Each watched repository's nightly hook on its origin/main: python -m baton.nightly.

For checks CI doesn't run (such as a Playwright suite), so a merged change is run again every
night. The hook gets a fresh worktree with the repository's env files; exit 0 is green and its
last output line is the summary. It takes the runner's lock because such checks may bind fixed
ports, and a run in progress wins: the night is skipped. Anything but green reaches the owner.
"""
import fcntl
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

from .dev import copy_env_files

WORKTREE = "nightly"
BUSY = 75
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)


def _remove(repo, wt, env):
    _git(repo, "worktree", "remove", "--force", str(wt), env=env)
    _git(repo, "worktree", "prune", env=env)


def summary(output):
    """The hook's last nonempty line."""
    lines = [line.strip() for line in ANSI.sub("", output).replace("\r", "").splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def run(*, state, logs, wt_root, repo, env_files, hook, tools, notify, log):
    if hook is None:
        log("nightly: no nightly hook configured; nothing to run")
        return 0
    with open(Path(state, "lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("nightly: pipeline busy; skipped tonight")
            return BUSY
        wt = Path(wt_root, WORKTREE)
        _remove(repo, wt, tools)
        fetched = _git(repo, "fetch", "-q", "--prune", "origin", env=tools)
        added = _git(repo, "worktree", "add", "-q", "--detach", str(wt), "origin/main", env=tools)
        if fetched.returncode or added.returncode:
            log("nightly: could not set up the worktree")
            notify("nightly could not start", (fetched.stderr + added.stderr)[-500:])
            return 1
        try:
            copy_env_files(wt, env_files)
            file = Path(logs, f"{time.strftime('%F-%H%M%S')}-nightly.log")
            started = time.monotonic()
            with file.open("w") as out:
                rc = subprocess.run([str(hook), str(wt)], stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=tools).returncode
            result = summary(file.read_text(errors="replace"))
            log(f"nightly rc={rc} {result} duration={int(time.monotonic() - started)}s log={file}")
            if rc == 0:
                return 0
            head = _git(repo, "rev-parse", "--short", "origin/main", env=tools).stdout.strip()
            notify("nightly is not green", f"main at {head}: {result}. Log on {socket.gethostname()}: {file}")
            return 1
        finally:
            _remove(repo, wt, tools)


def main(argv=None):
    """Every configured repository's nightly hook, one after another: 1 if any night is not green,
    else 75 if any was skipped for a busy pipeline."""
    import argparse
    from . import notify, session
    from .config import load
    from .initialization import prepare
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.toml")
    config = load(parser.parse_args(argv).config)
    prepare(config)
    results = []
    for repo in config.repos:
        env = session.create(config, repo=repo)
        token = session.bind(env)
        try:
            results.append(run(state=config.paths.state, logs=env.dirs.logs, wt_root=env.dirs.worktrees, repo=env.dirs.clone,
                               env_files=repo.env_files, hook=repo.nightly_hook, tools=env.tools,
                               notify=notify.notify, log=notify.log))
        finally:
            session._active.reset(token)
    return 1 if 1 in results else BUSY if BUSY in results else 0


if __name__ == "__main__":
    sys.exit(main())
