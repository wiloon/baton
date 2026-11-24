"""The ADR branch update through `spec.run` (#151, section 6): conditional on the saved head, never
resent when inconclusive, verified, and bounded by one saved deadline. The fake `gh` and the fake
wall clock are test_spec's."""
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import sh
from test_spec import (
    ACCEPT,
    ACCEPTED,
    CHANGES,
    REAL_VERIFY,
    N,
    merges,
    resume_pending,
    reviser,
    updates,
    wall_clock,
)
from test_spec import loop as loop  # noqa: PLC0414

from baton import merge, pr, spec, tick

SAVED, UPDATED = "a" * 40, "b" * 40


def update_file(loop):
    return Path(loop.env["STATE"], "adr-update-pr-77.json")


def pin(call):
    return call[call.index("--match-head-commit") + 1]


def test_the_adr_update_carries_the_saved_head_and_the_merge_is_pinned_to_the_new_head(loop, monkeypatch):
    resume_pending(loop, monkeypatch)
    loop.gh.merge_state = "BEHIND"
    loop.gh.update_heads = [UPDATED]
    assert loop.run() is True
    [update] = updates(loop)
    assert update[:4] == ["api", "-X", "PUT", "repos/owner/app/pulls/77/update-branch"]
    assert f"expected_head_sha={SAVED}" in update
    assert not any(c[:2] == ["pr", "update-branch"] for c in loop.gh.calls)
    [merged] = merges(loop)
    assert pin(merged) == UPDATED
    assert not update_file(loop).exists()


def test_an_adr_merge_without_an_update_is_still_pinned_to_the_validated_head(loop, monkeypatch):
    resume_pending(loop, monkeypatch)
    loop.gh.merge_state = "CLEAN"
    assert loop.run() is True
    [merged] = merges(loop)
    assert pin(merged) == SAVED


def test_a_push_between_the_read_and_the_update_leaves_that_head_untouched_and_never_merges(loop, monkeypatch):
    notes = resume_pending(loop, monkeypatch)
    loop.gh.merge_state = "BEHIND"
    loop.gh.update_heads = [UPDATED]
    loop.gh.push_before_update = "c" * 40
    monkeypatch.setattr(merge.Merger, "verify_updated_head", lambda self, *args: False)   # a stranger's push
    assert loop.run() is False
    assert len(updates(loop)) == 1 and merges(loop) == []
    assert loop.gh.head_oid == "c" * 40   # GitHub answered 422 and left the branch alone
    assert any("expected head sha" in text for text in notes)
    assert not update_file(loop).exists()


def test_a_lost_update_response_is_never_resent_and_a_verified_head_is_merged(loop, monkeypatch):
    resume_pending(loop, monkeypatch)
    loop.gh.merge_state = "BEHIND"
    loop.gh.update_heads = [UPDATED]
    loop.gh.update_lost = 'Put "https://api.github.com/x": unexpected EOF'
    loop.gh.lag_reads = 3   # the old head stays visible for three reads
    assert loop.run() is True
    assert len(updates(loop)) == 1
    [merged] = merges(loop)
    assert pin(merged) == UPDATED


def test_an_inconclusive_update_that_never_shows_a_new_head_hands_off_at_its_deadline(loop, monkeypatch):
    notes = resume_pending(loop, monkeypatch)
    clock = wall_clock(monkeypatch)
    start = clock["t"]
    loop.gh.merge_state = "BEHIND"
    loop.gh.update_heads = [UPDATED]
    loop.gh.update_lost = "HTTP 502: bad gateway"
    loop.gh.lag_reads = 10_000
    assert loop.run() is False
    assert len(updates(loop)) == 1 and merges(loop) == []
    assert clock["t"] - start < 70   # one deadline: nothing waited past it
    assert any("not confirmed before its deadline" in text for text in notes)
    assert not update_file(loop).exists() and not loop.gh.posted(ACCEPTED)


def test_an_unverified_updated_head_hands_off_without_a_merge(loop, monkeypatch):
    notes = resume_pending(loop, monkeypatch)
    monkeypatch.setattr(merge.Merger, "verify_updated_head", lambda self, *args: False)
    loop.gh.merge_state = "BEHIND"
    loop.gh.update_heads = [UPDATED]
    assert loop.run() is False
    assert len(updates(loop)) == 1 and merges(loop) == []
    assert any("not a clean merge" in text for text in notes)


class Interrupted(Exception):
    pass


def run_spec(loop):
    return spec.run(SimpleNamespace(issue=N), loop.env)["rc"]


def record_handoffs(monkeypatch):
    seen = []
    original = spec.Spec.handoff

    def handoff(self, why, reason, **kwargs):
        seen.append(reason)
        return original(self, why, reason, **kwargs)

    monkeypatch.setattr(spec.Spec, "handoff", handoff)
    return seen


@pytest.mark.parametrize("expired", [False, True])
def test_spec_restart_from_the_pending_post_and_update_files(loop, monkeypatch, expired):
    clock = wall_clock(monkeypatch)
    handoffs = record_handoffs(monkeypatch)
    loop.script = [CHANGES, reviser(commit=True), ACCEPT]
    loop.gh.merge_state = "BEHIND"
    loop.gh.update_heads = [UPDATED]
    original = pr.call

    def interrupt(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == "api" and args[3:4] == ("repos/owner/app/pulls/77/update-branch",):
            raise Interrupted
        return result

    with monkeypatch.context() as patch:
        patch.setattr(pr, "call", interrupt)
        with pytest.raises(Interrupted):
            run_spec(loop)
    assert update_file(loop).is_file() and loop.gh.head_oid == UPDATED
    assert spec.owner_handoff.load_retry(spec.session.resolve(loop.env), N,
                                         spec.owner_handoff.SPEC_PENDING_ACCEPT_POST)
    if not expired:
        current_tick = tick.Tick(loop.env, (N,))
        monkeypatch.setattr(current_tick, "item", lambda run: run())
        monkeypatch.setattr(current_tick, "resume_spec", lambda issue: 1 if issue == N else 0)
        assert current_tick.pick_issue() == 1  # No wait for the pending comment's retry deadline.
    loop.gh.merge_state = "CLEAN"
    if expired:
        clock["t"] += 61
        monkeypatch.setattr(merge.Merger, "observation",
                            lambda *a, **kw: pytest.fail("handoff tried to observe the expired PR"))
    before = len(loop.gh.calls)
    assert run_spec(loop) == int(expired)
    assert len(updates(loop)) == 1 and not update_file(loop).exists()
    if expired:
        assert handoffs == ["spec_adr_merge_failed"]
        assert merges(loop) == [] and not loop.gh.posted(ACCEPTED)
        assert not any(c[0] == "pr" for c in loop.gh.calls[before:])
    else:
        assert pin(merges(loop)[0]) == UPDATED and loop.gh.posted(ACCEPTED)


def test_an_unrelated_merged_head_with_a_saved_update_cannot_publish(loop, monkeypatch):
    resume_pending(loop, monkeypatch)
    clock = wall_clock(monkeypatch)
    update_file(loop).write_text(json.dumps({"head": SAVED, "base": "main", "count": 0,
                                             "deadline": clock["t"] + 40, "response": None}))
    loop.gh.head_oid, loop.gh.pr_state = "c" * 40, "MERGED"
    monkeypatch.setattr(merge.Merger, "verify_updated_head", lambda self, *args: False)
    assert run_spec(loop) == 1
    assert updates(loop) == [] and merges(loop) == [] and not loop.gh.posted(ACCEPTED)


def test_a_headless_legacy_acceptance_cannot_update_or_merge(loop, monkeypatch):
    resume_pending(loop, monkeypatch)
    loop.gh.comments[0]["body"] = loop.gh.comments[0]["body"].replace(f" head={SAVED}", "")
    loop.gh.merge_state = "BEHIND"
    assert run_spec(loop) == 1
    assert updates(loop) == [] and merges(loop) == [] and not loop.gh.posted(ACCEPTED)


@pytest.mark.parametrize("exhausted", [False, True])
def test_restart_after_verification_keeps_the_head_and_consumed_updates(loop, monkeypatch, exhausted):
    resume_pending(loop, monkeypatch)
    clock = wall_clock(monkeypatch)
    if exhausted:
        update_file(loop).write_text(json.dumps({"head": SAVED, "base": "main", "count": 2,
                                                 "deadline": clock["t"] + 40, "response": None}))
        loop.gh.head_oid = UPDATED
    else:
        loop.gh.update_heads = [UPDATED]
    loop.gh.merge_state = "BEHIND"

    def interrupt(self, minutes):
        if loop.gh.head_oid == UPDATED:
            raise Interrupted
        return True

    with monkeypatch.context() as patch:
        patch.setattr(merge.Merger, "ci_green", interrupt)
        with pytest.raises(Interrupted):
            run_spec(loop)
    clock["t"] += 61
    if not exhausted:
        loop.gh.merge_state = "CLEAN"
    assert run_spec(loop) == int(exhausted)
    if exhausted:
        assert updates(loop) == [] and merges(loop) == [] and not loop.gh.posted(ACCEPTED)
    else:
        assert len(updates(loop)) == 1 and pin(merges(loop)[0]) == UPDATED
        assert loop.gh.posted(ACCEPTED)


@pytest.mark.parametrize("text", [
    "not json",
    json.dumps({"head": "a" * 40}),
    json.dumps({"head": "a" * 40, "base": "main", "count": 9, "deadline": 0, "response": None}),
    json.dumps({"head": "a" * 40, "base": "main", "count": 0, "deadline": 9e12, "response": None}),
    json.dumps({"head": "zz", "base": "main", "count": 0, "deadline": 0, "response": None}),
])
def test_a_damaged_saved_update_hands_off_without_an_update_or_merge(loop, monkeypatch, text):
    notes = resume_pending(loop, monkeypatch)
    update_file(loop).write_text(text)
    assert loop.run() is False
    assert updates(loop) == [] and merges(loop) == []
    assert any("damaged" in note for note in notes)


def test_a_directory_at_the_update_record_hands_off_without_removing_its_contents(loop, monkeypatch):
    resume_pending(loop, monkeypatch)
    handoffs = record_handoffs(monkeypatch)
    path = update_file(loop)
    path.mkdir()
    (path / "keep").touch()
    assert run_spec(loop) == 1
    assert handoffs == ["spec_adr_merge_failed"]
    assert updates(loop) == [] and merges(loop) == [] and (path / "keep").is_file()


@pytest.mark.parametrize("status, attempts", [("HTTP 403", 2), ("HTTP 502", 1), ("", 1)])
def test_only_a_confirmed_secondary_rate_limit_refusal_repeats(loop, monkeypatch, status, attempts):
    resume_pending(loop, monkeypatch)
    loop.gh.merge_state = "BEHIND"
    loop.gh.update_heads = [UPDATED]
    if attempts == 1:
        loop.gh.update_lost = f"{status} secondary rate limit"
    else:
        loop.gh.flaky["pr update-branch"] = [(f"{status} secondary rate limit", False)]
    assert run_spec(loop) == 0
    assert len(updates(loop)) == attempts
    assert all(f"expected_head_sha={SAVED}" in call for call in updates(loop))


def test_a_refused_update_cannot_use_verification_that_finishes_at_the_deadline(loop, monkeypatch):
    resume_pending(loop, monkeypatch)
    clock = wall_clock(monkeypatch)
    loop.gh.merge_state = "BEHIND"
    loop.gh.push_before_update = UPDATED

    def verify(self, saved, new, base, deadline):
        clock["t"] = deadline
        return True

    monkeypatch.setattr(merge.Merger, "verify_updated_head", verify)
    assert run_spec(loop) == 1
    assert len(updates(loop)) == 1 and merges(loop) == [] and not loop.gh.posted(ACCEPTED)


def test_a_stalled_bounded_gh_call_is_killed_at_the_deadline(tmp_path, monkeypatch):
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\ntrap '' TERM\n: > \"$ADR_STARTED\"\nexec sleep 2\n")
    gh.chmod(0o755)
    started = tmp_path / "started"
    monkeypatch.setenv("ADR_STARTED", str(started))
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    start = time.monotonic()
    result = pr.gh("pr", "view", "77", until=time.time() + 0.2)
    assert started.is_file() and result.returncode != 0
    assert time.monotonic() - start < 0.9


def test_a_submillisecond_budget_never_starts_an_unbounded_call(monkeypatch):
    clock = wall_clock(monkeypatch)
    monkeypatch.setattr(pr, "call", lambda *args, **kwargs: pytest.fail("call started below timer resolution"))
    result = pr.gh("pr", "view", "77", until=clock["t"] + 0.0001)
    assert result.returncode == 124


def test_a_bounded_gh_call_rejects_a_success_returned_at_its_deadline(monkeypatch):
    clock = wall_clock(monkeypatch)
    deadline = clock["t"] + 1

    def late(*args, **kwargs):
        clock["t"] = deadline
        return subprocess.CompletedProcess(args, 0, '{"headRefOid": "abc"}', "")

    monkeypatch.setattr(pr, "call", late)
    assert pr.gh("pr", "view", "77", until=deadline).returncode == 124


def head(repo, ref="HEAD"):
    return sh(repo, "git", "rev-parse", ref)


def test_the_updated_head_must_be_a_clean_merge_of_the_saved_head_and_the_base(loop, worktree):
    repo = worktree.repo
    sh(repo, "git", "checkout", "-q", "-b", "adr", "main")
    (repo / "adr.md").write_text("adr\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "adr")
    saved = head(repo)
    sh(repo, "git", "checkout", "-q", "main")
    (repo / "m.txt").write_text("m\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "main moves")
    sh(repo, "git", "push", "-q", "origin", "main")
    main_head = head(repo)
    sh(repo, "git", "checkout", "-q", "-b", "stray", saved)
    (repo / "z.txt").write_text("z\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "stray")
    stray = head(repo)
    sh(repo, "git", "checkout", "-q", "adr")
    sh(repo, "git", "merge", "-q", "--no-ff", "-m", "Merge main", "main")
    good = head(repo)
    sh(repo, "git", "reset", "-q", "--hard", saved)
    sh(repo, "git", "merge", "-q", "--no-ff", "-m", "Merge stray", "stray")
    off_base = head(repo)
    sh(repo, "git", "reset", "-q", "--hard", good)
    (repo / "extra.txt").write_text("sneaked in\n")
    sh(repo, "git", "add", "-A")
    tree = sh(repo, "git", "write-tree")
    evil = sh(repo, "git", "commit-tree", tree, "-p", saved, "-p", main_head, "-m", "evil")
    one_parent = sh(repo, "git", "commit-tree", f"{good}^{{tree}}", "-p", saved, "-m", "plain")
    for sha in (saved, stray):
        sh(repo, "git", "branch", "-f", f"keep-{sha[:6]}", sha)
    sp = spec.Spec(loop.env, spec.telemetry.from_env(loop.env, issue=N), N)
    merger = merge.Merger(sp.env, sp.hooks, 77, N)
    far = time.time() + 30
    for name, sha, expected in (("good", good, True), ("one parent", one_parent, False),
                                ("off base", off_base, False), ("tree differs", evil, False)):
        sh(repo, "git", "push", "-q", "-f", "origin", f"{sha}:refs/pull/77/head")
        assert REAL_VERIFY(merger, saved, sha, "main", far) is expected, name
    assert REAL_VERIFY(merger, saved, good, "main", time.time() - 1) is False   # no time left: nothing starts
