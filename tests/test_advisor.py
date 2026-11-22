"""baton.advisor (#17, ADR 0008): the DeepSeek routine advisor at the review and spec round limits."""
import os
import urllib.error
from dataclasses import replace
from email.message import Message
from pathlib import Path

import pytest

from baton import advisor, session
from baton.config import Advisor

KEY = "sk-" + "k" * 24   # built at run time: not a key a scanner should report


@pytest.fixture
def adv(tmp_path, monkeypatch):
    """A live advisor whose DeepSeek reply is scripted; calls and requests are recorded."""
    for name in ("state", "ctx", "logs"):
        (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setenv("STATE", str(tmp_path / "state"))
    monkeypatch.setenv("LOGS", str(tmp_path / "logs"))
    monkeypatch.setenv("CTX", str(tmp_path / "ctx"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(advisor.KEY, KEY)

    class State:
        env_file = None
        mode = "live"
        replies = []
        calls = []
        notes = []
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    state = State()
    state.replies, state.calls, state.notes = [], [], []
    monkeypatch.setattr(advisor, "settings", lambda env: Advisor(env_file=state.env_file, mode=state.mode))

    def call(cfg, key, snapshot, actions):
        state.calls.append({"key": key, "snapshot": snapshot, "actions": actions, "model": cfg.model})
        reply = state.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply, state.usage
    monkeypatch.setattr(advisor, "call_deepseek", call)
    monkeypatch.setattr(advisor.notify, "notify", lambda title, text="": state.notes.append((title, text)))
    state.env = session.resolve({"STATE": str(tmp_path / "state"), "LOGS": str(tmp_path / "logs"),
                                 "CTX": str(tmp_path / "ctx"), "GH_REPO": "owner/app", "OWNER": "owner"})
    state.records = lambda: advisor.read_records(state.env)
    return state


EXTRA = {"action": "extra_round", "reason": "only tests are missing", "evidence": ["Must fix: add a test"]}


def test_live_extra_round_is_executed_recorded_and_granted_once(adv):
    adv.replies = [EXTRA]
    d = advisor.advise(adv.env, "review_limit", 12, "snapshot", ("extra_round",), revalidate=lambda: True)
    assert d.extra_round and d.advised == "extra_round" and d.called
    [record] = adv.records()
    assert record["executed"] == "extra_round" and record["actions"] == ["handoff", "extra_round"]
    assert record["backend"] == "deepseek" and record["mode"] == "live" and record["usage"]["total_tokens"] == 15
    assert advisor.granted(adv.env, "review_limit", 12)
    # A second limit in the same episode: no call, the rule (handoff) stands.
    again = advisor.advise(adv.env, "review_limit", 12, "snapshot 2", ("extra_round",))
    assert not again.extra_round and not again.called and len(adv.calls) == 1


def test_observe_records_the_advice_and_runs_the_rule(adv):
    adv.mode = "observe"
    adv.replies = [EXTRA]
    d = advisor.advise(adv.env, "spec_limit", 7, "s", ("extra_round",))
    assert d.action == "handoff" and d.advised == "extra_round"
    assert adv.records()[0]["executed"] == "handoff" and not advisor.granted(adv.env, "spec_limit", 7)


def test_off_and_a_repository_switch_make_no_call(adv):
    adv.mode = "off"
    assert advisor.advise(adv.env, "review_limit", 1, "s", ("extra_round",)).action == "handoff"
    adv.mode = "live"
    off = replace(adv.env, repo=replace(adv.env.repo, advisor=False))   # [[repos]] advisor = false
    assert advisor.advise(off, "review_limit", 1, "s", ("extra_round",)).action == "handoff"
    assert adv.calls == [] and adv.records() == []


@pytest.mark.parametrize(("point", "result"), [
    ("review_limit", "handoff"),
    ("review_limit", "pass"),
    ("spec_limit", "accept"),
])
def test_a_new_episode_after_a_terminal_outcome_may_ask_again(adv, point, result):
    adv.replies = [EXTRA, EXTRA]
    assert advisor.advise(adv.env, point, 21, "first", ("extra_round",)).extra_round
    advisor.outcome(adv.env, point, 21, result)
    again = advisor.advise(adv.env, point, 21, "new episode", ("extra_round",))
    assert again.extra_round and again.called
    assert len(adv.calls) == 2
    assert [record["result"] for record in adv.records() if record["kind"] == "outcome"] == [result]


def test_an_identical_snapshot_may_grant_a_round_in_a_new_episode(adv):
    adv.replies = [EXTRA, EXTRA]
    assert advisor.advise(adv.env, "review_limit", 22, "same", ("extra_round",), revalidate=lambda: True).extra_round
    advisor.outcome(adv.env, "review_limit", 22, "handoff")
    repeat = advisor.advise(adv.env, "review_limit", 22, "same", ("extra_round",), revalidate=lambda: True)
    assert repeat.action == "extra_round" and len(adv.calls) == 2
    first, outcome, second = adv.records()
    assert outcome["decision_id"] == first["decision_id"]
    assert outcome["episode"] == first["episode"] != second["episode"]


@pytest.mark.parametrize("reply, failure", [
    ({"action": "merge_now", "reason": "x"}, "outside the allowed set"),
    (urllib.error.URLError("down"), "unavailable"),
    (ValueError("not json"), "malformed reply"),
    (urllib.error.HTTPError("u", 401, "no", Message(), None), "HTTP 401"),
])
def test_a_failure_hands_off_and_is_recorded(adv, reply, failure):
    adv.replies = [reply]
    d = advisor.advise(adv.env, "review_limit", 3, "s", ("extra_round",), revalidate=lambda: True)
    assert d.action == "handoff" and failure in d.failure
    assert failure in adv.records()[0]["failure"] and not advisor.granted(adv.env, "review_limit", 3)


def test_no_key_is_a_failure_without_a_call(adv, monkeypatch):
    monkeypatch.delenv(advisor.KEY)
    d = advisor.advise(adv.env, "review_limit", 4, "s", ("extra_round",))
    assert d.action == "handoff" and "no DEEPSEEK_API_KEY" in d.failure and adv.calls == []


def test_a_stale_snapshot_never_executes(adv):
    adv.replies = [EXTRA]
    d = advisor.advise(adv.env, "review_limit", 5, "s", ("extra_round",), revalidate=lambda: False)
    assert d.action == "handoff" and "stale" in d.failure
    assert not advisor.granted(adv.env, "review_limit", 5)


def test_an_unchanged_snapshot_makes_no_second_call(adv):
    adv.replies = [{"action": "handoff", "reason": "same disagreement"}]
    first = advisor.advise(adv.env, "spec_limit", 6, "same", ("extra_round",))
    second = advisor.advise(adv.env, "spec_limit", 6, "same", ("extra_round",))
    assert first.action == second.action == "handoff" and len(adv.calls) == 1


def test_secrets_never_reach_the_request_or_the_record(adv, monkeypatch):
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "planted-env-secret-value")
    snapshot = ("ghp_" + "a" * 36 + "\nAuthorization: Bearer abc.def.ghi\nhttps://user:pw@example.com/x\n"
                "api_key=zzz-secret\nsk-" + "b" * 30 + "\nplanted-env-secret-value\n"
                + f"key {KEY}\n123456789:" + "c" * 35)
    adv.replies = [{"action": "handoff", "reason": f"saw {KEY}", "evidence": ["planted-env-secret-value"]}]
    advisor.advise(adv.env, "review_limit", 8, snapshot, ("extra_round",))
    sent = adv.calls[0]["snapshot"]
    for secret in ("ghp_aaaa", "abc.def.ghi", "user:pw", "zzz-secret", "sk-bbbb", "planted-env-secret-value", KEY,
                   "cccccccccc"):
        assert secret not in sent, secret
    stored = Path(advisor.records_path(adv.env)).read_text()
    assert KEY not in stored and "planted-env-secret-value" not in stored
    log = Path(os.environ["LOGS"], "runner.log")
    assert not log.exists() or KEY not in log.read_text()


@pytest.mark.parametrize("echo_in_action", [False, True])
def test_advisor_key_from_env_file_is_redacted(adv, tmp_path, monkeypatch, echo_in_action):
    # Use an opaque value so the token patterns alone cannot redact it.
    key = "fixture-" + "opaque-value"
    monkeypatch.delenv(advisor.KEY)
    adv.env_file = tmp_path / "deepseek.env"
    adv.env_file.touch(mode=0o600)
    adv.env_file.write_text(f"{advisor.KEY}={key}\n")
    adv.replies = [{"action": key if echo_in_action else "handoff", "reason": f"saw {key}", "evidence": [key]}]
    adv.usage = {"total_tokens": key}

    decision = advisor.advise(adv.env, "review_limit", 8, f"snapshot contains {key}", ("extra_round",))

    [call] = adv.calls
    assert call["key"] == key
    assert call["snapshot"] == "snapshot contains [redacted]"
    assert decision.action == "handoff"
    assert bool(decision.failure) == echo_in_action
    assert decision.reason == "saw [redacted]"
    assert decision.evidence == ("[redacted]",)
    assert key not in advisor.handoff_note(decision)
    assert key not in advisor.records_path(adv.env).read_text()
    assert key not in Path(os.environ["LOGS"], "runner.log").read_text()


def test_the_digest_is_sent_once_a_day_with_outcomes(adv):
    adv.replies = [EXTRA]
    advisor.advise(adv.env, "review_limit", 9, "s", ("extra_round",), revalidate=lambda: True)
    advisor.outcome(adv.env, "review_limit", 9, "pass")
    now = adv.records()[-1]["ts"] + 10
    text = advisor.digest(adv.env, now=now)
    assert "Extra rounds granted 1, ended in pass/accept 1" in text and adv.notes[-1][0] == "Advisor digest"
    assert advisor.digest(adv.env, now=now + 60) == ""           # not a day yet
    assert advisor.digest(adv.env, now=now + 2 * 86400) == ""     # due, but nothing new
    assert len(adv.notes) == 1


def test_the_handoff_note_says_what_the_advisor_chose():
    d = advisor.Decision("handoff", "needs the owner", (), "handoff", "", True)
    assert advisor.handoff_note(d) == " Advisor (DeepSeek) chose handoff: needs the owner."
    assert advisor.handoff_note(advisor.Decision("handoff")) == ""
    failed = advisor.Decision("handoff", failure="HTTP 500", called=True)
    assert "no decision (HTTP 500)" in advisor.handoff_note(failed)
