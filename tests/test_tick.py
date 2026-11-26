"""baton.tick and baton.dev: the tick's order, the item boundary and the dev run, around
a real git clone, a fake `gh` and stubs for the agent runs. Ported from tests/cooldown.sh (the
production item boundary) and the control flow of python -m baton."""
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from baton import capacity, dev, fix, handoff, merge, notify, pick, review, scan, spec, tick
from baton.common import Deferred
from conftest import issue_snapshot_pages, sh

REAL_VERDICT_PLAN = spec.verdict_plan   # the devrun fixture stubs it

BIN = Path(__file__).resolve().parents[1] / "bin"
LOGIN = r"^cursor$"
DONE = {"rc": 0, "status": "success", "outcome": "RESULT: ready", "reply": "done\nRESULT: ready", "model": "gpt-x", "defer": ""}


class FakeGH:
    """`gh` answers by its first two words; every call is kept."""

    def __init__(self):
        self.calls, self.answers, self.fails = [], {}, set()

    def __call__(self, *args, input=None, **kwargs):
        # Spec/review writes may call this as `gh(..., reconcile=...)`; ignore write helpers.
        self.calls.append(list(args))
        graphql = args[:2] == ("api", "graphql")
        head = "issue view" if graphql else " ".join(args[:2])
        if head in self.fails:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        answer = self.answers.get(head, "[]" if list(args[1:2]) == ["list"] else "")
        if callable(answer):
            issue = next(arg.removeprefix("issue=") for arg in args if arg.startswith("issue=")) if graphql else None
            answer = answer(("issue", "view", issue, "--json", "title,body,comments") if graphql else args)
        if graphql and (
                "issue view" not in self.answers or isinstance(answer, dict) and "comments" not in answer):
            answer = {"title": "Issue", "body": "Body", **(answer if isinstance(answer, dict) else {}), "comments": [
                {"author": {"login": "owner"}, "body": "<!-- baton-verdict: accept -->\nPlan"}
            ]}
        if graphql and isinstance(answer, dict) and "comments" in answer:
            answer = issue_snapshot_pages(answer)
        if isinstance(answer, subprocess.CompletedProcess):
            return answer
        if not isinstance(answer, str):
            answer = json.dumps(answer)
        return subprocess.CompletedProcess(args, 0, answer, "")

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


@pytest.fixture
def env(worktree, tmp_path, monkeypatch):
    for name in ("state", "ctx", "logs"):
        (tmp_path / name).mkdir(exist_ok=True)
    values = {"STATE": str(tmp_path / "state"), "CTX": str(tmp_path / "ctx"), "LOGS": str(tmp_path / "logs"),
              "GH_REPO": "owner/app", "OWNER": "owner", "SELF_DIR": str(BIN), "VENDOR_POOL": "claude codex",
              "CURSOR_BRANCH_RE": "^cursor/", "CURSOR_LOGIN_RE": LOGIN, "WT_ROOT": str(tmp_path / "worktrees"),
              "REPO_DIR": str(worktree.repo), "HOME": str(tmp_path / "home"), "CROSS_REVIEW": "1", "SCAN": "1",
              "SCAN_INTERVAL_HOURS": "6", "SCAN_MAX_WAITING": "2", "AGENT_MILESTONE": "Beta", "CHAIN_MAX": "2",
              "SCAN_AREAS_TEXT": "api/billing\nweb/src/lib"}
    monkeypatch.setenv("HOME", values["HOME"])   # no telegram.env: no network
    monkeypatch.setenv("LOGS", values["LOGS"])
    monkeypatch.setenv("GH_REPO", values["GH_REPO"])
    monkeypatch.setattr(pick.settle, "probe", lambda *args: None)
    return values


@pytest.fixture
def ctx(env, worktree, monkeypatch):
    fake = FakeGH()
    monkeypatch.setattr(tick.pr_helpers, "gh", fake)   # the one module every baton module shares
    monkeypatch.setattr(tick.merge, "remove_worktree", lambda repo, wt: fake.calls.append(["remove-worktree", str(wt)]) or True)
    monkeypatch.setattr(tick.Tick, "sweep", lambda self, n: fake.calls.append(["sweep", str(n)]))
    state = SimpleNamespace(env=env, gh=fake, loops=[], execs=[], scans=[], repo=worktree.repo)

    def make(*argv, **extra):
        t = tick.Tick({**env, **extra}, argv, run_sh="python -m baton")
        t.exec = lambda path, args: state.execs.append((path, args))
        return t
    state.make = make
    state.path = lambda name: Path(env["STATE"], name)
    state.log = lambda: Path(env["LOGS"], "runner.log").read_text() if Path(env["LOGS"], "runner.log").exists() else ""
    monkeypatch.setattr(tick.Tick, "review_loop", lambda self, *args, **kw: state.loops.append((args, kw)) or 0)

    def scanned(env, index, area):
        state.scans.append((index, area))
        return "SCAN: none", ""
    monkeypatch.setattr(tick.scan, "scan_area", scanned)
    return state


def checkpoint(state, pr="11", feedback="", **values):
    from baton import checkpoint as cp
    if feedback:
        values.update(reviewed_head=values["fix_head"], base_branch="main", reviewed_issue=values["ISSUE"],
                      verdict_round=1, verdict_digest=hashlib.sha256(feedback.encode()).hexdigest())
    cp.write(state.path(f"review-pending-{pr}"), {"pr": pr, "wt": "/wt", "note": "n", "fixer": "cursor",
                                                "reviewer": "codex", "round": 1, "step": "review", "fix_head": "",
                                                "ISSUE": "", **values})


# --- arguments, pause, lock ---------------------------------------------------------------------------------
def test_scan_list_prints_the_areas(ctx, capsys):
    assert ctx.make("scan", "list").run() == 0
    assert capsys.readouterr().out == "0 api/billing\n1 web/src/lib\n"


@pytest.mark.parametrize("argv", [("pr",), ("review",)])
def test_pr_and_review_need_a_number(ctx, argv, capsys):
    with pytest.raises(tick.Stop) as stop:
        ctx.make(*argv)
    assert stop.value.rc == 1 and f"python -m baton {argv[0]} <number>" in capsys.readouterr().err


def test_a_paused_runner_does_nothing_unless_a_target_is_given(ctx):
    assert ctx.make(PAUSED="1").run() == 0
    assert "paused" in ctx.log()
    assert not ctx.gh.calls


def test_a_second_tick_waits_for_the_lock(ctx):
    lock = ctx.path("repos/owner/app/lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert ctx.make().run() == 0
    assert "busy (another tick holds the lock)" in ctx.log()
    assert not ctx.gh.calls


def test_the_legacy_cooldown_upgrade_holds_the_shared_state_lock(ctx, monkeypatch):
    """A cooldown writer must not land between the check and the rename, which would overwrite it (#276)."""
    ctx.path("cooldown-until").write_text("123\n")
    locked, rename = [], Path.rename

    def probe(path, target):
        if path.name == "cooldown-until":
            lock = ctx.path("quota-locks") / "shared-state"
            lock.parent.mkdir(exist_ok=True)
            with lock.open("a") as file:
                try:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked.append(False)
                except BlockingIOError:
                    locked.append(True)
        return rename(path, target)
    monkeypatch.setattr(Path, "rename", probe)
    assert ctx.make().run() == 0
    assert locked == [True]
    assert ctx.path("claude-cooldown-until").read_text() == "123\n"


# --- Cursor's verdicts --------------------------------------------------------------------------------
def test_cursor_verdicts_move_the_labels(ctx, monkeypatch):
    def comment(login, body):
        return {"author": {"login": login}, "body": body}
    # like gh: an issue whose needs-triage label was removed is no longer listed
    ctx.gh.answers["issue list"] = lambda args: [
        i for i in listed if str(i["number"]) not in
        {e[2] for e in ctx.gh.ran("issue", "edit") if e[5:7] == ["--remove-label", "needs-triage"]}]
    sent = []
    monkeypatch.setattr(tick.Tick, "notify", lambda self, title, text="": sent.append(title))
    listed = [
        {"number": 1, "author": {"login": "owner"}, "comments": [comment("cursor", "<!-- baton-verdict: accept -->\nplan")]},
        {"number": 2, "author": {"login": "owner"}, "comments": [comment("owner", "<!-- baton-verdict: reject by=cursor -->")]},
        {"number": 3, "author": {"login": "owner"}, "comments": [comment("other", "<!-- baton-verdict: accept -->")]},
        {"number": 4, "author": {"login": "stranger"}, "comments": [comment("cursor", "<!-- baton-verdict: accept -->")]}]
    t = ctx.make()
    t.advance_cursor_verdicts()
    t.advance_cursor_verdicts()
    edit = lambda n, *added: ["issue", "edit", n, "-R", "owner/app", "--remove-label", "needs-triage", *added]
    assert ctx.gh.ran("issue", "edit") == [edit("1"), edit("2", "--add-label", "ready-for-human")]
    assert sent == ["#1 waits for your ready-for-agent (Cursor: accept)", "#2 needs you (Cursor: reject)"]
    assert "#1: Cursor verdict accept; waits for the owner's ready-for-agent" in ctx.log()
    assert "#2: Cursor verdict reject" in ctx.log()


# --- the item boundary ---------------------------------------------------------------------------------
def test_a_deferred_item_yields_and_the_tick_goes_on(ctx, monkeypatch):
    """A paused Codex review must not hold up the Claude scan in the same tick (cooldown.sh)."""
    checkpoint(ctx)
    monkeypatch.setattr(tick.Tick, "review_loop", lambda self, *a, **k: (_ for _ in ()).throw(Deferred("codex", "cooldown until epoch 1999999999")))
    ctx.gh.answers["pr view"] = "OPEN\n"
    assert ctx.make().run() == 0
    assert ctx.scans == [("0", "api/billing")], ctx.log()   # the scan ran in the same tick
    assert ctx.path("review-pending-11").exists()     # the checkpoint survives a deferral
    assert "DEFERRED: codex cooldown until epoch 1999999999" in ctx.log()


def test_a_manual_review_returns_the_deferral(ctx, capsys, monkeypatch):
    checkpoint(ctx)
    monkeypatch.setattr(tick.Tick, "review_loop", lambda self, *a, **k: (_ for _ in ()).throw(Deferred("codex", "cooldown until epoch 1999999999")))
    ctx.gh.answers["pr view"] = "OPEN\n"
    assert ctx.make("review", "11").run() == 75
    assert "codex cooldown" in capsys.readouterr().err
    assert ctx.path("review-pending-11").exists()


def test_a_failed_item_ends_the_tick_and_drops_its_markers(ctx):
    ctx.path("spec-paused-5").write_text("x")
    t = ctx.make()

    def body():
        t.active["spec"] = "5"
        return 1
    assert t.after_item(t.item(body)) == 1
    assert not ctx.path("spec-paused-5").exists()


def test_a_deferral_keeps_the_markers_and_releases_the_issue(ctx):
    ctx.path("spec-paused-5").write_text("x")
    t = ctx.make()

    def body():
        t.active["spec"] = t.active["issue"] = "5"
        raise Deferred("claude", "cooldown")
    assert t.item(body) == 75
    assert ctx.path("spec-paused-5").exists()
    assert ctx.gh.ran("issue", "edit", "5", "-R", "owner/app", "--remove-label", "agent-wip")


def test_an_item_that_crashes_is_a_failure_not_a_traceback(ctx):
    t = ctx.make()
    assert t.item(lambda: 1 / 0) == 1
    assert "item failed: ZeroDivisionError" in ctx.log()


def test_the_telemetry_item_file_lives_for_one_item(ctx):
    t = ctx.make()
    seen = []
    t.item(lambda: seen.append(Path(t.env.runtime.telemetry_item)))
    assert seen and not seen[0].exists() and not t.env.runtime.telemetry_item


# --- chain_next -----------------------------------------------------------------------------------------
def test_a_merged_slice_starts_the_next_pass(ctx):
    ctx.path("chain-next").touch()
    t = ctx.make()
    assert t.after_item(80) == 0
    assert ctx.execs and ctx.execs[0][1][-2:] == ["--chain", "1"]
    assert not ctx.path("chain-next").exists() and "chain: a slice merged" in ctx.log()


def test_the_chain_is_bounded(ctx):
    ctx.path("chain-next").touch()
    assert ctx.make(BATON_CHAIN="2").after_item(80) == 0
    assert not ctx.execs and "chain: 2 passes in a row" in ctx.log()


def test_a_manual_run_never_chains(ctx):
    ctx.path("chain-next").touch()
    assert ctx.make("review", "11").after_item(80) == 0
    assert not ctx.execs


# --- cleanup --------------------------------------------------------------------------------------------
def test_finished_worktrees_are_cleaned_up(ctx):
    root = Path(ctx.env["WT_ROOT"])
    for name in ("agent-6", "agent-5", "cursor-7", "cursor-8", "spec-9", "pr-10"):
        (root / name).mkdir(parents=True)
    ctx.gh.answers["issue view"] = lambda a: "CLOSED\n" if a[2] in ("6", "7", "9") else "OPEN\n"
    ctx.gh.answers["pr view"] = "MERGED\n"
    ctx.make().cleanup()
    removed = [c[1] for c in ctx.gh.ran("remove-worktree")]
    assert removed == [str(root / "agent-6"), str(root / "cursor-7"), str(root / "spec-9"), str(root / "pr-10")]
    assert ["sweep", "7"] in ctx.gh.calls and ["sweep", "10"] in ctx.gh.calls
    assert ctx.gh.ran("issue", "edit", "7", "-R", "owner/app", "--remove-label", "agent-wip")
    log = ctx.log()
    assert "cleaned up agent worktree for closed #7" in log and "cleaned up agent worktree for closed #6" in log and "cleaned up spec worktree for closed #9" in log
    assert "cleaned up worktree for MERGED PR #10" in log


# --- pending reviews ------------------------------------------------------------------------------------
def test_a_pending_review_resumes_from_its_checkpoint(ctx):
    checkpoint(ctx, ISSUE="42")
    ctx.gh.answers["pr view"] = "OPEN\n"
    assert ctx.make().run() == 0
    (args, kw), = ctx.loops
    assert args == ("11", "/wt", "n", "cursor", "codex") and kw == {"issue": "42"}
    assert not ctx.scans   # an item ended the tick


@pytest.fixture
def granted_round(ctx, monkeypatch):
    """A spent advisor extra round for PR #11 and for the spec of issue #9; the next ask is a new call."""
    from baton import advisor
    from baton.config import Advisor
    calls = []
    monkeypatch.setenv(advisor.KEY, "sk-" + "t" * 24)
    monkeypatch.setattr(advisor, "settings", lambda env: Advisor(env_file=None, mode="live"))
    monkeypatch.setattr(advisor, "call_deepseek", lambda *a, **k: (
        calls.append(a) or {"action": "extra_round", "reason": "fixable", "evidence": []}, {}))
    tick_env = ctx.make().env

    def ask(point, item, snapshot):
        return advisor.advise(tick_env, point, item, snapshot, ("extra_round",), revalidate=lambda: True)
    assert ask("review_limit", 11, "first").extra_round and ask("spec_limit", 9, "first").extra_round
    assert not ask("review_limit", 11, "second").called
    return SimpleNamespace(calls=calls, ask=ask, env=tick_env, advisor=advisor)


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_a_pending_review_of_a_closed_pr_ends_its_advisor_episode(ctx, granted_round, state):
    checkpoint(ctx)
    ctx.gh.answers["pr view"] = f"{state}\n"
    ctx.make().pending_reviews()
    assert [r["result"] for r in granted_round.advisor.read_records(granted_round.env) if r["kind"] == "outcome"
            ] == [state.lower()]
    # Reopened: a new episode may ask once more.
    assert granted_round.ask("review_limit", 11, "after the reopen").extra_round and len(granted_round.calls) == 3


def test_cleanup_ends_a_closed_prs_episode_with_no_pr_worktree(ctx, granted_round):
    """#156 review: a PR reviewed in its issue's worktree (no pr-N dir, checkpoint dropped by the
    pass) still gets its spent grant released when it closes while the issue stays open."""
    ctx.gh.answers["pr view"] = "CLOSED\n"
    ctx.gh.answers["issue view"] = "OPEN\n"
    ctx.make().cleanup()
    assert not granted_round.advisor.granted(granted_round.env, "review_limit", 11)
    assert granted_round.ask("review_limit", 11, "after the reopen").extra_round and len(granted_round.calls) == 3


def test_closing_an_issue_ends_its_spec_episode(ctx, granted_round):
    (Path(ctx.env["WT_ROOT"]) / "spec-9").mkdir(parents=True)
    ctx.gh.answers["issue view"] = "CLOSED\n"
    ctx.make().cleanup()
    assert not granted_round.advisor.granted(granted_round.env, "spec_limit", 9)
    assert granted_round.ask("spec_limit", 9, "after the reopen").extra_round and len(granted_round.calls) == 3


def test_a_pending_review_of_a_merged_pr_is_dropped(ctx):
    checkpoint(ctx)
    ctx.gh.answers["pr view"] = "MERGED\n"
    ctx.make().pending_reviews()
    assert not ctx.path("review-pending-11").exists() and not ctx.loops


def test_a_manual_target_skips_other_pending_reviews(ctx):
    checkpoint(ctx, pr="11")
    ctx.gh.answers["pr view"] = "OPEN\n"
    ctx.make("review", "12").pending_reviews()
    assert not ctx.loops
    ctx.make("5").pending_reviews()
    assert not ctx.loops


# --- agent-review ---------------------------------------------------------------------------------------
def pr_json(branch="feature/x", login="owner", body="Fixes the thing.\n\nRefs #5"):
    return {"headRefName": branch, "author": {"login": login}, "isCrossRepository": False, "state": "OPEN", "isDraft": False,
            "body": body}


def test_the_lowest_labelled_pr_is_reviewed_in_a_worktree(ctx, worktree):
    ctx.gh.answers["pr list"] = [{"number": 9}, {"number": 4}]
    ctx.gh.answers["pr view"] = pr_json(branch=worktree.branch, login="cursor")
    sh(worktree.repo, "git", "worktree", "remove", "--force", str(worktree.wt))
    assert ctx.make().pick_review() == 0
    (args, kw), = ctx.loops
    assert args[0] == "4" and args[1] == str(Path(ctx.env["WT_ROOT"], "pr-4")) and args[3:] == ("agent", "pool")
    assert "Cursor-authored" in args[2]
    assert ctx.gh.ran("pr", "edit", "4", "-R", "owner/app", "--remove-label", "agent-review")
    assert "PR #4: independent review resumes" in ctx.log()


def test_an_untrusted_pr_is_not_taken(ctx):
    ctx.gh.answers["pr list"] = [{"number": 4}]
    ctx.gh.answers["pr view"] = pr_json(login="stranger")
    assert ctx.make().pick_review() == 0
    assert not ctx.loops and "PR #4: not taken for review" in ctx.log()


def test_the_owners_pr_review_note_names_the_issue(ctx, worktree):
    branch = "feature/x"
    sh(worktree.repo, "git", "push", "-q", "origin", f"origin/main:refs/heads/{branch}")
    sh(worktree.repo, "git", "fetch", "-q", "origin")
    ctx.gh.answers["pr view"] = pr_json(branch=branch)
    ctx.make("review", "4").pick_review()
    (args, kw), = ctx.loops
    assert "and issue #5 give the problem" in args[2] and kw == {"issue": "5"}


def test_a_pr_on_a_non_agent_branch_takes_its_issue_from_the_closing_reference(ctx, worktree):
    branch = "feat/715-article-pick"
    sh(worktree.repo, "git", "push", "-q", "origin", f"origin/main:refs/heads/{branch}")
    sh(worktree.repo, "git", "fetch", "-q", "origin")
    ctx.gh.answers["pr view"] = {**pr_json(branch=branch, body="Picks the article. Closes #715 and #716."),
                                 "closingIssuesReferences": [
                                     {"number": 5, "repository": {"name": "other", "owner": {"login": "owner"}}},
                                     {"number": 715, "repository": {"name": "APP", "owner": {"login": "Owner"}}},
                                     {"number": 716, "repository": {"name": "app", "owner": {"login": "owner"}}}]}
    ctx.make("review", "746").pick_review()
    (args, kw), = ctx.loops
    assert "and issue #715 give the problem" in args[2] and kw == {"issue": "715"}


def test_foreign_or_unidentified_closing_references_are_not_used(ctx, worktree):
    branch = "feat/foreign-issue"
    sh(worktree.repo, "git", "push", "-q", "origin", f"origin/main:refs/heads/{branch}")
    sh(worktree.repo, "git", "fetch", "-q", "origin")
    ctx.gh.answers["pr view"] = {**pr_json(branch=branch, body="A tweak."),
                                 "closingIssuesReferences": [
                                     {"number": 5, "repository": {"name": "other", "owner": {"login": "owner"}}},
                                     {"number": 6}]}
    ctx.make("review", "746").pick_review()
    (args, kw), = ctx.loops
    assert "issue #" not in args[2] and kw == {"issue": ""}


def test_a_pr_with_no_linked_issue_is_reviewed_without_one(ctx, worktree):
    branch = "feat/no-issue"
    sh(worktree.repo, "git", "push", "-q", "origin", f"origin/main:refs/heads/{branch}")
    sh(worktree.repo, "git", "fetch", "-q", "origin")
    ctx.gh.answers["pr view"] = {**pr_json(branch=branch, body="Just a tweak."), "closingIssuesReferences": []}
    ctx.make("review", "746").pick_review()
    (args, kw), = ctx.loops
    assert "issue #" not in args[2] and kw == {"issue": ""}


def test_review_is_skipped_when_an_issue_or_pr_is_forced(ctx):
    assert ctx.make("5").pick_review() is None and ctx.make("pr", "5").pick_review() is None
    assert not ctx.gh.calls


# --- agent-fix ------------------------------------------------------------------------------------------
def fix_ctx(ctx, worktree, monkeypatch, feedback="please rename", fixer="codex"):
    ctx.gh.answers["pr list"] = [{"number": 20, "headRefName": "cursor/issue-297"}, {"number": 3, "headRefName": "feature/x"}]
    ctx.gh.answers["pr view"] = lambda a: "cursor/issue-297" if "headRefName" in a[-1] else json.dumps(
        {"headRefName": "cursor/issue-297", "author": {"login": "cursor"}}) if False else "cursor/issue-297"
    monkeypatch.setattr(tick.review, "human_feedback",
                        lambda repo, owner, pr: review.HumanFeedback(owner=feedback, findings=""))
    monkeypatch.setattr(tick.vendors, "seed_authors", lambda *args: 0)
    monkeypatch.setattr(tick.vendors, "seed_contrib", lambda *args: ["abc1234"])
    monkeypatch.setattr(tick.Tick, "pick_vendor", lambda self, *a, **k: fixer)
    monkeypatch.setattr(tick.Tick, "fix_pr", lambda self, *a, **k: ctx.loops.append(("fix", a, k)) or 0)


def test_an_agent_fix_label_runs_the_fix_then_the_review(ctx, worktree, monkeypatch):
    fix_ctx(ctx, worktree, monkeypatch)
    assert ctx.make().pick_fix() == 0
    kinds = [x[0] if x[0] == "fix" else "review" for x in ctx.loops]
    assert kinds == ["fix", "review"]
    fixed = ctx.loops[0]
    assert fixed[1][:5] == ("20", str(worktree.wt), "codex", "owner (the repository owner)", "please rename")
    args, kw = ctx.loops[1]
    assert args[0] == "20" and args[3:] == ("agent", "pool", "codex", "297") and "<feedback>\nplease rename\n</feedback>" in args[2]
    assert len(ctx.gh.ran("pr", "edit", "20", "-R", "owner/app", "--remove-label", "agent-fix")) == 1
    assert "PR #20: commit abc1234 has no known vendor; not counted" in ctx.log()


def test_a_waiting_fix_push_resumes_with_its_saved_fixer_before_any_read_or_notice(ctx, worktree, monkeypatch):
    from baton import handoff
    fix_ctx(ctx, worktree, monkeypatch)
    t = ctx.make()
    binding = {"commit": "d" * 40, "branch": "cursor/issue-297", "ref": "HEAD", "remote": "e" * 40,
               "upstream": False, "wt": str(worktree.wt), "issue": "297", "pr": "20", "next": "done",
               "outcome": "", "handoff_reason": "", "description_rc": 0, "description_reason": "",
               "who": "", "fixer": "claude", "clear_cursor": False, "feedback": "f" * 64,
               "caller": "standalone", "ci_head": ""}
    assert handoff.prepare_retry(t.env, "297", "fix_push_failed", "20", binding)
    Path(t.env.config.paths.state, "fix-push-waiting-20").write_text("1")
    Path(t.env.config.paths.state, "owner-fix-feedback-20").write_text("please rename")
    forbidden = lambda *a, **k: pytest.fail("read, notified or chose a vendor before the push was due")
    monkeypatch.setattr(tick.review, "human_feedback", forbidden)
    monkeypatch.setattr(tick.vendors, "seed_authors", forbidden)
    monkeypatch.setattr(tick.Tick, "pick_vendor", forbidden)
    monkeypatch.setattr(tick.Tick, "notify", forbidden)
    assert t.pick_fix() == 0
    assert ctx.loops[0][0] == "fix" and ctx.loops[0][1][2] == "claude"


def test_an_agent_fix_review_excludes_every_attempt_author(ctx, worktree, monkeypatch):
    from types import SimpleNamespace

    fix_ctx(ctx, worktree, monkeypatch, fixer="claude")

    def fixed(self, *args, **kwargs):
        self.fix_runner = SimpleNamespace(last_fixer="codex", attempt_authors=["claude", "codex"])
        return 0

    monkeypatch.setattr(tick.Tick, "fix_pr", fixed)
    assert ctx.make().pick_fix() == 0
    args, _ = ctx.loops[0]
    assert args[2].startswith("codex just addressed")
    assert set(args[5].split()) == {"claude", "codex"}


def test_no_feedback_means_nothing_to_fix(ctx, worktree, monkeypatch):
    fix_ctx(ctx, worktree, monkeypatch, feedback=" \n")
    assert ctx.make().pick_fix() == 0
    assert not ctx.loops and "PR #20: nothing to fix" in ctx.log()
    assert ctx.gh.ran("pr", "edit", "20", "-R", "owner/app", "--remove-label", "agent-fix")


def test_a_failed_fix_hands_back_without_a_review(ctx, worktree, monkeypatch):
    fix_ctx(ctx, worktree, monkeypatch)
    monkeypatch.setattr(tick.Tick, "fix_pr", lambda self, *a, **k: 1)
    assert ctx.make().pick_fix() == 0
    assert not ctx.loops and ctx.gh.ran("pr", "edit", "20", "-R", "owner/app", "--remove-label", "agent-fix")


def test_a_deferred_fix_keeps_the_label(ctx, worktree, monkeypatch):
    fix_ctx(ctx, worktree, monkeypatch)
    monkeypatch.setattr(tick.Tick, "fix_pr", lambda self, *a, **k: (_ for _ in ()).throw(Deferred("codex", "quota")))
    assert ctx.make().pick_fix() is None   # the tick goes on to other work
    assert not ctx.gh.ran("pr", "edit", "20", "-R", "owner/app", "--remove-label", "agent-fix")


def test_a_forced_pr_must_be_an_agent_branch(ctx, capsys, monkeypatch):
    ctx.gh.answers["pr view"] = "feature/x\n"
    assert not ctx.make("pr", "3").is_agent_branch("3")
    ctx.gh.answers["pr view"] = "cursor/issue-3\n"
    assert ctx.make("pr", "3").is_agent_branch("3")
    ctx.gh.answers["pr view"] = "agent/issue-3\n"
    assert ctx.make("pr", "3").is_agent_branch("3")


# --- the issue pick ----------------------------------------------------------------------------------------
def issue(n, login="owner", labels=(), milestone="Beta", blocked_by=()):
    return {"number": n, "author": {"login": login}, "labels": [{"name": x} for x in labels],
            "milestone": {"title": milestone} if milestone else None,
            "blockedBy": {"nodes": [{"number": b, "state": s} for b, s in blocked_by], "totalCount": len(blocked_by)}}


def issues(ctx, ready=(), found=(), open_prs=(), pending=0):
    def answer(args):
        if "agent-wip" in args:
            return [{"number": i} for i in range(pending)]
        return list(found) if "claude-found" in args else list(ready)
    ctx.gh.answers["issue list"] = answer
    ctx.gh.answers["pr list"] = [{"headRefName": b} for b in open_prs]
    for item in ready:
        label_history(ctx, item["number"], [label_event(1, "owner")])


def label_event(event_id, actor, event="labeled", label="ready-for-agent"):
    return {"id": event_id, "event": event, "actor": {"login": actor}, "label": {"name": label}}


def label_history(ctx, number, events):
    ctx.gh.answers[f"api repos/owner/app/issues/{number}/events?per_page=100"] = [events]


def test_the_lowest_eligible_issue_is_picked(ctx, monkeypatch):
    issues(ctx, ready=[issue(30, labels=["ready-for-agent"]), issue(12, labels=["ready-for-agent"]),
                       issue(13, milestone="Later"), issue(14, login="stranger"), issue(15, labels=["agent-wip"]),
                       issue(16, labels=["needs-info"])])
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    assert ctx.make().next_issue() == "12"


def test_a_maintainer_label_admits_an_external_issue(ctx, monkeypatch):
    issues(ctx, ready=[issue(7, login="contributor", labels=["ready-for-agent"])])
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    assert ctx.make().next_issue() == "7"


@pytest.mark.parametrize("author", ["contributor", "owner", "cursor"])
def test_another_maintainers_label_cannot_authorize_work(ctx, author):
    issues(ctx, ready=[issue(7, login=author, labels=["ready-for-agent"])])
    label_history(ctx, 7, [label_event(1, "collaborator")])
    assert ctx.make().next_issue() == ""


@pytest.mark.parametrize("events, allowed", [
    ([], False),
    ([label_event(1, "owner"), label_event(2, "collaborator", "unlabeled"), label_event(3, "collaborator")], False),
    ([label_event(1, "collaborator"), label_event(2, "owner", "unlabeled"), label_event(3, "owner")], True),
    ([label_event(1, "owner"), label_event(2, "owner", "unlabeled")], False),
    ([label_event(1, "OWNER")], True),
    ([label_event(1, "owner"), label_event(2, "collaborator", label="bug")], True),
    ([label_event(1, "owner"), label_event(2, "")], False),
])
def test_only_the_current_owner_label_authorizes_work(ctx, events, allowed):
    label_history(ctx, 7, events)
    assert ctx.make().owner_ready(7) is allowed


def test_label_authorization_reads_every_page(ctx):
    key = "api repos/owner/app/issues/7/events?per_page=100"
    ctx.gh.answers[key] = [[label_event(1, "owner")], [label_event(2, "collaborator")]]
    assert not ctx.make().owner_ready(7)
    assert ctx.gh.calls[-1][-2:] == ["--paginate", "--slurp"]


@pytest.mark.parametrize("answer", ["", "not-json", {}, [[]], [[{"event": "labeled", "label": {"name": "ready-for-agent"}}]]])
def test_missing_or_malformed_label_history_never_authorizes(ctx, answer):
    ctx.gh.answers["api repos/owner/app/issues/7/events?per_page=100"] = answer
    assert not ctx.make().owner_ready(7)


def test_a_failed_label_read_never_authorizes(ctx):
    key = "api repos/owner/app/issues/7/events?per_page=100"
    label_history(ctx, 7, [label_event(1, "owner")])
    ctx.gh.fails.add(key)
    assert not ctx.make().owner_ready(7)


@pytest.mark.parametrize("author", ["contributor", "owner", "cursor"])
def test_scanner_labels_alone_never_make_a_candidate(ctx, monkeypatch, author):
    issues(ctx, found=[issue(7, login=author, milestone=None,
                             labels=["claude-found", "needs-triage"])])
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    assert ctx.make().next_issue() == ""


@pytest.mark.parametrize("force, bound", [("", False), ("8", False), ("7", True)])
def test_an_unlabelled_finding_is_bound_only_by_a_manual_run_on_it(ctx, monkeypatch, force, bound):
    """So a scheduled recovery retry of a finding without ready-for-agent does not run."""
    payload = {"labels": [{"name": "claude-found"}, {"name": "needs-triage"}]}
    monkeypatch.setattr(dev.spec, "read_issue", lambda env, issue, **kw: (payload, False))
    monkeypatch.setattr(dev, "issue_binding", lambda wt, issue, branch, fresh: "binding")
    t = ctx.make(*([force] if force else []))
    assert dev.live_issue_binding(t, t.env, "/wt", "7", "agent/issue-7") == ("binding" if bound else None)


@pytest.mark.parametrize("label", ["needs-info", "ready-for-human", "agent-wip"])
def test_an_external_issue_still_obeys_handoff_labels(ctx, label):
    issues(ctx, ready=[issue(7, login="contributor", labels=["ready-for-agent", label])])
    assert ctx.make().next_issue() == ""


@pytest.mark.parametrize("branch", ["agent/issue-12", "cursor/issue-12"])
def test_an_issue_with_a_pr_is_skipped(ctx, monkeypatch, branch):
    issues(ctx, ready=[issue(12), issue(13)], open_prs=[branch])
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    assert ctx.make().next_issue() == "13"


def test_new_work_waits_while_an_issue_awaits_review(ctx):
    issues(ctx, ready=[issue(12)], pending=1)
    ctx.gh.answers["pr list"] = [{"number": 30, "headRefName": "agent/issue-0", "labels": []}]
    ctx.path("review-pending-30").write_text("x")
    assert ctx.make().next_issue() == ""
    assert "1 issue(s) still waiting on review (agent-wip), cap 1, 0 deferred, 1 not deferred" in ctx.log()
    assert "#0: PR #30 in review" in ctx.log()


# --- a PR deferred on quota lends its slot (#99) --------------------------------------------------------------
ORIGINAL_REVIEW_LOOP = tick.Tick.review_loop
QUOTA = "quota claude=blocked(5h 94% >= 90%)"


def flight(ctx, wip, ready=(), prs=None, labels=None, cap=None, in_review=True):
    """WIP issues are in flight; PRS maps an issue to its open PR number, LABELS a PR to its labels."""
    prs, labels = prs or {}, labels or {}
    ctx.gh.answers["issue list"] = lambda args: [{"number": n} for n in wip] if "agent-wip" in args else \
        ([] if "claude-found" in args else list(ready))
    open_prs = [{"number": pr, "headRefName": f"agent/issue-{n}", "labels": [{"name": x} for x in labels.get(pr, ())]}
                for n, pr in prs.items()]
    ctx.gh.answers["pr list"] = lambda args: [] if "--label" in args else open_prs
    for item in ready:
        label_history(ctx, item["number"], [label_event(1, "owner")])
    ctx.cap = cap
    for n, pr in prs.items():   # a PR in review has saved state (#265); without it the tick calls it stalled
        if in_review:
            ctx.path(f"recovery-{n}-{pr}.json").write_text("{}")


def pr_state(ctx, state="OPEN", branch="agent/issue-5"):
    def answer(args):
        return f"{state}\n" if "--jq" in args else {"state": state, "headRefName": branch, "labels": []}
    ctx.gh.answers["pr view"] = answer
    ctx.gh.answers["issue view"] = {"state": "OPEN", "labels": [{"name": "agent-wip"}]}


def defer_loop(monkeypatch, reason=QUOTA, vendor="pool"):
    def loop(self, *a, **k):
        raise Deferred(vendor, reason)
    monkeypatch.setattr(tick.Tick, "review_loop", loop)


def deferred_review(ctx, prs, **extra):
    """Checkpoints for PRS, each yields on quota in the review loop: one pass of pending_reviews."""
    for pr in prs:
        checkpoint(ctx, pr=str(pr))
    ctx.make(**extra).pending_reviews()


def no_vendor_blocked(monkeypatch, seen=None):
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    monkeypatch.setattr(tick.Tick, "pick_vendor", lambda self, *a, **k: "codex")


def test_a_review_deferred_on_quota_lends_its_slot(ctx, monkeypatch):
    pr_state(ctx)
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11})
    defer_loop(monkeypatch)
    deferred_review(ctx, [11])
    assert ctx.path("slot-deferred-11").exists()
    assert ctx.log().count("lends its agent-wip slot") == 1
    no_vendor_blocked(monkeypatch)
    assert ctx.make().next_issue() == "12"
    deferred_review(ctx, [11])   # a repeat deferral keeps the marker and does not log again
    assert ctx.path("slot-deferred-11").exists()
    assert ctx.log().count("lends its agent-wip slot") == 1
    assert ctx.make().next_issue() == "12"


@pytest.mark.parametrize("same_vendor", [False, True])
def test_a_lease_only_wait_in_vendor_selection_keeps_the_cap(ctx, monkeypatch, same_vendor):
    pr_state(ctx)
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11})
    if same_vendor:
        ctx.path("pr-11-authors").write_text("codex\n")
        ctx.path("pr-11-author-models").write_text("codex another-model\n")
        ctx.env["VENDOR_EXTRA"] = ""

    def review_loop(self, *args, **kwargs):
        if same_vendor:
            pick.choose(self.env, "reviewer", "", "review", ["claude", "codex"], pr="11")
        else:
            self.pick_vendor("reviewer", "pool", ["claude"], current="")
        pytest.fail("selection should defer")

    monkeypatch.setattr(tick.Tick, "review_loop", review_loop)
    checkpoint(ctx, pr="11")
    t = ctx.make()
    with capacity.lease(t.env.config.paths.state, "claude"):
        t.pending_reviews()
    expected = ("waiting for claude (vendor capacity reserved) instead of same-vendor codex" if same_vendor
                else "no vendor can run now; claude: vendor capacity reserved")
    assert expected in ctx.log()
    assert not ctx.path("slot-deferred-11").exists()
    no_vendor_blocked(monkeypatch)
    assert ctx.make().next_issue() == ""


def test_a_lent_slot_starts_nothing_when_every_vendor_is_blocked(ctx, monkeypatch):
    pr_state(ctx)
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11})
    defer_loop(monkeypatch)
    deferred_review(ctx, [11])
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    monkeypatch.setattr(tick.Tick, "pick_vendor", lambda self, *a, **k: (_ for _ in ()).throw(Deferred("pool", QUOTA)))
    assert ctx.make().next_issue() == ""
    assert not ctx.path("slot-deferred-12").exists()   # the candidate's own deferral records nothing


def test_the_tick_hands_the_candidate_to_dev_run_while_a_review_is_deferred(ctx, monkeypatch):
    pr_state(ctx)
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11})
    no_vendor_blocked(monkeypatch)
    defer_loop(monkeypatch)
    checkpoint(ctx, pr="11")
    started = []
    monkeypatch.setattr(dev, "run", lambda t, n: started.append(n) or 80)
    monkeypatch.setattr(tick.session, "run_process", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(tick.Tick, "cleanup", lambda self: None)
    monkeypatch.setattr(tick.Tick, "file_merged_follow_ups", lambda self: None)
    assert ctx.make().work() == 0
    assert started == ["12"]


@pytest.mark.parametrize("cap, wip, deferred, started", [
    (1, [5, 6], [5, 6], False),        # two deferred issues fill cap + 1
    (2, [5, 6], [5], True),            # one deferred of two lets a third start
    (2, [5, 6, 7], [5, 6, 7], False),  # three in flight block even when all are deferred
    (1, [5], [], False),               # no deferral: the slot is held
])
def test_the_slots_lent_are_bounded(ctx, monkeypatch, cap, wip, deferred, started):
    pr_state(ctx)
    prs = {n: 10 + n for n in wip}
    flight(ctx, wip=wip, ready=[issue(12)], prs=prs)
    no_vendor_blocked(monkeypatch)
    defer_loop(monkeypatch)

    def view(args):
        if "--jq" in args:
            return "OPEN\n"
        return {"state": "OPEN", "headRefName": f"agent/issue-{int(args[2]) - 10}", "labels": []}
    ctx.gh.answers["pr view"] = view
    for n in deferred:
        checkpoint(ctx, pr=str(10 + n))
    ctx.make(MAX_PENDING_PRS=str(cap)).pending_reviews()
    assert {p.name for p in ctx.path(".").glob("slot-deferred-*")} == {f"slot-deferred-{10 + n}" for n in deferred}
    assert (ctx.make(MAX_PENDING_PRS=str(cap)).next_issue() == "12") is started


def test_the_cap_log_names_issues_prs_states_and_reasons(ctx, monkeypatch):
    pr_state(ctx)
    flight(ctx, wip=[5, 6], ready=[issue(12)], prs={5: 11, 6: 12})
    defer_loop(monkeypatch)
    deferred_review(ctx, [11])
    assert ctx.make().next_issue() == ""
    log = ctx.log()
    assert "2 issue(s) still waiting on review (agent-wip), cap 1, 1 deferred, total limit 2 reached" in log
    assert f"#5: PR #11 deferred (pool {QUOTA})" in log and "#6: PR #12 in review" in log


def test_a_resumed_review_that_completes_returns_the_slot(ctx, monkeypatch):
    pr_state(ctx)
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11})
    no_vendor_blocked(monkeypatch)
    defer_loop(monkeypatch)
    deferred_review(ctx, [11])
    assert ctx.path("slot-deferred-11").exists()
    monkeypatch.setattr(tick.Tick, "review_loop", lambda self, *a, **k: 0)
    ctx.make().pending_reviews()
    assert not ctx.path("slot-deferred-11").exists()
    assert ctx.make().next_issue() == ""   # the PR still counts, and is no longer deferred


def test_a_handoff_returns_the_slot(ctx, monkeypatch):
    pr_state(ctx)
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11})
    no_vendor_blocked(monkeypatch)
    defer_loop(monkeypatch)
    deferred_review(ctx, [11])
    t = ctx.make()

    def handoff():
        t.active["pr"] = "11"
        return 80
    assert t.item(handoff) == 80
    assert not ctx.path("slot-deferred-11").exists()
    assert ctx.make().next_issue() == ""


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_a_checkpoint_of_a_finished_pr_returns_the_slot(ctx, monkeypatch, state):
    pr_state(ctx)
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11})
    defer_loop(monkeypatch)
    deferred_review(ctx, [11])
    pr_state(ctx, state)
    ctx.make().pending_reviews()
    assert not ctx.path("slot-deferred-11").exists() and not ctx.path("review-pending-11").exists()


@pytest.mark.parametrize("skip", ["other-pr", "lookup-failed"])
def test_a_checkpoint_that_does_not_resume_keeps_its_loan(ctx, monkeypatch, skip):
    pr_state(ctx)
    defer_loop(monkeypatch)
    deferred_review(ctx, [11])
    assert ctx.path("slot-deferred-11").exists()
    if skip == "lookup-failed":
        ctx.gh.fails.add("pr view")
    args = ("review", "12") if skip == "other-pr" else ()
    assert ctx.make(*args).pending_reviews() is None
    assert ctx.path("slot-deferred-11").exists()
    assert ctx.path("review-pending-11").exists()


def test_failed_wip_listing_blocks_admission(ctx, monkeypatch):
    pr_state(ctx)
    flight(ctx, wip=[5, 6], ready=[issue(12)], prs={5: 11, 6: 13})
    defer_loop(monkeypatch)
    deferred_review(ctx, [11])
    no_vendor_blocked(monkeypatch)
    answer = ctx.gh.answers["issue list"]
    ctx.gh.answers["issue list"] = lambda args: (
        subprocess.CompletedProcess(args, 1, "", "boom") if "agent-wip" in args else answer(args))
    assert ctx.make().next_issue() == ""
    assert ctx.path("slot-deferred-11").exists()
    ctx.gh.answers["issue list"] = answer
    assert ctx.make().next_issue() == ""


def test_a_live_pr_beyond_the_first_page_keeps_its_loan(ctx, monkeypatch):
    pr_state(ctx)
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11})
    defer_loop(monkeypatch)
    deferred_review(ctx, [11])
    no_vendor_blocked(monkeypatch)
    newer = [{"number": n, "head": {"ref": f"feature/{n}"}, "labels": []} for n in range(100, 200)]
    older = {"number": 11, "head": {"ref": "agent/issue-5"}, "labels": []}
    ctx.gh.answers["pr list"] = [{**p, "headRefName": p["head"]["ref"]} for p in newer]
    ctx.gh.answers["api repos/owner/app/pulls?state=open&per_page=100"] = [newer, [older]]
    assert ctx.make().next_issue() == "12"
    assert ctx.path("slot-deferred-11").exists()


def test_wip_issues_beyond_the_first_page_count_toward_the_cap(ctx, monkeypatch):
    numbers = list(range(100, 201))
    flight(ctx, wip=numbers[:100], ready=[issue(12)], prs={n: n + 1000 for n in numbers})
    no_vendor_blocked(monkeypatch)
    ctx.gh.answers["api repos/owner/app/issues?state=open&labels=agent-wip&per_page=100"] = [
        [{"number": n} for n in numbers[:100]], [{"number": numbers[-1]}]]
    assert ctx.make(MAX_PENDING_PRS="101").next_issue() == ""


def test_an_agent_fix_deferred_in_vendor_selection_lends_its_slot(ctx, monkeypatch):
    pr_state(ctx)
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11}, in_review=False)
    ctx.gh.answers["pr list"] = lambda args: [{"number": 11, "headRefName": "agent/issue-5", "labels": []}]
    Path(ctx.env["WT_ROOT"], "agent-5").mkdir(parents=True)
    monkeypatch.setattr(tick.session, "run_process", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(tick.review, "human_feedback",
                        lambda *a: review.HumanFeedback(owner="please fix", findings=""))
    monkeypatch.setattr(tick.vendors, "seed_authors", lambda *a: 0)
    monkeypatch.setattr(tick.vendors, "seed_contrib", lambda *a: [])
    t = ctx.make()
    for vendor in t.all_vendors():
        cooldown = Path(t.env.config.paths.state, f"{vendor}-cooldown-until")
        cooldown.write_text(f"{int(time.time()) + 3600}\n")
    assert t.item(lambda: t.fix_item("11", "agent/issue-5")) == 75
    assert ctx.path("slot-deferred-11").exists()
    no_vendor_blocked(monkeypatch)
    assert ctx.make().next_issue() == "12"
    # closure of the PR with no review checkpoint: the next admission discards the marker and releases the issue
    flight(ctx, wip=[5], ready=[issue(12)], prs={})
    history(ctx, 5, closed=[11])
    assert ctx.make().next_issue() == "12"
    assert not ctx.path("slot-deferred-11").exists()


def test_a_handoff_clears_an_agent_fix_marker(ctx, monkeypatch):
    pr_state(ctx)
    t = ctx.make()
    t.active = {"pr": ""}
    ctx.path("slot-deferred-11").write_text(json.dumps({"vendor": "pool", "reason": QUOTA}))
    assert t.item(lambda: t.active.update(pr="11") or 80) == 80
    assert not ctx.path("slot-deferred-11").exists()


def defer_inside_review(monkeypatch):
    monkeypatch.setattr(tick.Tick, "review_loop", ORIGINAL_REVIEW_LOOP)

    def defer(self, *args):
        raise Deferred("codex", QUOTA)

    monkeypatch.setattr(review.Review, "loop", defer)


def test_the_first_review_after_opening_a_pr_can_lend_the_slot(devrun, monkeypatch):
    pr_state(devrun, branch="agent/issue-300")
    devrun.gh.answers["issue view"].update(title="A title", body="A body")
    flight(devrun, wip=[300], ready=[issue(12)], prs={300: 301})
    defer_inside_review(monkeypatch)
    devrun.script = [implements()]
    _, rc = devrun.go()
    assert rc == 75
    assert devrun.gh.ran("pr", "create")
    assert not devrun.gh.ran("issue", "edit", "300", "-R", "owner/app", "--remove-label", "agent-wip")
    assert devrun.path("slot-deferred-301").exists()
    assert devrun.make().next_issue() == "12"


@pytest.mark.parametrize("how", ["lease", "candidate", "closed", "no-wip"])
def test_other_deferrals_leave_no_marker(ctx, monkeypatch, how):
    pr_state(ctx)
    t = ctx.make()
    if how == "lease":
        defer_loop(monkeypatch, "vendor capacity reserved", "codex")
        checkpoint(ctx, pr="11")
        ctx.make().pending_reviews()
    elif how == "candidate":
        flight(ctx, wip=[], ready=[issue(12)])
        monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
        monkeypatch.setattr(tick.Tick, "pick_vendor", lambda self, *a, **k: (_ for _ in ()).throw(Deferred("pool", QUOTA)))
        assert t.next_issue() == ""
    elif how == "closed":
        pr_state(ctx, "CLOSED")
        defer_inside_review(monkeypatch)
        assert t.item(lambda: t.review_loop("11", "/wt", "n")) == 75
    else:
        ctx.gh.answers["issue view"]["labels"] = []
        defer_loop(monkeypatch)
        checkpoint(ctx, pr="11")
        ctx.make().pending_reviews()
    assert not list(ctx.path(".").glob("slot-deferred-*"))


def test_a_github_description_update_failure_does_not_lend_a_slot(ctx, monkeypatch):
    from baton import handoff
    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    monkeypatch.setattr(fix.pr_helpers, "sleep", lambda *_: None)
    pr_state(ctx)
    body = "<!-- baton-task issue=5 completion=partial -->\n\nRefs #5\n\n## Manual checks\nnone\n"
    original_view = ctx.gh.answers["pr view"]
    feedback = "<!-- baton-review round=1 by=claude -->\nFix"
    head = sh(ctx.repo, "git", "rev-parse", "HEAD")

    def view_pr(args):
        fields = args[args.index("--json") + 1] if "--json" in args else ""
        if fields == "body":
            return body
        if fields == "headRefName,body":
            return {"headRefName": "agent/issue-5", "body": body}
        if fields == "state,labels,headRefOid,body":
            return {"state": "OPEN", "labels": [{"name": "agent-fix"}], "headRefOid": head, "body": body}
        if fields == "headRefOid":
            return {"headRefOid": head}
        if "comments" in fields:
            return {"comments": [{"author": {"login": "owner"}, "body": feedback}], "state": "OPEN",
                    "headRefName": "agent/issue-5", "headRefOid": head, "body": body, "baseRefName": "main"}
        return original_view(args)

    ctx.gh.answers["pr view"] = view_pr
    ctx.gh.answers["pr edit"] = lambda args: (
        subprocess.CompletedProcess(args, 1, "", "HTTP 503 unavailable") if "--body-file" in args else "")
    ctx.gh.answers["issue view"] = {"state": "OPEN", "labels": [{"name": "agent-wip"}]}
    ctx.path("pr-body-base-11").write_text(body)
    Path(ctx.env["CTX"], "pr-11-body.md").write_text(body + "\n## Summary\nCorrected description.\n")
    checkpoint(ctx, pr="11", step="fix", wt=str(ctx.repo), ISSUE="5", fix_head=head, feedback=feedback)
    monkeypatch.setattr(tick.Tick, "review_loop", ORIGINAL_REVIEW_LOOP)
    monkeypatch.setattr(pick, "choose", lambda *args, **kwargs: "cursor")
    assert ctx.make().pending_reviews() is None
    assert "PR #11: description update deferred after GitHub failure" in ctx.log()
    assert not ctx.path("slot-deferred-11").exists()


@pytest.mark.parametrize("route", ["review", "owner", "switch", "switch-no-commit", "switch-review"])
def test_a_second_tick_finishes_a_deferred_description_edit(ctx, worktree, monkeypatch, request, route):
    from baton import handoff
    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    monkeypatch.setattr(fix.pr_helpers, "sleep", lambda *_: None)
    pr = "11"
    original = ("<!-- baton-task issue=297 completion=complete -->\n\nCloses #297\n\n"
                "## Summary\nOld.\n\n## Checks to verify\nnone\n")
    live = original
    resumes_review = route in ("review", "switch-review")
    labelled = not resumes_review
    fail_edit = True
    switched = False
    agent_calls, verified, reviewed, handoffs = [], [], [], []
    comments = [{"author": {"login": "owner"}, "createdAt": "2026-11-20T00:00:01Z",
                 "body": "Revise the description."}]
    if resumes_review:
        comments[0]["body"] = "<!-- baton-review round=1 by=claude -->\nRevise the description."
    remote_before = sh(worktree.origin, "git", "rev-parse", worktree.branch)

    def view(args):
        oid = sh(worktree.origin, "git", "rev-parse", worktree.branch)
        if "--jq" in args:
            jq = args[-1]
            if jq == ".body":
                return live
            if jq == ".headRefName":
                return worktree.branch
            if jq == ".headRefOid":
                return oid
            return "OPEN"
        fields = args[args.index("--json") + 1] if "--json" in args else ""
        data = {"body": live, "headRefName": worktree.branch, "state": "OPEN", "comments": comments,
                "headRefOid": oid, "baseRefName": "main",
                "labels": [{"name": "agent-fix"}] if labelled else []}
        if fields:
            return {k: data[k] for k in fields.split(",") if k in data}
        return data

    def edit(args):
        nonlocal live, labelled
        if "--body-file" in args:
            if fail_edit:
                # Transient miss: do not write, so the shared budget defers (#151 section 5).
                return subprocess.CompletedProcess(args, 1, "", "HTTP 503 unavailable")
            live = Path(args[args.index("--body-file") + 1]).read_text()
        if "--remove-label" in args and "agent-fix" in args:
            labelled = False
        if "--add-label" in args and "ready-for-human" in args:
            handoffs.append(args)
        return ""

    def agent(vendor, role, prompt, tag, cwd, settings, output, env, hooks):
        agent_calls.append(vendor)
        if vendor == "cursor":
            if agent_calls == ["cursor", "codex", "cursor"]:
                return {**DONE, "outcome": "RESULT: fixed",
                        "reply": "## Checks to verify\nnone\n\nRESULT: fixed"}
            if route != "switch-no-commit":
                (Path(cwd) / "cursor.txt").write_text("Cursor's fix.\n")
                sh(cwd, "git", "add", "cursor.txt")
                sh(cwd, "git", "commit", "-q", "-m", "Cursor fix")
            Path(ctx.env["CTX"], f"pr-{pr}-body.md").write_text(original.replace("Old.", "Revised."))
            return {**DONE, "outcome": "RESULT: fixed",
                    "reply": "## Checks to verify\nnone\n\nTASK: partial\nRESULT: fixed"}
        assert vendor == "codex"
        assert Path(ctx.env["CTX"], f"pr-{pr}-description.md").read_text().rstrip("\n") == live.rstrip("\n")
        Path(f"{output}.body").write_text(live)
        if route in ("switch", "switch-review"):
            Path(output).write_text("## Checks to verify\nnone\n\nRESULT: fixed\n")
            request.addfinalizer(lambda: Path(output).unlink(missing_ok=True))
        if route == "switch-review":
            (Path(cwd) / "codex.txt").write_text("Codex's fix.\nAnother line.\n")
            sh(cwd, "git", "add", "codex.txt")
            sh(cwd, "git", "commit", "-q", "-m", "Codex fix")
        return {**DONE, "outcome": "RESULT: fixed", "reply": "RESULT: fixed"}

    def comment(args):
        comments.append({"author": {"login": "owner"}, "createdAt": "2026-11-20T00:00:02Z",
                         "body": args[args.index("--body") + 1]})
        return ""

    ctx.gh.answers["pr view"] = view
    ctx.gh.answers["pr edit"] = edit
    ctx.gh.answers["pr comment"] = comment
    ctx.gh.answers["issue view"] = {"state": "OPEN", "labels": [{"name": "agent-wip"}]}
    ctx.gh.answers[f"api repos/owner/app/issues/{pr}/comments"] = lambda args: [[
        {**row, "user": row["author"], "id": i} for i, row in enumerate(comments, 1)]]
    for endpoint in ("reviews", "comments"):
        # Empty list (not [[]]): paginated flattens one JSON value, so [[]] yields a list row.
        ctx.gh.answers[f"api repos/owner/app/pulls/{pr}/{endpoint}"] = []
    ctx.gh.answers["pr list"] = lambda args: (
        [{"number": int(pr), "headRefName": worktree.branch}] if labelled and "agent-fix" in args else [])
    monkeypatch.setattr(review, "read_rows", lambda repo, route, number, resource: (
        [{"user": c["author"], "body": c["body"], "created_at": c["createdAt"]} for c in comments]
        if route == "issues" else []))
    monkeypatch.setattr(review, "agent_review", lambda *args: "Revise the description.")
    monkeypatch.setattr(pick, "choose", lambda *args, **kwargs: (
        "codex" if switched and "codex" not in agent_calls else "cursor"))
    monkeypatch.setattr(fix.dispatch, "dispatch", agent)
    monkeypatch.setattr(fix.Runner, "verify", lambda self, wt, *args: verified.append(sh(wt, "git", "rev-parse", "HEAD")) or (0, "", 0))
    monkeypatch.setattr(merge.Merger, "merge_or_handoff", lambda *args: reviewed.append(True))
    monkeypatch.setattr(review.Review, "review_vendor", lambda self, reviewer: "claude")
    def passing_review(self, round, note, reviewer):
        self.save_binding(reviewed_head=sh(self.wt, "git", "rev-parse", "HEAD"))
        return "REVIEW: pass"

    monkeypatch.setattr(review.Review, "run_review", passing_review)
    if route == "switch-review":
        def review_with_changes(self, round, note, reviewer):
            comments.append({"author": {"login": "owner"}, "createdAt": "2026-11-20T00:00:02Z",
                             "body": f"<!-- baton-review round={round} by={reviewer} -->\nRevise the description."})
            head = sh(self.wt, "git", "rev-parse", "HEAD")
            self.save_binding(reviewed_head=head, base_branch="main", reviewed_issue="297", verdict_round=str(round))
            self.capture_feedback(comments[-1]["body"], "verdict_digest")
            return "REVIEW: changes"

        monkeypatch.setattr(review.Review, "run_review", review_with_changes)
    if resumes_review:
        checkpoint(ctx, pr=pr, step="fix", wt=str(worktree.wt), ISSUE="297", fix_head=remote_before,
                   feedback=comments[0]["body"], extra="1" if route == "switch-review" else "")
    monkeypatch.setattr(tick.Tick, "review_loop", ORIGINAL_REVIEW_LOOP)

    assert ctx.make().run() == 0, ctx.log()
    fix_head = sh(worktree.wt, "git", "rev-parse", "HEAD")
    assert agent_calls == ["cursor"] and not verified and not handoffs
    assert sh(worktree.origin, "git", "rev-parse", worktree.branch) == remote_before
    assert ctx.path(f"cursor-fix-task-{pr}").is_file()
    if resumes_review:
        assert ctx.path(f"review-pending-{pr}").is_file()
    else:
        assert labelled
    assert live == original
    assert "description update deferred after GitHub failure" in ctx.log()

    fail_edit = False
    switched = route.startswith("switch")
    clock["now"] = 1_060
    before_log = len(ctx.log().splitlines())
    assert ctx.make().run() == 0, "\n".join(ctx.log().splitlines()[before_log:])
    # A commit still to push takes the shared push budget after the description lands (#151).
    pending_push = list(ctx.path(".").glob("handoff-*-fix_push_failed.json"))
    if pending_push:
        clock["now"] = 1_120
        assert ctx.make().run() == 0, "\n".join(ctx.log().splitlines()[before_log:])
    new_log = "\n".join(ctx.log().splitlines()[before_log:])
    assert "completion=partial" in live and "Refs #297" in live and "Revised." in live, (live, new_log)
    if route == "switch-no-commit":
        # Description text change counts as progress; no second worker is required.
        assert agent_calls == ["cursor"] and not verified
        assert not ctx.path(f"cursor-fix-task-{pr}").exists()
        return
    if route == "switch":
        # Commit + description text change: one vendor finishes; no switch (#151 section 5).
        assert agent_calls == ["cursor"], (agent_calls, new_log)
        assert sh(worktree.origin, "git", "rev-parse", worktree.branch) == fix_head
        assert not handoffs and verified == [fix_head], (handoffs, new_log)
        assert reviewed or ctx.loops
        assert ctx.path(f"pr-{pr}-contrib").read_text().splitlines() == ["claude 1", "cursor 1"]
        assert not labelled and not ctx.path(f"review-pending-{pr}").exists()
        assert not ctx.path(f"cursor-fix-task-{pr}").exists()
        return
    if route == "switch-review":
        # After the description lands, review changes picks codex; another changes hits the round limit.
        assert "codex" in agent_calls, (agent_calls, new_log)
        assert handoffs, (handoffs, new_log)
        assert "claude 1" in ctx.path(f"pr-{pr}-contrib").read_text()
        assert "cursor 1" in ctx.path(f"pr-{pr}-contrib").read_text()
        assert not ctx.path(f"cursor-fix-task-{pr}").exists()
        return
    expected_agents = ["cursor"]
    assert agent_calls == expected_agents, (agent_calls, new_log)
    assert sh(worktree.origin, "git", "rev-parse", worktree.branch) == fix_head
    assert not labelled and not ctx.path(f"review-pending-{pr}").exists()
    assert not ctx.path(f"cursor-fix-task-{pr}").exists()
    assert not ctx.path(f"owner-fix-feedback-{pr}").exists()
    assert not handoffs and verified == [fix_head], (handoffs, new_log)
    assert reviewed or ctx.loops


def test_tick_discards_a_directory_request_after_a_fix_needs_info(ctx, worktree, monkeypatch):
    request = Path(ctx.env["CTX"], "pr-11-body.md")

    def agent(*args):
        request.mkdir()
        (request / "draft").write_text("Unfinished.")
        return {**DONE, "outcome": "RESULT: needs-info", "reply": "1. Which option?"}

    monkeypatch.setattr(fix.dispatch, "dispatch", agent)
    runner = ctx.make()
    assert runner.item(lambda: runner.fix_pr("11", str(worktree.wt), "cursor", "owner", "feedback")) == 1
    assert not request.exists() and "Cursor PR #11 has questions" in ctx.log()


def test_a_relabelled_pr_cannot_use_an_old_marker(ctx, monkeypatch):
    pr_state(ctx)
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11})
    defer_loop(monkeypatch)
    deferred_review(ctx, [11])
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11}, labels={11: ["ready-for-human"]})
    no_vendor_blocked(monkeypatch)
    assert ctx.make().next_issue() == "12"
    assert not ctx.path("slot-deferred-11").exists()
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11})   # back in the agents' hands, no deferral on record
    assert ctx.make().next_issue() == ""
    assert "deferred (" not in ctx.log().rsplit("not starting new work", 1)[1]


def test_a_closed_issue_with_agent_wip_cannot_lend_a_slot(ctx, monkeypatch):
    pr_state(ctx)
    ctx.gh.answers["issue view"]["state"] = "CLOSED"
    defer_loop(monkeypatch)
    deferred_review(ctx, [11])
    assert not ctx.path("slot-deferred-11").exists()
    assert "lends its agent-wip slot" not in ctx.log()


def test_a_pr_waiting_for_the_owner_does_not_hold_up_new_work(ctx, monkeypatch):
    issues(ctx, ready=[issue(12)], pending=1)
    ctx.gh.answers["pr list"] = [{"headRefName": "agent/issue-0", "labels": [{"name": "ready-for-human"}]}]
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    assert ctx.make().next_issue() == "12"


def test_a_pr_still_in_review_holds_up_new_work(ctx):
    issues(ctx, ready=[issue(12)], pending=1)
    ctx.gh.answers["pr list"] = [{"number": 30, "headRefName": "agent/issue-0", "labels": []}]
    ctx.path("review-pending-30").write_text("x")
    assert ctx.make().next_issue() == ""


def test_an_issue_no_vendor_can_run_is_skipped_for_the_next(ctx, monkeypatch):
    issues(ctx, ready=[issue(12), issue(13)])
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    calls = iter([Deferred("pool", "no vendor can run now"), None])

    def pick_vendor(self, *a, **k):
        if err := next(calls):
            raise err
        return "claude"
    monkeypatch.setattr(tick.Tick, "pick_vendor", pick_vendor)
    assert ctx.make().next_issue() == "13"
    assert "DEFERRED: pool no vendor can run now" in ctx.log()


def test_an_issue_blocked_by_an_open_issue_is_not_a_candidate(ctx):
    issues(ctx, ready=[issue(12, blocked_by=[(10, "OPEN"), (11, "CLOSED")]), issue(13, blocked_by=[(11, "CLOSED")]),
                       issue(14)])
    assert ctx.make().candidates() == [13, 14]
    assert "#12: skipped; blocked by #10" in ctx.log()


def test_blockers_github_did_not_list_count_as_open(ctx):
    item = issue(12, blocked_by=[(11, "CLOSED")])
    item["blockedBy"]["totalCount"] = 2
    issues(ctx, ready=[item])
    assert ctx.make().candidates() == []
    assert "#12: skipped; blocked by #?" in ctx.log()


def test_a_forced_blocked_issue_warns_and_runs(ctx, capsys):
    ctx.gh.answers["issue view"] = {"labels": [{"name": "ready-for-agent"}], "author": {"login": "owner"},
                                    "blockedBy": {"nodes": [{"number": 62, "state": "OPEN"}], "totalCount": 1}}
    label_history(ctx, 7, [label_event(1, "owner")])
    ctx.gh.answers["pr list"] = ""
    assert ctx.make("7").forced_issue() == "7"
    assert "#7: still blocked by #62; running it as asked" in capsys.readouterr().err


def test_a_spec_in_review_needs_a_pool_vendor(ctx, monkeypatch):
    issues(ctx, ready=[issue(12)])
    ctx.gh.answers["issue view"] = {"title": "T", "body": "B", "comments": [
        {"author": {"login": "owner"}, "body": "<!-- baton-spec-review round=1 by=claude -->"}
    ]}
    seen = []
    monkeypatch.setattr(tick.Tick, "pick_vendor", lambda self, role, mode, cands, **k: seen.append(cands) or "claude")
    assert ctx.make().next_issue() == "12"
    assert seen == [["claude", "codex"]]


@pytest.mark.parametrize("labels, pr, ok", [
    (["ready-for-agent"], "", True), (["claude-found", "needs-triage"], "", True), (["claude-found"], "", True),
    (["needs-triage"], "", False), (["ready-for-agent"], "5", False)])
def test_a_forced_issue_needs_a_label_and_no_pr(ctx, capsys, labels, pr, ok):
    ctx.gh.answers["issue view"] = {"labels": [{"name": n} for n in labels], "author": {"login": "owner"}}
    label_history(ctx, 7, [label_event(1, "owner")])
    ctx.gh.answers["pr list"] = pr
    t = ctx.make("7")
    if ok:
        assert t.forced_issue() == "7"
    else:
        with pytest.raises(tick.Stop) as stop:
            t.forced_issue()
        assert stop.value.rc == 2 and capsys.readouterr().err


@pytest.mark.parametrize("actor", ["collaborator", ""])
def test_manual_issue_execution_requires_the_owner_label_too(ctx, actor, capsys):
    ctx.gh.answers["issue view"] = {"labels": [{"name": "ready-for-agent"}], "author": {"login": "contributor"}}
    label_history(ctx, 7, [label_event(1, actor)])
    with pytest.raises(tick.Stop) as stop:
        ctx.make("7").forced_issue()
    assert stop.value.rc == 2 and "cannot verify owner authorization" in capsys.readouterr().err


@pytest.mark.parametrize("labels", [("claude-found",), ("claude-found", "needs-triage")])
def test_manual_execution_cannot_use_scanner_labels_for_external_issues(ctx, capsys, labels):
    ctx.gh.answers["issue view"] = {"labels": [{"name": n} for n in labels],
                                    "author": {"login": "contributor"}}
    with pytest.raises(tick.Stop) as stop:
        ctx.make("7").forced_issue()
    assert stop.value.rc == 2 and "external issues" in capsys.readouterr().err


def test_a_forced_pr_or_review_skips_the_issue_pick(ctx):
    assert ctx.make("pr", "5").pick_issue() is None and ctx.make("review", "5").pick_issue() is None


# --- the order of one tick -----------------------------------------------------------------------------------
def test_the_items_come_in_order(ctx, monkeypatch):
    order = []
    t = ctx.make()
    for name in ("pending_openings", "pending_reviews", "pick_review", "pick_fix", "pick_issue"):
        monkeypatch.setattr(t, name, lambda name=name: order.append(name))
    monkeypatch.setattr(t, "scan_fallback", lambda: order.append("scan") or 0)
    assert t.run() == 0
    assert order == ["pending_openings", "pending_reviews", "pick_review", "pick_fix", "pick_issue", "scan"]
    listings = [c for c in ctx.gh.ran("issue", "list") if "claude-found" not in c]
    assert [("P0" in c) for c in listings] == [True]   # no P0 issue: one probe, nothing more


def test_the_first_item_that_ends_the_tick_stops_it(ctx, monkeypatch):
    t = ctx.make()
    order = []
    monkeypatch.setattr(t, "pending_reviews", lambda: order.append("pending"))
    monkeypatch.setattr(t, "pick_review", lambda: order.append("review") or 0)
    monkeypatch.setattr(t, "pick_fix", lambda: order.append("fix"))
    assert t.run() == 0 and order == ["pending", "review"]


def test_the_old_global_cooldown_becomes_claudes(ctx):
    ctx.path("cooldown-until").write_text("1999999999\n")
    assert ctx.make().run() == 0
    assert ctx.path("claude-cooldown-until").read_text() == "1999999999\n" and not ctx.path("cooldown-until").exists()


def test_a_failed_fetch_ends_the_tick(ctx, monkeypatch):
    t = ctx.make(REPO_DIR="/nonexistent")
    assert t.run() == 1 and "git fetch failed" in ctx.log()


# --- the manual scan commands ----------------------------------------------------------------------------------
def test_scan_by_hand_prints_the_outcome_and_the_draft(ctx, monkeypatch, capsys):
    monkeypatch.setattr(tick.scan, "scan_area", lambda env, i, area: ("SCAN: found a bug", "/d/draft"))
    assert ctx.make("scan", "1").run() == 0
    assert capsys.readouterr().out == "SCAN: found a bug\ndraft: /d/draft.title /d/draft.md\nfile it: python -m baton file-scan /d/draft\n"


def test_scan_by_hand_takes_the_next_area_by_default(ctx, capsys):
    ctx.path("scan-area").write_text("1\n")
    ctx.make("scan").run()
    assert ctx.scans == [("1", "web/src/lib")]


def test_scan_by_hand_rejects_a_bad_index(ctx, capsys):
    assert ctx.make("scan", "x").run() == 2


def test_scan_by_hand_reports_a_deferral(ctx, monkeypatch, capsys):
    monkeypatch.setattr(tick.scan, "scan_area", lambda *a: (_ for _ in ()).throw(Deferred("claude", "cooldown")))
    assert ctx.make("scan", "0").run() == 75
    assert "DEFERRED: claude cooldown" in capsys.readouterr().err


def test_file_scan_needs_a_draft(ctx, capsys):
    assert ctx.make("file-scan", "/nonexistent/draft").run() == 2
    assert "no scan draft at '/nonexistent/draft'" in capsys.readouterr().err


def test_file_scan_files_a_draft(ctx, monkeypatch, tmp_path):
    draft = tmp_path / "draft"
    for suffix in ("title", "md", "area"):
        Path(f"{draft}.{suffix}").write_text("x\n")
    filed = []
    monkeypatch.setattr(tick.scan, "file_scan", lambda env, d: filed.append(d) or True)
    assert ctx.make("file-scan", str(draft)).run() == 0 and filed == [str(draft)]


# --- the scan when there is nothing else ----------------------------------------------------------------------------
def fallback(ctx, waiting=0, **extra):
    ctx.gh.answers["issue list"] = [{"number": n} for n in range(waiting)]
    return ctx.make(**extra)


def test_the_scan_takes_the_next_area_and_remembers(ctx, monkeypatch):
    filed = []
    monkeypatch.setattr(tick.scan, "scan_area", lambda env, i, area: ctx.scans.append((i, area)) or ("SCAN: found x", "/d/x"))
    monkeypatch.setattr(tick.scan, "file_scan", lambda env, d: filed.append(d))
    ctx.path("scan-area").write_text("1\n")
    t = fallback(ctx)
    assert t.scan_fallback() == 0
    assert ctx.scans == [("1", "web/src/lib")] and filed == ["/d/x"]
    assert ctx.path("scan-area").read_text() == "0\n" and ctx.path("scan-last").read_text() == f"{t.now}\n"


@pytest.mark.parametrize("extra", [{"SCAN": "0"}, {"CROSS_REVIEW": "0"}, {"SCAN_AREAS_TEXT": ""}])
def test_the_scan_can_be_switched_off(ctx, extra):
    assert fallback(ctx, **extra).scan_fallback() is None   # this repository scans nothing now
    assert not ctx.scans


def test_the_scan_waits_for_its_interval(ctx):
    t = fallback(ctx)
    ctx.path("scan-last").write_text(f"{t.now - 3600}\n")
    assert t.scan_fallback() is None
    assert not ctx.scans and "no scan; next after" in ctx.log()


def test_the_scan_waits_for_cursor_to_clear_the_queue(ctx):
    assert fallback(ctx, waiting=2).scan_fallback() is None
    assert not ctx.scans and "no scan; 2 claude-found issue(s) still wait for Cursor" in ctx.log()


def test_the_scan_does_not_run_blind_when_gh_fails(ctx):
    ctx.gh.fails.add("issue list")
    ctx.make().scan_fallback()
    assert not ctx.scans


def test_the_scan_is_claude_only(ctx):
    ctx.path("claude-cooldown-until").write_text("1999999999\n")
    fallback(ctx).scan_fallback()
    assert not ctx.scans and "DEFERRED: claude cooldown" in ctx.log()


def test_a_failed_scan_worktree_keeps_the_rotation(ctx, monkeypatch):
    monkeypatch.setattr(tick.scan, "scan_area", lambda *a: (None, ""))
    fallback(ctx).scan_fallback()
    assert not ctx.path("scan-area").exists() and not ctx.path("scan-last").exists()


def test_scan_fallback_skips_when_no_vendor_has_scan_role(ctx, monkeypatch):
    from baton.config import Config
    real = Config.for_role
    monkeypatch.setattr(Config, "for_role",
                        lambda self, role, pool_only=False: () if role == "scan" else real(self, role, pool_only=pool_only))
    t = fallback(ctx)
    ctx.path("scan-area").write_text("1\n")
    assert t.scan_fallback() is None
    assert not ctx.scans
    assert "scan skipped: no vendor with scan role" in ctx.log()
    assert ctx.path("scan-area").read_text() == "1\n"
    assert not ctx.path("scan-last").exists()


def test_scan_fallback_skips_when_the_only_scan_holder_cannot_scan(ctx, monkeypatch):
    from baton import agents
    holders = fallback(ctx).env.config.for_role("scan")
    assert holders
    for v in holders:
        monkeypatch.setattr(agents.ADAPTERS[v], "SCAN", False)
    t = fallback(ctx)
    ctx.path("scan-area").write_text("1\n")
    assert t.scan_fallback() is None
    assert not ctx.scans
    assert "scan skipped: no vendor with scan role" in ctx.log()
    assert not ctx.path("scan-last").exists()


# --- follow-ups of owner-merged PRs -----------------------------------------------------------------------------------
def test_follow_ups_are_filed_for_pr_the_owner_merged(ctx, monkeypatch):
    reviewed = {"number": 5, "headRefName": "cursor/issue-9", "body": "Refs #9",
                "comments": [{"author": {"login": "owner"}, "body": "<!-- baton-review round=1 by=codex -->\nok"}]}
    plain = {"number": 6, "headRefName": "cursor/issue-10", "body": "", "comments": []}
    ctx.gh.answers["pr list"] = [reviewed, plain]
    seen = []
    monkeypatch.setattr(merge.Merger, "finish_merged_task", lambda self: seen.append(("finish", self.pr, self.issue)) or False)
    monkeypatch.setattr(merge.Merger, "after_merge", lambda self: seen.append(("after", self.pr, self.issue)))
    ctx.make().file_merged_follow_ups()
    assert seen == [("finish", "5", "9"), ("after", "5", "9")] and "PR #5: task recovery will retry" in ctx.log()


def test_an_owner_merge_is_recorded_once_per_pr(ctx, monkeypatch):
    reviewed = {"number": 5, "headRefName": "cursor/issue-9", "body": "Refs #9",
                "comments": [{"author": {"login": "owner"}, "body": "<!-- baton-review round=1 by=codex -->\nok"}]}
    ctx.gh.answers["pr list"] = [reviewed]
    monkeypatch.setattr(merge.Merger, "finish_merged_task", lambda self: True)
    monkeypatch.setattr(merge.Merger, "after_merge", lambda self: None)
    recorded, answers = [], [False, True]
    monkeypatch.setattr(tick.telemetry.Telemetry, "owner_merged",
                        lambda self: recorded.append((self.issue, self.pr)) or answers.pop(0))
    for _ in range(3):
        ctx.make().file_merged_follow_ups()
    assert recorded == [(9, 5), (9, 5)]


# --- the dev run --------------------------------------------------------------------------------------------------------------
@pytest.fixture
def devrun(ctx, worktree, monkeypatch):
    """dev.run(): a real clone and bare origin, stubbed spec, agent and verify."""
    issue = "300"
    wt_root = Path(ctx.env["WT_ROOT"])
    wt_root.mkdir(exist_ok=True)
    state = ctx
    state.issue, state.runs, state.verify, state.script = issue, [], (0, "", 0), []
    state.gh.answers["issue view"] = {"title": "A title", "body": "A body", "comments": []}
    state.gh.answers["pr create"] = "https://github.com/owner/app/pull/301\n"
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "the plan")
    monkeypatch.setattr(tick.Tick, "pick_vendor", lambda self, role, mode, cands, **k: "codex")
    verify = fix.Runner.verify
    monkeypatch.setattr(fix.Runner, "verify", lambda self, *a: state.verify if state.verify else verify(self, *a))

    def agent(vendor, role, prompt, tag, cwd, settings="", output="", env=None, hooks=None):
        state.runs.append({"vendor": vendor, "role": role, "tag": tag, "prompt": prompt, "cwd": cwd, "output": output, "env": env})
        return state.script.pop(0)(cwd, output) if state.script else dict(DONE)
    monkeypatch.setattr(dev.dispatch, "dispatch", agent)
    state.filed = []
    monkeypatch.setattr(dev.after_merge, "file_pr_bugs", lambda pr, issue: state.filed.append((pr, issue)))
    state.body = Path("/tmp/baton-dev-300.pr.md")
    state.body.unlink(missing_ok=True)
    state.go = lambda **extra: (lambda t: (t, t.item(lambda: dev.run(t, issue))))(ctx.make(**extra))
    yield state
    state.body.unlink(missing_ok=True)


def body_of(call):
    return call[call.index("--body") + 1]


def implements(title="Add the thing", body="Summary\n\nRefs #300", commit=True, fence=False):
    def run(cwd, output):
        if commit:
            Path(cwd, "new.txt").write_text("new\n")
            sh(cwd, "git", "add", "-A")
            sh(cwd, "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "work")
        text = f"{title}\n\n{body}\n"
        Path(output).write_text(f"```markdown\n{text}```\n" if fence else text)
        return dict(DONE)
    return run


def test_the_dev_run_opens_a_pr_and_starts_the_review(devrun, monkeypatch):
    original_replace = Path.replace

    def separate_tmp(self, target):
        # A file from /tmp moving into CTX; on Linux pytest's own tmp_path (and CTX) is under /tmp too.
        ctx = devrun.env["CTX"]
        if str(self).startswith("/tmp/") and not str(self).startswith(ctx) and str(target).startswith(ctx):
            raise OSError(18, "Invalid cross-device link")
        return original_replace(self, target)
    monkeypatch.setattr(Path, "replace", separate_tmp)
    devrun.script = [implements(fence=True)]
    t, rc = devrun.go()
    assert rc == 80
    run, = devrun.runs
    assert run["vendor"] == "codex" and run["role"] == "dev" and run["tag"] == "issue-300-dev"
    assert run["env"].runtime.shots_dir == str(Path(devrun.env["CTX"]) / "issue-300-shots")
    assert "the plan" in Path(re_context(run["prompt"])).read_text()
    create, = devrun.gh.ran("pr", "create")
    assert create[create.index("--title") + 1] == "Add the thing" and create[create.index("--head") + 1] == "agent/issue-300"
    assert "Refs #300" in create[create.index("--body") + 1]
    (args, kw), = devrun.loops
    assert args[:2] == ("301", str(Path(devrun.env["WT_ROOT"], "agent-300"))) and kw == {"issue": "300"}
    assert "plan agreed on issue #300" in args[2] and "the codex agent" in args[2]
    assert devrun.gh.ran("issue", "edit", "300", "-R", "owner/app", "--add-label", "agent-wip")
    assert Path(devrun.env["STATE"], "pr-301-authors").read_text() == "codex\n"
    assert Path(devrun.env["STATE"], "pr-301-author-models").read_text() == "codex gpt-x\n"
    assert Path(devrun.env["STATE"], "pr-301-contrib").read_text().startswith("codex ")
    assert not devrun.body.exists() and (Path(devrun.env["CTX"]) / "pr-301-shots").is_dir()
    assert "#300 started (codex)" in devrun.log() and "#300: codex opened PR #301, reviewing" in devrun.log()
    # the branch is on origin
    assert "agent/issue-300" in sh(devrun.repo, "git", "ls-remote", "--heads", "origin")


def re_context(prompt):
    import re
    return re.search(r"[^\s`]*issue-300-dev-[^\s`]+\.md", prompt).group(0)


def test_the_dev_body_is_published_under_the_new_checks_heading(devrun):
    check_lines = ["- Open the page  ", "- Open the map  "]
    devrun.script = [implements(body="Summary\n\nRefs #300\n\n## Manual checks\n" + "\n".join(check_lines) + "\n")]
    devrun.go()
    create, = devrun.gh.ran("pr", "create")
    body = create[create.index("--body") + 1]
    assert body.splitlines()[-3:] == ["## Checks to verify", *check_lines]
    assert "## Manual checks" not in body


def test_the_gate_result_goes_in_the_pr_description(devrun, monkeypatch):
    def verify(self, *args):
        self.gate_result = "E2E gate passed: e2e/a.spec.ts --repeat-each=15 (15 passed)"
        return 0, "", 0
    monkeypatch.setattr(dev.fix.Runner, "verify", verify)
    devrun.script = [implements()]
    devrun.go()
    create, = devrun.gh.ran("pr", "create")
    body = create[create.index("--body") + 1]
    assert "## E2E gate\nE2E gate passed: e2e/a.spec.ts --repeat-each=15 (15 passed), on " in body and "Refs #300" in body


def test_a_partial_dev_slice_skips_the_gate_and_a_complete_one_runs_it(devrun, monkeypatch):
    seen = []
    monkeypatch.setattr(dev.fix.Runner, "verify", lambda self, *args: (seen.append(self.gate_applies), (0, "", 0))[1])
    devrun.script = [implements()]     # says Refs: a partial slice
    devrun.go()
    assert seen == [False]
    assert dev.is_partial("Title\n\nCloses #300\n", "300") is False


def test_a_verified_repair_makes_the_model_unknown(devrun):
    devrun.script, devrun.verify = [implements()], (0, "", 1)
    devrun.go()
    assert Path(devrun.env["STATE"], "pr-301-author-models").read_text() == "codex unknown\n"


def test_pr_creation_interruption_resumes_without_rerunning_dev(devrun, monkeypatch):
    """The PR exists, but the process died before screenshots/authors/review were recorded."""
    devrun.script = [implements()]
    original = dev.finish_opening
    monkeypatch.setattr(dev, "finish_opening", lambda *a: (_ for _ in ()).throw(OSError("interrupted")))
    t, rc = devrun.go()
    assert rc == 1 and not devrun.loops
    path = dev.opening_path(t, "300")
    assert path.is_file() and json.loads(path.read_text())["pr"] == "301"
    wt = Path(devrun.env["WT_ROOT"], "agent-300")
    created, = devrun.gh.ran("pr", "create")
    devrun.gh.answers["pr view"] = {
        "state": "OPEN", "headRefName": "agent/issue-300", "headRefOid": sh(wt, "git", "rev-parse", "HEAD"),
        "baseRefName": "main", "isCrossRepository": False, "isDraft": False,
        "author": {"login": "owner"}, "body": created[created.index("--body") + 1], "labels": []}
    monkeypatch.setattr(dev, "finish_opening", original)
    assert devrun.make().pending_openings() == 0
    assert len(devrun.runs) == 1 and len(devrun.loops) == 1 and not path.exists()
    assert Path(devrun.env["STATE"], "pr-301-authors").read_text() == "codex\n"
    assert Path(devrun.env["STATE"], "pr-301-contrib").read_text().count("codex ") == 1


def test_lost_pr_create_response_finds_the_one_matching_pr(devrun, monkeypatch):
    devrun.script = [implements()]
    devrun.gh.fails.add("pr create")
    t, rc = devrun.go()
    assert rc == 1
    path = dev.opening_path(t, "300")
    assert json.loads(path.read_text())["pr"] == ""
    wt = Path(devrun.env["WT_ROOT"], "agent-300")
    devrun.gh.fails.remove("pr create")
    devrun.gh.answers["pr list"] = [{"number": 301}]
    devrun.gh.answers["pr view"] = {
        "state": "OPEN", "headRefName": "agent/issue-300", "headRefOid": sh(wt, "git", "rev-parse", "HEAD"),
        "baseRefName": "main", "isCrossRepository": False, "isDraft": False,
        "author": {"login": "owner"}, "body": "<!-- baton-task issue=300 completion=partial -->\nRefs #300",
        "labels": []}
    assert devrun.make().pending_openings() == 0
    assert not path.exists() and len(devrun.runs) == 1 and len(devrun.loops) == 1


def test_opening_recovery_rejects_mismatched_head(devrun, monkeypatch):
    devrun.script = [implements()]
    monkeypatch.setattr(dev, "finish_opening", lambda *a: (_ for _ in ()).throw(OSError("interrupted")))
    t, rc = devrun.go()
    assert rc == 1
    path = dev.opening_path(t, "300")
    devrun.gh.answers["pr view"] = {
        "state": "OPEN", "headRefName": "agent/issue-300", "headRefOid": "0" * 40,
        "baseRefName": "main", "isCrossRepository": False, "isDraft": False,
        "author": {"login": "owner"}, "body": "Refs #300", "labels": []}
    assert devrun.make().pending_openings() == 1
    assert path.exists() and not devrun.loops


def test_opening_recovery_waits_for_the_repository_lock(devrun, monkeypatch):
    devrun.script = [implements()]
    monkeypatch.setattr(dev, "finish_opening", lambda *a: (_ for _ in ()).throw(OSError("interrupted")))
    t, rc = devrun.go()
    assert rc == 1
    lock = devrun.path("repos/owner/app/lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert devrun.make().run() == 0
    assert dev.opening_path(t, "300").exists() and not devrun.loops


def test_opening_finalization_does_not_repeat_contribution_record(devrun):
    devrun.script = [implements()]
    t, rc = devrun.go()
    assert rc == 80
    state = Path(devrun.env["STATE"])
    contrib = (state / "pr-301-contrib").read_text()
    intent = {"issue": "300", "pr": "301", "vendor": "codex", "model": "gpt-x",
              "wt": str(Path(devrun.env["WT_ROOT"], "agent-300")),
              "base": sh(devrun.repo, "git", "rev-parse", "origin/main")}
    (state / "review-pending-301").touch()
    path = dev.opening_path(t, "300")
    dev.save_opening(path, intent)
    assert dev.finish_opening(t, intent, path) == 0
    assert (state / "pr-301-contrib").read_text() == contrib
    assert not path.exists()


def test_a_resumed_worktree_says_so(devrun):
    devrun.script = [implements()]
    sh(devrun.repo, "git", "worktree", "add", "-q", "-B", "agent/issue-300", str(Path(devrun.env["WT_ROOT"], "agent-300")), "origin/main")
    devrun.go()
    assert "earlier work on this issue" in devrun.runs[0]["prompt"]


def test_a_legacy_cursor_worktree_keeps_its_branch(devrun):
    devrun.script = [implements()]
    sh(devrun.repo, "git", "worktree", "add", "-q", "-B", "cursor/issue-300", str(Path(devrun.env["WT_ROOT"], "cursor-300")), "origin/main")
    devrun.go()
    assert "earlier work on this issue" in devrun.runs[0]["prompt"]
    create, = devrun.gh.ran("pr", "create")
    assert create[create.index("--head") + 1] == "cursor/issue-300"
    assert not Path(devrun.env["WT_ROOT"], "agent-300").exists()


def test_the_spec_runs_first_when_there_is_no_plan(devrun, monkeypatch):
    plans = iter(["", "the plan"])
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: next(plans))
    called = []
    monkeypatch.setattr(tick.Tick, "spec_loop", lambda self, n: called.append(n) or False)
    t, rc = devrun.go()
    assert rc == 80 and called == ["300"] and not devrun.runs   # the owner took over


def test_an_accepted_spec_without_a_plan_comment_is_an_error(devrun, monkeypatch):
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "")
    monkeypatch.setattr(tick.Tick, "spec_loop", lambda self, n: True)
    t, rc = devrun.go()
    assert rc == 1 and "spec accepted but no plan comment found" in devrun.log()


@pytest.mark.parametrize("step, reason", [
    (lambda cwd, out: {**DONE, "outcome": "RESULT: blocked no plan", "reply": "stuck\nRESULT: blocked no plan"}, "blocked no plan"),
    (lambda cwd, out: {**DONE, "outcome": "RESULT: failed boom", "reply": "x"}, "RESULT: failed boom"),
    (implements(commit=False), "reported ready but made no commit"),
    (implements(commit=True, body="", title="").__class__ and (lambda cwd, out: (implements()(cwd, out), Path(out).unlink(), dict(DONE))[2]), "wrote no PR description"),
    (implements(title=" "), "has no title on its first line")])
def test_a_run_that_is_not_ready_goes_to_the_owner(devrun, step, reason):
    devrun.script = [step]
    t, rc = devrun.go()
    assert rc == 80 and not devrun.gh.ran("pr", "create")
    note, = [c for c in devrun.gh.ran("issue", "comment")]
    assert body_of(note).startswith("<!-- baton-note by=codex -->\ncodex stopped: ") and reason in body_of(note)
    assert devrun.gh.ran("issue", "edit", "300", "-R", "owner/app", "--remove-label", "agent-wip", "--add-label", "ready-for-human")
    assert "#300: codex stopped" in devrun.log()


def _watch_handoffs(monkeypatch):
    seen = {"events": [], "notices": []}
    monkeypatch.setattr(tick.telemetry.Telemetry, "task", lambda self, *a: seen["events"].append(a))
    monkeypatch.setattr(tick.notify if hasattr(tick, "notify") else __import__("baton.notify").notify, "notify",
                        lambda title, text: seen["notices"].append((title, text)))
    return seen


def _reasons(seen):
    return [a[a.index("--reason") + 1] for a in seen["events"] if a[0] == "handoff"]


@pytest.mark.parametrize("step, reason", [
    (lambda cwd, out: {**DONE, "outcome": "RESULT: blocked no plan", "reply": "stuck\nRESULT: blocked no plan"}, "dev_blocked"),
    (lambda cwd, out: {**DONE, "outcome": "RESULT: failed boom", "reply": "x"}, "dev_run_failed"),
    (implements(commit=False), "dev_result_invalid"),
    (implements(body="Closes #999\n\nRefs #300"), "dev_task_invalid")])
def test_each_dev_failure_hands_off_once_with_its_reason(devrun, monkeypatch, step, reason):
    seen = _watch_handoffs(monkeypatch)
    devrun.script = [step]
    t, rc = devrun.go()
    assert rc == 80 and _reasons(seen) == [reason]
    assert [t for t, _ in seen["notices"] if "started" not in t] == [seen["notices"][-1][0]]
    assert len(devrun.gh.ran("issue", "edit", "300", "-R", "owner/app", "--remove-label", "agent-wip",
                             "--add-label", "ready-for-human")) == 1


@pytest.fixture
def advised_dev(devrun, monkeypatch):
    from baton import advisor
    from baton.config import Advisor

    monkeypatch.setenv(advisor.KEY, "sk-" + "d" * 24)
    monkeypatch.setattr(advisor, "settings", lambda env: Advisor(env_file=None, mode="live"))
    devrun.gh.answers["issue view"].update(state="OPEN", labels=[{"name": "ready-for-agent"}])
    monkeypatch.setattr(tick.Tick, "owner_ready", lambda self, issue: True)
    devrun.advice_calls, devrun.action = [], "retry_step_other_vendor"

    def advise(cfg, key, snapshot, actions, **kwargs):
        devrun.advice_calls.append((snapshot, actions))
        reason = snapshot.splitlines()[0].split(".")[0]
        return {"action": devrun.action, "reason": "try again", "evidence": [reason]}, {}

    monkeypatch.setattr(advisor, "call_deepseek", advise)
    return devrun


def test_dev_retry_does_not_publish_the_previous_description(advised_dev, monkeypatch):
    def failed(cwd, output):
        implements()(cwd, output)
        return {**DONE, "outcome": "RESULT: failed after committing"}

    advised_dev.script = [failed, lambda cwd, output: dict(DONE)]
    seen = _watch_handoffs(monkeypatch)
    _, rc = advised_dev.go()
    assert rc == 80 and not advised_dev.gh.ran("pr", "create")
    assert [attempt["vendor"] for attempt in advised_dev.runs] == ["codex", "claude"]
    assert _reasons(seen) == ["dev_result_invalid"]


def test_dev_retry_keeps_all_attempt_authors(advised_dev):
    def failed(cwd, output):
        implements()(cwd, output)
        return {**DONE, "outcome": "RESULT: failed after committing"}

    def second(cwd, output):
        Path(cwd, "second.txt").write_text("second\n")
        sh(cwd, "git", "add", "-A")
        sh(cwd, "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "more")
        return implements(commit=False)(cwd, output)

    advised_dev.script = [failed, second]
    _, rc = advised_dev.go()
    assert rc == 80 and advised_dev.gh.ran("pr", "create")
    assert set(advised_dev.path("pr-301-authors").read_text().split()) == {"codex", "claude"}


def test_dev_attempt_without_a_commit_is_not_an_author(advised_dev):
    advised_dev.script = [lambda cwd, output: {**DONE, "outcome": "RESULT: failed boom"}, implements()]
    _, rc = advised_dev.go()
    assert rc == 80 and advised_dev.gh.ran("pr", "create")
    assert advised_dev.path("pr-301-authors").read_text().split() == ["claude"]


def _commit(cwd, name):
    Path(cwd, name).write_text(f"{name}\n")
    sh(cwd, "git", "add", "-A")
    sh(cwd, "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", name)


def _defers(cwd, output):
    return {**DONE, "rc": 75, "defer": "short block"}


def test_dev_keeps_the_contribution_of_a_run_that_committed_and_deferred(devrun, monkeypatch):
    """Advisor off: the commit of a deferred run still counts for its author when another vendor finishes."""
    vendors_ = iter(["codex", "claude"])
    monkeypatch.setattr(tick.Tick, "pick_vendor", lambda self, role, mode, cands, **k: next(vendors_))

    def commits_and_defers(cwd, output):
        _commit(cwd, "first.txt")
        return _defers(cwd, output)

    def finishes(cwd, output):
        _commit(cwd, "second.txt")
        return implements(commit=False)(cwd, output)

    devrun.script = [commits_and_defers, finishes]
    assert devrun.go()[1] == 75
    assert devrun.go()[1] == 80 and devrun.gh.ran("pr", "create")
    assert set(devrun.path("pr-301-authors").read_text().split()) == {"codex", "claude"}
    owners = {row.split()[0] for row in devrun.path("pr-301-contrib").read_text().splitlines()}
    assert owners == {"codex", "claude"}   # the retained commit has its own row


def test_dev_deferral_after_a_granted_retry_committed_continues(advised_dev, monkeypatch):
    advised_dev.action = "retry_step"
    seen = _watch_handoffs(monkeypatch)

    def failed(cwd, output):
        _commit(cwd, "first.txt")
        return {**DONE, "outcome": "RESULT: failed after committing"}

    def retry(cwd, output):
        _commit(cwd, "second.txt")
        return implements(commit=False)(cwd, output)

    advised_dev.script = [failed, retry]
    calls = []

    def verify(self, *args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise Deferred("codex", "short block")
        return 0, "", 0

    monkeypatch.setattr(fix.Runner, "verify", verify)
    assert advised_dev.go()[1] == 75
    assert advised_dev.go()[1] == 80 and advised_dev.gh.ran("pr", "create")
    assert len(advised_dev.runs) == 2 and len(advised_dev.advice_calls) == 1   # the worker did not run again
    assert _reasons(seen) == []


def test_dev_worker_retry_survives_main_advancing_during_the_block(advised_dev, monkeypatch):
    from baton import names
    advised_dev.action = "retry_step"
    seen = _watch_handoffs(monkeypatch)
    advised_dev.script = [lambda cwd, output: {**DONE, "outcome": "RESULT: failed boom"}, _defers, implements()]
    assert advised_dev.go()[1] == 75
    _, wt = names.for_issue(Path(advised_dev.env["WT_ROOT"]), "300")
    tree = sh(wt, "git", "rev-parse", "origin/main^{tree}")
    tip = sh(wt, "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit-tree", tree,
             "-p", "origin/main", "-m", "main moved")
    sh(wt, "git", "update-ref", "refs/remotes/origin/main", tip)
    assert advised_dev.go()[1] == 80 and advised_dev.gh.ran("pr", "create")
    assert _reasons(seen) == [] and len(advised_dev.runs) == 3


def test_dev_blocked_after_a_granted_retry_hands_off_once(advised_dev, monkeypatch):
    advised_dev.action = "retry_step"
    seen = _watch_handoffs(monkeypatch)
    advised_dev.script = [lambda cwd, output: {**DONE, "outcome": "RESULT: failed boom"},
                          lambda cwd, output: {**DONE, "outcome": "RESULT: blocked no plan", "reply": "stuck"}]
    assert advised_dev.go()[1] == 80
    assert _reasons(seen) == ["dev_blocked"] and len(advised_dev.advice_calls) == 1
    assert len(advised_dev.runs) == 2


def test_dev_verification_retry_resumes_after_deferral(advised_dev, monkeypatch):
    monkeypatch.setattr(spec, "verdict_plan", lambda *a, **k: "<!-- baton-verdict: accept by=claude -->\nPlan")
    advised_dev.action = "retry_step"
    advised_dev.script = [implements()]
    calls = []

    def verify(self, *args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return 1, "lint broke\nLast 200 lines:\nraw excerpt", 2
        if len(calls) == 2:
            raise Deferred("codex", "short block")
        return 0, "", 2

    monkeypatch.setattr(fix.Runner, "verify", verify)
    assert advised_dev.go()[1] == 75
    assert advised_dev.go()[1] == 80
    assert len(advised_dev.runs) == len(advised_dev.advice_calls) == 1
    assert calls[1:] == [{"repairs": 0, "repairs_before": 2}] * 2
    assert "raw excerpt" not in advised_dev.advice_calls[0][0]
    assert advised_dev.gh.ran("pr", "create")


def test_dev_retry_hands_off_when_the_owner_withdraws_authorization(advised_dev, monkeypatch):
    from baton import advisor

    call = advisor.call_deepseek

    def withdraw(*args, **kwargs):
        advised_dev.gh.answers["issue view"]["labels"] = []
        return call(*args, **kwargs)

    monkeypatch.setattr(advisor, "call_deepseek", withdraw)
    advised_dev.script = [lambda cwd, output: {**DONE, "outcome": "RESULT: failed"}]
    assert advised_dev.go()[1] == 80
    assert len(advised_dev.runs) == len(advised_dev.advice_calls) == 1
    assert not advised_dev.gh.ran("pr", "create")


def test_dirty_fix_worktree_hands_off_without_advisor(ctx, monkeypatch):
    from baton import advisor
    from baton.config import Advisor
    pr_state(ctx)
    ctx.gh.answers["pr list"] = lambda args: (
        [{"number": 11, "headRefName": "agent/issue-5"}] if "agent-fix" in args else [])
    wt = Path(ctx.env["WT_ROOT"], "agent-5")
    wt.mkdir(parents=True)
    sh(wt, "git", "init", "-q", "-b", "agent/issue-5")
    sh(wt, "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "commit", "-q", "--allow-empty", "-m", "base")
    sh(wt, "git", "update-ref", "refs/remotes/origin/agent/issue-5", "HEAD")
    (wt / "stray.txt").write_text("local changes")
    ctx.gh.answers["pr view"] = {
        "state": "OPEN", "headRefOid": sh(wt, "git", "rev-parse", "HEAD"), "baseRefName": "main",
        "headRefName": "agent/issue-5", "labels": [{"name": "agent-fix"}],
        "body": "Refs #5", "comments": [], "reviews": [],
    }
    seen = _watch_handoffs(monkeypatch)
    calls = []
    monkeypatch.setenv(advisor.KEY, "sk-" + "d" * 24)
    monkeypatch.setattr(advisor, "settings", lambda env: Advisor(env_file=None, mode="live"))
    monkeypatch.setattr(advisor, "call_deepseek", lambda *a, **k: calls.append(1) or ({"action": "handoff"}, {}))
    real = tick.session.run_process

    def process(args, **kwargs):
        if "--ff-only" in args:
            return subprocess.CompletedProcess(args, 1, "", "")
        return real(args, **kwargs)

    monkeypatch.setattr(tick.session, "run_process", process)
    assert ctx.make().run() == 0
    assert not calls and _reasons(seen) == ["fix_worktree_diverged"]


@pytest.mark.parametrize("stale", [False, True])
def test_worktree_retry_checks_the_pr_before_retrying(ctx, worktree, monkeypatch, stale):
    from baton import advisor
    from baton.config import Advisor

    fix_ctx(ctx, worktree, monkeypatch)
    head = sh(worktree.wt, "git", "rev-parse", "HEAD")
    pr = {"state": "OPEN", "headRefOid": head, "baseRefName": "main",
          "headRefName": "cursor/issue-297", "labels": [{"name": "agent-fix"}],
          "body": "Refs #297", "comments": [], "reviews": []}
    ctx.gh.answers["pr view"] = lambda args: dict(pr)
    monkeypatch.setenv(advisor.KEY, "sk-" + "d" * 24)
    monkeypatch.setattr(advisor, "settings", lambda env: Advisor(env_file=None, mode="live"))

    def advise(*args, **kwargs):
        if stale:
            pr["state"] = "CLOSED"
        return {"action": "retry_step", "reason": "transient",
                "evidence": ["Decision point: fix_worktree_diverged"]}, {}

    monkeypatch.setattr(advisor, "call_deepseek", advise)
    real, merges = tick.session.run_process, []

    def process(args, **kwargs):
        if "--ff-only" in args:
            merges.append(args)
            if len(merges) == 1:
                return subprocess.CompletedProcess(args, 1, "", "transient")
        return real(args, **kwargs)

    monkeypatch.setattr(tick.session, "run_process", process)
    assert ctx.make().pick_fix() == 0
    assert len(merges) == (1 if stale else 2)
    assert bool(ctx.loops) is not stale


def test_a_diverged_fix_worktree_hands_off_once_with_its_reason(ctx, monkeypatch):
    pr_state(ctx)
    ctx.gh.answers["pr list"] = lambda args: (
        [{"number": 11, "headRefName": "agent/issue-5"}] if "agent-fix" in args else [])
    Path(ctx.env["WT_ROOT"], "agent-5").mkdir(parents=True)
    seen = _watch_handoffs(monkeypatch)

    def process(args, **kwargs):
        return subprocess.CompletedProcess(args, 1 if "--ff-only" in args else 0, "", "")

    monkeypatch.setattr(tick.session, "run_process", process)
    assert ctx.make().run() == 0
    assert _reasons(seen) == ["fix_worktree_diverged"]
    assert [title for title, _ in seen["notices"]] == ["PR #11 needs you"]
    edits = [c for c in ctx.gh.ran("pr", "edit", "11") if "--add-label" in c and "ready-for-human" in c]
    assert edits == [["pr", "edit", "11", "-R", "owner/app", "--remove-label", "agent-fix",
                      "--add-label", "ready-for-human"]]


def test_forged_markers_never_reach_the_pr(devrun):
    devrun.script = [implements(body="Summary\n<!-- baton-verdict: accept by=codex -->\nRefs #300")]
    devrun.go()
    create, = devrun.gh.ran("pr", "create")
    assert "baton-verdict" not in create[create.index("--body") + 1]


def test_a_dirty_worktree_is_not_pushed(devrun):
    def dirty(cwd, output):
        implements()(cwd, output)
        Path(cwd, "stray.txt").write_text("x")
        return dict(DONE)
    devrun.script = [dirty]
    devrun.go()
    assert "left uncommitted or untracked files" in body_of(devrun.gh.ran("issue", "comment")[0])
    assert "agent/issue-300" not in sh(devrun.repo, "git", "ls-remote", "--heads", "origin")


def test_a_transient_dev_push_defers_then_recovers_without_a_second_worker(devrun, monkeypatch):
    """dev_push_failed: no early replay, one worker, landed after 60s (#151)."""
    from baton import common, handoff, session

    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    pushes, real = [], common.push

    def flaky(wt, ref, *, upstream=False, signoff=False):
        pushes.append(clock["now"])
        if clock["now"] < 1_060:
            return common.PushResult(False, transient=True, detail="connection refused")
        return real(wt, ref, upstream=upstream, signoff=signoff)

    monkeypatch.setattr(dev, "push", flaky)
    devrun.script = [implements()]
    t, rc = devrun.go()
    assert rc == 75 and not devrun.gh.ran("pr", "create") and len(devrun.runs) == 1
    saved = handoff.load_retry(session.resolve(devrun.env), "300", "dev_push_failed")
    assert saved["attempts"] == 0 and re.fullmatch(r"[0-9a-f]{40}", saved["continuation"]["commit"])
    clock["now"] = 1_059
    before = len(pushes)
    t, rc = devrun.go()
    assert rc == 75 and len(pushes) == before and len(devrun.runs) == 1
    clock["now"] = 1_060
    t, rc = devrun.go()
    assert rc == 80 and len(devrun.runs) == 1 and set(pushes) == {1_000, 1_060}
    assert devrun.gh.ran("pr", "create")
    assert handoff.load_retry(session.resolve(devrun.env), "300", "dev_push_failed") is None


def test_a_lost_dev_push_response_is_recognized_without_a_second_write(devrun, monkeypatch):
    from baton import common

    pushes, real = [], common.push

    def write_then_fail(wt, ref, *, upstream=False, signoff=False):
        pushes.append(ref)
        result = real(wt, ref, upstream=upstream, signoff=signoff)
        assert result.ok
        return common.PushResult(False, transient=True, detail="connection reset")

    monkeypatch.setattr(dev, "push", write_then_fail)
    devrun.script = [implements()]
    t, rc = devrun.go()
    assert rc == 80 and len(pushes) == 1 and devrun.gh.ran("pr", "create")


def test_an_exhausted_dev_push_hands_off_exactly_once(devrun, monkeypatch):
    from baton import common, handoff, session

    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    sent = []
    monkeypatch.setattr(handoff.notify, "notify", lambda title, text="": sent.append((title, text)))
    monkeypatch.setattr(dev, "push",
                        lambda *a, **k: common.PushResult(False, transient=True, detail="connection refused"))
    devrun.script = [implements()]
    t, rc = devrun.go()
    assert rc == 75
    for now in (1_060, 1_360):
        clock["now"] = now
        t, rc = devrun.go()
        assert rc == (80 if now == 1_360 else 75)
    assert len(devrun.runs) == 1 and not devrun.gh.ran("pr", "create")
    assert sum("dev_push_failed" in f"{title} {text}" for title, text in sent) == 1
    assert handoff.load_retry(session.resolve(devrun.env), "300", "dev_push_failed") is None


def test_failed_verification_posts_the_failure(devrun):
    devrun.script, devrun.verify = [implements()], (1, "tests failed: x", 2)
    t, rc = devrun.go()
    assert rc == 80 and not devrun.gh.ran("pr", "create")
    note, = devrun.gh.ran("issue", "comment")
    assert "Local CI verification failed before push after 2 of 2 fix rounds. Nothing was pushed.\n\ntests failed: x" in body_of(note)
    assert "#300: local CI verification failed" in devrun.log()


def test_a_failed_pr_creation_keeps_the_pushed_branch(devrun):
    devrun.script = [implements()]
    devrun.gh.fails.add("pr create")
    t, rc = devrun.go()
    assert rc == 1 and "#300: PR creation needs reconciliation" in devrun.log()
    assert dev.opening_path(t, "300").exists()
    assert not devrun.loops


def test_a_deferred_dev_run_releases_the_issue(devrun):
    devrun.script = [lambda cwd, out: {**DONE, "rc": 75, "status": "deferred", "outcome": "", "defer": "codex cooldown"}]
    t, rc = devrun.go()
    assert rc == 75 and "DEFERRED: codex codex cooldown" in devrun.log()
    assert devrun.gh.ran("issue", "edit", "300", "-R", "owner/app", "--remove-label", "agent-wip")
    assert not list(devrun.path(".").glob("slot-deferred-*"))


def test_the_worktree_gets_the_agents_env_files(devrun, worktree):
    (worktree.repo / ".worktreeinclude").write_text("# secrets\n\nsub/.env.local\n")
    sh(worktree.repo, "git", "add", "-A")
    sh(worktree.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "include")
    sh(worktree.repo, "git", "push", "-q", "origin", "main")
    sh(worktree.repo, "git", "fetch", "-q", "origin")
    source = Path(devrun.env["HOME"], ".config/baton/env-files/sub/.env.local")
    source.parent.mkdir(parents=True)
    source.write_text("KEY=1\n")
    devrun.script = [implements()]
    devrun.go()
    assert Path(devrun.env["WT_ROOT"], "agent-300/sub/.env.local").read_text() == "KEY=1\n"


def test_opening_a_pr_files_its_side_bugs_before_the_review(devrun, monkeypatch):
    devrun.script = [implements()]
    seen = []
    monkeypatch.setattr(tick.Tick, "review_loop", lambda self, *a, **k: seen.append(list(devrun.filed)))
    devrun.go()
    assert seen == [[("301", "300")]]


def test_a_failure_filing_side_bugs_does_not_stop_the_review(devrun, monkeypatch):
    devrun.script = [implements()]
    monkeypatch.setattr(dev.after_merge, "file_pr_bugs", lambda pr, issue: (_ for _ in ()).throw(OSError("gh down")))
    seen = []
    monkeypatch.setattr(tick.Tick, "review_loop", lambda self, *a, **k: seen.append(True))
    devrun.go()
    assert seen == [True] and "filing side bugs failed" in devrun.log()


def test_queue_skips_blocked_spec_review_for_runnable_dev_issue(ctx, monkeypatch):
    issues(ctx, ready=[issue(12), issue(13)])
    ctx.gh.answers["issue view"] = lambda args: {"title": "T", "body": "B", "comments": [
        {"author": {"login": "owner"}, "body": "<!-- baton-verdict: accept -->"}
    ] if args[2] == "13" else []}
    t = ctx.make()
    config = t.env.config
    from dataclasses import replace
    from baton import pick, session

    cursor = config.vendor("cursor")
    config = replace(config, vendors=tuple(replace(v, roles=("dev",)) if v == cursor else v
                                           for v in config.vendors))
    t.env = session.create(config, repo=config.repos[0])
    for vendor in ("claude", "codex"):
        (config.paths.state / f"{vendor}-cooldown-until").write_text(str(t.now + 3600))
    probed = []
    monkeypatch.setattr(pick.settle, "probe", lambda state, logs, vendor, model: probed.append(vendor))
    assert t.next_issue() == "13"
    assert probed == ["cursor"]


def test_queue_admits_a_pending_accept_issue_when_only_dev_can_run(ctx, monkeypatch):
    """Pending ADR acceptance uses the dev role for vendor availability (#122)."""
    issues(ctx, ready=[issue(12)])
    monkeypatch.setattr(spec, "accept_pending",
                        lambda env, n, listed=None: {"pr": "77", "verdict": "x", "index": 0} if n == "12" else None)
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "")
    monkeypatch.setattr(spec, "spec_state", lambda env, n: ("", 0))
    t = ctx.make()
    config = t.env.config
    from dataclasses import replace
    from baton import pick, session

    cursor = config.vendor("cursor")
    config = replace(config, vendors=tuple(replace(v, roles=("dev",)) if v == cursor else v
                                           for v in config.vendors))
    t.env = session.create(config, repo=config.repos[0])
    for vendor in ("claude", "codex"):
        (config.paths.state / f"{vendor}-cooldown-until").write_text(str(t.now + 3600))
    probed = []
    monkeypatch.setattr(pick.settle, "probe", lambda state, logs, vendor, model: probed.append(vendor))
    assert t.next_issue() == "12"
    assert probed == []


def test_dev_run_does_not_dispatch_while_pending_accept_leaves_no_plan(devrun, monkeypatch):
    """While acceptance is pending, verdict_plan is empty so the tick resumes via spec_loop (#122)."""
    called = []
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "")
    monkeypatch.setattr(tick.Tick, "spec_loop", lambda self, n: called.append(n) or False)
    monkeypatch.setattr(tick.Tick, "pick_vendor", lambda *a, **k: pytest.fail("implementation vendor picked"))
    t = devrun.make()
    assert t.item(lambda: dev.run(t, devrun.issue)) == 80
    assert called == [str(devrun.issue)]


def test_dev_implements_the_saved_verdict_once_the_pending_accept_resumes(devrun, monkeypatch):
    """spec_loop publishes the verdict (superseding the pending marker); dev then gets that plan (#122)."""
    verdict = "<!-- baton-verdict: accept by=codex -->\n## Spec review (codex), round 1: accept\n\nsaved plan"
    listed = [{"author": {"login": "owner"}, "body": f"<!-- baton-spec-accept-pending pr=77 -->\n{verdict}"}]
    monkeypatch.setattr(spec, "verdict_plan", REAL_VERDICT_PLAN)
    devrun.gh.answers["issue view"] = {"title": "A title", "body": "A body", "comments": listed}
    assert spec.verdict_plan(devrun.make().env, "300") == ""   # pending: no plan yet
    called = []

    def resume(self, n):
        called.append(n)
        listed.append({"author": {"login": "owner"}, "body": verdict})
        return True
    monkeypatch.setattr(tick.Tick, "spec_loop", resume)
    devrun.script = [implements()]
    t, rc = devrun.go()
    assert rc == 80 and called == ["300"]
    run, = devrun.runs
    text = Path(re_context(run["prompt"])).read_text()
    assert f"<plan>\n{verdict}\n</plan>" in text


def test_dev_writer_without_fixer_role_hands_off_after_zero_repairs(devrun, monkeypatch):
    from subprocess import CompletedProcess

    devrun.script, devrun.verify = [implements()], None
    monkeypatch.setattr(tick.Tick, "pick_vendor", lambda *a, **kw: "cursor")
    monkeypatch.setattr(fix.Runner, "e2e_gate", lambda *a: "")
    monkeypatch.setattr(fix.Runner, "agent", lambda *a: pytest.fail("verification repair started"))
    run = fix.session.run_process

    def failing_verify(args, **kwargs):
        if Path(args[0]).name == "verify.sh":
            return CompletedProcess(args, 1, "CI failed", "")
        return run(args, **kwargs)

    monkeypatch.setattr(fix.session, "run_process", failing_verify)
    events = []
    monkeypatch.setattr(tick.telemetry.Telemetry, "task", lambda self, *a: events.append(a))
    t = devrun.make()
    from dataclasses import replace
    cfg = t.env.config
    cursor = cfg.vendor("cursor")
    cfg = replace(cfg, vendors=tuple(replace(v, roles=("dev",)) if v == cursor else v for v in cfg.vendors))
    t.env = replace(t.env, config=cfg)
    assert t.item(lambda: dev.run(t, devrun.issue)) == 80
    note, = devrun.gh.ran("issue", "comment")
    assert "after 0 of 2 fix rounds" in body_of(note)
    assert ("handoff", "dev_verify_failed", "--reason", "dev_verify_failed") in events
    assert not devrun.gh.ran("pr", "create")
    assert "agent/issue-300" not in sh(devrun.repo, "git", "ls-remote", "--heads", "origin")


@pytest.mark.parametrize("forced", [False, True])
def test_tick_run_counts_comment_reads_across_both_delays(ctx, monkeypatch, forced):
    from baton import handoff
    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    issues(ctx, ready=[issue(12)])
    views, workers = [], []
    ctx.gh.answers["pr list"] = lambda args: "" if "--jq" in args else []

    def issue_view(args):
        if "title,body,comments" in args:
            views.append(clock["now"])
            if clock["now"] < 1_360:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 503 boom")
            return {"title": "Recovered title", "body": "Recovered body", "comments": [
                {"id": 1, "author": {"login": "owner"},
                 "body": "<!-- baton-spec-reply round=2 by=claude -->\nRecovered reply"}
            ]}
        return {"labels": [{"name": "ready-for-agent"}], "author": {"login": "owner"},
                "state": "OPEN", "body": "Recovered body"}

    def worker(vendor, role, prompt, tag, *args, **kwargs):
        context, = Path(ctx.env["CTX"]).glob("issue-12-spec-3-review-*.md")
        workers.append((vendor, role, tag, context.read_text()))
        return {**DONE, "outcome": "RESULT: failed test stops after reading context"}

    ctx.gh.answers["issue view"] = issue_view
    monkeypatch.setattr(spec.dispatch, "dispatch", worker)
    for now, expected in [(1_000, [1_000]), (1_059, [1_000]), (1_060, [1_000, 1_060]),
                          (1_359, [1_000, 1_060]), (1_360, [1_000, 1_060, 1_360])]:
        clock["now"] = now
        ctx.make(*(("12",) if forced else ())).run()
        assert views == expected
        if now < 1_360:
            assert workers == []
    (vendor, role, tag, context), = workers
    assert vendor != "claude"
    assert (role, tag) == ("spec_review", "issue-12-spec-3")
    assert all(x in context for x in ("Recovered title", "Recovered body", "Recovered reply"))


def test_tick_resumes_pending_adr_even_with_agent_wip_and_no_vendor(ctx, monkeypatch):
    from baton import handoff
    now = 1_000
    monkeypatch.setattr(handoff, "clock", lambda: now)
    ready = issue(12, labels=["ready-for-agent"])
    issues(ctx, ready=[ready])
    reads = []
    verdict = ("<!-- baton-verdict: accept by=claude -->\n"
               "## Spec review (claude), round 1: accept\n\nAccepted plan")
    pending = f"<!-- baton-spec-accept-pending pr=77 head={'a' * 40} -->\n{verdict}"

    def read_issue(args):
        posted = [{"author": {"login": "owner"}, "body": body_of(call)} for call in ctx.gh.ran("issue", "comment")]
        return {"title": "Plan", "body": "Body", "state": "OPEN", "labels": ready["labels"], "comments": [
            {"author": {"login": "owner"}, "body": pending}, *posted]}

    def read_pr(args):
        if "state,headRefOid" not in args:   # dev's own look-up after the resumed accept
            return {}
        reads.append(now)
        if len(reads) == 1:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 503 unavailable")
        return {"state": "MERGED", "headRefOid": "a" * 40}

    ctx.gh.answers.update({"issue view": read_issue, "pr view": read_pr})
    # Accept write reconciliation reads through call (once=True); keep it on the same fake.
    monkeypatch.setattr(tick.pr_helpers, "call", ctx.gh)
    monkeypatch.setattr(tick.Tick, "pick_vendor", lambda *a, **k: pytest.fail("acceptance needs no worker"))
    started = []
    monkeypatch.setattr(dev, "setup_worktree", lambda *a: started.append(a) or ("", False))
    ctx.make().run()
    assert reads == [1_000]
    ready["labels"].append({"name": "agent-wip"})
    issues(ctx, ready=[ready], pending=1)
    ready["labels"].append({"name": "ready-for-human"})
    now = 1_060
    ctx.make().run()
    assert reads == [1_000]
    ready["labels"].pop()
    ctx.make().run()
    # Resume reads the PR once for acceptance, then again to revalidate the accept write (#151).
    assert reads == [1_000, 1_060, 1_060]
    assert [body_of(call) for call in ctx.gh.ran("issue", "comment")] == [verdict]
    assert len(started) == 1   # the accept went straight on to dev, not back to the queue


@pytest.mark.parametrize("changed", ["branch", "remote", "interrupted continuation", None])
def test_a_forced_fix_push_without_a_label_is_resumed_by_the_scheduler(ctx, worktree, monkeypatch, changed):
    from baton import common, handoff

    monkeypatch.setattr(tick.review, "human_feedback", lambda *a: tick.review.HumanFeedback(owner="please rename", findings=""))
    monkeypatch.setattr(tick.vendors, "seed_authors", lambda *a: 0)
    monkeypatch.setattr(tick.vendors, "seed_contrib", lambda *a: [])
    monkeypatch.setattr(tick.Tick, "pick_vendor", lambda *a, **kw: "codex")
    monkeypatch.setattr(fix.Runner, "verify", lambda *a: (0, "", 0))
    ctx.gh.answers["pr list"] = []

    def view(args):
        if "--jq" in args and ".headRefName" in args:
            return worktree.branch if args[2] == "20" else "feature/unrelated"
        return {"state": "OPEN", "labels": [], "headRefName": worktree.branch}

    ctx.gh.answers["pr view"] = view
    ctx.gh.answers["issue view"] = {"state": "OPEN", "labels": []}
    for route in ("issues/20/comments", "pulls/20/reviews", "pulls/20/comments"):
        ctx.gh.answers[f"api repos/owner/app/{route}"] = [[]]
    clock, workers, pushes = [1_000], [], []
    monkeypatch.setattr(handoff, "clock", lambda: clock[0])

    def worker(vendor, role, prompt, tag, cwd, *args):
        workers.append(vendor)
        sh(cwd, "git", "commit", "-qm", "Worker fix", "--allow-empty")
        return {"rc": 0, "outcome": "RESULT: fixed", "reply": "", "model": "test"}

    def push(*args, **kwargs):
        pushes.append(clock[0])
        if clock[0] == 1_000:
            return common.PushResult(False, transient=True, detail="connection refused")
        return common.push(*args, **kwargs)

    monkeypatch.setattr(fix.dispatch, "dispatch", worker)
    monkeypatch.setattr(fix, "push", push)
    monkeypatch.setattr(fix.Runner, "refresh_checks", lambda *a, **k: 0)
    assert ctx.make("pr", "20").run() == 75
    if changed == "branch":
        sh(worktree.wt, "git", "checkout", "-qb", "unrelated")
    if changed == "remote":
        old = sh(worktree.wt, "git", "rev-parse", "HEAD~1")
        tree = sh(worktree.wt, "git", "rev-parse", "HEAD^{tree}")
        other = sh(worktree.wt, "git", "commit-tree", tree, "-p", old, "-m", "Remote fix")
        sh(worktree.wt, "git", "push", "-q", "origin", f"{other}:refs/heads/{worktree.branch}")
    head = sh(worktree.wt, "git", "rev-parse", "HEAD")
    branch = sh(worktree.wt, "git", "branch", "--show-current")
    clock[0] = 1_059
    ctx.make().run()
    assert sh(worktree.wt, "git", "rev-parse", "HEAD") == head
    assert sh(worktree.wt, "git", "branch", "--show-current") == branch
    assert pushes == [1_000] and "handoff fix_" not in ctx.log()
    clock[0] = 1_060
    assert ctx.make("pr", "21").run() == 2
    assert pushes == [1_000] and "handoff fix_" not in ctx.log()
    if changed == "interrupted continuation":
        def interrupt(*args, **kwargs):
            raise InterruptedError("stopped during checks refresh")

        with monkeypatch.context() as stopped:
            stopped.setattr(fix.Runner, "refresh_checks", interrupt)
            assert ctx.make().run() == 1
        assert json.loads(ctx.path("owner-fix-feedback-20").read_text()) == {"owner": "please rename", "findings": ""}
    ctx.make().run()
    assert workers == ["codex"]
    if changed in ("branch", "remote"):
        assert pushes == [1_000] and "handoff fix_push_failed" in ctx.log()
    else:
        assert pushes == [1_000, 1_060] and len(ctx.loops) == 1
        assert ctx.loops[0][0][-2:] == ("codex", "297")
        assert "<feedback>\nplease rename\n</feedback>" in ctx.loops[0][0][2]


def test_a_push_wait_ends_a_previous_quota_slot_loan(ctx, monkeypatch):
    pr_state(ctx)
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11})
    defer_loop(monkeypatch)
    deferred_review(ctx, [11])
    no_vendor_blocked(monkeypatch)
    assert ctx.make().next_issue() == "12"

    def awaiting_push(*args, **kwargs):
        raise Deferred("handoff-retry", "fix_push_failed", lends_slot=False)

    monkeypatch.setattr(tick.Tick, "review_loop", awaiting_push)
    ctx.make().pending_reviews()
    assert ctx.make().next_issue() == ""
    assert not ctx.path("slot-deferred-11").exists()


def test_the_gate_line_names_the_signed_off_commit_that_is_pushed(devrun, monkeypatch):
    def verify(self, *args):
        self.gate_result = "E2E gate passed: e2e/a.spec.ts (1 passed)"
        return 0, "", 0
    monkeypatch.setattr(dev.fix.Runner, "verify", verify)
    from baton import config
    read = config.Repo.__getattribute__
    monkeypatch.setattr(config.Repo, "__getattribute__",
                        lambda self, name: True if name == "signoff" else read(self, name))
    devrun.script = [implements()]
    devrun.go()
    create, = devrun.gh.ran("pr", "create")
    pushed = sh(devrun.repo, "git", "ls-remote", "origin", "refs/heads/agent/issue-300").split()[0]
    assert f", on {pushed[:7]}." in body_of(create)


def _waiting_dev_push(devrun, monkeypatch, open_prs):
    from baton import common, handoff
    clock = {"now": 1_000}
    monkeypatch.setattr(handoff, "clock", lambda: clock["now"])
    monkeypatch.setattr(dev, "push",
                        lambda *a, **k: common.PushResult(False, transient=True, detail="connection refused"))
    devrun.script = [implements()]
    assert devrun.go()[1] == 75
    devrun.gh.answers["pr list"] = open_prs
    clock["now"] = 1_060
    return clock


def test_a_fork_pr_from_the_same_branch_name_does_not_supersede_a_dev_push(devrun, monkeypatch):
    from baton import handoff, session
    _waiting_dev_push(devrun, monkeypatch, [{"number": 9, "isCrossRepository": True}])
    t, rc = devrun.go()
    # The fork's PR is absence: the push is retried (it fails again, still transient), not dropped.
    assert handoff.load_retry(session.resolve(devrun.env), "300", "dev_push_failed") is not None
    assert len(devrun.runs) == 1


def test_a_same_repo_pr_from_the_branch_supersedes_a_dev_push(devrun, monkeypatch):
    from baton import handoff, session
    _waiting_dev_push(devrun, monkeypatch, [{"number": 9, "isCrossRepository": False}])
    t, rc = devrun.go()
    assert rc == 80 and not devrun.gh.ran("pr", "create")
    assert handoff.load_retry(session.resolve(devrun.env), "300", "dev_push_failed") is None


def test_tick_resumes_a_waiting_dev_push_only_when_due_and_unpaused(devrun, monkeypatch, capsys):
    from baton import common

    clock = _waiting_dev_push(devrun, monkeypatch, [])
    payload = {"title": "A title", "body": "A body", "comments": [], "state": "OPEN", "labels": []}
    devrun.gh.answers["issue view"] = lambda args: "OPEN" if "--jq" in args else payload
    pushes = []

    def push(wt, ref, **kwargs):
        pushes.append(clock["now"])
        return common.push(wt, ref, **kwargs)

    monkeypatch.setattr(dev, "push", push)
    clock["now"] = 1_059
    assert devrun.make("300").run() == 75
    assert "DEFERRED: handoff-retry dev_push_failed" in capsys.readouterr().err
    assert not pushes

    clock["now"] = 1_060
    payload["labels"] = [{"name": "ready-for-human"}]
    assert devrun.make().run() == 0
    saved = handoff.load_retry(devrun.make().env, "300", handoff.DEV_PUSH)
    assert saved["attempts"] == 0 and not pushes
    assert not devrun.gh.ran("pr", "create")

    payload["labels"] = []
    assert devrun.make().run() == 0
    assert pushes == [1_060] and len(devrun.runs) == 1
    assert len(devrun.gh.ran("pr", "create")) == 1
    assert handoff.load_retry(devrun.make().env, "300", handoff.DEV_PUSH) is None


def test_tick_cleanup_keeps_the_worktree_of_a_waiting_push(devrun, monkeypatch):
    clock = _waiting_dev_push(devrun, monkeypatch, [])
    wt = Path(devrun.runs[0]["cwd"])
    closed_worktree = wt.with_name("agent-301")
    closed_worktree.mkdir()
    devrun.gh.answers["issue view"] = lambda args: "CLOSED" if args[2] in {"300", "301"} else "OPEN"
    clock["now"] = 1_059

    assert devrun.make().run() == 0
    assert devrun.gh.ran("remove-worktree") == [["remove-worktree", str(closed_worktree)]]
    assert (wt / "new.txt").read_text() == "new\n"
    assert not devrun.gh.ran("pr", "create") and len(devrun.runs) == 1
    assert handoff.load_retry(devrun.make().env, "300", handoff.DEV_PUSH) is not None


@pytest.mark.parametrize("change", ["damaged", "missing", "feedback", "closed"])
def test_dev_push_resume_checks_the_saved_operation_and_issue(devrun, monkeypatch, change):
    _waiting_dev_push(devrun, monkeypatch, [])
    seen = _watch_handoffs(monkeypatch)
    env, reason = devrun.make().env, handoff.DEV_PUSH
    record = handoff.retry_path(env, "300", reason)
    if change == "damaged":
        record.write_text("{")
    elif change == "missing":
        record.unlink()
        handoff.waiting_path(env, "300", reason).unlink()
    elif change == "feedback":
        devrun.gh.answers["issue view"]["comments"] = [
            {"id": 1, "author": {"login": "owner"}, "body": "Please change the implementation"},
        ]
    else:
        devrun.gh.answers["issue view"]["state"] = "CLOSED"

    def unexpected_push(*args, **kwargs):
        pytest.fail("The original push is no longer safe to resume")

    monkeypatch.setattr(dev, "push", unexpected_push)
    assert devrun.go()[1] == 80
    assert len(devrun.runs) == 1 and not devrun.gh.ran("pr", "create")
    assert _reasons(seen) == ([] if change == "closed" else [reason])
    if change == "closed":
        assert not seen["notices"]
    assert handoff.load_retry(env, "300", reason) is None


def test_a_saved_landed_dev_push_resumes_without_a_second_push(devrun, monkeypatch):
    from baton import common

    monkeypatch.setattr(handoff, "clock", lambda: 1_000)
    restore = handoff.restore_retry
    pushes = []

    def push(wt, ref, **kwargs):
        pushes.append(ref)
        return common.push(wt, ref, **kwargs)

    def interrupt(env, episode, reason, item, pending):
        restore(env, episode, reason, item, pending)
        if reason == handoff.DEV_PUSH and pending.get("phase") == "landed":
            raise InterruptedError

    monkeypatch.setattr(dev, "push", push)
    devrun.script = [implements()]
    with monkeypatch.context() as crash:
        crash.setattr(handoff, "restore_retry", interrupt)
        with pytest.raises(InterruptedError):
            dev.run(devrun.make(), "300")
    env = devrun.make().env
    assert handoff.load_retry(env, "300", handoff.DEV_PUSH)["phase"] == "landed"
    assert not devrun.gh.ran("pr", "create")

    assert devrun.go()[1] == 80
    assert len(pushes) == len(devrun.runs) == 1
    assert len(devrun.gh.ran("pr", "create")) == 1
    assert handoff.load_retry(env, "300", handoff.DEV_PUSH) is None


def test_item_finalization_clears_the_recorded_reply_owner_files(ctx):
    tick_run = ctx.make()
    tick_run.begin_item()
    tick_run.active["cursor_fix"] = "21"
    names = ("pr-21-fix-owner", "stub-fix-base-21", "stub-fix-task-21", "cursor-fix-base-21", "pr-body-base-21")
    for name in names:
        ctx.path(name).write_text('{"fixer": "stub", "base": "abc"}' if name.endswith("owner") else "x\n")
    tick_run.finish_item(1)
    for name in names:
        assert not ctx.path(name).exists(), name


# --- an agent-wip issue without an open PR releases its slot (#155) ----------------------------------------------
def history(ctx, n, closed=(), merged=(), fail=False):
    """PR history of issue N on its agent branch; the legacy branch has none."""
    def pr(number, merged_at):
        return {"number": number, "state": "closed", "merged_at": merged_at, "base": {"ref": "main"},
                "head": {"ref": f"agent/issue-{n}", "repo": {"full_name": "owner/app"}}}
    key = f"api repos/owner/app/pulls?state=all&head=owner:agent/issue-{n}&per_page=100"
    if not fail:
        ctx.gh.answers[key] = [[pr(x, None) for x in closed] + [pr(x, "2026-01-01T00:00:00Z") for x in merged]]
    ctx.gh.answers[f"api repos/owner/app/pulls?state=all&head=owner:cursor/issue-{n}&per_page=100"] = [[]]


def wip(ctx, n=5, ready=()):
    ctx.gh.answers["issue list"] = lambda args: [{"number": n}] if "agent-wip" in args else (
        [] if "claude-found" in args else list(ready))
    for item in ready:
        label_history(ctx, item["number"], [label_event(1, "owner")])


def released(ctx, n):
    return [c for c in ctx.gh.calls if c[:3] == ["issue", "edit", str(n)] and "agent-wip" in c]


def test_an_issue_whose_only_pr_was_closed_is_released_and_the_next_one_starts(ctx, monkeypatch):
    wip(ctx, ready=[issue(12)])
    history(ctx, 5, closed=[146])
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    assert ctx.make().next_issue() == "12"
    assert released(ctx, 5) and "#5: released agent-wip (no open PR)" in ctx.log()


def test_a_released_issue_that_is_not_eligible_is_not_started(ctx):
    wip(ctx, ready=[issue(12, labels=["ready-for-agent", "needs-info"])])
    history(ctx, 5, closed=[146])
    assert ctx.make().next_issue() == ""
    assert released(ctx, 5)


def test_the_newest_pr_decides_not_an_older_merged_one(ctx):
    wip(ctx)
    history(ctx, 5, closed=[9], merged=[7])
    ctx.make().next_issue()
    assert released(ctx, 5)


def test_an_unsettled_merge_keeps_the_label_without_holding_a_slot(ctx, monkeypatch):
    wip(ctx, ready=[issue(12)])
    history(ctx, 5, merged=[7])
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    assert ctx.make().next_issue() == "12"
    assert not released(ctx, 5) and "#5: merged PR #7 is not settled" in ctx.log()


def test_a_settled_merge_releases_the_label(ctx):
    wip(ctx)
    history(ctx, 5, merged=[7])
    ctx.path("task-merged-pr-7").touch()
    ctx.make().next_issue()
    assert released(ctx, 5)


@pytest.mark.parametrize("failure", ["lookup", "label"])
def test_a_failed_release_leaves_the_label_and_does_not_block_another_issue(ctx, monkeypatch, failure):
    wip(ctx, ready=[issue(12)])
    history(ctx, 5, closed=[146], fail=failure == "lookup")
    if failure == "label":
        ctx.gh.fails.add("issue edit")
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    assert ctx.make().next_issue() == "12"
    assert "#5:" in ctx.log() and "not released" in ctx.log()


def test_run_releases_a_closed_pr_issue_and_dispatches_dev_for_the_next(ctx, monkeypatch):
    started = []
    monkeypatch.setattr(dev, "run", lambda t, n: started.append(n) or 80)
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    wip(ctx, ready=[issue(12)])
    history(ctx, 5, closed=[146])
    assert ctx.make().run() == 0
    assert released(ctx, 5) and "#5: released agent-wip (no open PR)" in ctx.log()
    assert started == ["12"]


@pytest.mark.parametrize("failure", ["unsettled", "lookup"])
def test_run_keeps_an_unreleasable_label_uncounted_and_starts_another(ctx, monkeypatch, failure):
    started = []
    monkeypatch.setattr(dev, "run", lambda t, n: started.append(n) or 80)
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    wip(ctx, ready=[issue(12)])
    if failure == "unsettled":
        history(ctx, 5, merged=[7])
    else:
        history(ctx, 5, closed=[146], fail=True)
    assert ctx.make().run() == 0
    assert not released(ctx, 5)
    assert started == ["12"]


# --- settlement records, ownership, worktree selection & intent reconciliation (#155) ----------------------------
REAL_REMOVE_WORKTREE = merge.remove_worktree


def rest_pr(number, branch, *, state="closed", merged_at=None, sha="a" * 40, login="owner", body="",
            repo="owner/app", base="main"):
    return {
        "number": number,
        "state": state,
        "merged_at": merged_at,
        "base": {"ref": base},
        "head": {"ref": branch, "sha": sha, "repo": {"full_name": repo}},
        "user": {"login": login},
        "body": body,
    }


def set_branch_history(ctx, branch, pages, fail=False):
    key = f"api repos/owner/app/pulls?state=all&head=owner:{branch}&per_page=100"
    if fail:
        ctx.gh.answers[key] = subprocess.CompletedProcess(["gh", "api", key], 1, "", "boom")
    else:
        ctx.gh.answers[key] = pages


def test_numberless_intent_for_old_merged_partial_pr_settles_and_enables_next_slice(devrun, monkeypatch):
    monkeypatch.setattr(merge, "remove_worktree", REAL_REMOVE_WORKTREE)
    wt = Path(devrun.env["WT_ROOT"]) / "agent-300"
    sh(devrun.repo, "git", "worktree", "add", "-q", "-b", "agent/issue-300", str(wt), "origin/main")
    (wt / "slice1.txt").write_text("one\n")
    sh(wt, "git", "add", "-A")
    sh(wt, "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "slice 1")
    head = sh(wt, "git", "rev-parse", "HEAD")
    base = sh(devrun.repo, "git", "rev-parse", "origin/main")
    sh(wt, "git", "push", "-q", "origin", "HEAD:main")
    sh(devrun.repo, "git", "fetch", "-q", "origin")

    intent_path = dev.opening_path(devrun.make(), "300")
    dev.save_opening(intent_path, {
        "issue": "300", "branch": "agent/issue-300", "wt": str(wt),
        "vendor": "codex", "model": "gpt-x", "head": head, "base": base,
    })
    body = "<!-- baton-task issue=300 completion=partial -->\n\nRefs #300\n\n## Acceptance advanced\n- one\n"
    devrun.gh.answers["pr list"] = []
    set_branch_history(devrun, "agent/issue-300", [[
        rest_pr(301, "agent/issue-300", merged_at="2025-01-01T00:00:00Z", sha=head, body=body)
    ]])
    set_branch_history(devrun, "cursor/issue-300", [[]])
    devrun.gh.answers["pr view"] = {"headRefName": "agent/issue-300", "body": body}
    devrun.gh.answers["issue view"] = lambda args: "OPEN\n" if "--jq" in args else {"title": "T", "body": "B", "comments": []}
    after_calls = []
    monkeypatch.setattr(merge.after_merge, "follow_ups", lambda pr, issue: after_calls.append(("follow_ups", pr, issue)))
    monkeypatch.setattr(merge.after_merge, "owner_actions", lambda pr, issue: after_calls.append(("owner_actions", pr, issue)))

    assert devrun.make().run() == 0
    assert not intent_path.exists()
    assert devrun.path("task-merged-pr-301").exists()
    assert not devrun.path("task-settle-pr-301.json").exists()
    assert not wt.exists()
    assert ("follow_ups", "301", "300") in after_calls and ("owner_actions", "301", "300") in after_calls
    edits = [c for c in devrun.gh.ran("issue", "edit", "300") if "--remove-label" in c and "agent-wip" in c]
    assert edits and "--add-label" in edits[0] and "ready-for-agent" in edits[0]


def test_complete_merged_pr_settles_across_two_ticks_via_record(ctx, monkeypatch):
    started = []
    monkeypatch.setattr(dev, "run", lambda t, n: started.append(n) or 80)
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    after_calls = []
    monkeypatch.setattr(merge.after_merge, "follow_ups", lambda pr, issue: after_calls.append(("follow_ups", pr, issue)))
    monkeypatch.setattr(merge.after_merge, "owner_actions", lambda pr, issue: after_calls.append(("owner_actions", pr, issue)))

    body = "<!-- baton-task issue=5 completion=complete -->\n\nCloses #5\n"
    wip(ctx, n=5, ready=[issue(12)])
    set_branch_history(ctx, "agent/issue-5", [[
        rest_pr(7, "agent/issue-5", merged_at="2025-01-01T00:00:00Z", body=body)
    ]])
    set_branch_history(ctx, "cursor/issue-5", [[]])
    ctx.gh.answers["pr view"] = {"headRefName": "agent/issue-5", "body": body}
    ctx.gh.answers["issue view"] = lambda args: "OPEN\n" if "--jq" in args else {"title": "T", "body": "B", "comments": []}

    assert ctx.make().run() == 0
    assert ctx.path("task-settle-pr-7.json").exists()
    assert not ctx.path("task-merged-pr-7").exists()
    assert not released(ctx, 5)
    assert started == ["12"]

    # Second tick: issue #5 is closed and absent from open listing; retry settles it.
    started.clear()
    ctx.gh.answers["issue list"] = []
    ctx.gh.answers["issue view"] = lambda args: "CLOSED\n" if "--jq" in args else {"title": "T", "body": "B", "comments": []}
    assert ctx.make().run() == 0
    assert ctx.path("task-merged-pr-7").exists()
    assert not ctx.path("task-settle-pr-7.json").exists()
    edits = [c for c in ctx.gh.ran("issue", "edit", "5") if "--remove-label" in c]
    assert edits and "agent-wip" in edits[-1] and "ready-for-agent" in edits[-1]


@pytest.mark.parametrize("mode", ["numberless_intent", "numbered_intent", "numbered_intent_and_checkpoint", "checkpoint_only"])
def test_intent_and_checkpoint_settle_merged_pr_after_issue_closed(ctx, monkeypatch, mode):
    after_calls = []
    monkeypatch.setattr(merge.after_merge, "follow_ups", lambda pr, issue: after_calls.append(("follow_ups", pr, issue)))
    monkeypatch.setattr(merge.after_merge, "owner_actions", lambda pr, issue: after_calls.append(("owner_actions", pr, issue)))
    body = "<!-- baton-task issue=5 completion=complete -->\n\nCloses #5\n"
    head, base = "a" * 40, "b" * 40
    wt = Path(ctx.env["WT_ROOT"]) / "agent-5"

    intent_path = dev.opening_path(ctx.make(), "5")
    if mode in ("numberless_intent", "numbered_intent", "numbered_intent_and_checkpoint"):
        intent = {"issue": "5", "branch": "agent/issue-5", "wt": str(wt), "vendor": "codex",
                  "model": "gpt-x", "head": head, "base": base}
        if mode != "numberless_intent":
            intent["pr"] = "7"
        dev.save_opening(intent_path, intent)
    if mode in ("numbered_intent_and_checkpoint", "checkpoint_only"):
        checkpoint(ctx, pr="7", ISSUE="5")

    ctx.gh.answers["issue list"] = []
    ctx.gh.answers["pr list"] = []
    set_branch_history(ctx, "agent/issue-5", [[
        rest_pr(7, "agent/issue-5", merged_at="2025-01-01T00:00:00Z", sha=head, body=body)
    ]])
    set_branch_history(ctx, "cursor/issue-5", [[]])
    view_payload = {"state": "MERGED", "headRefName": "agent/issue-5", "headRefOid": "c" * 40,
                    "baseRefName": "main", "isCrossRepository": False, "isDraft": False,
                    "author": {"login": "owner"}, "body": body, "labels": []}
    ctx.gh.answers["pr view"] = lambda args: "MERGED\n" if "--jq" in args else view_payload
    ctx.gh.answers["issue view"] = lambda args: "CLOSED\n" if "--jq" in args else {"title": "T", "body": "B", "comments": []}

    assert ctx.make().run() == 0
    assert ctx.path("task-merged-pr-7").exists()
    assert not intent_path.exists()
    assert not ctx.path("review-pending-7").exists()
    assert not ctx.path("task-settle-pr-7.json").exists()


@pytest.mark.parametrize("mode", ["numbered_intent_and_checkpoint", "checkpoint_only"])
def test_failed_record_write_keeps_intent_or_checkpoint(ctx, monkeypatch, mode):
    body = "<!-- baton-task issue=5 completion=complete -->\n\nCloses #5\n"
    wt = Path(ctx.env["WT_ROOT"]) / "agent-5"
    intent_path = dev.opening_path(ctx.make(), "5")
    if mode == "numbered_intent_and_checkpoint":
        dev.save_opening(intent_path, {"issue": "5", "pr": "7", "branch": "agent/issue-5", "wt": str(wt),
                                       "vendor": "codex", "model": "gpt-x", "head": "a" * 40, "base": "b" * 40})
    checkpoint(ctx, pr="7", ISSUE="5")
    view_payload = {"state": "MERGED", "headRefName": "agent/issue-5", "baseRefName": "main",
                    "isCrossRepository": False, "author": {"login": "owner"}, "body": body}
    ctx.gh.answers["pr view"] = lambda args: "MERGED\n" if "--jq" in args else view_payload
    monkeypatch.setattr(tick.Tick, "record_merged_settlement", lambda self, pr, issue: False)

    ctx.make().run()
    if mode == "numbered_intent_and_checkpoint":
        assert intent_path.exists()
    else:
        assert ctx.path("review-pending-7").exists()


def test_failed_settlement_keeps_record_without_blocking_other_work(ctx, monkeypatch):
    started = []
    monkeypatch.setattr(dev, "run", lambda t, n: started.append(n) or 80)
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    ctx.path("task-settle-pr-7.json").write_text(json.dumps({"pr": "7", "issue": "5"}) + "\n")
    body = "<!-- baton-task issue=5 completion=partial -->\n\nRefs #5\n"
    set_branch_history(ctx, "agent/issue-5", [[]])
    set_branch_history(ctx, "cursor/issue-5", [[]])
    ctx.gh.answers["pr view"] = {"headRefName": "agent/issue-5", "body": body}
    ctx.gh.answers["issue view"] = lambda args: subprocess.CompletedProcess(args, 1, "", "boom") if "--jq" in args else {"title": "T", "body": "B", "comments": []}
    ctx.gh.answers["issue list"] = lambda args: [] if ("agent-wip" in args or "claude-found" in args) else [issue(12)]
    label_history(ctx, 12, [label_event(1, "owner")])

    assert ctx.make().run() == 0
    assert ctx.path("task-settle-pr-7.json").exists()
    assert not ctx.path("task-merged-pr-7").exists()
    assert started == ["12"]


@pytest.mark.parametrize("path_kind", ["scan", "record", "intent"])
def test_superseded_merged_partial_pr_leaves_task_alone_on_every_path(ctx, monkeypatch, path_kind):
    monkeypatch.setattr(merge, "remove_worktree", REAL_REMOVE_WORKTREE)
    after_calls = []
    monkeypatch.setattr(merge.after_merge, "follow_ups", lambda pr, issue: after_calls.append(("follow_ups", pr, issue)))
    monkeypatch.setattr(merge.after_merge, "owner_actions", lambda pr, issue: after_calls.append(("owner_actions", pr, issue)))
    wt_root = Path(ctx.env["WT_ROOT"])
    wt_root.mkdir(exist_ok=True)
    wt_cursor = wt_root / "cursor-5"
    wt_agent = wt_root / "agent-5"
    sh(ctx.repo, "git", "worktree", "add", "-q", "-b", "cursor/issue-5", str(wt_cursor), "origin/main")
    sh(ctx.repo, "git", "worktree", "add", "-q", "-b", "agent/issue-5", str(wt_agent), "origin/main")

    body7 = "<!-- baton-task issue=5 completion=partial -->\n\nRefs #5\n"
    head7 = sh(wt_cursor, "git", "rev-parse", "HEAD")
    wip(ctx, n=5)
    set_branch_history(ctx, "cursor/issue-5", [[
        rest_pr(7, "cursor/issue-5", merged_at="2025-01-01T00:00:00Z", sha=head7, body=body7)
    ]])
    set_branch_history(ctx, "agent/issue-5", [[
        rest_pr(9, "agent/issue-5", merged_at=None, body="Refs #5")
    ]])
    ctx.gh.answers["pr view"] = {"state": "MERGED", "headRefName": "cursor/issue-5", "headRefOid": head7,
                                 "baseRefName": "main", "isCrossRepository": False, "isDraft": False,
                                 "author": {"login": "owner"}, "body": body7, "labels": []}
    ctx.gh.answers["issue view"] = lambda args: "OPEN\n" if "--jq" in args else {"title": "T", "body": "B", "comments": []}
    if path_kind == "scan":
        ctx.gh.answers["pr list"] = lambda args: [
            {"number": 7, "headRefName": "cursor/issue-5", "body": body7,
             "comments": [{"author": {"login": "owner"}, "body": "<!-- baton-review by=codex -->\nLGTM"}]}
        ] if "--state" in args and args[args.index("--state") + 1] == "merged" else []
    elif path_kind == "record":
        ctx.path("task-settle-pr-7.json").write_text(json.dumps({"pr": "7", "issue": "5"}) + "\n")
    else:
        dev.save_opening(dev.opening_path(ctx.make(), "5"), {
            "issue": "5", "pr": "7", "branch": "cursor/issue-5", "wt": str(wt_cursor),
            "vendor": "codex", "model": "gpt-x", "head": head7, "base": head7,
        })

    assert ctx.make().run() == 0
    assert ctx.path("task-merged-pr-7").exists()
    assert not ctx.path("task-settle-pr-7.json").exists()
    assert wt_cursor.is_dir() and wt_agent.is_dir()
    assert "PR #7: superseded by PR #9; task left alone" in ctx.log()
    assert ("follow_ups", "7", "5") in after_calls
    # Part 1 release still ran for #5 because its newest PR #9 is closed.
    assert released(ctx, 5)
    assert not [c for c in ctx.gh.ran("issue", "edit", "5") if "--add-label" in c]


def test_failed_newer_pr_lookup_changes_nothing_and_retries(ctx, monkeypatch):
    monkeypatch.setattr(merge, "remove_worktree", REAL_REMOVE_WORKTREE)
    wt_root = Path(ctx.env["WT_ROOT"])
    wt_root.mkdir(exist_ok=True)
    wt_cursor = wt_root / "cursor-5"
    sh(ctx.repo, "git", "worktree", "add", "-q", "-b", "cursor/issue-5", str(wt_cursor), "origin/main")
    ctx.path("task-settle-pr-7.json").write_text(json.dumps({"pr": "7", "issue": "5"}) + "\n")
    body7 = "<!-- baton-task issue=5 completion=partial -->\n\nRefs #5\n"
    set_branch_history(ctx, "cursor/issue-5", [[rest_pr(7, "cursor/issue-5", merged_at="2025-01-01T00:00:00Z", body=body7)]])
    set_branch_history(ctx, "agent/issue-5", [], fail=True)
    ctx.gh.answers["pr view"] = {"headRefName": "cursor/issue-5", "body": body7}
    ctx.gh.answers["issue view"] = lambda args: "OPEN\n" if "--jq" in args else {"title": "T", "body": "B", "comments": []}
    ctx.gh.answers["issue list"] = []

    assert ctx.make().run() == 0
    assert ctx.path("task-settle-pr-7.json").exists()
    assert not ctx.path("task-merged-pr-7").exists()
    assert wt_cursor.is_dir()
    assert not ctx.gh.ran("issue", "edit")


@pytest.mark.parametrize("head_branch", ["agent/issue-300", "cursor/issue-300"])
def test_settlement_removes_only_merged_branches_worktree(ctx, monkeypatch, head_branch):
    monkeypatch.setattr(merge, "remove_worktree", REAL_REMOVE_WORKTREE)
    monkeypatch.setattr(merge.after_merge, "follow_ups", lambda *a: None)
    monkeypatch.setattr(merge.after_merge, "owner_actions", lambda *a: None)
    wt_root = Path(ctx.env["WT_ROOT"])
    wt_root.mkdir(exist_ok=True)
    wt_agent = wt_root / "agent-300"
    wt_cursor = wt_root / "cursor-300"
    sh(ctx.repo, "git", "worktree", "add", "-q", "-b", "agent/issue-300", str(wt_agent), "origin/main")
    sh(ctx.repo, "git", "worktree", "add", "-q", "-b", "cursor/issue-300", str(wt_cursor), "origin/main")
    merged_wt, other_wt = (wt_agent, wt_cursor) if head_branch.startswith("agent/") else (wt_cursor, wt_agent)
    (other_wt / "dirty.txt").write_text("keep me\n")

    body = "<!-- baton-task issue=300 completion=partial -->\n\nRefs #300\n"
    ctx.path("task-settle-pr-7.json").write_text(json.dumps({"pr": "7", "issue": "300"}) + "\n")
    set_branch_history(ctx, head_branch, [[rest_pr(7, head_branch, merged_at="2025-01-01T00:00:00Z", body=body)]])
    other_branch = "cursor/issue-300" if head_branch.startswith("agent/") else "agent/issue-300"
    set_branch_history(ctx, other_branch, [[]])
    ctx.gh.answers["pr view"] = {"headRefName": head_branch, "body": body}
    ctx.gh.answers["issue view"] = lambda args: "OPEN\n" if "--jq" in args else {"title": "T", "body": "B", "comments": []}
    ctx.gh.answers["issue list"] = []

    assert ctx.make().run() == 0
    assert ctx.path("task-merged-pr-7").exists()
    assert not merged_wt.exists()
    assert other_wt.is_dir() and (other_wt / "dirty.txt").read_text() == "keep me\n"


def test_open_pr_on_other_branch_prevents_settlement(ctx, monkeypatch):
    monkeypatch.setattr(merge, "remove_worktree", REAL_REMOVE_WORKTREE)
    monkeypatch.setattr(merge.after_merge, "follow_ups", lambda *a: None)
    monkeypatch.setattr(merge.after_merge, "owner_actions", lambda *a: None)
    wt_root = Path(ctx.env["WT_ROOT"])
    wt_root.mkdir(exist_ok=True)
    wt_agent = wt_root / "agent-300"
    wt_cursor = wt_root / "cursor-300"
    sh(ctx.repo, "git", "worktree", "add", "-q", "-b", "agent/issue-300", str(wt_agent), "origin/main")
    sh(ctx.repo, "git", "worktree", "add", "-q", "-b", "cursor/issue-300", str(wt_cursor), "origin/main")

    body = "<!-- baton-task issue=300 completion=partial -->\n\nRefs #300\n"
    ctx.path("task-settle-pr-7.json").write_text(json.dumps({"pr": "7", "issue": "300"}) + "\n")
    set_branch_history(ctx, "cursor/issue-300", [[rest_pr(7, "cursor/issue-300", merged_at="2025-01-01T00:00:00Z", body=body)]])
    set_branch_history(ctx, "agent/issue-300", [[rest_pr(10, "agent/issue-300", state="open", merged_at=None, body=body)]])
    ctx.gh.answers["pr view"] = {"headRefName": "cursor/issue-300", "body": body}
    ctx.gh.answers["issue view"] = lambda args: "OPEN\n" if "--jq" in args else {"title": "T", "body": "B", "comments": []}
    ctx.gh.answers["issue list"] = []

    assert ctx.make().run() == 0
    assert wt_agent.is_dir() and wt_cursor.is_dir()
    assert not ctx.gh.ran("issue", "edit")


def test_numberless_intent_closed_pr_retires_and_picker_runs(ctx, monkeypatch):
    started = []
    monkeypatch.setattr(dev, "run", lambda t, n: started.append(n) or 80)
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    head, base = "a" * 40, "b" * 40
    wt = Path(ctx.env["WT_ROOT"]) / "agent-5"
    intent_path = dev.opening_path(ctx.make(), "5")
    dev.save_opening(intent_path, {"issue": "5", "branch": "agent/issue-5", "wt": str(wt),
                                   "vendor": "codex", "model": "gpt-x", "head": head, "base": base})
    wip(ctx, n=5, ready=[issue(12)])
    ctx.gh.answers["pr list"] = []
    set_branch_history(ctx, "agent/issue-5", [[rest_pr(146, "agent/issue-5", sha=head, body="Refs #5")]])
    set_branch_history(ctx, "cursor/issue-5", [[]])

    assert ctx.make().run() == 0
    assert not intent_path.exists()
    assert released(ctx, 5)
    assert started == ["12"]


@pytest.mark.parametrize("reason", ["lookup_failed", "mismatched_head", "mismatched_author",
                                    "ambiguous_second_page", "malformed_second_page"])
def test_numberless_intent_retained_on_failed_mismatched_or_ambiguous_history(ctx, reason):
    head, base = "a" * 40, "b" * 40
    wt = Path(ctx.env["WT_ROOT"]) / "agent-5"
    intent_path = dev.opening_path(ctx.make(), "5")
    dev.save_opening(intent_path, {"issue": "5", "branch": "agent/issue-5", "wt": str(wt),
                                   "vendor": "codex", "model": "gpt-x", "head": head, "base": base})
    ctx.gh.answers["pr list"] = []
    page1 = [rest_pr(100 + i, "agent/issue-5", sha="f" * 40, body="Refs #5") for i in range(99)]
    if reason == "lookup_failed":
        set_branch_history(ctx, "agent/issue-5", [], fail=True)
    elif reason == "mismatched_head":
        set_branch_history(ctx, "agent/issue-5", [[rest_pr(146, "agent/issue-5", sha="0" * 40, body="Refs #5")]])
    elif reason == "mismatched_author":
        set_branch_history(ctx, "agent/issue-5", [[rest_pr(146, "agent/issue-5", sha=head, login="stranger", body="Refs #5")]])
    elif reason == "ambiguous_second_page":
        set_branch_history(ctx, "agent/issue-5", [
            page1 + [rest_pr(200, "agent/issue-5", sha=head, body="Refs #5")],
            [rest_pr(201, "agent/issue-5", sha=head, body="Refs #5")],
        ])
    else:
        set_branch_history(ctx, "agent/issue-5", [
            page1 + [rest_pr(200, "agent/issue-5", sha=head, body="Refs #5")],
            "not-a-page",
        ])

    assert ctx.make().run() == 1
    assert intent_path.exists()


def test_malformed_second_page_does_not_release_label(ctx, monkeypatch):
    started = []
    monkeypatch.setattr(dev, "run", lambda t, n: started.append(n) or 80)
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    wip(ctx, n=5, ready=[issue(12)])
    page1 = [rest_pr(100 + i, "agent/issue-5", body="Refs #5") for i in range(100)]
    set_branch_history(ctx, "agent/issue-5", [page1, [{"bad": "entry"}]])
    set_branch_history(ctx, "cursor/issue-5", [[]])

    assert ctx.make().run() == 0
    assert not released(ctx, 5)
    assert started == ["12"]


# --- a stalled PR and a ready label that could not be written (#265) -----------------------------------------
STATES = {
    "review-pending": lambda ctx: checkpoint(ctx, pr="11"),
    "fix-push-waiting": lambda ctx: ctx.path("fix-push-waiting-11").write_text("5"),
    "slot-deferred": lambda ctx: ctx.path("slot-deferred-11").write_text("{}"),
    "ready-label-pending": lambda ctx: ctx.path("ready-label-pending-11").write_text('{"attempts": 0}'),
    "retry-record": lambda ctx: ctx.path("handoff-retry-5-11-fix_push_failed.json").write_text("{}"),
    "recovery-record": lambda ctx: ctx.path("recovery-5-11.json").write_text("{}"),
}


def ready_edits(ctx):
    return [c for c in ctx.gh.ran("pr", "edit") if "ready-for-human" in c]


def test_an_unlabelled_pr_with_no_saved_state_is_handed_off_not_logged_in_review(ctx):
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11}, in_review=False)
    assert ctx.make().next_issue() == "12"   # the handed-off PR holds no slot
    assert [c[2] for c in ready_edits(ctx)] == ["11"]
    assert "in review" not in ctx.log()
    assert "handoff pr_stalled category=3" in ctx.log()
    assert not [c for c in ctx.gh.ran("issue", "edit") if "ready-for-human" in c]


@pytest.mark.parametrize("saved", sorted(STATES))
def test_a_pr_with_saved_state_is_not_handed_off(ctx, saved):
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11}, in_review=False)
    STATES[saved](ctx)
    ctx.make().next_issue()
    assert not ready_edits(ctx)
    assert "pr_stalled" not in ctx.log()


@pytest.mark.parametrize("label", ["agent-review", "agent-fix", "ready-for-human"])
def test_a_pr_with_a_pipeline_label_is_not_handed_off(ctx, label):
    flight(ctx, wip=[5], ready=[issue(12)], prs={5: 11}, labels={11: [label]}, in_review=False)
    ctx.make().next_issue()
    assert not ready_edits(ctx)


def test_a_following_tick_applies_a_pending_ready_label_and_removes_the_record(ctx):
    ctx.path("ready-label-pending-11").write_text('{"attempts": 0}')
    handoff.retry_ready_labels(ctx.make().env)
    assert [c[2] for c in ready_edits(ctx)] == ["11"]
    assert not ctx.path("ready-label-pending-11").exists()


def test_a_pending_ready_label_is_given_up_after_five_ticks_and_the_owner_is_told(ctx, monkeypatch):
    told = []
    monkeypatch.setattr(handoff.notify, "notify", lambda title, text="": told.append(title))
    ctx.gh.fails.add("pr edit")
    ctx.path("ready-label-pending-11").write_text('{"attempts": 0}')
    runner = ctx.make().env
    for _ in range(4):
        handoff.retry_ready_labels(runner)
    assert ctx.path("ready-label-pending-11").exists() and not told
    handoff.retry_ready_labels(runner)
    assert not ctx.path("ready-label-pending-11").exists()
    assert told == ["PR #11 needs you"]


# --- priority labels (#194) ----------------------------------------------------------------------------------
def numbered(*specs):
    """ready-for-agent issues from (number, *priority labels)."""
    return [issue(n, labels=["ready-for-agent", *labels]) for n, *labels in specs]


@pytest.mark.parametrize("specs, expected", [
    ([(5, "P1"), (9, "P0")], [9, 5]),
    ([(5,), (9, "P1")], [9, 5]),
    ([(5, "P3"), (9,)], [5, 9]),
    ([(5, "P2"), (9, "P1", "P3")], [9, 5]),
    ([(9, "P1"), (5, "P1"), (7,), (3, "P2")], [5, 9, 3, 7]),
    ([(3,), (5, "p1"), (9, "p0")], [9, 5, 3]),
    ([(3,), (5, "P4"), (9, "priority:P0"), (10, "P1")], [10, 3, 5, 9]),
])
def test_candidates_are_ordered_by_priority_then_number(ctx, specs, expected):
    issues(ctx, ready=numbered(*specs))
    assert ctx.make().candidates() == expected


def test_p3_is_picked_before_an_issue_with_no_priority_label(ctx, monkeypatch):
    issues(ctx, ready=numbered((5,), (9, "P3")))
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    assert ctx.make().next_issue() == "9"


def test_a_priority_candidate_beyond_the_first_50_listed_issues_comes_first(ctx):
    ready = numbered(*[(n,) for n in range(100, 160)], (160, "P1"))
    issues(ctx, ready=ready)

    def answer(args):   # like gh: no more than --limit
        listed = ready if "ready-for-agent" in args else []
        return listed[:int(args[args.index("--limit") + 1])]
    ctx.gh.answers["issue list"] = answer
    assert ctx.make().candidates()[:2] == [160, 100]
    assert all("blockedBy" in c[-1] for c in ctx.gh.ran("issue", "list"))


@pytest.fixture
def queue(ctx, monkeypatch):
    """Tick.run() over a described queue; `ran` is what ran, in order."""
    state = SimpleNamespace(ran=[], defer=set())
    monkeypatch.setattr(spec, "verdict_plan", lambda env, n, listed=None: "plan")
    monkeypatch.setattr(tick.Tick, "pick_vendor", lambda self, *a, **k: "codex")

    def ran(name, rc=80):
        state.ran.append(name)
        if name in state.defer:
            raise Deferred("pool", QUOTA)
        return rc
    monkeypatch.setattr(tick.Tick, "review_loop", lambda self, pr, *a, **k: ran(f"review {pr}", 0))
    monkeypatch.setattr(tick.Tick, "fix_item", lambda self, pr, head: ran(f"fix {pr}"))
    monkeypatch.setattr(tick.Tick, "resume_standalone_push", lambda self, pr, episodes: ran(f"push {pr}"))
    monkeypatch.setattr(tick.dev, "run", lambda t, n: ran(f"dev {n}"))
    monkeypatch.setattr(tick.dev, "resume_opening", lambda t, path: ran("opening"))

    def p0_issues(numbers):
        listed = ctx.gh.answers["issue list"]
        labelled = [{"number": n, "labels": [{"name": "P0"}]} for n in numbers]
        ctx.gh.answers["issue list"] = lambda args: labelled if "P0" in args else listed(args)

    def setup(ready=(), p0=(), wip=(), prs=(), fix=(), review=()):
        """P0 lists the open P0 issues; PRS, FIX (labelled agent-fix) and REVIEW (agent-review) are (pr, issue)."""
        ctx.gh.answers["issue list"] = lambda args: (
            [{"number": n} for n in wip] if "agent-wip" in args else [] if "claude-found" in args else list(ready))
        p0_issues(p0)
        for item in ready:
            label_history(ctx, item["number"], [label_event(1, "owner")])

        def rows(pairs):
            return [{"number": pr, "headRefName": f"agent/issue-{n}", "labels": []} for pr, n in pairs]

        def listed(args):
            if "merged" in args:
                return []
            if "agent-fix" in args:
                return rows(fix)
            if "agent-review" in args:
                return rows(review)
            return rows([*prs, *fix, *review])
        ctx.gh.answers["pr list"] = listed
        ctx.gh.answers["pr view"] = lambda args: "OPEN\n" if "--jq" in args else {"state": "OPEN", "labels": []}
        ctx.gh.answers["issue view"] = {"state": "OPEN", "labels": []}
        for pr, n in prs:
            ctx.path(f"recovery-{n}-{pr}.json").write_text("{}")
    state.setup = setup
    state.checkpoint = lambda pr, n: checkpoint(ctx, pr=str(pr), ISSUE=str(n))
    state.spec = lambda n: ctx.path(f"handoff-waiting-{n}-spec_comments_read_failed.json").write_text("{}")
    state.push = lambda pr, n: ctx.path(f"fix-push-waiting-{pr}").write_text(f"{n}\n")
    state.run = lambda *argv: ctx.make(*argv).run()
    return state


@pytest.mark.parametrize("lower", ["review", "fix", "push", "spec"])
def test_a_new_p0_issue_starts_ahead_of_lower_priority_work_and_the_cap(ctx, queue, lower):
    queue.setup(ready=numbered((12,), (20, "p0")), p0=[20], wip=[5], prs=[(11, 5)],
                fix=[(13, 6)] if lower == "fix" else [])
    if lower == "review":
        queue.checkpoint(11, 5)
    elif lower == "push":
        queue.push(11, 5)
    elif lower == "spec":
        queue.spec(5)
    queue.run()
    assert queue.ran == ["dev 20"]
    assert "#20: starting (P0), ahead of lower-priority work; max_pending_prs not applied" in ctx.log()


def test_in_flight_p0_work_runs_before_a_new_p0_issue(ctx, queue):
    queue.setup(ready=numbered((12,), (20, "P0")), p0=[20, 21, 22, 23, 24], wip=[5, 21],
                prs=[(11, 5), (31, 21), (32, 22)], fix=[(13, 6), (33, 23)], review=[(40, 24)])
    queue.checkpoint(11, 5)
    queue.checkpoint(31, 21)
    queue.push(32, 22)
    queue.spec(24)
    queue.defer = {"review 31", "push 32", "fix 33", "dev 24", "dev 20"}
    queue.run()
    # P0 in flight, the new P0, then today's order; nothing is tried twice and agent-review stays below P0.
    assert queue.ran == ["review 31", "push 32", "fix 33", "dev 24", "dev 20", "review 11"]


def test_a_deferred_p0_item_is_not_tried_again_in_the_tick(ctx, queue):
    queue.setup(ready=numbered((12,), (20, "P0")), p0=[20, 21], prs=[(31, 21)])
    queue.checkpoint(31, 21)
    queue.defer = {"review 31", "dev 20"}
    queue.run()
    assert queue.ran == ["review 31", "dev 20", "dev 12"]
    assert "#12: starting (no priority)" in ctx.log()


def test_a_review_left_by_a_deferred_p0_start_is_not_retried_in_the_tick(ctx, queue, monkeypatch):
    queue.setup(ready=numbered((20, "P0")), p0=[20])

    def opens_a_pr(t, n):
        queue.ran.append(f"dev {n}")
        queue.checkpoint(50, n)
        t.active["pr"] = "50"
        raise Deferred("pool", QUOTA)
    monkeypatch.setattr(tick.dev, "run", opens_a_pr)
    queue.run()
    assert queue.ran == ["dev 20"]


def test_pending_openings_run_before_p0_work(ctx, queue):
    queue.setup(ready=numbered((20, "P0")), p0=[20, 21], prs=[(31, 21)])
    queue.checkpoint(31, 21)
    ctx.path("pr-opening-3.json").write_text("{}")
    queue.run()
    assert queue.ran == ["opening"]


@pytest.mark.parametrize("probe", ["empty", "fails"])
def test_without_a_p0_issue_the_order_is_todays(ctx, queue, probe):
    queue.setup(ready=numbered((12,), (14, "P1")), prs=[(11, 5)], fix=[(13, 6)])
    queue.checkpoint(11, 5)
    queue.defer = {"review 11", "fix 13"}
    if probe == "fails":
        listed = ctx.gh.answers["issue list"]
        ctx.gh.answers["issue list"] = lambda args: (
            subprocess.CompletedProcess(args, 1, "", "boom") if "P0" in args else listed(args))
    queue.run()
    assert queue.ran == ["review 11", "fix 13", "dev 14"]
    assert "#14: starting (P1)" in ctx.log()
    assert ("could not list open P0 issues" in ctx.log()) == (probe == "fails")


@pytest.mark.parametrize("how", ["vendor", "payload", "read-defers", "item"])
def test_a_p0_that_cannot_run_does_not_stall_lower_priority_work(ctx, queue, monkeypatch, how):
    queue.setup(ready=numbered((20, "P0")), p0=[20], prs=[(11, 5)])
    queue.checkpoint(11, 5)
    reads = []

    def payload(self):
        reads.append(self.issue)
        if how == "read-defers":
            raise Deferred("handoff-retry", "spec_comments_read_failed")
        return None if how == "payload" else {"comments": []}
    monkeypatch.setattr(spec.Spec, "issue_payload", payload)
    if how == "vendor":
        monkeypatch.setattr(tick.Tick, "pick_vendor",
                            lambda self, *a, **k: (_ for _ in ()).throw(Deferred("pool", QUOTA)))
    queue.defer = {"dev 20"}
    queue.run()
    assert queue.ran == (["dev 20"] if how == "item" else []) + ["review 11"]
    assert reads == ["20"]


def test_a_deferred_p0_goes_on_to_the_next_p0(ctx, queue):
    queue.setup(ready=numbered((22, "P0"), (20, "P0")), p0=[20, 21, 22, 23], fix=[(31, 21), (33, 23)])
    queue.defer = {"fix 31", "fix 33", "dev 20"}
    queue.run()
    assert queue.ran == ["fix 31", "fix 33", "dev 20", "dev 22"]


@pytest.mark.parametrize("skip", ["open-pr", "pending-spec", "blocked", "unauthorized", "pr-list-fails"])
def test_the_p0_start_keeps_the_filters(ctx, queue, skip):
    ready = numbered((20, "P0"))
    if skip == "blocked":
        ready = [issue(20, labels=["ready-for-agent", "P0"], blocked_by=[(2, "OPEN")])]
    queue.setup(ready=ready, p0=[20], prs=[(31, 20)] if skip == "open-pr" else [])
    if skip == "pending-spec":
        queue.spec(20)
        ctx.gh.answers["issue view"] = {"state": "OPEN", "labels": [{"name": "ready-for-human"}]}
    if skip == "unauthorized":
        label_history(ctx, 20, [label_event(1, "collaborator")])
    if skip == "pr-list-fails":
        listed = ctx.gh.answers["pr list"]
        ctx.gh.answers["pr list"] = lambda args: (
            subprocess.CompletedProcess(args, 1, "", "boom") if "--label" not in args and "merged" not in args
            else listed(args))
    queue.run()
    assert queue.ran == []


def test_open_p0_prs_count_toward_the_cap(ctx, queue):
    queue.setup(ready=numbered((12,)), p0=[5], wip=[5], prs=[(11, 5)])
    queue.run()
    assert queue.ran == []
    assert "not starting new work" in ctx.log()


def test_a_forced_issue_has_no_p0_pass_and_logs_its_priority(ctx, queue):
    queue.setup(ready=numbered((20, "P0")), p0=[20])
    ctx.gh.answers["issue view"] = {"labels": [{"name": "ready-for-agent"}, {"name": "p1"}],
                                    "author": {"login": "owner"}}
    label_history(ctx, 7, [label_event(1, "owner")])
    ctx.gh.answers["pr list"] = lambda args: "" if "--head" in args else []
    queue.run("7")
    assert queue.ran == ["dev 7"]
    assert "#7: starting (P1)" in ctx.log()
    assert not [c for c in ctx.gh.ran("issue", "list") if "P0" in c]

