"""bin/verify.sh: the repository's checks before a push, then the E2E gate its issue asks for.

What the checks are is the repository's: a verify hook on this host or, with none, its CI command.
A deployment's own hooks are tested where they live."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"


def hook(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@pytest.fixture
def world(tmp_path):
    bin_dir, hooks, wt = tmp_path / "bin", tmp_path / "hooks", tmp_path / "wt"
    for directory in (bin_dir, hooks, wt):
        directory.mkdir()
    shutil.copy(BIN / "verify.sh", bin_dir)
    calls = tmp_path / "calls"

    class World:
        def __init__(self):
            self.calls, self.fail = calls, tmp_path / "fail"
            self.verify_hook = hook(hooks / "verify", f'echo "verify $* gate=$E2E_GATE" >> "{calls}"\necho checks-output\n'
                                                      f'[ ! -e "{self.fail}-verify" ]\n')
            self.gate_hook = hook(hooks / "gate", f'echo "gate $*" >> "{calls}"\necho gate-output\n'
                                                  f'printf "  \\033[32m15 passed\\033[39m (7.9m)\\r\\n\\n"\n[ ! -e "{self.fail}-gate" ]\n')

        def lines(self):
            return self.calls.read_text().splitlines() if self.calls.exists() else []

        def verify(self, gate="", verify_hook=True, gate_hook=True, ci=f'echo "ci $(pwd)" >> "{calls}"'):
            env = {**os.environ, "LOGS": str(tmp_path / "logs"), "VERIFY_TAG": "t", "E2E_GATE": gate, "CI_COMMAND": ci,
                   "VERIFY_HOOK": str(self.verify_hook) if verify_hook else "",
                   "GATE_HOOK": str(self.gate_hook) if gate_hook else ""}
            return subprocess.run([str(bin_dir / "verify.sh"), str(wt), "origin/main"], capture_output=True, text=True, env=env)
    return World()


def test_the_verify_hook_runs_with_the_worktree_base_and_gate(world):
    done = world.verify(gate="e2e/a.spec.ts")
    assert done.returncode == 0, done.stderr
    assert world.lines()[0] == f"verify {world.calls.parent / 'wt'} origin/main gate=e2e/a.spec.ts"
    assert "Verification passed" in done.stdout


def test_without_a_verify_hook_the_ci_command_runs_in_the_worktree(world):
    assert world.verify(verify_hook=False).returncode == 0
    assert world.lines() == [f"ci {world.calls.parent / 'wt'}"]


def test_a_red_check_fails_with_its_output_and_a_logged_duration(world):
    Path(f"{world.fail}-verify").touch()
    done = world.verify()
    assert done.returncode != 0 and "checks-output" in done.stderr
    assert "duration=" in (world.calls.parent / "logs/runner.log").read_text()


def test_the_gate_runs_after_green_checks_and_reports_its_last_line(world):
    done = world.verify(gate="e2e/invite-link.spec.ts --repeat-each=15")
    assert done.returncode == 0, done.stderr
    assert world.lines()[1] == f"gate {world.calls.parent / 'wt'} e2e/invite-link.spec.ts --repeat-each=15"
    assert "E2E gate passed: e2e/invite-link.spec.ts --repeat-each=15 (15 passed (7.9m))" in done.stdout


def test_a_red_gate_fails_verification(world):
    Path(f"{world.fail}-gate").touch()
    done = world.verify(gate="e2e/a.spec.ts")
    assert done.returncode != 0 and "gate-output" in done.stderr and "E2E gate passed" not in done.stdout


def test_red_checks_stop_before_the_gate(world):
    Path(f"{world.fail}-verify").touch()
    assert world.verify(gate="e2e/a.spec.ts").returncode != 0
    assert [line for line in world.lines() if line.startswith("gate")] == []


def test_a_gate_with_no_gate_hook_fails_rather_than_passing_unrun(world):
    done = world.verify(gate="e2e/a.spec.ts", gate_hook=False)
    assert done.returncode != 0 and "gate hook" in done.stderr


def test_no_gate_means_no_gate_run(world):
    assert world.verify().returncode == 0
    assert [line for line in world.lines() if line.startswith("gate")] == []
