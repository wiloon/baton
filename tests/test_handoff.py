"""Shared task handoff entry (#125): reason table, sole ready-for-human writes, telemetry."""
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from baton import handoff, session
from baton.config import Advisor

ROOT = Path(__file__).resolve().parents[1]
BATON = ROOT / "baton"

# Cursor triage and after-merge tracker creation may still write ready-for-human directly.
ALLOWED_READY_WRITES = {
    BATON / "handoff.py",
    BATON / "tick.py",         # Cursor triage only (asserted below)
    BATON / "after_merge.py",  # tracker / checks-to-try issues
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("state", "ctx", "logs"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("STATE", str(tmp_path / "state"))
    monkeypatch.setenv("LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("CTX", str(tmp_path / "ctx"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GH_REPO", "owner/app")
    monkeypatch.setenv("OWNER", "owner")
    return session.resolve({"STATE": str(tmp_path / "state"), "LOGS": str(tmp_path / "logs"),
                            "CTX": str(tmp_path / "ctx"), "GH_REPO": "owner/app", "OWNER": "owner"})


def test_an_unlisted_reason_fails_before_any_label_change(env, monkeypatch):
    labelled, events, notes = [], [], []

    class Hooks:
        def task(self, kind, key, *args):
            events.append((kind, key, args))

    monkeypatch.setattr(handoff, "label_ready", lambda *a, **k: labelled.append(1))
    monkeypatch.setattr(handoff.notify, "notify", lambda title, text="": notes.append((title, text)))
    with pytest.raises(handoff.UnknownReason, match="not-a-reason"):
        handoff.perform(env, "not-a-reason", kind="pr", number="12",
                        title="t", text="x", hooks=Hooks())
    assert labelled == [] and events == [] and notes == []


def test_a_category_4_reason_hands_off_with_no_advisor_call(env, monkeypatch):
    labelled, events, notes, calls = [], [], [], []

    class Hooks:
        def task(self, kind, key, *args):
            events.append((kind, key) + args)

    monkeypatch.setattr(handoff, "label_ready",
                        lambda env, kind, number, remove=(): labelled.append((kind, number, remove)) or MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda title, text="": notes.append((title, text)))
    monkeypatch.setattr(handoff.advisor, "advise", lambda *a, **k: calls.append(1) or handoff.advisor.Decision("handoff"))
    monkeypatch.setattr(handoff.advisor, "settings", lambda e: Advisor(env_file=None, mode="live"))
    result = handoff.perform(env, "auto_merge_off", kind="pr", number="1",
                             title="PR #1 review passed", text="auto-merge is off", hooks=Hooks())
    assert result.action == handoff.HANDOFF and result.handed_off
    assert labelled == [("pr", "1", ())] and calls == []
    assert events == [("handoff", "auto_merge_off", "--reason", "auto_merge_off")]
    assert notes == [("PR #1 review passed", "auto-merge is off")]
    assert handoff.REASONS["auto_merge_off"].category == 4


def test_ready_for_human_writes_outside_the_entry_are_only_the_allowed_exceptions():
    """Tracked runner source: only handoff.label_ready, Cursor triage and after-merge trackers."""
    add_ready = re.compile(r"""--add-label["'],\s*["']ready-for-human["']|--label["'],\s*["']ready-for-human["']""")
    offenders = []
    for path in sorted(BATON.glob("*.py")):
        text = path.read_text()
        if path in ALLOWED_READY_WRITES:
            if path.name == "tick.py":
                for i, line in enumerate(text.splitlines(), 1):
                    if add_ready.search(line) and "needs-triage" not in line:
                        offenders.append(f"tick.py:{i}")
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if add_ready.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{i}")
    assert offenders == []
    handoff_src = (BATON / "handoff.py").read_text()
    assert 'READY = "ready-for-human"' in handoff_src
    assert '["--add-label", READY]' in handoff_src or "('--add-label', READY)" in handoff_src or 'READY)' in handoff_src


def test_python_and_alloy_reason_filters_include_every_handoff_reason():
    text = (ROOT / "bin" / "telemetry.py").read_text()
    match = re.search(r"^REASONS = (\([^)]+\))", text, re.M | re.S)
    assert match, "bin/telemetry.py must define REASONS"
    reasons = set(eval(match.group(1), {"__builtins__": {}}))
    assert "none" in reasons
    assert set(handoff.REASONS) <= reasons
    assert "owner_review_paths" in reasons
    alloy = (ROOT / "telemetry" / "runner.alloy").read_text()
    filters = re.findall(r'IsMatch\(attributes\["reason"\], "\^\(([^"]+)\)\$"\)', alloy)
    assert len(filters) == 2
    for pattern in filters:
        names = set(pattern.split("|"))
        assert set(handoff.REASONS) <= names
        assert "owner_review_paths" in names


def test_category_4_sites_have_direct_mode_and_no_recovery_actions():
    for reason in ("auto_merge_off", "owner_review_paths", "manual_checks", "spec_rejected",
                   "fix_needs_info", "dev_blocked", "reviewer_unavailable",
                   "review_independence_denied", "reviewer_role_denied"):
        entry = handoff.REASONS[reason]
        assert entry.category == 4 and entry.mode == "direct" and entry.actions == ()


def _hooks():
    events = []

    class Hooks:
        def task(self, kind, key, *args):
            events.append((kind, key) + args)

    return Hooks(), events


def test_category_1_transient_failure_defers_without_advisor_or_labels(env, monkeypatch):
    """After gh backoff, a category-1 transient schedules the first later attempt (60s)."""
    labelled, notes, calls = [], [], []
    hooks, events = _hooks()
    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    monkeypatch.setattr(handoff, "label_ready", lambda *a, **k: labelled.append(1))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: notes.append(1))
    monkeypatch.setattr(handoff.advisor, "advise", lambda *a, **k: calls.append(1) or handoff.advisor.Decision("handoff"))
    result = handoff.perform(
        env, "spec_body_edit_failed", kind="issue", number="7", title="t", text="x",
        hooks=hooks, item="7", episode="7", transient=True,
        continuation={"op": "issue_edit", "path": "/tmp/body"})
    assert result.action == handoff.DEFER and not result.handed_off
    assert labelled == [] and notes == [] and calls == [] and events == []
    state = handoff.load_retry(env, "7", "spec_body_edit_failed")
    assert state["attempts"] == 0 and state["due"] == 1_060
    assert state["continuation"] == {"op": "issue_edit", "path": "/tmp/body"}


def test_category_1_permanent_failure_hands_off_immediately(env, monkeypatch):
    labelled, notes = [], []
    hooks, events = _hooks()
    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    monkeypatch.setattr(handoff, "label_ready",
                        lambda *a, **k: labelled.append(1) or MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: notes.append(1))
    result = handoff.perform(
        env, "spec_body_edit_failed", kind="issue", number="7", title="t", text="x",
        hooks=hooks, item="7", episode="7", transient=False)
    assert result.action == handoff.HANDOFF and result.handed_off
    assert labelled == [1] and notes == [1]
    assert events == [("handoff", "spec_body_edit_failed", "--reason", "spec_body_edit_failed")]
    assert handoff.load_retry(env, "7", "spec_body_edit_failed") is None


def test_category_1_retry_waits_until_due_then_consumes_an_attempt(env, monkeypatch):
    hooks, _ = _hooks()
    monkeypatch.setattr(handoff, "label_ready", lambda *a, **k: MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    first = handoff.perform(
        env, "review_comment_post_failed", kind="pr", number="3", title="t", text="x",
        hooks=hooks, item="3", episode="9", transient=True,
        continuation={"op": "pr_comment", "body": "saved"})
    assert first.action == handoff.DEFER
    clock["now"] = 1_059
    early = handoff.perform(
        env, "review_comment_post_failed", kind="pr", number="3", title="t", text="x",
        hooks=hooks, item="3", episode="9", resume=True)
    assert early.action == handoff.DEFER
    assert handoff.load_retry(env, "9", "review_comment_post_failed")["attempts"] == 0
    clock["now"] = 1_060
    due = handoff.perform(
        env, "review_comment_post_failed", kind="pr", number="3", title="t", text="x",
        hooks=hooks, item="3", episode="9", resume=True)
    assert due.action == handoff.RETRY and due.continuation == {"op": "pr_comment", "body": "saved"}
    assert handoff.load_retry(env, "9", "review_comment_post_failed")["attempts"] == 1


def test_category_1_exhausts_after_two_later_attempts(env, monkeypatch):
    labelled = []
    hooks, events = _hooks()
    monkeypatch.setattr(handoff, "label_ready",
                        lambda *a, **k: labelled.append(1) or MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    handoff.perform(env, "fix_push_failed", kind="pr", number="1", title="t", text="x",
                    hooks=hooks, item="1", episode="1", transient=True, continuation={"op": "push"})
    clock["now"] = 1_060
    handoff.perform(env, "fix_push_failed", kind="pr", number="1", title="t", text="x",
                    hooks=hooks, item="1", episode="1", resume=True)
    # First later attempt failed again: schedule the 300s delay.
    again = handoff.perform(env, "fix_push_failed", kind="pr", number="1", title="t", text="x",
                            hooks=hooks, item="1", episode="1", transient=True, continuation={"op": "push"})
    assert again.action == handoff.DEFER
    state = handoff.load_retry(env, "1", "fix_push_failed")
    assert state["attempts"] == 1 and state["due"] == 1_360
    clock["now"] = 1_360
    handoff.perform(env, "fix_push_failed", kind="pr", number="1", title="t", text="x",
                    hooks=hooks, item="1", episode="1", resume=True)
    # Second later attempt failed: hand off.
    done = handoff.perform(env, "fix_push_failed", kind="pr", number="1", title="t", text="x",
                           hooks=hooks, item="1", episode="1", transient=True, continuation={"op": "push"})
    assert done.action == handoff.HANDOFF and done.handed_off and labelled == [1]
    assert handoff.load_retry(env, "1", "fix_push_failed") is None
    assert events[-1][0] == "handoff"


def test_category_1_success_clears_pending_retry(env, monkeypatch):
    hooks, _ = _hooks()
    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    monkeypatch.setattr(handoff, "label_ready", lambda *a, **k: MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    handoff.perform(env, "dev_push_failed", kind="issue", number="2", title="t", text="x",
                    hooks=hooks, item="2", episode="2", transient=True, continuation={"op": "push"})
    assert handoff.load_retry(env, "2", "dev_push_failed") is not None
    handoff.clear_retry(env, "2", "dev_push_failed")
    assert handoff.load_retry(env, "2", "dev_push_failed") is None


def test_every_category_1_reason_is_rule_mode_with_retry_budget():
    for reason, entry in handoff.REASONS.items():
        if entry.category != 1:
            continue
        assert entry.mode == "rule" and entry.actions == ()
    assert "fix_description_write_failed" in handoff.REASONS
    assert handoff.REASONS["fix_description_write_failed"].category == 1


@pytest.mark.parametrize("reason", [r for r, e in handoff.REASONS.items() if e.category == 1])
def test_each_category_1_reason_defers_then_hands_off_through_perform(env, monkeypatch, reason):
    """Every table reason shares the same budget: no advisor, two later attempts, then handoff."""
    labelled, calls = [], []
    hooks, _ = _hooks()
    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    monkeypatch.setattr(handoff, "label_ready",
                        lambda *a, **k: labelled.append(1) or MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    monkeypatch.setattr(handoff.advisor, "advise", lambda *a, **k: calls.append(1) or handoff.advisor.Decision("handoff"))
    assert handoff.perform(env, reason, kind="pr", number="1", title="t", text="x",
                           hooks=hooks, item="1", episode="ep", transient=True,
                           continuation={"op": reason}).action == handoff.DEFER
    clock["now"] = 1_060
    assert handoff.perform(env, reason, kind="pr", number="1", title="t", text="x",
                           hooks=hooks, item="1", episode="ep", resume=True).action == handoff.RETRY
    assert handoff.perform(env, reason, kind="pr", number="1", title="t", text="x",
                           hooks=hooks, item="1", episode="ep", transient=True,
                           continuation={"op": reason}).action == handoff.DEFER
    clock["now"] = 1_360
    assert handoff.perform(env, reason, kind="pr", number="1", title="t", text="x",
                           hooks=hooks, item="1", episode="ep", resume=True).action == handoff.RETRY
    done = handoff.perform(env, reason, kind="pr", number="1", title="t", text="x",
                           hooks=hooks, item="1", episode="ep", transient=True,
                           continuation={"op": reason})
    assert done.handed_off and labelled == [1] and calls == []
