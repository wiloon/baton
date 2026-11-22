"""Persisted recovery records for live worker retries (#125 follow-up)."""
import json
from unittest.mock import MagicMock

import pytest

from baton import handoff, recovery


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("state", "ctx", "logs"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("STATE", str(tmp_path / "state"))
    monkeypatch.setenv("LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("CTX", str(tmp_path / "ctx"))
    monkeypatch.setenv("GH_REPO", "owner/app")
    monkeypatch.setenv("OWNER", "owner")
    from baton import session
    return session.resolve({"STATE": str(tmp_path / "state"), "LOGS": str(tmp_path / "logs"),
                            "CTX": str(tmp_path / "ctx"), "GH_REPO": "owner/app", "OWNER": "owner"})


def _record(**changes):
    base = {"action": "retry_step", "failed_vendor": "claude", "exclusions": ["claude"],
            "remaining_budget": {"verify_repairs": 1}, "fingerprint": {"local_head": "a" * 40},
            "role": "fixer", "episode": "9", "item": "3", "reason": "fix_run_failed", "decision_id": "abc"}
    base.update(changes)
    return base


def test_verify_status_omits_raw_log_tail():
    failure = "step failed\nLast 200 lines:\n" + recovery.LOG_EXCERPT_MARKER + " secret log"
    with pytest.raises(ValueError):
        recovery.verify_status(repairs_used=2, max_repairs=2, failure=failure)
    text = recovery.verify_status(repairs_used=1, max_repairs=2, failure="lint broke\nmore")
    assert recovery.LOG_EXCERPT_MARKER not in text
    assert "Last 200 lines" not in text
    assert "repairs 1 of 2" in text


def test_recovery_persists_before_dispatch_and_survives_reload(env):
    recovery.persist(env, _record())
    loaded = recovery.load(env, "9", "3")
    assert loaded["action"] == "retry_step" and loaded["failed_vendor"] == "claude"


def test_fingerprint_mismatch_blocks_resume(env):
    recovery.persist(env, _record())
    loaded = recovery.load(env, "9", "3")
    assert recovery.fingerprint_matches(loaded, {"local_head": "b" * 40}) is False
    assert recovery.fingerprint_matches(loaded, {"local_head": "a" * 40}) is True


def test_terminal_handoff_clears_recovery(env, monkeypatch):
    recovery.persist(env, _record())
    monkeypatch.setattr(handoff, "label_ready", lambda *a, **k: MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    handoff.perform(env, "fix_run_failed", kind="pr", number="3", title="t", text="x", hooks=type("H", (), {"task": lambda *a: None})(),
                    item="3", episode="9")
    assert recovery.load(env, "9", "3") is None


def test_malformed_recovery_raises(env):
    recovery.path(env, "9", "3").write_text("{")
    with pytest.raises(recovery.RecoveryUnreadable):
        recovery.load(env, "9", "3")