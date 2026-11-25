"""A transport-failed ADR `gh pr merge` replays across ticks (#151, section 6): two later attempts after
60 s and 300 s, no worker, revalidated inputs, then the usual `spec_adr_merge_failed` handoff."""
from pathlib import Path

import pytest
from test_adr_update import SAVED, pin, record_handoffs
from test_spec import ACCEPTED, N, _spec_clock, merges, resume_pending
from test_spec import loop as loop  # noqa: PLC0414

from baton import tick
from baton.common import Deferred

REASON = "spec_adr_merge_failed"
FAIL = ("HTTP 502 Bad Gateway", False)


def retry_files(loop):
    return sorted(Path(loop.env["STATE"]).glob(f"handoff-*-{N}-{REASON}.json"))


def owner_handoffs(loop):
    return sum("--add-label" in c and c[c.index("--add-label") + 1] == "ready-for-human"
               for c in loop.gh.calls if c[:2] == ["issue", "edit"])


def failing_merges(loop, count=4):
    loop.gh.flaky["pr merge"] = [FAIL] * count


def test_a_transport_failed_adr_merge_replays_after_60s_without_a_second_worker(loop, monkeypatch):
    clock = _spec_clock(monkeypatch)
    resume_pending(loop, monkeypatch)
    failing_merges(loop)
    with pytest.raises(Deferred, match=REASON):
        loop.run()
    assert len(merges(loop)) == 4 and retry_files(loop)
    assert "ready-for-human" not in loop.gh.labels
    before = len(loop.gh.calls)
    with pytest.raises(Deferred, match=REASON):   # no early attempt: nothing is read or written
        loop.run()
    assert len(loop.gh.calls) == before
    clock["now"] = 1_060
    assert loop.run() is True
    assert pin(merges(loop)[-1]) == SAVED and len(merges(loop)) == 5
    assert len(loop.gh.posted(ACCEPTED)) == 1 and loop.runs == []
    assert not retry_files(loop)


def test_an_adr_merge_that_keeps_failing_replays_twice_then_hands_off_once(loop, monkeypatch):
    clock = _spec_clock(monkeypatch)
    resume_pending(loop, monkeypatch)
    failing_merges(loop, 40)
    with pytest.raises(Deferred):
        loop.run()
    clock["now"] = 1_060
    with pytest.raises(Deferred):
        loop.run()
    clock["now"] = 1_359   # the second delay counts from the first attempt
    with pytest.raises(Deferred):
        loop.run()
    clock["now"] = 1_360
    assert loop.run() is False
    assert len(merges(loop)) == 12
    assert owner_handoffs(loop) == 1 and "ready-for-human" in loop.gh.labels
    assert not retry_files(loop) and not loop.gh.posted(ACCEPTED)


def test_a_permanent_adr_merge_failure_hands_off_without_a_replay(loop, monkeypatch):
    _spec_clock(monkeypatch)
    resume_pending(loop, monkeypatch)
    handoffs = record_handoffs(monkeypatch)
    loop.gh.flaky["pr merge"] = [("HTTP 405 not mergeable", False)]
    assert loop.run() is False
    assert handoffs == [REASON] and len(merges(loop)) == 1 and not retry_files(loop)


def test_an_adr_merge_that_landed_before_the_replay_publishes_without_a_second_merge(loop, monkeypatch):
    clock = _spec_clock(monkeypatch)
    resume_pending(loop, monkeypatch)
    failing_merges(loop)
    with pytest.raises(Deferred):
        loop.run()
    loop.gh.pr_state = "MERGED"
    clock["now"] = 1_060
    assert loop.run() is True
    assert len(merges(loop)) == 4 and len(loop.gh.posted(ACCEPTED)) == 1


def test_a_changed_adr_head_during_the_wait_hands_off_instead_of_merging(loop, monkeypatch):
    clock = _spec_clock(monkeypatch)
    resume_pending(loop, monkeypatch)
    failing_merges(loop)
    with pytest.raises(Deferred):
        loop.run()
    loop.gh.head_oid = "c" * 40
    clock["now"] = 1_060
    assert loop.run() is False
    assert owner_handoffs(loop) == 1 and len(merges(loop)) == 4 and not retry_files(loop)


def test_a_damaged_adr_merge_record_hands_off_without_a_merge(loop, monkeypatch):
    clock = _spec_clock(monkeypatch)
    resume_pending(loop, monkeypatch)
    failing_merges(loop)
    with pytest.raises(Deferred):
        loop.run()
    retry = next(path for path in retry_files(loop) if "retry" in path.name)
    retry.write_text("{")
    clock["now"] = 1_060
    assert loop.run() is False
    assert owner_handoffs(loop) == 1 and len(merges(loop)) == 4


def test_the_tick_finds_a_waiting_adr_merge_from_saved_state(loop, monkeypatch):
    _spec_clock(monkeypatch)
    resume_pending(loop, monkeypatch)
    failing_merges(loop)
    with pytest.raises(Deferred):
        loop.run()
    current = tick.Tick(loop.env, ())
    assert current.pending_specs() == {str(N): REASON}
    monkeypatch.setattr(current, "item", lambda run: run())
    monkeypatch.setattr(current, "resume_spec", lambda issue: pytest.fail("resumed before it was due"))
    assert current.pick_issue() is None
