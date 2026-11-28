"""Per-repository work locks (#270, ADR 0003): ticks for different repositories may overlap.

Real Sessions over a two-repository config, real flock contention from independent descriptors
and, for the overlap itself, two real `bootstrap.main` processes."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import pytest

from baton import advisor, bootstrap, maintenance, migrate, tick
from conftest import in_flight, probing
from test_repos import TWO, load, two  # noqa: F401  (two is a fixture)

ROOT = Path(__file__).resolve().parents[1]
BUSY = "busy (another tick holds the lock)"
APP, SHOP = "repos/acme/app/lock", "repos/acme/shop/lock"


@contextmanager
def held(config, *names):
    files = []
    try:
        for name in names:
            path = config.paths.state / name
            path.parent.mkdir(parents=True, exist_ok=True)
            files.append(open(path, "a"))
            fcntl.flock(files[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        for file in files:
            file.close()


def locked(config, name):
    return maintenance.held(config.paths.state / name)


def runner_log(config):
    path = config.paths.logs / "runner.log"
    return path.read_text() if path.exists() else ""


def config_file(config):
    return str(config.source)


# --- the walk ------------------------------------------------------------------------------------
def test_a_locked_repository_is_skipped_and_the_next_one_worked(two):
    c, busy, handled, ticks = two
    busy.update({"acme/app", "acme/shop"})
    with held(c, APP):
        assert tick.run(ticks()) == 0
    log = runner_log(c)
    assert handled == ["acme/shop"]
    assert f"[acme/app] {BUSY}" in log and "[acme/app] nothing here" not in log


def test_all_repositories_locked_is_a_quiet_success(two):
    c, busy, handled, ticks = two
    busy.update({"acme/app", "acme/shop"})
    with held(c, APP, SHOP):
        assert tick.run(ticks()) == 0
    log = runner_log(c)
    assert handled == [] and log.count(BUSY) == 2 and "no work in any repository" not in log


def test_a_failed_fetch_beside_a_locked_repository_still_fails(two, monkeypatch):
    c, _, _, ticks = two

    def work(self):
        self.fetch_failed = True
    monkeypatch.setattr(tick.Tick, "work", work)
    with held(c, APP):
        assert tick.run(ticks()) == 1
    assert "no work in any repository" not in runner_log(c)


def test_a_manual_command_for_a_locked_repository_says_busy(two, monkeypatch):
    c, _, handled, _ = two
    monkeypatch.setattr(tick.Tick, "work", lambda self: pytest.fail("worked a locked repository"))
    with held(c, APP):
        assert bootstrap.main(["--config", config_file(c), "--repo", "acme/app", "542"]) == 0
    assert f"[acme/app] {BUSY}" in runner_log(c) and handled == []


def test_a_tick_holds_one_repository_lock_at_a_time_and_none_afterwards(two, monkeypatch):
    c, _, _, ticks = two
    seen = []

    def work(self):
        seen.append((self.gh_repo, locked(c, APP), locked(c, SHOP), locked(c, "housekeeping.lock"),
                     locked(c, "scan.lock")))
    monkeypatch.setattr(tick.Tick, "work", work)
    assert tick.run(ticks()) == 0
    assert seen == [("acme/app", True, False, False, False), ("acme/shop", False, True, False, False)]
    assert not any(locked(c, name) for name in (APP, SHOP))


def test_a_flag_set_mid_walk_stops_before_the_next_repository(two, monkeypatch):
    c, _, _, ticks = two
    worked = []

    def work(self):
        worked.append(self.gh_repo)
        maintenance.write_flag(c.paths.state, time.time() + 600, "deploy", "token")
    monkeypatch.setattr(tick.Tick, "work", work)
    assert tick.run(ticks()) == 0
    assert worked == ["acme/app"] and "maintenance (deploy)" in runner_log(c)
    assert "no work in any repository" not in runner_log(c)


# --- housekeeping and the scan -------------------------------------------------------------------
@pytest.mark.parametrize("busy_elsewhere", [False, True])
def test_housekeeping_runs_in_one_tick_at_a_time(two, monkeypatch, busy_elsewhere):
    c, busy, handled, ticks = two
    busy.add("acme/app")
    digests = []
    monkeypatch.setattr(advisor, "digest", lambda env: digests.append(locked(c, "housekeeping.lock")))
    with held(c, *(["housekeeping.lock"] if busy_elsewhere else [])):
        assert tick.run(ticks()) == 0
    assert digests == ([] if busy_elsewhere else [True])
    assert handled == ["acme/app"]   # repository work happens either way


@pytest.fixture
def scans(two, monkeypatch):
    c = two[0]
    seen = []

    def scan_fallback(self):
        seen.append((self.gh_repo, locked(c, "scan.lock"), locked(c, f"repos/{self.gh_repo}/lock")))
        return 0
    monkeypatch.setattr(tick.Tick, "scan_fallback", scan_fallback)
    return seen


def test_the_scan_holds_the_scan_lock_and_its_repositorys_lock(two, scans):
    _, _, _, ticks = two
    assert tick.run(ticks()) == 0
    assert scans == [("acme/app", True, True)]


def test_with_the_scan_lock_held_nothing_is_scanned_but_repositories_are_worked(two, scans):
    c, _, _, ticks = two
    with held(c, "scan.lock"):
        assert tick.run(ticks()) == 0
    log = runner_log(c)
    assert scans == []
    assert log.count("nothing here") == 2   # both were worked (conftest's resolve labels them alike)


def test_a_locked_repository_is_not_scanned_but_an_idle_one_may_be(two, scans):
    c, _, _, ticks = two
    with held(c, APP):
        assert tick.run(ticks()) == 0
    assert scans == [("acme/shop", True, True)]


@pytest.mark.parametrize("busy_elsewhere", [False, True])
def test_one_repository_keeps_its_lock_into_the_scan(two, scans, busy_elsewhere):
    c, _, _, ticks = two
    with held(c, *(["scan.lock"] if busy_elsewhere else [])):
        assert ticks()[0].run() == 0
    assert scans == ([] if busy_elsewhere else [("acme/app", True, True)])
    assert "[acme/app] nothing here" in runner_log(c)


@pytest.mark.parametrize("name", ["scan.lock", APP])
def test_a_manual_scan_takes_both_locks(two, monkeypatch, name):
    c = two[0]
    seen = []
    monkeypatch.setattr(tick.Tick, "work",
                        lambda self: seen.append((locked(c, APP), locked(c, "scan.lock"))) or 0)
    command = ["--config", config_file(c), "--repo", "acme/app", "scan"]
    with held(c, name):
        assert bootstrap.main(command) == 0
    assert seen == [] and f"[acme/app] {BUSY}" in runner_log(c)
    assert bootstrap.main(command) == 0
    assert seen == [(True, True)]


def test_file_scan_takes_only_the_repository_lock(two, monkeypatch):
    c = two[0]
    seen = []
    monkeypatch.setattr(tick.Tick, "work", lambda self: seen.append(locked(c, "scan.lock")) or 0)
    with held(c, "scan.lock"):
        assert bootstrap.main(["--config", config_file(c), "--repo", "acme/app", "file-scan", "/tmp/draft"]) == 0
    assert seen == [True]   # held by the test alone: the command did not need it


# --- state files ---------------------------------------------------------------------------------
def test_a_tick_still_starts_once_the_lock_files_exist(two, capsys):
    c = two[0]
    command = ["--config", config_file(c)]
    assert bootstrap.main(command) == 0
    assert (c.paths.state / "housekeeping.lock").exists() and (c.paths.state / "scan.lock").exists()
    assert migrate.pending(c) == ""
    assert bootstrap.main(command) == 0
    assert "Single-repository state" not in capsys.readouterr().err


def test_the_vendor_run_marker_and_probe_lock_are_global_state(two):
    c = two[0]
    with in_flight(c.paths.state, "claude"), probing(c.paths.state, "claude"):
        pass
    assert migrate.pending(c) == ""


def test_each_repository_keeps_its_own_chain_flag(two, monkeypatch):
    c, _, _, ticks = two
    flag = c.paths.state / "repos/acme/app/chain-next"
    execs = []

    def work(self):
        if self.gh_repo != "acme/app":
            return None
        flag.touch()   # a partial slice merged

        def execv(path, args):
            execs.append([locked(c, name) for name in (APP, "housekeeping.lock", "scan.lock")])
        self.exec = execv
        assert tick.run(ticks()) == 0   # another tick: A is locked, it works B
        assert flag.exists(), "the other tick cleared this repository's flag"
        return self.after_item(80)
    monkeypatch.setattr(tick.Tick, "work", work)
    assert tick.run(ticks()) == 0
    assert execs == [[False, False, False]] and not flag.exists()


def test_a_stale_chain_flag_is_cleared_by_the_next_holder_of_its_repository(two):
    c, _, _, ticks = two
    flag = c.paths.state / "repos/acme/app/chain-next"
    flag.touch()
    with held(c, APP):
        assert tick.run(ticks()) == 0
    assert flag.exists()
    assert tick.run(ticks()) == 0
    assert not flag.exists()


# --- two processes -------------------------------------------------------------------------------
DRIVER = '''"""A tick whose item is one real dispatch of the vendor selection picks."""
import sys
from pathlib import Path
from baton import bootstrap, dispatch, tick, vendors

def work(self):
    def body():
        chosen, rc = vendors.pick(self.env.config.paths.state, ["claude", "codex"], "dev", ["claude", "codex"])
        assert rc == 0, chosen
        env = self.env
        env.tools["STUB_REPO"] = self.gh_repo
        out = dispatch.dispatch(chosen["vendor"], "dev", "p", "item", sys.argv[2], env=env)
        assert out["defer"] == "", out
    self.item(body)
    return 0

tick.Tick.work = work
sys.exit(bootstrap.main(["--config", sys.argv[1]]))
'''

STUB = '''#!/bin/sh
name=$(basename "$0")
touch "{sync}/$name.$(echo "$STUB_REPO" | tr / _).started"
if [ "$STUB_REPO" = acme/app ]; then
  i=0
  while [ ! -e "{sync}/release" ] && [ $i -lt 600 ]; do sleep 0.05; i=$((i+1)); done
fi
touch "{sync}/$name.$(echo "$STUB_REPO" | tr / _).ended"
'''


def wait_for(ready, process, log):
    deadline = time.monotonic() + 30
    while not ready():
        assert process.poll() is None and time.monotonic() < deadline, f"the first run never started\n{log()}"
        time.sleep(0.02)


def test_two_ticks_run_items_in_different_repositories_at_the_same_time(tmp_path):
    sync, bin_dir, home = tmp_path / "sync", tmp_path / "bin", tmp_path / "home"
    for path in (sync, bin_dir, home):
        path.mkdir()
    for name in ("claude", "codex"):
        (bin_dir / name).write_text(STUB.format(sync=sync))
        (bin_dir / name).chmod(0o755)
    text = re.sub(r'(?m)^path_prefix = .*$', f'path_prefix = ["{bin_dir}"]', TWO)
    config = load(tmp_path, text)
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    work_tree = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", str(work_tree)], check=True)
    child = {"HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "PYTHONPATH": str(ROOT)}

    def start():
        return subprocess.Popen([sys.executable, str(driver), str(config.source), str(work_tree)], cwd=ROOT,
                                env=child, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def marks(repo, state):
        """The vendors whose stub run in REPO reached STATE."""
        return sorted(path.name.split(".")[0] for path in sync.glob(f"*.{repo}.{state}"))
    first = start()
    try:
        wait_for(lambda: marks("acme_app", "started"), first, lambda: runner_log(config))
        (config.paths.state / "last-repo").unlink()   # the second tick's rotation then reaches A first
        second = start()
        output = second.communicate(timeout=60)[0]
        assert second.returncode == 0, output + runner_log(config)
        # B's whole run happened inside A's run.
        assert marks("acme_shop", "ended"), output + runner_log(config)
        assert marks("acme_app", "ended") == []
        assert f"[acme/app] {BUSY}" in runner_log(config)
    finally:
        (sync / "release").touch()
        output = first.communicate(timeout=60)[0]
    assert first.returncode == 0, output
    assert marks("acme_app", "ended")


# --- two runs of one vendor (#299) ---------------------------------------------------------------
SAME_VENDOR_DRIVER = '''"""A tick whose item is one real Claude dispatch."""
import json
import sys
from pathlib import Path
from baton import bootstrap, dispatch, tick, vendors

def work(self):
    def body():
        chosen, rc = vendors.pick(self.env.config.paths.state, ["claude"], "dev", ["claude"])
        assert rc == 0, chosen
        env = self.env
        env.tools["STUB_REPO"] = self.gh_repo
        out = dispatch.dispatch(chosen["vendor"], "dev", "p", "item", sys.argv[2], env=env)
        Path(sys.argv[3], self.gh_repo.replace("/", "_") + ".out").write_text(json.dumps(out))
    self.item(body)
    return 0

tick.Tick.work = work
sys.exit(bootstrap.main(["--config", sys.argv[1]]))
'''

# Both runs end in a quota rejection. The one that ends last (acme/app) names the earlier reset.
QUOTA_STUB = '''#!/bin/sh
case "$*" in *--version*) exit 0 ;; esac
repo=$(echo "$STUB_REPO" | tr / _)
touch "{sync}/claude.$repo.started"
if [ "$STUB_REPO" = acme/app ]; then
  until_epoch={early}
  i=0
  while [ ! -e "{sync}/release" ] && [ $i -lt 600 ]; do sleep 0.05; i=$((i+1)); done
else
  until_epoch={late}
fi
touch "{sync}/claude.$repo.ended"
printf '{{"type":"result","subtype":"success","is_error":true,"result":"Claude usage limit reached. resets_at: %s"}}\\n' "$until_epoch"
exit 1
'''


def test_two_runs_of_one_vendor_in_different_repositories_overlap(tmp_path):
    sync, bin_dir, home = tmp_path / "sync", tmp_path / "bin", tmp_path / "home"
    for path in (sync, bin_dir, home):
        path.mkdir()
    early, late = int(time.time()) + 3600, int(time.time()) + 7200
    (bin_dir / "claude").write_text(QUOTA_STUB.format(sync=sync, early=early, late=late))
    (bin_dir / "claude").chmod(0o755)
    text = re.sub(r'(?m)^path_prefix = .*$', f'path_prefix = ["{bin_dir}"]', TWO)
    config = load(tmp_path, text)
    driver = tmp_path / "driver.py"
    driver.write_text(SAME_VENDOR_DRIVER)
    work_tree = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", str(work_tree)], check=True)
    child = {"HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.defpath}", "PYTHONPATH": str(ROOT)}

    def start():
        return subprocess.Popen([sys.executable, str(driver), str(config.source), str(work_tree), str(sync)],
                                cwd=ROOT, env=child, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def result(repo):
        return json.loads((sync / f"{repo}.out").read_text())
    first = start()
    try:
        wait_for(lambda: (sync / "claude.acme_app.started").exists(), first, lambda: runner_log(config))
        (config.paths.state / "last-repo").unlink()
        second = start()
        output = second.communicate(timeout=60)[0]
        assert second.returncode == 0, output + runner_log(config)
        # The second run started, and ended, while the first was still in flight.
        assert (sync / "claude.acme_shop.started").exists(), "the second run never started\n" + runner_log(config)
        assert not (sync / "claude.acme_app.ended").exists()
        assert result("acme_shop")["status"] == "quota"
    finally:
        (sync / "release").touch()
        output = first.communicate(timeout=60)[0]
    assert first.returncode == 0, output
    assert result("acme_app")["status"] == "quota"
    state = config.paths.state
    assert (state / f"runs-{time.strftime('%F')}").read_text() == "2\n"
    assert (state / "claude-cooldown-until").read_text() == f"{late}\n"
