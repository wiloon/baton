"""The ADR branch update through `spec.run` (#151, section 6): conditional on the saved head, never
resent when inconclusive, verified, and bounded by one saved deadline. The fake `gh` and the fake
wall clock are test_spec's."""
import json
import time
from pathlib import Path

import pytest

from baton import merge, spec
from conftest import sh
from test_spec import (ACCEPTED, N, REAL_VERIFY, loop, merges, resume_pending, updates, wall_clock)  # noqa: F401

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


def test_a_restart_after_a_saved_update_polls_without_resending_it(loop, monkeypatch):
    resume_pending(loop, monkeypatch)
    clock = wall_clock(monkeypatch)
    update_file(loop).write_text(json.dumps({"head": SAVED, "base": "main", "count": 0,
                                             "deadline": clock["t"] + 40, "response": None}))
    loop.gh.head_oid = UPDATED   # the lost request had landed
    assert loop.run() is True
    assert updates(loop) == []
    [merged] = merges(loop)
    assert pin(merged) == UPDATED
    assert not update_file(loop).exists()


def test_a_restart_after_the_saved_deadline_hands_off_without_merging(loop, monkeypatch):
    notes = resume_pending(loop, monkeypatch)
    clock = wall_clock(monkeypatch)
    update_file(loop).write_text(json.dumps({"head": SAVED, "base": "main", "count": 0,
                                             "deadline": clock["t"] - 1, "response": None}))
    loop.gh.head_oid = UPDATED   # a valid head, too late
    assert loop.run() is False
    assert updates(loop) == [] and merges(loop) == []
    assert any("not confirmed before its deadline" in text for text in notes)
    assert not update_file(loop).exists()


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


def test_the_update_request_is_bounded_to_the_first_half_of_the_window_and_killed(loop, monkeypatch):
    resume_pending(loop, monkeypatch)
    seen = []
    boundary = spec.pr_helpers.call

    def watch(*args, **kwargs):
        if args[:1] == ("api",) and "update-branch" in " ".join(args):
            seen.append(kwargs)
        return boundary(*args, **kwargs)

    monkeypatch.setattr(spec.pr_helpers, "call", watch)
    loop.gh.merge_state = "BEHIND"
    loop.gh.update_heads = [UPDATED]
    assert loop.run() is True
    [kwargs] = seen
    assert kwargs["kill"] is True and 0 < kwargs["limit"] <= 30


def head(repo, ref="HEAD"):
    return sh(repo, "git", "rev-parse", ref)


def test_the_updated_head_must_be_a_clean_merge_of_the_saved_head_and_the_base(loop, worktree):
    repo, origin = worktree.repo, worktree.origin
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
