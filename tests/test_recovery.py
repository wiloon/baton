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
    marker = "ADVISOR_VERIFY_LOG_EXCERPT_MARKER"
    failure = f"lint broke\nLast 200 lines:\n{marker} secret log"
    text = recovery.verify_status(repairs_used=1, max_repairs=2, failure=failure)
    assert marker not in text
    assert "secret log" not in text
    assert "lint broke" in text
    assert "Last 200 lines" not in text
    assert "repairs 1 of 2" in text


def test_recovery_record_survives_reload(env):
    recovery.persist(env, _record())
    loaded = recovery.load(env, "9", "3")
    assert loaded["action"] == "retry_step" and loaded["failed_vendor"] == "claude"


def test_fingerprint_matches_only_equal_inputs(env):
    recovery.persist(env, _record())
    loaded = recovery.load(env, "9", "3")
    assert recovery.fingerprint_matches(loaded, {"local_head": "b" * 40}) is False
    assert recovery.fingerprint_matches(loaded, {"local_head": "a" * 40}) is True


def test_only_a_fix_record_counts_as_a_pending_fix(env):
    assert recovery.pending_fix(env, "9", "3") is False
    recovery.persist(env, _record(reason="review_run_failed", role="reviewer"))
    assert recovery.pending_fix(env, "9", "3") is False
    recovery.persist(env, _record(reason="fix_run_failed"))
    assert recovery.pending_fix(env, "9", "3") is True
    recovery.path(env, "9", "3").write_text("{")
    assert recovery.pending_fix(env, "9", "3") is True


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
