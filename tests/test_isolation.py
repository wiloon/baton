"""The suite runs inside a baton run when baton works on itself (#73): what that run hands its
children must not change any result."""
import os
import subprocess
import sys

from conftest import ROOT

LEAKY = {
    "TRACEPARENT": "00-ffffffffffffffffffffffffffffffff-ffffffffffffffff-01",
    "OTEL_RESOURCE_ATTRIBUTES": "baton.run_id=parent",
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "BATON_AGENT_LOGS": "/nonexistent/parent-logs",
    "RUNNER_LOG": "/nonexistent/parent-runner.log",
    "VERIFY_HOOK": "/nonexistent/verify.sh",
    "GATE_HOOK": "/nonexistent/gate.sh",
    "CI_COMMAND": "false",
}


def test_tests_pass_under_a_parent_runs_environment():
    selected = ["tests/test_dispatch.py::test_telemetry_is_off_without_the_enabled_file",
                "tests/test_dispatch.py::test_codex_gets_the_run_context_in_its_child_only",
                "tests/test_dispatch.py::test_claude_exports_metrics_only_to_the_loopback_collector",
                "tests/test_verify.py", "tests/test_fix.py::test_verification_gets_the_repositorys_hooks_and_ci_command"]
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *selected],
                            cwd=ROOT, env={**os.environ, **LEAKY}, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]


def test_a_test_session_leaves_no_baton_entry_in_the_temporary_directory(tmp_path):
    scratch = tmp_path / "tmpdir"   # not the host's /tmp: a concurrent runner tick may write there
    scratch.mkdir()
    selected = ["tests/test_fix.py::test_a_failed_fix_does_not_apply_its_description_request",
                "tests/test_tick.py::test_item_finalization_clears_the_recorded_reply_owner_files"]
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *selected],
                            cwd=ROOT, env={**os.environ, "TMPDIR": str(scratch)}, capture_output=True, text=True,
                            timeout=300)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    assert sorted(p.name for p in scratch.glob("baton-*")) == []
