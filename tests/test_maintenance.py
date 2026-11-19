"""baton.maintenance: the drain flag, its admission lock and every work starter's check (#59).

Everything goes through the production commands with isolated state, a controlled clock for
expiry and real flock contention from independent file descriptors."""
import fcntl
import json
import os
from pathlib import Path
import re
import threading
import time

import pytest

from baton import bootstrap, maintenance, migrate, nightly, notify
from baton import retrospectives as retro

ROOT = Path(__file__).resolve().parents[1]


class Host:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.state, self.logs = tmp_path / "state", tmp_path / "logs"
        text = (ROOT / "config.example.toml").read_text()
        for key in ("state", "logs", "ctx", "clones", "wt_root"):
            text = re.sub(rf'(?m)^{key} = .*$', f'{key} = "{tmp_path / key}"', text)
        self.hook = tmp_path / "nightly-hook"
        self.hook_ran = tmp_path / "hook-ran"
        self.hook.write_text(f"#!/bin/sh\ntouch {self.hook_ran}\n")
        self.hook.chmod(0o755)
        text = re.sub(r'(?m)^nightly_hook = .*$', f'nightly_hook = "{self.hook}"', text)
        self.text = text
        self.config = tmp_path / "config.toml"
        self.config.write_text(text)
        self.gh_calls = tmp_path / "gh-calls"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(f'#!/bin/sh\necho "$@" >> {self.gh_calls}\necho "[]"\n')
        (bin_dir / "gh").chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        self.clock = [1_000_000.0]
        monkeypatch.setattr(maintenance, "now", lambda: self.clock[0])
        monkeypatch.setattr(maintenance, "POLL", 0.02)
        self.state.mkdir()
        self.logs.mkdir()

    def maint(self, *args):
        return maintenance.main(["--config", str(self.config), *args])

    def runner_log(self):
        return "".join(path.read_text() for path in sorted(self.logs.rglob("runner.log")))

    def hold(self, name):
        path = self.state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        file = open(path, "a")
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return file

    def flag(self):
        return json.loads((self.state / "maintenance").read_text())


@pytest.fixture
def host(tmp_path, monkeypatch):
    return Host(tmp_path, monkeypatch)


def release_later(file, delay, before=None):
    def go():
        time.sleep(delay)
        if before:
            before()
        fcntl.flock(file, fcntl.LOCK_UN)
        file.close()
    thread = threading.Thread(target=go)
    thread.start()
    return thread


def run_thread(target):
    box = {}
    thread = threading.Thread(target=lambda: box.update(rc=target()))
    thread.start()
    return thread, box


# --- the command ------------------------------------------------------------------------

def test_on_status_and_off(host, capsys):
    assert host.maint("on", "--reason", "deploy v1", "--ttl", "10m") == 0
    assert host.flag()["reason"] == "deploy v1"
    assert host.flag()["expires"] == host.clock[0] + 600
    assert not list(host.state.glob("*.tmp"))
    capsys.readouterr()
    assert host.maint("status") == 0
    out = capsys.readouterr().out
    assert "maintenance: on" in out and "deploy v1" in out
    assert "runner lock: free" in out and "retrospectives lock: free" in out
    assert host.maint("off") == 0
    assert host.maint("off") == 0
    assert not (host.state / "maintenance").exists()
    assert (host.state / "maintenance.lock").exists()
    capsys.readouterr()
    assert host.maint("status") == 0
    assert "maintenance: off" in capsys.readouterr().out


def test_status_reports_each_work_lock_independently(host, capsys):
    runner = host.hold("lock")
    host.maint("status")
    out = capsys.readouterr().out
    assert "runner lock: held" in out and "retrospectives lock: free" in out
    fcntl.flock(runner, fcntl.LOCK_UN)
    worker = host.hold("retrospectives/worker.lock")
    host.maint("status")
    out = capsys.readouterr().out
    assert "runner lock: free" in out and "retrospectives lock: held" in out


@pytest.mark.parametrize("bad", ["0s", "-5m", "1.5h", "2d", "h", "10", ""])
def test_durations_are_positive_integers_with_a_unit(host, bad):
    with pytest.raises(SystemExit) as error:
        host.maint("on", "--ttl", bad)
    assert error.value.code == 2
    assert not (host.state / "maintenance").exists()


def test_default_ttl_is_two_hours(host):
    host.maint("on")
    assert host.flag()["expires"] == host.clock[0] + 7200


def test_an_expired_flag_is_off_and_a_malformed_one_lasts_the_default_ttl(host, capsys):
    host.maint("on", "--ttl", "1m")
    host.clock[0] += 61
    capsys.readouterr()
    host.maint("status")
    assert "maintenance: off" in capsys.readouterr().out
    path = host.state / "maintenance"
    path.write_text("not json")
    os.utime(path, (host.clock[0], host.clock[0]))
    host.clock[0] += 7199
    host.maint("status")
    out = capsys.readouterr().out
    assert "maintenance: on" in out and "invalid flag" in out
    host.clock[0] += 2
    host.maint("status")
    assert "maintenance: off" in capsys.readouterr().out
    path.write_text(json.dumps({"reason": "no expiry"}))
    os.utime(path, (host.clock[0], host.clock[0]))
    host.maint("status")
    assert "invalid flag" in capsys.readouterr().out


# --- waiting for the drain ---------------------------------------------------------------

@pytest.mark.parametrize("name", ["lock", "retrospectives/worker.lock"])
def test_wait_returns_only_after_the_holder_releases(host, name):
    held = host.hold(name)
    released = []
    thread = release_later(held, 0.4, before=lambda: released.append(True))
    started = time.monotonic()
    assert host.maint("on", "--wait", "--timeout", "30s", "--reason", "deploy") == 0
    assert released and time.monotonic() - started >= 0.35
    thread.join()
    assert host.flag()["reason"] == "deploy"


@pytest.mark.parametrize("name", ["lock", "retrospectives/worker.lock"])
def test_wait_timeout_exits_75_and_leaves_the_flag(host, name, monkeypatch):
    held = host.hold(name)
    ticks = iter(range(100))
    monkeypatch.setattr(maintenance, "monotonic", lambda: next(ticks) * 0.5)
    assert host.maint("on", "--wait", "--timeout", "1s", "--ttl", "5m", "--reason", "deploy") == 75
    assert host.flag()["reason"] == "deploy"
    assert host.flag()["expires"] == host.clock[0] + 1 + 300
    held.close()


def test_wait_keeps_the_flag_active_and_restarts_the_ttl_when_the_drain_ends(host, capsys):
    held = host.hold("lock")

    def later():
        host.clock[0] += 5000
    thread = release_later(held, 0.5, before=later)
    waiter, box = run_thread(lambda: host.maint("on", "--wait", "--timeout", "2h", "--ttl", "10s", "--reason", "r"))
    time.sleep(0.2)
    capsys.readouterr()
    host.maint("status")
    assert "maintenance: on" in capsys.readouterr().out
    assert host.flag()["expires"] == 1_000_000.0 + 7200 + 10
    waiter.join()
    thread.join()
    assert box["rc"] == 0
    assert host.flag()["expires"] == host.clock[0] + 10


def test_a_waiter_leaves_a_later_request_intact(host):
    held = host.hold("lock")
    waiter, box = run_thread(lambda: host.maint("on", "--wait", "--timeout", "30s", "--reason", "old"))
    time.sleep(0.2)
    host.maint("on", "--reason", "new")
    fcntl.flock(held, fcntl.LOCK_UN)
    waiter.join()
    assert box["rc"] == 75
    assert host.flag()["reason"] == "new"
    assert host.flag()["expires"] == host.clock[0] + 7200


def test_a_waiter_does_not_recreate_a_cleared_flag(host):
    held = host.hold("lock")
    waiter, box = run_thread(lambda: host.maint("on", "--wait", "--timeout", "30s"))
    time.sleep(0.2)
    host.maint("off")
    fcntl.flock(held, fcntl.LOCK_UN)
    waiter.join()
    assert box["rc"] == 75
    assert not (host.state / "maintenance").exists()


# --- the starters ------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [[], ["123"], ["--chain", "1"], ["--repo", "acme/app", "pr", "5"]])
def test_an_active_flag_skips_every_tick_without_github_calls(host, argv):
    host.config.write_text(host.text.replace("paused = false", "paused = true"))
    host.maint("on", "--reason", "deploy v2")
    assert bootstrap.main(["--config", str(host.config), *argv]) == 0
    assert "maintenance (deploy v2)" in host.runner_log()
    assert "paused" not in host.runner_log()
    assert not host.gh_calls.exists()
    assert not (host.state / "lock").exists() or fcntl_free(host.state / "lock")


def fcntl_free(path):
    with open(path, "a") as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return True


def test_a_starter_queued_at_admission_sees_a_flag_published_meanwhile(host):
    gate = host.hold("maintenance.lock")
    starter, box = run_thread(lambda: bootstrap.main(["--config", str(host.config)]))
    time.sleep(0.3)
    assert starter.is_alive()
    maintenance.write_flag(host.state, maintenance.now() + 600, "late", "x")
    fcntl.flock(gate, fcntl.LOCK_UN)
    starter.join()
    assert box["rc"] == 0
    assert "maintenance (late)" in host.runner_log()
    assert not host.gh_calls.exists()


def test_the_first_tick_after_expiry_removes_the_flag_and_proceeds(host):
    host.maint("on", "--ttl", "1m")
    host.clock[0] += 61
    bootstrap.main(["--config", str(host.config)])
    log = host.runner_log()
    assert "maintenance expired" in log
    assert "maintenance (" not in log
    assert not (host.state / "maintenance").exists()


def test_a_malformed_flag_skips_ticks_for_the_fallback_window(host):
    path = host.state / "maintenance"
    path.write_text("garbage")
    os.utime(path, (host.clock[0], host.clock[0]))
    assert bootstrap.main(["--config", str(host.config)]) == 0
    assert "maintenance (invalid flag)" in host.runner_log()
    host.clock[0] += 7201
    bootstrap.main(["--config", str(host.config)])
    assert "maintenance expired" in host.runner_log()
    assert not path.exists()


def test_nightly_skips_before_any_worktree_or_hook(host, monkeypatch):
    host.maint("on", "--reason", "deploy v3")
    logged = []
    monkeypatch.setattr(notify, "log", logged.append)
    monkeypatch.setattr(nightly.subprocess, "run", lambda *a, **k: pytest.fail("git or hook ran"))
    assert nightly.main(["--config", str(host.config)]) == 0
    assert any("maintenance (deploy v3)" in line for line in logged)
    assert not host.hook_ran.exists()


def test_a_busy_runner_lock_is_not_a_maintenance_skip_for_nightly(host):
    held = host.hold("lock")
    assert nightly.main(["--config", str(host.config)]) == nightly.BUSY
    held.close()


def lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


@pytest.mark.parametrize("command", [["catch-up"], ["episode", "7d6d2b8e-5e8f-4f43-a0d0-4a1d5a2d6f10"]])
@pytest.mark.parametrize("override", [False, True])
def test_retrospectives_report_maintenance_and_do_nothing(host, command, override, capsys, monkeypatch):
    host.maint("on", "--reason", "deploy v4")
    monkeypatch.setattr(retro, "Ledger", lambda *a: pytest.fail("analysis started"))
    args = ["--config", str(host.config)]
    if override:
        args += ["--work-state", str(host.root / "journal")]
    capsys.readouterr()
    assert retro.main([*args, *command]) == 0
    assert lines(capsys) == [{"reason": "deploy v4", "state": "maintenance"}]
    assert not (host.state / "retrospectives" / "reports").exists()
    assert not (host.root / "journal" / "reports").exists()


def test_a_separate_source_ignores_the_flag(host, capsys):
    host.maint("on", "--reason", "deploy")
    other = host.root / "offline"
    other.mkdir()
    capsys.readouterr()
    assert retro.main(["--config", str(host.config), "--state", str(other), "catch-up"]) == 0
    assert lines(capsys)[0]["state"] != "maintenance"


def test_a_work_state_override_still_takes_the_default_worker_lock(host, capsys):
    held = host.hold("retrospectives/worker.lock")
    assert retro.main(["--config", str(host.config), "--work-state", str(host.root / "journal"), "catch-up"]) == 0
    assert lines(capsys)[0]["state"] == "busy"
    held.close()


# --- migrate -----------------------------------------------------------------------------

def test_migrate_acquires_its_lock_while_the_flag_is_active_and_keeps_global_files(host):
    host.maint("on", "--reason", "deploy")
    before = (host.state / "maintenance").read_bytes()
    (host.state / "checkpoint-123").write_text("old\n")
    assert migrate.main(["acme/app", "--config", str(host.config)]) == 0
    assert (host.state / "maintenance").read_bytes() == before
    assert (host.state / "maintenance.lock").exists()
    assert not (host.state / "checkpoint-123").exists()
    assert (host.state / "repos" / "acme" / "app" / "checkpoint-123").exists()


def test_migrate_still_reports_a_busy_pipeline(host, capsys):
    held = host.hold("lock")
    assert migrate.main(["acme/app", "--config", str(host.config)]) == 75
    held.close()


def test_the_flag_files_are_not_legacy_state(host):
    host.maint("on")
    assert migrate.pending(maintenance_config(host)) == ""


def maintenance_config(host):
    from baton.config import load
    return load(host.config)


def test_the_tick_sees_the_flag_not_a_legacy_state_error(host, capsys):
    host.maint("on", "--reason", "deploy")
    capsys.readouterr()
    assert bootstrap.main(["--config", str(host.config)]) == 0
    assert "Single-repository" not in capsys.readouterr().err


def test_readme_documents_the_command():
    text = (ROOT / "README.md").read_text()
    assert "python -m baton.maintenance" in text
    for word in ("--wait", "--timeout", "--ttl", "2h", "75"):
        assert word in text
