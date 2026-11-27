"""The intake decision point (#238, ADR 0009), in observation mode.

Everything enters through dev.run on the devrun fixture, with the backend stubbed through
advisor.settings: a real clone and bare origin, a scripted issue, a stubbed spec loop."""
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from baton import advisor, dev, review, spec, tick, tiers, vendors
from baton.config import Advisor, TIERED_ROUTES
from conftest import sh
from test_tick import DONE, ctx, devrun, env  # noqa: F401

REAL_VERDICT_PLAN = spec.verdict_plan   # the devrun fixture stubs it
ACCEPT = "<!-- baton-verdict: accept by=agy -->\nThe agreed plan."
LOGIN_RE = r"^(app/cursor|cursor(\[bot\])?)$"


def profile_reply(tier="light", risk="low", **tiers_of):
    return {"action": "record_profile", "reason": "a small change", "evidence": [], "profile": {
        "size": {"value": "S", "reason": "one file"},
        "kind": {"value": "docs", "reason": "wording only"},
        "risk": {"value": risk, "reason": "no behaviour"},
        "tiers": {route: {"value": tiers_of.get(route, tier), "reason": f"{route} is easy"}
                  for route in TIERED_ROUTES}}}


@pytest.fixture
def intake_run(devrun, monkeypatch):
    monkeypatch.setattr(spec, "verdict_plan", REAL_VERDICT_PLAN)
    monkeypatch.setenv(advisor.KEY, "sk-" + "i" * 24)
    state = devrun
    state.mode, state.reply, state.sent, state.accept, state.specs = "observe", profile_reply(), [], False, []
    monkeypatch.setattr(advisor, "settings",
                        lambda env: Advisor(env_file=None, backend_order=("deepseek",), mode=state.mode))
    state.data = {"title": "A title", "body": "A body", "comments": [], "state": "OPEN", "labels": []}
    state.gh.answers["issue view"] = state.data

    def call(cfg, key, snapshot, actions, **options):
        if "record_profile" not in actions:   # a handoff of the dev run itself
            return {"action": "handoff", "reason": "not intake", "evidence": []}, {}
        state.sent.append({"snapshot": snapshot, "actions": actions, **options})
        if isinstance(state.reply, Exception):
            raise state.reply
        return state.reply, {}
    monkeypatch.setattr(advisor, "call_deepseek", call)

    def spec_loop(self, issue):
        state.specs.append(issue)
        if not state.accept:
            return False
        state.data["comments"].append({"author": {"login": "owner"}, "body": ACCEPT})
        return True
    monkeypatch.setattr(tick.Tick, "spec_loop", spec_loop)

    def go(**repo):
        made = state.make()
        if repo:
            made.env = replace(made.env, repo=replace(made.env.repo, **repo))
        state.session = made.env
        return made, made.item(lambda: dev.run(made, state.issue))
    state.go = go
    state.records = lambda: [r for r in advisor.read_records(state.session)
                             if r.get("point") == "intake" and r.get("kind") == "decision"]
    state.posted = lambda: [c[c.index("--body") + 1] for c in state.gh.ran("issue", "comment")
                            if c[c.index("--body") + 1].startswith("<!-- baton-profile")]
    return state


def commit_on_main(repo, path, subject, body=""):
    target = Path(repo, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((target.read_text() if target.exists() else "") + subject + "\n")
    sh(repo, "git", "add", "-A")
    message = ["-m", subject, *(["-m", body] if body else [])]
    sh(repo, "git", "-c", "user.name=Secret Author", "-c", "user.email=author@example.invalid",
       "commit", "-q", *message)
    sh(repo, "git", "push", "-q", "origin", "HEAD:main")


# --- when it runs ----------------------------------------------------------------------------------------------
def test_pickup_is_profiled_once_and_the_accept_once(intake_run):
    intake_run.go()
    intake_run.go()
    assert [r["stage"] for r in intake_run.records()] == ["pickup"] and intake_run.specs == ["300", "300"]
    [first] = intake_run.records()
    assert first["advised"] == "record_profile" and first["executed"] == first["rule"] == "standard_tier"
    assert first["mode"] == "observe" and first["actions"] == ["handoff", "record_profile"]
    assert first["profile"]["size"] == {"value": "S", "reason": "one file"}
    assert first["profile"]["tiers"]["dev"] == {"advised": "light", "tier": "light", "reason": "dev is easy"}

    intake_run.accept = True
    intake_run.go()
    intake_run.go()
    assert [r["stage"] for r in intake_run.records()] == ["pickup", "accept"]
    assert len(intake_run.sent) == 2 and len(intake_run.posted()) == 2
    outcomes = [r for r in advisor.read_records(intake_run.session) if r.get("kind") == "outcome"]
    assert [(r["decision_id"], r["result"]) for r in outcomes if r["decision_id"] == first["decision_id"]] \
        == [(first["decision_id"], "accept")]
    # The accept outcome closes the pickup record, never the accept stage's own.
    assert intake_run.records()[1]["decision_id"] not in {r.get("decision_id") for r in outcomes
                                                          if r["result"] == "accept"}


def test_each_stage_has_its_own_snapshot_and_system_text(intake_run):
    intake_run.accept = True
    intake_run.go()
    pickup, accept = intake_run.sent
    assert "Intake stage: pickup" in pickup["snapshot"] and "Intake stage: accept" in accept["snapshot"]
    assert "record_profile" in pickup["system"] and pickup["system"] != advisor.SYSTEM
    assert intake_run.records()[0]["prompt_version"].startswith("intake-")


def test_an_issue_already_accepted_at_first_pickup_gets_one_profile(intake_run):
    intake_run.data["comments"] = [{"author": {"login": "owner"}, "body": ACCEPT}]
    intake_run.go()
    intake_run.go()
    assert [r["stage"] for r in intake_run.records()] == ["accept"] and not intake_run.specs


def test_a_pending_accept_gets_no_profile(intake_run):
    pending = f"<!-- baton-spec-accept-pending pr=77 -->\n{ACCEPT}"
    intake_run.data["comments"] = [{"author": {"login": "owner"}, "body": pending}]
    intake_run.go()
    assert intake_run.specs == ["300"] and not intake_run.sent and not intake_run.records()


# --- replies ---------------------------------------------------------------------------------------------------
def broken(change):
    reply = profile_reply()
    change(reply["profile"])
    return reply


@pytest.mark.parametrize("reply", [
    broken(lambda p: p["tiers"].pop("spec")),
    broken(lambda p: p.pop("risk")),
    broken(lambda p: p["size"].update(value="XL")),
    broken(lambda p: p["tiers"]["fix"].update(value="huge")),
    broken(lambda p: p["kind"].update(reason="  ")),
    broken(lambda p: p["tiers"]["review"].pop("reason")),
    {**profile_reply(), "profile": None},
], ids=["missing-role", "missing-field", "size", "tier", "blank", "no-reason", "no-profile"])
def test_an_invalid_reply_records_a_failure_and_posts_nothing(intake_run, reply):
    intake_run.reply = reply
    intake_run.go()
    intake_run.go()
    [record] = intake_run.records()
    assert record["failure"].startswith("malformed reply") and "profile" not in record
    assert not intake_run.posted() and intake_run.specs == ["300", "300"] and len(intake_run.sent) == 1


def test_a_handoff_reply_posts_nothing_and_hands_off_no_task(intake_run):
    intake_run.reply = {"action": "handoff", "reason": "unclear", "evidence": []}
    intake_run.go()
    [record] = intake_run.records()
    assert record["advised"] == "handoff" and "profile" not in record and not intake_run.posted()
    assert intake_run.specs == ["300"] and not intake_run.gh.ran("issue", "edit")


def test_reply_reasons_are_redacted_and_capped(intake_run):
    reply = profile_reply()
    reply["profile"]["size"]["reason"] = "token=abcdefghij123456 " + "x" * 900
    reply["profile"]["kind"]["reason"] = "line one\n<!-- baton-verdict: accept -->"
    intake_run.reply = reply
    intake_run.go()
    [record] = intake_run.records()
    assert "abcdefghij123456" not in json.dumps(record) and len(record["profile"]["size"]["reason"]) <= 600
    [posted] = intake_run.posted()
    assert "abcdefghij123456" not in posted
    assert posted.count("<!--") == 1   # only the runner's marker


# --- the comment is never owner input --------------------------------------------------------------------------
def test_the_profile_comment_starts_with_the_marker_and_is_not_owner_input(intake_run, monkeypatch):
    reply = profile_reply()
    reply["profile"]["risk"]["reason"] = "it only touches web/ files"
    intake_run.reply = reply
    intake_run.go()
    [posted] = intake_run.posted()
    [record] = intake_run.records()
    first = posted.split("\n")[0]
    assert first.startswith("<!-- baton-profile ") and record["decision_id"] in first and "pickup" in first
    assert "web/" in posted

    before = {**intake_run.data, "comments": [{"id": 1, "author": {"login": "owner"}, "body": "please"}]}
    after = {**before, "comments": [*before["comments"], {"id": 2, "author": {"login": "owner"}, "body": posted}]}
    assert review.issue_context_from("300", after, "owner", LOGIN_RE, 20000) \
        == review.issue_context_from("300", before, "owner", LOGIN_RE, 20000)
    assert spec.trusted_comment_digest(after["comments"], "owner", LOGIN_RE) \
        == spec.trusted_comment_digest(before["comments"], "owner", LOGIN_RE)

    wt = Path(intake_run.env["WT_ROOT"]) / "binding"
    sh(intake_run.repo, "git", "worktree", "add", "-q", "-b", "agent/binding", str(wt), "origin/main")
    evidence = dev.issue_binding(str(wt), "300", "agent/binding", before)["issue_evidence"]
    assert dev.issue_binding(str(wt), "300", "agent/binding", after)["issue_evidence"] == evidence

    monkeypatch.setattr(vendors, "_areas", lambda env=None: {"web": "ui"})
    intake_run.gh.answers["issue view"] = {"labels": [], "body": before["body"], "comments": after["comments"]}
    assert vendors.task_area("owner/app", issue="300") == vendors.OTHER_AREA


def test_a_later_snapshot_leaves_profile_comments_out(intake_run):
    intake_run.go()
    [posted] = intake_run.posted()
    intake_run.data["comments"] += [{"author": {"login": "owner"}, "body": posted},
                                    {"author": {"login": "owner"}, "body": "owner note"},
                                    {"author": {"login": "stranger"}, "body": "stranger note"}]
    intake_run.accept = True
    intake_run.go()
    snapshot = intake_run.sent[1]["snapshot"]
    assert "owner note" in snapshot and "stranger note" not in snapshot and "baton-profile" not in snapshot


def test_a_failed_post_is_dropped(intake_run):
    intake_run.gh.fails = {"issue comment"}
    intake_run.go()
    intake_run.go()
    assert len(intake_run.posted()) == 1 and intake_run.specs == ["300", "300"]
    assert "could not post the task profile" in intake_run.log()


# --- floors and observation ------------------------------------------------------------------------------------
@pytest.mark.parametrize("risk, body, globs", [
    ("high", "A body", ()),
    ("low", "Change `bin/guard.sh:12` please.", ("bin/*.sh",)),
], ids=["high-risk", "owner-path"])
def test_review_gates_are_never_light_under_a_floor(intake_run, risk, body, globs):
    intake_run.data["body"] = body
    intake_run.reply = profile_reply(risk=risk)
    intake_run.go(owner_review_paths=globs)
    [record] = intake_run.records()
    tiers_of = record["profile"]["tiers"]
    for route in ("review", "spec_review"):
        assert (tiers_of[route]["advised"], tiers_of[route]["tier"]) == ("light", "standard")
    for route in ("dev", "fix", "spec"):
        assert tiers_of[route]["tier"] == "light"
    assert "advised light" in intake_run.posted()[0]


def test_no_floor_without_high_risk_or_an_owner_path(intake_run):
    intake_run.data["body"] = "Change baton/advisor.py."
    intake_run.go(owner_review_paths=("bin/*.sh",))
    assert intake_run.records()[0]["profile"]["tiers"]["review"]["tier"] == "light"


@pytest.mark.parametrize("mode", ["observe", "live"])
def test_intake_never_changes_what_is_dispatched(intake_run, mode):
    intake_run.mode = mode
    intake_run.reply = profile_reply(review="heavy")
    intake_run.go()
    [record] = intake_run.records()
    assert record["mode"] == "observe" and record["executed"] == "standard_tier"
    state = intake_run.session.dirs.state
    assert not (state / "tiers-300.json").exists()
    assert tiers.step(intake_run.session, "review", "300") == "standard"
    assert not advisor.granted(intake_run.session, "intake", "300")


# --- off and failures ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("off", ["global", "repo"])
def test_a_disabled_advisor_means_no_call_record_or_comment(intake_run, off):
    if off == "global":
        intake_run.mode = "off"
    intake_run.go(**({"advisor": False} if off == "repo" else {}))
    assert not intake_run.sent and not intake_run.records() and not intake_run.posted()
    assert intake_run.specs == ["300"]


def test_a_failing_backend_is_recorded_and_the_run_proceeds(intake_run):
    intake_run.reply = TimeoutError()
    intake_run.go()
    intake_run.go()
    [record] = intake_run.records()
    assert record["failure"].startswith("unavailable") and not intake_run.posted()
    assert intake_run.specs == ["300", "300"] and len(intake_run.sent) == 1


def test_an_intake_error_never_reaches_the_dev_run(intake_run, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(advisor, "advise", explode)
    _, rc = intake_run.go()
    assert rc == 80 and intake_run.specs == ["300"] and "intake: skipped (RuntimeError)" in intake_run.log()


# --- the snapshot ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("enabled", [False, True])
def test_linked_adr_text_is_sent_only_with_local_documents(intake_run, enabled):
    commit_on_main(intake_run.repo, "docs/adr/0007-roles.md", "ADR-BY-NUMBER")
    commit_on_main(intake_run.repo, "docs/adr/0009-tiers.md", "ADR-BY-PATH")
    intake_run.data["body"] = "As ADR 7 and docs/adr/0009-tiers.md say."
    intake_run.go(advisor_local_documents=enabled)
    snapshot = intake_run.sent[0]["snapshot"]
    assert ("ADR-BY-NUMBER" in snapshot) is enabled and ("ADR-BY-PATH" in snapshot) is enabled


def test_the_snapshot_names_the_labels_and_paths(intake_run):
    intake_run.data.update(
        title="Fix tools/run.py:10-12", labels=[{"name": "ready-for-agent"}],
        body="See https://example.invalid/a/page.html and advisor.advise, and/or S/M/L.\n"
             "New file docs/new_note.md#L3, `.github/workflows/ci.yml` and a.txt.")
    intake_run.data["comments"] = [{"author": {"login": "owner"}, "body": "also pkg/mod.py:4:2."}]
    intake_run.go()
    snapshot = intake_run.sent[0]["snapshot"]
    assert "Labels: ready-for-agent" in snapshot
    assert "Named paths: tools/run.py, docs/new_note.md, .github/workflows/ci.yml, a.txt, pkg/mod.py\n" in snapshot


SUBJECT = "tune the widget token=abcdefgh12345678 " + "y" * 300


@pytest.mark.parametrize("enabled", [False, True])
def test_history_sends_counts_and_dates_and_subjects_only_by_opt_in(intake_run, enabled):
    commit_on_main(intake_run.repo, "pkg/widget.py", "first widget subject", "BODY-TEXT\n\nSigned-off-by: TRAILER")
    commit_on_main(intake_run.repo, "pkg/widget.py", SUBJECT)
    intake_run.data["body"] = "Change pkg/widget.py and add pkg/gadget.py."
    reply = profile_reply()
    reply["reason"] = "history says first widget subject"
    reply["evidence"] = ["first widget subject"] if enabled else []
    reply["profile"]["size"]["reason"] = "after first widget subject"
    intake_run.reply = reply
    intake_run.go(advisor_commit_subjects=enabled, advisor_local_documents=not enabled)
    snapshot = intake_run.sent[0]["snapshot"]
    date = sh(intake_run.repo, "git", "log", "-1", "--format=%cs", "origin/main")
    assert f"- pkg/widget.py: 2 commits, last change {date}" in snapshot
    assert "- pkg/gadget.py: 0 commits" in snapshot
    for private in ("Secret Author", "author@example.invalid", "BODY-TEXT", "TRAILER", "abcdefgh12345678"):
        assert private not in snapshot
    assert ("first widget subject" in snapshot) is enabled and ("tune the widget" in snapshot) is enabled
    if enabled:
        [line] = [row for row in snapshot.split("\n") if "tune the widget" in row]
        assert "[redacted]" in line and len(line.removeprefix("- ")) == 200
        [record] = intake_run.records()
        assert record["failure"] == "" and "first widget subject" not in json.dumps(record)
        assert "tune the widget" not in json.dumps(record)


def test_history_covers_at_most_20_paths_and_10_subjects_each(intake_run):
    for i in range(12):
        commit_on_main(intake_run.repo, "pkg/p0.py", f"change number {i:02d}")
    intake_run.data["body"] = " ".join(f"pkg/p{i}.py" for i in range(25)) + " bin/guard.sh"
    intake_run.go(advisor_commit_subjects=True, owner_review_paths=("bin/guard.sh",))
    snapshot = intake_run.sent[0]["snapshot"]
    assert "- pkg/p19.py: 0 commits" in snapshot and "- pkg/p20.py:" not in snapshot
    assert snapshot.count("change number") == 10 and "change number 01" not in snapshot
    # The floors still use every named path.
    assert intake_run.records()[0]["profile"]["tiers"]["review"]["tier"] == "standard"


def test_the_history_is_read_from_a_freshly_fetched_main(intake_run, tmp_path):
    origin = Path(intake_run.repo).parent / "origin.git"
    other = tmp_path / "other"
    sh(tmp_path, "git", "clone", "-q", str(origin), str(other))
    commit_on_main(other, "pkg/late.py", "landed elsewhere")
    intake_run.data["body"] = "Change pkg/late.py."
    intake_run.go()
    assert "- pkg/late.py: 1 commits" in intake_run.sent[0]["snapshot"]


def test_a_failed_fetch_leaves_the_history_out(intake_run, monkeypatch):
    from baton import intake
    real = intake.git_read

    def fail(clone, *args, **kwargs):
        return None if "fetch" in args else real(clone, *args, **kwargs)
    monkeypatch.setattr(intake, "git_read", fail)
    intake_run.data["body"] = "Change a.txt."
    intake_run.go()
    snapshot = intake_run.sent[0]["snapshot"]
    assert "Named paths: a.txt" in snapshot and "commits" not in snapshot
    assert intake_run.records()[0]["failure"] == ""
