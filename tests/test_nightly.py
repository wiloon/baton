"""baton.nightly: the repository's nightly hook on main, on its own schedule and worktree, under the runner's lock.

What green means is the hook's exit code; a Playwright hook would judge its counts."""
import fcntl
import subprocess

import pytest

from baton import nightly


@pytest.fixture
def world(tmp_path):
    """A base clone with an origin/main, and a fake e2e script that reports what it saw."""
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    git = ["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    (clone / ".worktreeinclude").write_text("# comment\nweb/.env.local\nmissing/.env\n")
    subprocess.run([*git, "checkout", "-q", "-b", "main"], capture_output=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
    subprocess.run([*git, "push", "-q", "origin", "main"], check=True, capture_output=True)

    secrets = tmp_path / "secrets"
    (secrets / "web").mkdir(parents=True)
    (secrets / "web" / ".env.local").write_text("APP_SECRET_KEY=sk_test_x\n")
    hooks = tmp_path / "hooks"
    hooks.mkdir()

    class World:
        state, logs, wt_root = tmp_path / "state", tmp_path / "logs", tmp_path / "wts"
        repo, env_files, seen, notices, root = clone, secrets, tmp_path / "seen", [], tmp_path
        hook = hooks / "nightly"

        def script(self, rc=0, output="  50 passed (6.7m)\n"):
            path = self.hook
            path.write_text(f'#!/bin/sh\nls "$1/web/.env.local" > "{self.seen}" && git -C "$1" rev-parse HEAD >> "{self.seen}"\n'
                            f"cat <<'OUT'\n{output}OUT\nexit {rc}\n")
            path.chmod(0o755)

        def run(self):
            for directory in (self.state, self.logs, self.wt_root):
                directory.mkdir(exist_ok=True)
            return nightly.run(state=self.state, logs=self.logs, wt_root=self.wt_root, repo=self.repo,
                               env_files=self.env_files, hook=self.hook, tools=None,
                               notify=lambda title, text="": self.notices.append((title, text)),
                               log=lambda message: (self.logs / "runner.log").open("a").write(message + "\n"))

        def runner_log(self):
            return (self.logs / "runner.log").read_text()
    return World()


def test_a_green_night_runs_main_with_the_env_files_and_stays_quiet(world):
    world.script()
    assert world.run() == 0
    seen = world.seen.read_text().splitlines()
    head = subprocess.run(["git", "-C", str(world.repo), "rev-parse", "origin/main"], capture_output=True, text=True).stdout.strip()
    assert seen[-1] == head
    assert world.notices == []
    assert "nightly rc=0" in world.runner_log() and "50 passed" in world.runner_log()
    assert not (world.wt_root / nightly.WORKTREE).exists()


def test_a_red_night_tells_the_owner_its_last_line_and_keeps_the_log(world):
    world.script(rc=1, output="  1) spec.ts\n  1 failed, 49 passed\n\n")
    assert world.run() == 1
    title, text = world.notices[0]
    assert "nightly" in title and "1 failed, 49 passed" in text
    log = next(world.logs.glob("*-nightly.log"))
    assert "spec.ts" in log.read_text() and str(log) in text
    assert not (world.wt_root / nightly.WORKTREE).exists()


def test_no_nightly_hook_means_nothing_runs(world):
    world.hook = None
    assert world.run() == 0
    assert not world.wt_root.exists() or not (world.wt_root / nightly.WORKTREE).exists()
    assert world.notices == [] and "no nightly hook" in world.runner_log()


def test_a_busy_pipeline_is_left_alone(world):
    world.script()
    world.state.mkdir()
    with open(world.state / "lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert world.run() == nightly.BUSY
    assert not world.seen.exists() and world.notices == []
    assert "busy" in world.runner_log()


def test_a_worktree_left_by_a_crash_does_not_stop_the_next_night(world):
    world.script()
    world.wt_root.mkdir()
    subprocess.run(["git", "-C", str(world.repo), "worktree", "add", "-q", "--detach",
                    str(world.wt_root / nightly.WORKTREE), "origin/main"], check=True)
    assert world.run() == 0
