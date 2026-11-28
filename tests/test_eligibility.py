"""Shared read-only eligibility for the serve supervisor and the tick (#291, ADR 0012)."""
import subprocess
import time

import pytest
from test_repos import TWO

from baton import config, eligibility, notify
from baton import pr as pr_helpers
from baton.eligibility import Candidate, RepoListing


def labelled_by_owner(event_id, login="alice"):
    return [[{"event": "labeled", "id": event_id, "label": {"name": "ready-for-agent"},
              "actor": {"login": login}}]]


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


@pytest.mark.parametrize("label, branch, expected", [
    ("agent-review", "feature/x", True),
    ("agent-fix", "agent/issue-4", True),
    ("agent-fix", "feature/x", False),
])
def test_list_repository_detects_in_flight_from_github_labels(tmp_path, label, branch, expected):
    gh = FakeGH()
    gh.answers["issue list"] = []
    gh.answers["pr list"] = [{"number": 1, "headRefName": branch, "labels": [{"name": label}]}]
    listing = eligibility.list_repository(gh, "acme/app", tmp_path, milestone="Now", owner="alice", cap=1)
    assert listing.in_flight is expected and not listing.failed


def test_list_repository_detects_in_flight_from_local_state_alone(tmp_path):
    (tmp_path / "pr-opening-3.json").write_text("{}")
    gh = FakeGH()
    gh.answers["issue list"] = []
    gh.answers["pr list"] = []
    listing = eligibility.list_repository(gh, "acme/app", tmp_path, milestone="Now", owner="alice", cap=1)
    assert listing.in_flight and not listing.failed


def test_list_repository_marks_a_failed_listing(tmp_path):
    gh = FakeGH()
    gh.answers["issue list"] = None
    listing = eligibility.list_repository(gh, "acme/app", tmp_path, milestone="Now", owner="alice", cap=1)
    assert listing.failed and listing.candidates == []


def test_an_unreadable_owner_check_is_not_cached():
    gh = FakeGH()
    cache = {}
    assert not eligibility.owner_ready(gh, "acme/app", 7, "alice", cache=cache, updated_at="t1")
    gh.answers["api repos/acme/app/issues/7/events?per_page=100"] = labelled_by_owner(7)
    assert eligibility.owner_ready(gh, "acme/app", 7, "alice", cache=cache, updated_at="t1")


def test_the_listing_reads_every_page_of_prs_and_wip_issues_past_100(tmp_path):
    gh = FakeGH()
    gh.answers["issue list"] = lambda args: (
        [issue(7, ("ready-for-agent", "P1")), issue(500, ("ready-for-agent", "P1"))]
        if "ready-for-agent" in args else [{"number": n} for n in range(1000, 1100)])
    first = [{"number": n, "headRefName": f"feature/{n}", "labels": []} for n in range(100)]
    gh.answers["pr list"] = first
    for n in (7, 500):
        gh.answers[f"api repos/acme/app/issues/{n}/events?per_page=100"] = labelled_by_owner(n)
    gh.answers["api repos/acme/app/pulls?state=open&per_page=100"] = [
        [{"number": p["number"], "head": {"ref": p["headRefName"]}, "labels": []} for p in first],
        [{"number": 101, "head": {"ref": "agent/issue-7"}, "labels": [{"name": "agent-review"}]},
         {"number": 102, "head": {"ref": "agent/issue-600"}, "labels": []}]]
    gh.answers["api repos/acme/app/issues?state=open&labels=agent-wip&per_page=100"] = [
        [{"number": n} for n in range(1000, 1100)], [{"number": 600}]]
    listing = eligibility.list_repository(gh, "acme/app", tmp_path, milestone="Now", owner="alice", cap=1)
    # #7 has a PR on page 2; #600 (page 2 of both) holds the only slot, so #500 is capped.
    assert listing.in_flight and listing.candidates == [] and not listing.failed


# --- one listing pass: bounded, stoppable, read-only ------------------------------------------------
REPOS = ["acme/app", "acme/shop"]


def two_repos(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(TWO.replace("~/", f"{tmp_path}/home/"))
    return config.load(path)


class PassGH:
    """A whole listing for any repository; every call takes SECONDS of the fake clock."""
    def __init__(self, seconds=0):
        self.t, self.seconds, self.calls = 0.0, seconds, []

    def __call__(self, *args):
        self.calls.append(args)
        self.t += self.seconds
        if args[0] == "api":
            return labelled_by_owner(1, "acme-owner" if "acme/app" in args[1] else "alice")
        if args[:2] == ("issue", "list") and "ready-for-agent" in args:
            return [issue(1, ("ready-for-agent", "claude-found"))]   # wanted at any milestone
        return []


def test_list_due_lists_every_repository_without_a_write(tmp_path):
    gh = PassGH()
    found = eligibility.list_due(two_repos(tmp_path), REPOS, gh_json=gh, clock=lambda: gh.t)
    assert [[c.number for c in found[name].candidates] for name in REPOS] == [[1], [1]]
    for call in gh.calls:
        assert call[:2] in (("issue", "list"), ("pr", "list")) or call[0] == "api"
        assert not {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"} & set(call)


def test_list_due_fails_the_repositories_not_listed_within_the_pass_budget(tmp_path):
    gh = PassGH(seconds=15)   # four calls per repository: 60 s for the first one
    found = eligibility.list_due(two_repos(tmp_path), REPOS, gh_json=gh, clock=lambda: gh.t)
    assert not found["acme/app"].failed and found["acme/shop"].failed
    assert len(gh.calls) == 4


def test_list_due_stops_between_calls(tmp_path):
    gh = PassGH()
    found = eligibility.list_due(two_repos(tmp_path), REPOS, gh_json=gh, clock=lambda: gh.t,
                                 stop=lambda: len(gh.calls) >= 1)
    assert len(gh.calls) == 1
    assert all(found[name].failed for name in REPOS)


@pytest.mark.parametrize("budget, bound", [(60, 20), (5, 5)])
def test_a_listing_call_and_its_retries_end_within_the_call_and_pass_bounds(tmp_path, monkeypatch, budget, bound):
    now, limits = [1000.0], []

    def call(*args, input=None, limit=None, kill=False):
        limits.append(limit)
        now[0] += limit   # GitHub hangs until the call is killed
        return subprocess.CompletedProcess(["gh", *args], 1, "", "HTTP 502")

    monkeypatch.setattr(pr_helpers, "call", call)
    monkeypatch.setattr(pr_helpers, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(notify, "log", lambda *args, **kwargs: None)
    eligibility.list_due(two_repos(tmp_path), REPOS[:1], clock=lambda: now[0], budget=budget)
    assert limits == [bound]
    assert now[0] - 1000 == bound


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


def test_a_suppressed_no_new_issue_turn_does_not_suppress_the_plain_turn():
    listings = {name: RepoListing(name, in_flight=True) for name in ("a", "b")}
    got = eligibility.rank(["a", "b"], listings, order=["a", "b"], last_child={}, born=0, now=0,
                           suppress={("a", "no-new", ""), ("b", "no-new", "")})
    assert got == [("a", None, False), ("b", None, False)]


def test_rank_breaks_equal_priority_ties_by_weight():
    listings = {
        "a": RepoListing("a", candidates=[Candidate(1, 1)]),
        "b": RepoListing("b", candidates=[Candidate(2, 1)]),
    }
    got = eligibility.rank(["a", "b"], listings, order=["a", "b"], last_child={}, born=0, now=0,
                           weight=lambda name: {"a": 1, "b": 5}[name])
    assert got[0] == ("b", "2", False)
    assert got[1] == ("a", "1", False)


def test_rank_equal_weight_keeps_rotation():
    listings = {
        "a": RepoListing("a", candidates=[Candidate(3, 1)]),
        "b": RepoListing("b", candidates=[Candidate(1, 1)]),
    }
    got = eligibility.rank(["a", "b"], listings, order=["a", "b"], last_child={}, born=0, now=0)
    assert got[0] == ("a", "3", False)
    assert got[1] == ("b", "1", False)


def test_rank_priority_beats_weight():
    listings = {
        "a": RepoListing("a", candidates=[Candidate(1, 1)]),
        "b": RepoListing("b", candidates=[Candidate(2, 2)]),
    }
    got = eligibility.rank(["a", "b"], listings, order=["a", "b"], last_child={}, born=0, now=0,
                           weight=lambda name: {"a": 1, "b": 5}[name])
    assert got[0] == ("a", "1", False)
    assert got[1] == ("b", "2", False)
