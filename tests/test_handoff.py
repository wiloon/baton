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
    assert notes == [("PR #1 review passed", "auto-merge is off Reason auto_merge_off (category 4).")]
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


def test_a_live_worker_reason_without_offered_actions_still_hands_off(env, monkeypatch):
    """Unwired callers keep today's handoff: live retry is opt-in via observed_actions."""
    labelled, calls = [], []

    class Hooks:
        def task(self, kind, key, *args):
            pass

    monkeypatch.setattr(handoff, "label_ready",
                        lambda *a, **k: labelled.append(1) or MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    monkeypatch.setattr(handoff.advisor, "advise", lambda *a, **k: calls.append(1) or handoff.advisor.Decision("handoff"))
    monkeypatch.setattr(handoff.advisor, "settings", lambda e: Advisor(env_file=None, mode="live"))
    result = handoff.perform(env, "review_run_failed", kind="pr", number="1",
                             title="t", text="failed", hooks=Hooks())
    assert result.handed_off and labelled == [1] and calls == []


def test_a_live_retry_step_is_returned_when_the_caller_offers_it(env, monkeypatch):
    labelled, offered = [], []

    class Hooks:
        def task(self, kind, key, *args):
            pass

    monkeypatch.setattr(handoff, "label_ready", lambda *a, **k: labelled.append(1))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    monkeypatch.setattr(handoff.advisor, "settings", lambda e: Advisor(env_file=None, mode="live"))
    monkeypatch.setattr(handoff.advisor, "enabled", lambda e: True)

    def advise(env, reason, number, snapshot, actions, **kw):
        offered.append(actions)
        assert kw.get("category") == 2
        assert kw.get("mode") == "live"
        assert snapshot == "snap"
        return handoff.advisor.Decision("retry_step", "try again", (), "retry_step", "", True)

    monkeypatch.setattr(handoff.advisor, "advise", advise)
    result = handoff.perform(
        env, "review_run_failed", kind="pr", number="9", title="t", text="failed",
        hooks=Hooks(), snapshot="snap", revalidate=lambda: True,
        observed_actions=("retry_step", "retry_step_other_vendor"))
    assert result.action == "retry_step" and not result.handed_off and labelled == []
    assert offered == [("retry_step", "retry_step_other_vendor")]
