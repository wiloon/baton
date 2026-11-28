"""baton.advisor backends (#236, ADR 0008): Claude Code first, DeepSeek as the fallback.

The claude process is scripted below the call function (`retrospective_analyst.bounded_process`);
no test here starts the real `claude`.
"""
import contextlib
import json
import os
import time
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from baton import advisor, quota, retrospective_analyst, vendor_policy
from baton.config import Advisor
from conftest import in_flight
from test_advisor import EXCERPT, EXTRA, adv, answer_reply, questions_snapshot  # noqa: F401  (adv is a fixture)

CLAUDE_USAGE = {"input_tokens": 100, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 3,
                "output_tokens": 7}
NORMALIZED = {"prompt_tokens": 123, "completion_tokens": 7, "total_tokens": 130}


def stream(*rows):
    return "".join(json.dumps(row) + "\n" for row in rows).encode()


def claude_rows(reply=EXTRA, *, text=None, subtype="success", is_error=False, extra=()):
    later = int(time.time()) + 3600
    windows = {"five_hour": {"utilization": 0.2, "resetsAt": later},
               "seven_day": {"utilization": 0.1, "resetsAt": later}}
    return stream(
        {"type": "system", "subtype": "init", "model": "claude-opus-4-9"},
        {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": windows}},
        *extra,
        {"type": "result", "subtype": subtype, "is_error": is_error, "usage": CLAUDE_USAGE,
         "result": json.dumps(reply) if text is None else text, "modelUsage": {"claude-opus-4-9": {}}})


def ran(raw=None, *, status="success", rc=0, stderr=""):
    return status, claude_rows() if raw is None else raw, rc, ({"error_text": stderr} if stderr else {})


@pytest.fixture
def dual(adv, monkeypatch):  # noqa: F811
    """The default backend order, with each claude process result scripted."""
    adv.claude, adv.processes, adv.overrides = [], [], {}
    monkeypatch.setattr(advisor, "settings", lambda env: replace(
        Advisor(env_file=adv.env_file, mode=adv.mode), **adv.overrides))

    def process(command, prompt, workspace, env, seconds, max_bytes):
        adv.processes.append({"command": command, "prompt": prompt, "cwd": Path(workspace),
                              "listing": list(Path(workspace).iterdir()), "env": dict(env), "seconds": seconds})
        return adv.claude.pop(0)
    monkeypatch.setattr(retrospective_analyst, "bounded_process", process)
    return adv


def decide(dual, item=12, **options):
    options.setdefault("revalidate", lambda: True)
    return advisor.advise(dual.env, "review_limit", item, options.pop("snapshot", f"snapshot {item}"),
                          ("extra_round",), **options)


def state_dir(dual):
    return dual.env.config.paths.state


def test_claude_decides_first_and_needs_no_deepseek_key(dual, monkeypatch):
    monkeypatch.delenv(advisor.KEY)
    dual.claude = [ran()]
    d = decide(dual)
    assert d.extra_round and d.backend == "claude" and d.failure == "" and dual.calls == []
    [record] = dual.records()
    assert record["backend"] == "claude" and record["model"] == "opus"
    assert record["actual_model"] == "claude-opus-4-9" and record["usage"] == NORMALIZED
    assert record["failure"] == "" and "fallback_from" not in record


def undeclare(dual):
    config = dual.env.config
    dual.env = replace(dual.env, config=replace(config, vendors=tuple(v for v in config.vendors if v.id != "claude")))


def used(dual, window, share):
    quota.record(state_dir(dual), "claude", {window: {"used": share, "resets_at": int(time.time()) + 3600}},
                 time.time())


@pytest.mark.parametrize("block, reason", [
    (undeclare, "not declared"),
    (lambda dual: used(dual, "seven_day", 1.0), "week 100%"),
    (lambda dual: used(dual, "five_hour", 1.0), "5h 100%"),
    (lambda dual: vendor_policy.rest(state_dir(dual), "claude", int(time.time()) + 600), "cooldown until"),
    (lambda dual: quota.credential(state_dir(dual), "claude", "auth_expired", time.time()), "credential expired"),
], ids=["undeclared", "cap", "five-hour", "cooldown", "credential"])
def test_an_unavailable_claude_is_skipped_without_a_call(dual, block, reason):
    block(dual)
    dual.replies = [EXTRA]
    d = decide(dual)
    assert d.extra_round and d.backend == "deepseek" and dual.processes == []
    [record] = dual.records()
    assert record["backend"] == "deepseek" and record["model"] == "deepseek-flash" and record["failure"] == ""
    first = record["fallback_from"]
    assert first["backend"] == "claude" and first["model"] == "opus" and reason in first["failure"]
    assert first["latency_ms"] == 0 and first["usage"] == {}


@pytest.mark.parametrize("result, failure", [
    (("unavailable", b"", 127, {}), "could not start"),
    (("success", b"", 127, {"error_text": "timeout: failed to run command"}), "exit 127"),
    (("timeout", b"", 124, {}), "timeout"),
    (("output_limit", b"", -9, {}), "output"),
    (("success", claude_rows(subtype="error_during_execution", is_error=True), 0, {}), "error result"),
    (("success", claude_rows(extra=({"type": "tool_use"},)), 0, {}), "used a tool"),
    (("success", claude_rows(text="not json {"), 0, {}), "malformed reply"),
    (("success", claude_rows(["extra_round"]), 0, {}), "malformed reply"),
    (("success", claude_rows({"action": "extra_round", "evidence": []}), 0, {}), "malformed reply"),
    (("success", claude_rows({"action": "merge_now", "reason": "x", "evidence": []}), 0, {}), "outside the allowed"),
], ids=["spawn", "exit", "timeout", "output", "error-result", "tool", "unparseable", "not-object", "schema",
        "outside-set"])
def test_a_claude_failure_falls_back_to_deepseek_once(dual, result, failure):
    dual.claude, dual.replies = [result], [EXTRA]
    d = decide(dual)
    assert d.extra_round and d.backend == "deepseek" and d.failure == ""
    assert len(dual.processes) == 1 and len(dual.calls) == 1
    [record] = dual.records()
    assert record["backend"] == "deepseek" and record["usage"] == dual.usage
    assert record["fallback_from"]["backend"] == "claude" and failure in record["fallback_from"]["failure"]
    assert set(record["fallback_from"]) == {"backend", "model", "failure", "latency_ms", "usage"}


def ask(dual):
    return advisor.advise(dual.env, "spec_questions", 12, questions_snapshot([f"[c1] (issue body): {EXCERPT}"]),
                          ("answer_spec_questions",), category=2, revalidate=lambda: True)


def test_invalid_claude_answers_fall_back(dual):
    dual.claude = [ran(claude_rows(answer_reply("not in any citation")[0]))]
    dual.replies = answer_reply()
    d = ask(dual)
    assert d.action == "answer_spec_questions" and d.backend == "deepseek"
    assert "not taken from its citations" in dual.records()[0]["fallback_from"]["failure"]


def test_non_grounded_claude_answers_do_not_fall_back(dual):
    dual.claude = [ran(claude_rows(answer_reply("Prefer the gate.", "reversible_default", ())[0]))]
    d = ask(dual)
    assert d.action == "handoff" and d.backend == "claude" and d.failure == "" and dual.calls == []


@pytest.mark.parametrize("reply, fresh", [
    ({"action": "handoff", "reason": "the owner decides", "evidence": []}, True),
    (EXTRA, False),
], ids=["handoff", "stale"])
def test_a_handoff_choice_and_a_stale_snapshot_do_not_fall_back(dual, reply, fresh):
    dual.claude = [ran(claude_rows(reply))]
    d = decide(dual, revalidate=lambda: fresh)
    assert d.action == "handoff" and d.backend == "claude" and dual.calls == []
    assert "fallback_from" not in dual.records()[0]


def test_a_fenced_claude_reply_is_accepted(dual):
    dual.claude = [ran(claude_rows(text="```json\n" + json.dumps(EXTRA) + "\n```"))]
    d = decide(dual)
    assert d.extra_round and d.backend == "claude" and dual.calls == []


@pytest.mark.parametrize("condition", ["run in flight", "stale", "missing", "invalid"])
def test_a_run_in_flight_and_unknown_quota_still_try_claude(dual, condition):
    if condition == "stale":
        quota.record(state_dir(dual), "claude", {"five_hour": {"used": 1.0, "resets_at": 1}}, 1)
    if condition == "invalid":
        Path(state_dir(dual), "quota-claude.json").write_text("[1]")
    dual.claude = [ran()]
    with in_flight(state_dir(dual), "claude") if condition == "run in flight" else contextlib.nullcontext():
        d = decide(dual)
    assert d.backend == "claude" and len(dual.processes) == 1


def test_a_successful_call_records_quota_windows_and_the_credential(dual):
    dual.claude = [ran()]
    decide(dual)
    windows = quota.load(state_dir(dual), "claude")["windows"]
    assert windows["five_hour"]["used"] == 0.2 and windows["seven_day"]["used"] == 0.1
    assert quota.credential_current(state_dir(dual), "claude", time.time())


def test_a_quota_rejection_cools_claude_down_for_the_next_decision(dual):
    resets = int(time.time()) + 7200
    raw = stream(
        {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "rateLimitType": "five_hour", "resetsAt": resets}},
        {"type": "result", "subtype": "success", "is_error": True,
         "result": f"Claude usage limit reached. resets_at: {resets}"})
    dual.claude, dual.replies = [ran(raw, rc=1)], [EXTRA, EXTRA]
    decide(dual, 1)
    assert vendor_policy.cooldown_until(state_dir(dual), "claude", time.time()) is not None
    decide(dual, 2)
    assert len(dual.processes) == 1
    assert "cooldown until" in dual.records()[-1]["fallback_from"]["failure"]


@pytest.mark.parametrize("message, expired", [("Failed to authenticate. API Error: 401", True),
                                             ("API Error: 529 overloaded_error", False)], ids=["auth", "busy"])
def test_auth_and_busy_rejections_set_a_cooldown(dual, message, expired):
    dual.claude, dual.replies = [ran(claude_rows(text=message, is_error=True), rc=1)], [EXTRA]
    decide(dual)
    until = vendor_policy.cooldown_until(state_dir(dual), "claude", time.time())
    assert until and quota.credential_expired(state_dir(dual), "claude") is expired
    if not expired:
        assert until <= time.time() + dual.env.config.models.busy_minutes * 60 + 5


def test_an_accounting_failure_keeps_the_reply(dual, monkeypatch):
    def fail(*args):
        raise OSError("read-only")
    monkeypatch.setattr(quota, "record", fail)
    dual.claude = [ran()]
    d = decide(dual)
    assert d.extra_round and d.backend == "claude" and d.failure == ""


def test_both_backends_failing_runs_the_rule_and_records_both(dual):
    dual.claude, dual.replies = [("timeout", b"", 124, {})], [urllib.error.URLError("down")]
    d = decide(dual)
    assert d.action == "handoff" and d.backend == "deepseek" and "unavailable: URLError" in d.failure
    [record] = dual.records()
    assert record["backend"] == "deepseek" and "unavailable: URLError" in record["failure"]
    assert "timeout" in record["fallback_from"]["failure"]
    assert advisor.handoff_note(d).startswith(" Advisor (DeepSeek): no decision")


def test_an_unusable_fallback_keeps_the_first_backend(dual, monkeypatch):
    monkeypatch.delenv(advisor.KEY)
    dual.claude = [("timeout", b"", 124, {})]
    d = decide(dual)
    assert d.action == "handoff" and d.backend == "claude"
    [record] = dual.records()
    assert record["backend"] == "claude" and record["model"] == "opus" and "fallback_from" not in record
    assert "timeout" in record["failure"]
    assert record["failure"].endswith("; fallback unavailable: no DEEPSEEK_API_KEY")
    assert advisor.handoff_note(d).startswith(" Advisor (Claude): no decision")


def test_no_usable_backend_names_the_first_in_order(dual, monkeypatch):
    monkeypatch.delenv(advisor.KEY)
    undeclare(dual)
    d = decide(dual)
    [record] = dual.records()
    assert d.backend == record["backend"] == "claude" and "fallback_from" not in record
    assert record["failure"] == "claude is not declared; fallback unavailable: no DEEPSEEK_API_KEY"


def test_an_unreadable_key_file_only_disables_deepseek(dual, tmp_path, monkeypatch):
    monkeypatch.delenv(advisor.KEY)
    dual.env_file = tmp_path / "deepseek.env"
    dual.env_file.write_text(f"{advisor.KEY}=x\n")
    dual.env_file.chmod(0o644)
    dual.claude = [ran()]
    assert decide(dual).failure == "" and dual.records()[0]["failure"] == ""


def test_the_configured_order_and_a_single_backend_are_followed(dual):
    dual.overrides = {"backend_order": ("deepseek", "claude")}
    dual.replies, dual.claude = [urllib.error.URLError("down")], [ran()]
    d = decide(dual, 1)
    assert d.backend == "claude" and dual.records()[0]["fallback_from"]["backend"] == "deepseek"
    dual.overrides = {"backend_order": ("claude",)}
    dual.claude = [("timeout", b"", 124, {})]
    d = decide(dual, 2)
    assert d.action == "handoff" and d.backend == "claude" and len(dual.calls) == 1
    assert "fallback_from" not in dual.records()[-1]


def test_deepseek_only_records_no_new_fields(dual):
    dual.overrides = {"backend_order": ("deepseek",)}
    dual.replies = [EXTRA]
    d = decide(dual)
    assert d.extra_round and dual.processes == []
    assert set(dual.records()[0]) == {
        "kind", "ts", "point", "repo", "item", "task_kind", "episode", "decision_id", "snapshot_sha", "actions",
        "backend", "model", "prompt_version", "mode", "rule", "advised", "reason", "evidence", "executed",
        "failure", "latency_ms", "usage"}


def test_the_claude_call_is_isolated_and_the_snapshot_stays_off_argv(dual, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "real-gh-token-value-123")
    dual.claude = [ran()]
    decide(dual, snapshot="a snapshot line to look for")
    [call] = dual.processes
    command = call["command"]
    assert command == [
        "claude", "--safe-mode", "-p", "--model", "opus", "--effort", "medium", "--system-prompt", advisor.SYSTEM,
        "--output-format", "stream-json", "--verbose", "--tools", "", "--disallowedTools", "*",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands",
        "--setting-sources", "", "--settings", '{"disableAllHooks":true}', "--permission-mode", "dontAsk",
        "--no-session-persistence", "--max-turns", "1"]
    assert not any("a snapshot line" in part for part in command)
    assert call["prompt"] == ('Allowed actions: ["handoff", "extra_round"]\n\n'
                              "<snapshot>\na snapshot line to look for\n</snapshot>")
    assert call["listing"] == [] and not call["cwd"].exists() and call["seconds"] == 60
    child = call["env"]
    assert advisor.KEY not in child and child["GH_TOKEN"] == child["GITHUB_TOKEN"] == "invalid"
    assert "GH_REPO" not in child and "STATE" not in child
    assert child["BATON_INVOCATION_KIND"] == "advisor" and child["CLAUDE_CODE_ENABLE_TELEMETRY"] == "0"


def test_both_backends_get_the_same_instructions(dual):
    dual.claude = [ran(claude_rows(answer_reply()[0]))]
    ask(dual)
    command = dual.processes[0]["command"]
    assert command[command.index("--system-prompt") + 1] == advisor.SYSTEM + "\n" + advisor.QUESTIONS_SYSTEM


def test_a_reply_task_gives_claude_its_own_system_text(dual):
    dual.claude = [ran(claude_rows({"action": "record_profile", "reason": "r", "evidence": [], "profile": {}}))]
    task = advisor.ReplyTask("record_profile", "PROFILE THIS", "intake-1", lambda raw, values: {"seen": raw})
    d = advisor.advise(dual.env, "intake", 12, "a snapshot", ("record_profile",), task_kind="issue",
                       rule="standard_tier", mode="observe", task=task)
    command = dual.processes[0]["command"]
    assert command[command.index("--system-prompt") + 1] == "PROFILE THIS"
    assert d.backend == "claude" and d.profile == {"seen": {}} and d.action == "standard_tier"


def test_the_notes_name_the_backend_that_answered(dual):
    dual.claude = [ran(claude_rows({"action": "handoff", "reason": "needs the owner", "evidence": []}))]
    d = decide(dual)
    assert advisor.handoff_note(d) == " Advisor (Claude) chose handoff: needs the owner."
    again = decide(dual)   # same snapshot: rebuilt from the saved record
    assert again.previous and again.backend == "claude" and "Advisor (Claude)" in advisor.handoff_note(again)


@pytest.mark.parametrize("saved, label", [({"backend": "claude"}, "Advisor (Claude)"), ({}, "Advisor (DeepSeek)")])
def test_a_rebuilt_decision_names_the_recorded_backend(dual, saved, label):
    advisor.append(dual.env, {"kind": "decision", "point": "review_limit", "repo": "owner/app", "item": "12",
                              "task_kind": "pr", "decision_id": "d1", "episode": "d1", "rule": "handoff",
                              "executed": "extra_round", "reason": "r", **saved})
    assert label in advisor.handoff_note(decide(dual))


def test_a_grant_file_alone_names_no_backend(dual):
    path = advisor.grant_path(dual.env, "review_limit", 12)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("d1\n")
    d = decide(dual)
    assert d.backend == "" and advisor.handoff_note(d).startswith(" Advisor: no decision")


def test_the_digest_counts_fallback_usage_and_lists_backends(dual):
    dual.claude = [ran(claude_rows(text="nope")), ran()]
    dual.replies = [EXTRA]
    decide(dual, 1)
    decide(dual, 2)
    text = advisor.digest(dual.env, now=time.time() + 1)
    assert f"{130 + 130 + 15} total tokens" in text
    assert "Tokens by backend: claude 260, deepseek 15." in text


def test_the_digest_without_claude_lists_no_backends(dual):
    dual.overrides = {"backend_order": ("deepseek",)}
    dual.replies = [EXTRA]
    decide(dual)
    text = advisor.digest(dual.env, now=time.time() + 1)
    assert "15 total tokens" in text and "by backend" not in text


@pytest.mark.parametrize("second_quotes, top", [([], None), (["snapshot 12", "second miss"], "second miss")],
                         ids=["valid-second", "second-drops"])
def test_a_rejected_first_backend_keeps_its_miss_in_fallback_from(dual, second_quotes, top):
    dual.claude = [ran(claude_rows({**EXTRA, "evidence": ["first\nmiss"]}))]
    dual.replies = [{**EXTRA, "evidence": second_quotes}]
    d = decide(dual)
    assert d.extra_round and d.backend == "deepseek" and len(dual.calls) == 1
    [record] = dual.records()
    assert record["fallback_from"]["evidence_miss"] == "first\nmiss"
    assert "evidence_dropped" not in record["fallback_from"]
    assert record.get("evidence_miss") == top and record.get("evidence_dropped") == (1 if top else None)
    log = Path(os.environ["LOGS"], "runner.log").read_text()
    assert "fallback_evidence_miss='first\\nmiss'" in log
    assert (" evidence_miss=" in log) == bool(top)


def test_a_first_backend_that_dropped_quotes_and_failed_keeps_the_count_in_fallback_from(dual):
    reply = {**EXTRA, "action": "merge_now", "evidence": ["snapshot 12", "first miss"]}
    dual.claude = [ran(claude_rows(reply))]
    dual.replies = [EXTRA]
    assert decide(dual).extra_round
    [record] = dual.records()
    assert record["fallback_from"]["evidence_dropped"] == 1
    assert record["fallback_from"]["evidence_miss"] == "first miss"
    assert "evidence_dropped" not in record and "evidence_miss" not in record
