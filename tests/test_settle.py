"""baton.settle: how a run ends (run count, cooldown file, quota, runner.log) and the
quota probe, ported from the settle half of tests/cooldown.sh. Telegram has no
credentials here, so a notification is a `title | text` line followed by `notify failed`."""
import re
import json
import os
from pathlib import Path
import stat
import sys
import time

import pytest

from baton import settle

PROBE_CLI = f"""#!{sys.executable}
import os, pathlib, sys
fake = pathlib.Path(os.environ["FAKE_DIR"])
(fake / "args.json").write_text(__import__("json").dumps(sys.argv[1:]))
(fake / "env.json").write_text(__import__("json").dumps(dict(os.environ)))
sys.stdout.write(os.environ.get("FAKE_OUT", ""))
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""


def unlabelled(line):
    """A runner.log line without its time and its [owner/name] label, which every line carries."""
    _, _, label, message = line.split(" ", 3)
    assert re.fullmatch(r"\[[^/]+/[^/]+\]", label), line
    return message


@pytest.fixture

def probe_cli(gh, tmp_path, monkeypatch):
    fake = tmp_path / "fake"
    fake.mkdir()
    for name in ("claude", "codex"):
        path = Path(gh.root.parent / "stubs" / name)
        path.write_text(PROBE_CLI)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    timeout = gh.root.parent / "stubs" / "timeout"   # `timeout --kill-after=10 2m CMD`
    timeout.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    monkeypatch.setenv("FAKE_DIR", str(fake))
    return fake


def log(tmp_path):
    path = tmp_path / "logs" / "runner.log"
    return [unlabelled(line) for line in path.read_text().splitlines() if line] if path.exists() else []


def run(tmp_path, status, **kwargs):
    arguments = dict(vendor="codex", rc=1, log="", tag="t", deadline="", outcome="")
    arguments.update(kwargs)
    return settle.settle(str(tmp_path / "state"), arguments.pop("vendor"), status, arguments.pop("rc"),
                         arguments.pop("log"), arguments.pop("tag"), arguments.pop("deadline"),
                         arguments.pop("outcome"), **arguments)


def test_runs_are_counted_per_vendor_and_day(gh, tmp_path):
    state = tmp_path / "state"
    settle.count_run(str(state), "claude", "2026-10-27")
    settle.count_run(str(state), "claude", "2026-10-27")
    settle.count_run(str(state), "codex", "2026-10-27")
    (state / "cursor-runs-2026-10-27").write_text("500\n")
    settle.count_run(str(state), "cursor", "2026-10-27")
    assert (state / "runs-2026-10-27").read_text() == "2\n"
    assert (state / "codex-runs-2026-10-27").read_text() == "1\n"
    assert (state / "cursor-runs-2026-10-27").read_text() == "501\n"   # no cap, however high


def test_success_changes_nothing(gh, tmp_path):
    assert run(tmp_path, "success", rc=0, outcome="REVIEW: pass") == (0, "REVIEW: pass", "")
    assert log(tmp_path) == ["t: vendor=codex outcome=success rc=0"]
    assert not (tmp_path / "state" / "codex-cooldown-until").exists()


@pytest.mark.parametrize("status, outcome, expected", [
    ("failure", "", "RESULT: failed codex failure rc=1 (log /l)"),
    ("timeout", "", "RESULT: failed codex timeout rc=1 (log /l)"),
    ("failure", "RESULT: failed no reply", "RESULT: failed codex failure rc=1: no reply (log /l)"),
])
def test_failures_rewrite_the_outcome_and_rest_nothing(gh, tmp_path, status, outcome, expected):
    assert run(tmp_path, status, log="/l", outcome=outcome) == (1, expected, "")
    assert not (tmp_path / "state" / "codex-cooldown-until").exists()


def test_quota_rests_the_vendor_and_notifies(gh, tmp_path):
    rc, outcome, defer = run(tmp_path, "quota", log="/l", deadline="1999999999", outcome="REVIEW: pass")
    assert (rc, outcome, defer) == (75, "REVIEW: pass", "quota exhausted; cooldown until epoch 1999999999")
    assert (tmp_path / "state" / "codex-cooldown-until").read_text() == "1999999999\n"
    assert log(tmp_path)[:2] == ["t: vendor=codex outcome=quota rc=1 reset=1999999999",
                                 "t: codex quota exhausted | Paused until epoch 1999999999; see /l"]


def test_auth_notifies_how_to_log_in_again(gh, tmp_path):
    rc, _, defer = run(tmp_path, "auth", deadline="123")
    assert (rc, defer) == (75, "authentication failed; cooldown until epoch 123")
    state = tmp_path / "state"
    assert log(tmp_path)[:2] == [
        "t: vendor=codex outcome=auth rc=1 retry=123 ",
        f"t: codex auth failure | Log codex in again on the runner host, then rm {state}/codex-cooldown-until "
        "to resume before epoch 123."]


def test_busy_defers_without_telegram(gh, tmp_path):
    rc, _, defer = run(tmp_path, "busy", deadline="456")
    assert (rc, defer) == (75, "model at capacity; cooldown until epoch 456")
    assert log(tmp_path) == ["t: vendor=codex outcome=busy rc=1 retry=456"]


@pytest.mark.parametrize("status, recorded", [("success", "authenticated"), ("auth", "auth_expired"),
                                              ("failure", None)])
def test_credential_follows_the_status_only_when_asked(gh, tmp_path, status, recorded):
    path = tmp_path / "state" / "credential-codex.json"
    run(tmp_path, status, rc=0, deadline="1")
    assert not path.exists()
    run(tmp_path, status, rc=0, deadline="1", credential=True)
    assert (json.loads(path.read_text())["result"] if path.exists() else None) == recorded


def test_the_quota_a_run_reported_is_kept(gh, tmp_path):
    log_file = tmp_path / "claude.jsonl"
    log_file.write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": 0.3, "resetsAt": 1793027400}}}}) + "\n")
    run(tmp_path, "success", vendor="claude", rc=0, log=str(log_file))
    snapshot = json.loads((tmp_path / "state" / "quota-claude.json").read_text())
    assert snapshot["windows"]["five_hour"]["used"] == pytest.approx(0.3)


# --- probe -------------------------------------------------------------------------------

def test_a_claude_probe_records_quota_and_a_working_login(gh, tmp_path, probe_cli, monkeypatch):
    monkeypatch.setenv("FAKE_OUT", json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": 0.1, "resetsAt": 1793027400}}}}) + "\n")
    settle.probe(str(tmp_path / "state"), str(tmp_path / "logs"), "claude", "haiku-test")
    state = tmp_path / "state"
    args = json.loads((probe_cli / "args.json").read_text())
    assert args[:4] == ["-p", "Reply with the word ok.", "--model", "haiku-test"]
    assert json.loads((state / "credential-claude.json").read_text())["result"] == "authenticated"
    assert json.loads((state / "quota-claude.json").read_text())["probed_at"] > 0
    assert log(tmp_path)[-1].startswith("quota probe claude (log ")


def test_a_codex_probe_has_no_usable_credentials(gh, tmp_path, probe_cli):
    settle.probe(str(tmp_path / "state"), str(tmp_path / "logs"), "codex", "")
    env = json.loads((probe_cli / "env.json").read_text())
    assert (env["GH_TOKEN"], env["GITHUB_TOKEN"], env["GIT_SSH_COMMAND"]) == ("invalid", "invalid", "/bin/false")
    assert "-s" in json.loads((probe_cli / "args.json").read_text())


@pytest.mark.parametrize("out, rc, result", [
    ("Failed to authenticate: OAuth session expired\n", 1, "auth_expired"),
    ("boom\n", 1, "probe_failed"),
    ("ok\n", 0, "authenticated"),
])
def test_a_probe_classifies_the_login(gh, tmp_path, probe_cli, monkeypatch, out, rc, result):
    monkeypatch.setenv("FAKE_OUT", out)
    monkeypatch.setenv("FAKE_RC", str(rc))
    settle.probe(str(tmp_path / "state"), str(tmp_path / "logs"), "claude", "m")
    assert json.loads((tmp_path / "state" / "credential-claude.json").read_text())["result"] == result


def test_a_cursor_probe_refreshes_from_the_usage_api_without_running_an_agent(gh, tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(settle.quota, "refresh_cursor", lambda state, now: {"seven_day": {"used": 0.4, "resets_at": 1999999999}})
    settle.probe(str(state), str(tmp_path / "logs"), "cursor", "haiku")
    assert "quota cursor: week 40%" in (tmp_path / "logs" / "runner.log").read_text()
    assert json.loads((state / "quota-cursor.json").read_text())["probed_at"] > 0
    assert not list((tmp_path / "logs").glob("*quota-probe*"))


def test_a_cursor_run_refreshes_its_quota_and_a_failure_is_only_logged(gh, tmp_path, monkeypatch):
    state = tmp_path / "state"
    calls = []
    monkeypatch.setattr(settle.quota, "refresh_cursor", lambda s, now: calls.append(s) or {})
    settle.settle(str(state), "cursor", "success", 0, "", "t")
    assert calls == [str(state)]

    def down(s, now):
        raise OSError("api down")
    monkeypatch.setattr(settle.quota, "refresh_cursor", down)
    assert settle.settle(str(state), "cursor", "success", 0, "", "t")[0] == 0
    assert "quota: refreshing cursor failed: api down" in (tmp_path / "logs" / "runner.log").read_text()
