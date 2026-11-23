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
        env, "spec_comments_read_failed", kind="issue", number="7", title="t", text="x",
        hooks=hooks, item="7", episode="7", transient=True,
        continuation={})
    assert result.action == handoff.DEFER and not result.handed_off
    assert labelled == [] and notes == [] and calls == [] and events == []
    state = handoff.load_retry(env, "7", "spec_comments_read_failed")
    assert state["attempts"] == 0 and state["due"] == 1_060
    assert state["continuation"] == {}


def test_category_1_permanent_failure_hands_off_immediately(env, monkeypatch):
    labelled, notes = [], []
    hooks, events = _hooks()
    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    monkeypatch.setattr(handoff, "label_ready",
                        lambda *a, **k: labelled.append(1) or MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: notes.append(1))
    result = handoff.perform(
        env, "spec_comments_read_failed", kind="issue", number="7", title="t", text="x",
        hooks=hooks, item="7", episode="7", transient=False)
    assert result.action == handoff.HANDOFF and result.handed_off
    assert labelled == [1] and notes == [1]
    assert events == [("handoff", "spec_comments_read_failed", "--reason", "spec_comments_read_failed")]
    assert handoff.load_retry(env, "7", "spec_comments_read_failed") is None


def review_binding(pr):
    return {"pr": pr, "round": 1, "head": "a" * 40, "base": "main", "digest": "b" * 64,
            "issue": "9", "screenshot_digest": ""}


def post_binding(pr, body="<!-- baton-review round=1 by=claude -->\npass"):
    return {"pr": pr, "round": 1, "head": "a" * 40, "base": "main", "issue": "9",
            "body": body, "digest": handoff.body_digest(body), "description": "c" * 64,
            "feedback": "d" * 64, "outcome": "REVIEW: changes", "run_id": "",
            "comment_ids": [], "author": "owner", "reviewer": "claude"}


def test_category_1_retry_waits_until_due_then_consumes_an_attempt(env, monkeypatch):
    hooks, _ = _hooks()
    monkeypatch.setattr(handoff, "label_ready", lambda *a, **k: MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    first = handoff.perform(
        env, "review_comment_missing", kind="pr", number="3", title="t", text="x",
        hooks=hooks, item="3", episode="9", transient=True,
        continuation=review_binding("3"))
    assert first.action == handoff.DEFER
    clock["now"] = 1_059
    early = handoff.perform(
        env, "review_comment_missing", kind="pr", number="3", title="t", text="x",
        hooks=hooks, item="3", episode="9", resume=True)
    assert early.action == handoff.DEFER
    assert handoff.load_retry(env, "9", "review_comment_missing", "3")["attempts"] == 0
    clock["now"] = 1_060
    due = handoff.perform(
        env, "review_comment_missing", kind="pr", number="3", title="t", text="x",
        hooks=hooks, item="3", episode="9", resume=True)
    assert due.action == handoff.RETRY and due.continuation == review_binding("3")
    assert handoff.load_retry(env, "9", "review_comment_missing", "3")["attempts"] == 1
    # A tick that stopped after consuming the attempt still observes the next delay.
    restarted = handoff.perform(
        env, "review_comment_missing", kind="pr", number="3", title="t", text="x",
        hooks=hooks, item="3", episode="9", resume=True)
    assert restarted.action == handoff.DEFER
    assert handoff.load_retry(env, "9", "review_comment_missing", "3")["attempts"] == 1


@pytest.mark.parametrize("restart", [False, True])
def test_category_1_exhausts_after_two_later_attempts(env, monkeypatch, restart):
    labelled = []
    hooks, events = _hooks()
    monkeypatch.setattr(handoff, "label_ready",
                        lambda *a, **k: labelled.append(1) or MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    handoff.perform(env, "spec_comments_read_failed", kind="pr", number="1", title="t", text="x",
                    hooks=hooks, item="1", episode="1", transient=True, continuation={})
    clock["now"] = 1_060
    handoff.perform(env, "spec_comments_read_failed", kind="pr", number="1", title="t", text="x",
                    hooks=hooks, item="1", episode="1", resume=True)
    # First later attempt failed again: schedule the 300s delay.
    again = handoff.perform(env, "spec_comments_read_failed", kind="pr", number="1", title="t", text="x",
                            hooks=hooks, item="1", episode="1", transient=True, continuation={})
    assert again.action == handoff.DEFER
    state = handoff.load_retry(env, "1", "spec_comments_read_failed")
    assert state["attempts"] == 1 and state["due"] == 1_360
    clock["now"] = 1_360
    handoff.perform(env, "spec_comments_read_failed", kind="pr", number="1", title="t", text="x",
                    hooks=hooks, item="1", episode="1", resume=True)
    # The second grant uses up the budget even if the process stops before reporting failure.
    done = handoff.perform(env, "spec_comments_read_failed", kind="pr", number="1", title="t", text="x",
                           hooks=hooks, item="1", episode="1", transient=True, continuation={},
                           resume=restart)
    assert done.action == handoff.HANDOFF and done.handed_off and labelled == [1]
    assert handoff.load_retry(env, "1", "spec_comments_read_failed") is None
    assert events[-1][0] == "handoff"


def test_a_saved_retry_that_its_caller_did_not_resume_hands_off_instead_of_looping(env, monkeypatch):
    hooks, _ = _hooks()
    labelled = []
    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    monkeypatch.setattr(handoff, "label_ready", lambda *a, **k: labelled.append(1) or MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    kwargs = {"kind": "issue", "number": "2", "title": "t", "text": "x", "hooks": hooks, "item": "2",
              "episode": "2", "transient": True, "continuation": {}}
    assert handoff.perform(env, "spec_comments_read_failed", **kwargs).action == handoff.DEFER
    assert handoff.perform(env, "spec_comments_read_failed", **kwargs).action == handoff.HANDOFF   # never resumed
    assert labelled == [1]


def test_a_terminal_handoff_clears_every_pending_retry_of_the_episode(env, monkeypatch):
    hooks, _ = _hooks()
    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    monkeypatch.setattr(handoff, "label_ready", lambda *a, **k: MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    common = {"kind": "pr", "number": "1", "title": "t", "text": "x", "hooks": hooks, "item": "1", "episode": "ep"}
    handoff.perform(env, "spec_comments_read_failed", transient=True, continuation={}, **common)
    handoff.perform(env, "spec_pending_adr_read_failed", transient=True, continuation={"step": "discovery"}, **common)
    assert handoff.perform(env, "fix_run_failed", **common).action == handoff.HANDOFF
    assert handoff.load_retry(env, "ep", "spec_comments_read_failed", "1") is None
    assert handoff.load_retry(env, "ep", "spec_pending_adr_read_failed", "1") is None


def test_review_retries_for_two_prs_on_one_issue_are_independent(env, monkeypatch):
    hooks, _ = _hooks()
    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    monkeypatch.setattr(handoff, "label_ready", lambda *a, **k: MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    reason = "review_comment_missing"

    def fail(pr, transient):
        return handoff.perform(env, reason, kind="pr", number=pr, title="t", text="x", hooks=hooks,
                               item=pr, episode="9", transient=transient, continuation=review_binding(pr))

    assert fail("3", True).action == fail("4", True).action == handoff.DEFER
    assert fail("3", False).action == handoff.HANDOFF
    assert handoff.load_retry(env, "9", reason, "3") is None
    assert handoff.load_retry(env, "9", reason, "4")["continuation"] == review_binding("4")


@pytest.mark.parametrize("damage", ["reason", "episode", "deadline", "binding"])
def test_damage_to_a_waiting_review_hands_off_once(env, monkeypatch, damage):
    import json

    hooks, events = _hooks()
    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    monkeypatch.setattr(handoff, "label_ready", lambda *a, **k: MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    reason = "review_comment_missing"
    common = {"kind": "pr", "number": "3", "title": "t", "text": "x", "hooks": hooks,
              "item": "3", "episode": "9"}
    assert handoff.perform(env, reason, transient=True, continuation=review_binding("3"), **common).action == handoff.DEFER
    path = handoff.retry_path(env, "9", reason, "3")
    saved = json.loads(path.read_text())
    if damage == "deadline":
        saved.update(due=100_060, scheduled=100_000)
    elif damage == "binding":
        del saved["continuation"]["digest"]
        checkpoint = handoff.waiting_path(env, "9", reason, "3")
        checkpoint.write_text(json.dumps({key: saved[key] for key in ("reason", "episode", "item", "continuation")}))
    else:
        saved[damage] = "different"
    path.write_text(json.dumps(saved))
    assert handoff.perform(env, reason, resume=True, **common).action == handoff.HANDOFF
    assert events == [("handoff", reason, "--reason", reason)]
    assert handoff.load_retry(env, "9", reason, "3") is None


def spec_post_binding(issue="7", *, reply=False):
    marker = "baton-spec-reply" if reply else "baton-spec-review"
    body = f"<!-- {marker} round=1 by=claude -->\npoints"
    return {"issue": issue, "pr": "", "round": 1, "reviewer": "claude", "author": "owner",
            "body": body, "digest": handoff.body_digest(body), "issue_body": "c" * 64,
            "adr_head": "", "feedback": "d" * 64, "comment_ids": [],
            "next": "review" if reply else "revise", "reply": "points", "max_rounds": 3,
            "mode_note": "", "limit_why": "still has points", "run_id": ""}


def accept_post_binding(issue="7", *, pending=False):
    verdict = ("<!-- baton-verdict: accept by=claude -->\n"
               "## Spec review (claude), round 1: accept\n\nall good")
    if pending:
        body = f"<!-- baton-spec-accept-pending pr=77 head={'a' * 40} -->\n{verdict}"
        next_step, pr, head = "merge", "77", "a" * 40
    else:
        body, next_step, pr, head = verdict, "transition", "", ""
    return {"issue": issue, "pr": pr, "round": 1, "reviewer": "claude", "author": "owner",
            "body": body, "verdict": verdict, "digest": handoff.body_digest(body),
            "issue_body": "c" * 64, "adr_head": head, "feedback": "d" * 64, "comment_ids": [],
            "next": next_step}


def test_every_category_1_reason_is_rule_mode_with_retry_budget():
    assert handoff.RETRY_REASONS == {
        "review_comment_missing", "spec_comments_read_failed", "spec_pending_adr_read_failed",
        "review_comment_post_failed", "screenshot_comment_post_failed",
        "spec_review_comment_post_failed", "spec_reply_post_failed",
        "spec_pending_accept_post_failed", "spec_accept_post_failed",
    }
    for reason in handoff.RETRY_REASONS:
        entry = handoff.REASONS[reason]
        assert (entry.category, entry.mode, entry.actions) == (1, "rule", ())


@pytest.mark.parametrize("reason,binding", [
    ("review_comment_missing", review_binding("1")),
    ("spec_comments_read_failed", {}),
    ("spec_pending_adr_read_failed", {"step": "discovery"}),
    ("review_comment_post_failed", post_binding("1")),
    ("screenshot_comment_post_failed",
     post_binding("1", body="<!-- baton-screenshot-review round=1 by=claude -->\npass")),
    ("spec_review_comment_post_failed", spec_post_binding("1")),
    ("spec_reply_post_failed", spec_post_binding("1", reply=True)),
    ("spec_pending_accept_post_failed", accept_post_binding("1", pending=True)),
    ("spec_accept_post_failed", accept_post_binding("1")),
])
def test_each_category_1_reason_defers_then_hands_off_through_perform(env, monkeypatch, reason, binding):
    """Every replayed read or comment post shares the same budget: no advisor, two later attempts, then handoff."""
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
                           continuation=binding).action == handoff.DEFER
    clock["now"] = 1_060
    assert handoff.perform(env, reason, kind="pr", number="1", title="t", text="x",
                           hooks=hooks, item="1", episode="ep", resume=True).action == handoff.RETRY
    assert handoff.perform(env, reason, kind="pr", number="1", title="t", text="x",
                           hooks=hooks, item="1", episode="ep", transient=True,
                           continuation=binding).action == handoff.DEFER
    clock["now"] = 1_360
    assert handoff.perform(env, reason, kind="pr", number="1", title="t", text="x",
                           hooks=hooks, item="1", episode="ep", resume=True).action == handoff.RETRY
    done = handoff.perform(env, reason, kind="pr", number="1", title="t", text="x",
                           hooks=hooks, item="1", episode="ep", transient=True,
                           continuation=binding)
    assert done.handed_off and labelled == [1] and calls == []


def test_a_comment_post_binding_without_its_body_hands_off(env, monkeypatch):
    hooks, _ = _hooks()
    labelled = []
    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    monkeypatch.setattr(handoff, "label_ready", lambda *a, **k: labelled.append(1) or MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    broken = post_binding("1")
    del broken["body"]
    result = handoff.perform(env, "review_comment_post_failed", kind="pr", number="1", title="t", text="x",
                             hooks=hooks, item="1", episode="ep", transient=True, continuation=broken)
    assert result.handed_off and labelled == [1]


def test_a_damaged_retry_record_hands_off_without_replaying(env, monkeypatch):
    """An unreadable retry must fail closed: no fresh budget, no lookup replay."""
    labelled = []
    hooks, events = _hooks()
    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    monkeypatch.setattr(handoff, "label_ready",
                        lambda *a, **k: labelled.append(1) or MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    path = handoff.retry_path(env, "7", "spec_comments_read_failed")
    path.write_text("{not-json")
    with pytest.raises(handoff.RetryUnreadable):
        handoff.load_retry(env, "7", "spec_comments_read_failed")
    # Resume after a restart that finds the damaged record: hand off, never RETRY.
    done = handoff.perform(env, "spec_comments_read_failed", kind="issue", number="7", title="t", text="x",
                           hooks=hooks, item="7", episode="7", resume=True)
    assert done.action == handoff.HANDOFF and done.handed_off and labelled == [1]
    assert events[-1][0] == "handoff"
    assert handoff.load_retry(env, "7", "spec_comments_read_failed") is None

    path.write_text('["not", "a", "dict"]')
    scheduled = handoff.perform(env, "spec_comments_read_failed", kind="issue", number="7", title="t", text="x",
                                hooks=hooks, item="7", episode="7", transient=True, continuation={})
    assert scheduled.action == handoff.HANDOFF
    assert handoff.load_retry(env, "7", "spec_comments_read_failed") is None


@pytest.mark.parametrize("payload", [
    {"due": 1_060, "continuation": {}, "reason": "spec_comments_read_failed", "episode": "7"},
    {"attempts": "1", "due": 1_060, "continuation": {}, "reason": "spec_comments_read_failed", "episode": "7"},
    {"attempts": -1, "due": 1_060, "continuation": {}, "reason": "spec_comments_read_failed", "episode": "7"},
    {"attempts": 3, "due": 1_060, "continuation": {}, "reason": "spec_comments_read_failed", "episode": "7"},
    {"attempts": 0, "continuation": {}, "reason": "spec_comments_read_failed", "episode": "7"},
    {"attempts": 0, "due": "soon", "continuation": {}, "reason": "spec_comments_read_failed", "episode": "7"},
    {"attempts": 0, "due": 1_060, "continuation": [], "reason": "spec_comments_read_failed", "episode": "7"},
])
def test_a_malformed_retry_record_hands_off_without_defaults(env, monkeypatch, payload):
    """Missing, mistyped or out-of-range fields fail closed; nothing defaults to zero (#151)."""
    import json
    labelled = []
    hooks, _ = _hooks()
    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    monkeypatch.setattr(handoff, "label_ready",
                        lambda *a, **k: labelled.append(1) or MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    path = handoff.retry_path(env, "7", "spec_comments_read_failed")
    path.write_text(json.dumps(payload))
    with pytest.raises(handoff.RetryUnreadable):
        handoff.load_retry(env, "7", "spec_comments_read_failed")
    done = handoff.perform(env, "spec_comments_read_failed", kind="issue", number="7", title="t", text="x",
                           hooks=hooks, item="7", episode="7", resume=True)
    assert done.action == handoff.HANDOFF and labelled == [1]
    assert handoff.load_retry(env, "7", "spec_comments_read_failed") is None


@pytest.mark.parametrize("damage", ["directory", "dangling link"])
def test_a_non_file_retry_path_hands_off(env, monkeypatch, tmp_path, damage):
    """A retry path that exists but is not a regular file fails closed (#151)."""
    labelled = []
    hooks, _ = _hooks()
    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    monkeypatch.setattr(handoff, "label_ready",
                        lambda *a, **k: labelled.append(1) or MagicMock(returncode=0))
    monkeypatch.setattr(handoff.notify, "notify", lambda *a, **k: None)
    path = handoff.retry_path(env, "7", "spec_comments_read_failed")
    if damage == "directory":
        path.mkdir()
        (path / "child").write_text("damaged retry")
    else:
        path.symlink_to(tmp_path / "missing")
    with pytest.raises(handoff.RetryUnreadable):
        handoff.load_retry(env, "7", "spec_comments_read_failed")
    done = handoff.perform(env, "spec_comments_read_failed", kind="issue", number="7", title="t", text="x",
                           hooks=hooks, item="7", episode="7", resume=True)
    assert done.action == handoff.HANDOFF and labelled == [1]
