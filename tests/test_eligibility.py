"""Shared read-only eligibility for the serve supervisor and the tick (#291, ADR 0012)."""
import json
from pathlib import Path

from baton import eligibility
from baton.eligibility import Candidate, RepoListing


class FakeGH:
    def __init__(self):
        self.calls, self.answers = [], {}

    def __call__(self, *args):
        self.calls.append(list(args))
        head = " ".join(args[:2])
        answer = self.answers.get(head)
        if callable(answer):
            answer = answer(args)
        return answer


def issue(number, labels=("ready-for-agent",), milestone="Now", blocked=None, updated="t1"):
    return {
        "number": number,
        "author": {"login": "alice"},
        "labels": [{"name": n} for n in labels],
        "milestone": {"title": milestone} if milestone else None,
        "blockedBy": {"nodes": [{"number": n, "state": s} for n, s in blocked or []], "totalCount": len(blocked or [])},
        "updatedAt": updated,
    }


def test_ready_candidates_order_by_priority_and_skip_blocked_and_non_owner(tmp_path):
    gh = FakeGH()
    gh.answers["issue list"] = [
        issue(1, ("ready-for-agent", "P3")),
        issue(2, ("ready-for-agent", "P0")),
        issue(3, ("ready-for-agent", "P1"), blocked=[(9, "OPEN")]),
        issue(4, ("ready-for-agent", "agent-wip", "P0")),
    ]
    events = {
        1: [[{"event": "labeled", "id": 1, "label": {"name": "ready-for-agent"}, "actor": {"login": "alice"}}]],
        2: [[{"event": "labeled", "id": 2, "label": {"name": "ready-for-agent"}, "actor": {"login": "alice"}}]],
        3: [[{"event": "labeled", "id": 3, "label": {"name": "ready-for-agent"}, "actor": {"login": "alice"}}]],
    }
    gh.answers["api repos/acme/app/issues/1/events?per_page=100"] = events[1]
    gh.answers["api repos/acme/app/issues/2/events?per_page=100"] = events[2]
    gh.answers["api repos/acme/app/issues/3/events?per_page=100"] = events[3]
    logs = []
    ready = eligibility.ready_candidates(gh, "acme/app", milestone="Now", owner="alice", log=logs.append)
    assert [(c.number, c.priority) for c in ready] == [(2, 0), (1, 3)]
    assert any("blocked" in line for line in logs)


def test_owner_ready_is_cached_per_repository_issue_and_updated_at():
    gh = FakeGH()
    pages = [[{"event": "labeled", "id": 1, "label": {"name": "ready-for-agent"}, "actor": {"login": "alice"}}]]
    gh.answers["api repos/acme/app/issues/7/events?per_page=100"] = pages
    cache = {}
    assert eligibility.owner_ready(gh, "acme/app", 7, "alice", cache=cache, updated_at="t1")
    assert eligibility.owner_ready(gh, "acme/app", 7, "alice", cache=cache, updated_at="t1")
    assert len([c for c in gh.calls if c[0] == "api"]) == 1
    assert eligibility.owner_ready(gh, "acme/app", 7, "alice", cache=cache, updated_at="t2")
    assert len([c for c in gh.calls if c[0] == "api"]) == 2


def test_eligible_candidates_respect_the_cap_but_not_for_p0(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    gh = FakeGH()
    gh.answers["issue list"] = lambda args: (
        [issue(10, ("ready-for-agent", "P1")), issue(11, ("ready-for-agent", "P0"))]
        if "ready-for-agent" in args else [{"number": 5}])
    gh.answers["pr list"] = [{"number": 50, "headRefName": "agent/issue-5", "labels": []}]
    for n in (10, 11):
        gh.answers[f"api repos/acme/app/issues/{n}/events?per_page=100"] = [[
            {"event": "labeled", "id": n, "label": {"name": "ready-for-agent"}, "actor": {"login": "alice"}}]]
    result = eligibility.eligible_candidates(gh, "acme/app", state, milestone="Now", owner="alice", cap=1)
    candidates, _ = result
    assert [(c.number, c.priority) for c in candidates] == [(11, 0)]


def test_eligible_candidates_skip_open_agent_prs_and_pending_specs(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "handoff-waiting-8-spec_comments_read_failed.json").write_text("{}")
    gh = FakeGH()
    gh.answers["issue list"] = lambda args: (
        [issue(7, ("ready-for-agent", "P1")), issue(8, ("ready-for-agent", "P1"))]
        if "ready-for-agent" in args else [])
    gh.answers["pr list"] = [{"number": 70, "headRefName": "agent/issue-7", "labels": []}]
    for n in (7, 8):
        gh.answers[f"api repos/acme/app/issues/{n}/events?per_page=100"] = [[
            {"event": "labeled", "id": n, "label": {"name": "ready-for-agent"}, "actor": {"login": "alice"}}]]
    candidates, flight = eligibility.eligible_candidates(
        gh, "acme/app", state, milestone="Now", owner="alice", cap=3)
    assert candidates == []
    assert flight is False


def test_list_repository_detects_in_flight_from_github_and_local_state(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "pr-opening-3.json").write_text("{}")
    gh = FakeGH()
    gh.answers["issue list"] = lambda args: [] if "ready-for-agent" in args else []
    gh.answers["pr list"] = [
        {"number": 1, "headRefName": "agent/issue-4", "labels": [{"name": "agent-review"}]}]
    listing = eligibility.list_repository(gh, "acme/app", state, milestone="Now", owner="alice", cap=1)
    assert listing.in_flight and not listing.failed


def test_list_repository_marks_failure_without_write_commands(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    gh = FakeGH()
    gh.answers["issue list"] = None
    listing = eligibility.list_repository(gh, "acme/app", state, milestone="Now", owner="alice", cap=1)
    assert listing.failed and listing.candidates == []
    assert not any(c[0] in ("issue", "pr") and "edit" in c for c in gh.calls)
    assert not any("create" in c for c in gh.calls)


def test_rank_orders_in_flight_hour_idle_candidates_then_plain():
    due = ["a", "b", "c", "d"]
    order = ["a", "b", "c", "d"]
    listings = {
        "a": RepoListing("a", candidates=[Candidate(1, 3)]),
        "b": RepoListing("b", in_flight=True),
        "c": RepoListing("c", candidates=[Candidate(9, 0)]),
        "d": RepoListing("d", failed=True),
    }
    # Hour-idle: a has had no child for an hour; b is in-flight so group 1.
    got = eligibility.rank(due, listings, order=order, last_child={"b": 0, "c": 3500}, born=0, now=3600,
                           suppress=())
    assert got[0] == ("b", None, True)            # in-flight
    assert got[1] == ("a", None, True)            # hour-idle (outranked P3 does not start)
    assert got[2] == ("c", "9", False)            # P0 candidate
    assert got[3] == ("d", None, False)           # failed listing → plain


def test_rank_skips_a_suppressed_assignment():
    due = ["a", "b"]
    listings = {
        "a": RepoListing("a", candidates=[Candidate(1, 0)]),
        "b": RepoListing("b", candidates=[Candidate(2, 1)]),
    }
    got = eligibility.rank(due, listings, order=due, last_child={}, born=0, now=0,
                           suppress={("a", "issue", "1")})
    assert got[0] == ("b", "2", False)
