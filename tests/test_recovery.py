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
            "role": "fixer", "episode": "9", "item": "3", "reason": "fix_run_failed", "decision_id": "abc",
            "state": "granted", "pinned": False}
    base.update(changes)
    return base


@pytest.fixture
def live(env, monkeypatch):
    """Live advisor with the grant of _record() on disk."""
    from baton import advisor
    monkeypatch.setattr(advisor, "enabled", lambda e: True)
    monkeypatch.setattr(advisor, "settings", lambda e: type("S", (), {"mode": "live"})())
    grant = advisor.grant_path(env, "fix_run_failed", "3")
    grant.parent.mkdir(parents=True, exist_ok=True)
    grant.write_text("abc")
    return env


def _resume(env, current, **kw):
    seen = []
    result = recovery.resume(env, "9", "3", lambda reason: reason.startswith("fix_"), lambda: current,
                             seen.append, local=True, **kw)
    return result, seen


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


def test_resume_hands_off_when_the_inputs_changed(live):
    recovery.persist(live, _record())
    result, seen = _resume(live, {"local_head": "b" * 40})
    assert result is False and "inputs changed" in seen[0]
    result, seen = _resume(live, {"local_head": "a" * 40})
    assert result[1] == "claude" and not seen


def test_a_dispatched_grant_is_not_dispatched_again_after_a_restart(live):
    from baton.common import Deferred
    current = {"local_head": "a" * 40}
    recovery.persist(live, _record())
    with pytest.raises(RuntimeError):   # the process died inside the step: no settling ran
        with recovery.dispatching(live, "9", "3", lambda: current):
            assert recovery.load(live, "9", "3")["state"] == "dispatched"
            raise RuntimeError("killed")
    result, seen = _resume(live, current)
    assert result is False and "already dispatched" in seen[0]


def test_a_deferral_without_progress_gives_the_grant_back(live):
    from baton.common import Deferred
    current = {"local_head": "a" * 40}
    recovery.persist(live, _record())
    with pytest.raises(Deferred):
        with recovery.dispatching(live, "9", "3", lambda: current):
            raise Deferred("claude", "short block")
    assert recovery.load(live, "9", "3")["state"] == "granted"
    result, seen = _resume(live, current)
    assert result[1] == "claude" and not seen


def test_a_deferral_after_progress_checkpoints_the_new_inputs(live):
    from baton.common import Deferred
    moved = {"local_head": "b" * 40, "pr_evidence": "own reply"}
    recovery.persist(live, _record())
    with pytest.raises(Deferred):
        with recovery.dispatching(live, "9", "3", lambda: moved):
            raise Deferred("claude", "short block")
    saved = recovery.load(live, "9", "3")
    assert saved["state"] == "progressed" and saved["fingerprint"] == moved
    result, seen = _resume(live, moved)
    assert result[0]["state"] == "progressed" and not seen


def test_a_foreign_change_is_not_progress(live):
    from baton.common import Deferred
    other = {"local_head": "a" * 40, "feedback": "new owner comment"}
    recovery.persist(live, _record(fingerprint={"local_head": "a" * 40, "feedback": "old"}))
    with pytest.raises(Deferred):
        with recovery.dispatching(live, "9", "3", lambda: other):
            raise Deferred("claude", "short block")
    assert recovery.load(live, "9", "3")["state"] == "granted"
    result, seen = _resume(live, other)
    assert result is False and "inputs changed" in seen[0]


def test_a_record_of_another_role_is_not_resumed(live):
    recovery.persist(live, _record(role="spec_reviewer"))
    result, seen = _resume(live, {"local_head": "a" * 40}, role="reviser")
    assert result is None and not seen


def test_an_alternate_vendor_is_not_pinned_on_resume(env, monkeypatch):
    from baton import pick
    record = _record(action="retry_step_other_vendor", selected_vendor="cursor", pinned=False)
    monkeypatch.setattr(pick, "choose", lambda *a, **k: "cursor")
    monkeypatch.setattr(recovery, "retry_candidates", lambda *a, **k: ["cursor"])
    assert recovery.select_vendor(env, record, mode="contrib", candidates=["claude", "cursor"],
                                  pinned=True) == "cursor"   # the caller's pin is the alternate's own


def test_only_a_fix_record_counts_as_a_pending_fix(env):
    assert recovery.pending_fix(env, "9", "3") == ""
    recovery.persist(env, _record(reason="review_run_failed", role="reviewer"))
    assert recovery.pending_fix(env, "9", "3") == ""
    recovery.persist(env, _record(reason="fix_run_failed"))
    assert recovery.pending_fix(env, "9", "3") == "claude"
    recovery.path(env, "9", "3").write_text("{")
    assert recovery.pending_fix(env, "9", "3") == "agent"


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
