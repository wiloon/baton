"""Read-only issue eligibility shared by the serve supervisor and the tick child (#291, ADR 0012).

The supervisor lists candidates and in-flight work; the child re-checks before it starts anything
and alone runs the writes, vendor probes and work locks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import time
from typing import NamedTuple

from . import handoff, names

CALL_LIMIT = 20          # seconds per GitHub call
PASS_BUDGET = 60         # seconds for one listing pass
HOUR = 3600

SPEC_PENDING = (
    "spec_comments_read_failed", "spec_pending_adr_read_failed",
    *handoff.SPEC_WRITE_POSTS, *handoff.ISSUE_PUSHES, handoff.SPEC_ADR_MERGE,
)
IN_FLIGHT_LOCAL = (
    *handoff.COMMENT_POSTS, handoff.FIX_PUSH, *handoff.FIX_META_WRITES, *SPEC_PENDING,
)


@dataclass
class Candidate:
    number: int
    priority: int


@dataclass
class RepoListing:
    name: str
    in_flight: bool = False
    candidates: list[Candidate] = field(default_factory=list)
    failed: bool = False


def open_blockers(item):
    """The open issues that block ITEM, from GitHub's issue dependencies (`blockedBy`, #70)."""
    blocked = item.get("blockedBy") or {}
    nodes = blocked.get("nodes") or []
    found = [n.get("number") for n in nodes if (n.get("state") or "").upper() != "CLOSED"]
    if (blocked.get("totalCount") or 0) > len(nodes):
        found.append("?")
    return found


def priority_of(labels):
    """The rank of an issue with these label names (ADR 0010): 0 for P0 to 3 for P3, the highest of
    several, and 4 (after P3) without one."""
    return min((int(name[1]) for name in labels if re.fullmatch(r"[Pp][0-3]", name)), default=4)


def owner_ready(gh_json, repo, issue, owner, *, cache=None, updated_at=None):
    """Whether the owner's latest ready-for-agent label event authorizes ISSUE. CACHE keeps only
    answers read from a whole history: an unreadable one is asked again."""
    key = (repo, str(issue), updated_at)
    if cache is not None and key in cache:
        return cache[key]
    pages = gh_json("api", f"repos/{repo}/issues/{issue}/events?per_page=100", "--paginate", "--slurp")
    ready = False
    if isinstance(pages, list) and all(isinstance(page, list) for page in pages):
        latest = None
        valid = True
        for page in pages:
            for event in page:
                if not isinstance(event, dict):
                    valid = False
                    break
                if event.get("event") not in ("labeled", "unlabeled") or (event.get("label") or {}).get("name") != "ready-for-agent":
                    continue
                if type(event.get("id")) is not int:
                    valid = False
                    break
                if latest is None or event["id"] > latest["id"]:
                    latest = event
            if not valid:
                break
        if valid:
            ready = bool(latest and latest["event"] == "labeled"
                         and ((latest.get("actor") or {}).get("login") or "").casefold() == owner.casefold())
            if cache is not None:
                cache[key] = ready
    return ready


def pending_specs(state: Path):
    """{issue: reason} of the saved spec reads, comment posts and issue-scoped pushes."""
    found = {}
    for reason in SPEC_PENDING:
        for prefix in ("waiting", "retry"):
            for path in state.glob(f"handoff-{prefix}-*-{reason}.json"):
                issue = path.name.removeprefix(f"handoff-{prefix}-").removesuffix(f"-{reason}.json")
                if issue.isdecimal():
                    found[issue] = reason
    return found


def local_in_flight(state: Path) -> bool:
    if any(state.glob("pr-opening-*.json")) or any(state.glob("review-pending-*")):
        return True
    if any(state.glob("fix-push-waiting-*")):
        return True
    for reason in IN_FLIGHT_LOCAL:
        if any(state.glob(f"handoff-*-{reason}.json")):
            return True
    return False


def github_in_flight(prs) -> bool:
    for pr in prs or []:
        labels = {label.get("name") for label in pr.get("labels") or []}
        if "agent-review" in labels:
            return True
        if "agent-fix" in labels and names.issue_of(pr.get("headRefName") or ""):
            return True
    return False


def slot_markers(state: Path, unreadable=None):
    """{PR number: "vendor reason"} of the PRs that lend their slots. UNREADABLE gets each marker
    that cannot be read: only the child, which writes the state, removes it."""
    found = {}
    for path in state.glob("slot-deferred-*"):
        try:
            data = json.loads(path.read_text())
            found[path.name.removeprefix("slot-deferred-")] = f"{data['vendor']} {data['reason']}"
        except (OSError, ValueError, KeyError, TypeError):
            if unreadable:
                unreadable(path)
    return found


def with_owner_issues(prs):
    """Issues whose PR waits for the owner (ready-for-human)."""
    return {names.issue_of(p["headRefName"]) for p in prs
            if any(label.get("name") == "ready-for-human" for label in p.get("labels") or [])}


class Slots(NamedTuple):
    pending: list    # agent-wip issues that hold a max_pending_prs slot
    deferred: set    # of those, the ones whose PR lends its slot
    held: dict       # pending issue -> its PR number
    markers: dict    # slot_markers()


def counted_wip(prs, wip_numbers, markers, with_owner=None):
    """The max_pending_prs count: agent-wip issues with an open PR that does not wait for the owner."""
    with_owner = with_owner_issues(prs) if with_owner is None else with_owner
    branches = {p["headRefName"] for p in prs}
    pending = [n for n in wip_numbers if n not in with_owner and branches.intersection(names.branches(n))]
    pr_of = {names.issue_of(p["headRefName"]): str(p.get("number", "")) for p in prs
             if names.issue_of(p["headRefName"]) not in with_owner}
    held = {n: pr_of.get(n) for n in pending}
    deferred = {n for n, pr in held.items() if pr and pr in markers}
    return Slots(pending, deferred, held, markers)


def cap_allows(priority, pending, deferred, cap):
    """Whether a new issue of PRIORITY may start under the pending-PR cap (P0 always)."""
    if priority == 0:
        return True
    return len(pending) - len(deferred) < cap and len(pending) < cap + 1


def ready_candidates(gh_json, repo, *, milestone, owner, only=None, cache=None, log=None):
    """Ready-for-agent issues the child would accept, ordered by (priority, number)."""
    listed = gh_json("issue", "list", "-R", repo, "--state", "open", "--label", "ready-for-agent",
                     "--limit", "1000", "--json", "number,author,labels,milestone,blockedBy,updatedAt")
    if not isinstance(listed, list):
        return None
    found = {}
    for item in listed:
        number = item["number"]
        if only is not None and str(number) not in only:
            continue
        label_names = [label["name"] for label in item["labels"]]
        if milestone and not ((item.get("milestone") or {}).get("title") == milestone
                              or "claude-found" in label_names):
            continue
        if any(x in label_names for x in ("ready-for-human", "needs-info", "agent-wip")):
            continue
        if blockers := open_blockers(item):
            if log:
                log(f"#{number}: skipped; blocked by {' '.join(f'#{n}' for n in blockers)}")
            continue
        if not owner_ready(gh_json, repo, number, owner, cache=cache,
                           updated_at=item.get("updatedAt")):
            if log:
                log(f"#{number}: skipped; cannot verify ready-for-agent was applied by {owner}")
            continue
        found[number] = priority_of(label_names)
    return [Candidate(n, found[n]) for n in sorted(found, key=lambda n: (found[n], n))]


def every_page(gh_json, repo, query):
    """Every item of a REST list, for an admission list that reached its limit; None on failure."""
    pages = gh_json("api", f"repos/{repo}/{query}&per_page=100", "--paginate", "--slurp")
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        return None
    return [item for page in pages for item in page]


def list_open_prs(gh_json, repo):
    """Every open PR (number, headRefName, labels), or None when they cannot be listed."""
    prs = gh_json("pr", "list", "-R", repo, "--state", "open", "--limit", "100",
                  "--json", "number,headRefName,labels")
    if isinstance(prs, list) and len(prs) == 100:
        pulls = every_page(gh_json, repo, "pulls?state=open")
        prs = None if pulls is None else [
            {"number": pr["number"], "headRefName": pr["head"]["ref"], "labels": pr["labels"]} for pr in pulls]
    return prs if isinstance(prs, list) else None


def list_wip_numbers(gh_json, repo):
    """Every open agent-wip issue number, as strings, or None when they cannot be listed."""
    listed = gh_json("issue", "list", "-R", repo, "--state", "open", "--label", "agent-wip",
                     "--limit", "100", "--json", "number")
    if isinstance(listed, list) and len(listed) == 100:
        issues = every_page(gh_json, repo, "issues?state=open&labels=agent-wip")
        listed = None if issues is None else [item for item in issues if "pull_request" not in item]
    if not isinstance(listed, list):
        return None
    return [str(item["number"]) for item in listed]


def eligible_candidates(gh_json, repo, state: Path, *, milestone, owner, cap, only=None, cache=None, log=None):
    """Candidates that may start now (no open agent PR, no pending spec, under the cap; P0 uncapped)."""
    ready = ready_candidates(gh_json, repo, milestone=milestone, owner=owner, only=only, cache=cache, log=log)
    if ready is None:
        return None
    prs = list_open_prs(gh_json, repo)
    if prs is None:
        return None
    wip = list_wip_numbers(gh_json, repo)
    if wip is None:
        return None
    branches = {p["headRefName"] for p in prs}
    waiting = pending_specs(state)
    slots = counted_wip(prs, wip, slot_markers(state))
    out = []
    for candidate in ready:
        issue = str(candidate.number)
        if issue in waiting or any(names.issue_of(b) == issue for b in branches):
            continue
        if not cap_allows(candidate.priority, slots.pending, slots.deferred, cap):
            continue
        out.append(candidate)
    return out, github_in_flight(prs)


def list_repository(gh_json, repo, state: Path, *, milestone, owner, cap, cache=None, log=None):
    """One repository's read-only listing for the supervisor."""
    local = local_in_flight(state)
    try:
        result = eligible_candidates(gh_json, repo, state, milestone=milestone, owner=owner, cap=cap,
                                     cache=cache, log=log)
    except Exception:
        return RepoListing(name=repo, in_flight=local, failed=True)
    if result is None:
        return RepoListing(name=repo, in_flight=local, failed=True)
    candidates, remote_flight = result
    return RepoListing(name=repo, in_flight=local or remote_flight, candidates=candidates)


def rank(due, listings, *, order, last_child, born, now, suppress=(), hour=HOUR):
    """Assignments that fill free slots: (repo, issue|None, no_new_issue). ORDER is rotation order."""
    suppress = set(suppress)
    used, chosen = set(), []
    index = {name: i for i, name in enumerate(order)}

    def take(name, issue=None, no_new=False):
        if name in used:
            return
        # Only the exact assignment is suppressed: a plain turn is its own.
        key = (name, "issue", str(issue)) if issue else (name, "no-new" if no_new else "plain", "")
        if key in suppress:
            return
        chosen.append((name, str(issue) if issue else None, no_new))
        used.add(name)

    for name in order:
        if name in due and not listings[name].failed and listings[name].in_flight:
            take(name, no_new=True)
    for name in order:
        if name in due and name not in used and not listings[name].failed \
                and now - last_child.get(name, born) >= hour:
            take(name, no_new=True)
    pool = []
    for name in due:
        listing = listings[name]
        if name in used or listing.failed:
            continue
        for candidate in listing.candidates:
            if (name, "issue", str(candidate.number)) in suppress:
                continue
            pool.append((candidate.priority, index.get(name, 0), candidate.number, name))
    for _, _, number, name in sorted(pool):
        if name not in used:
            take(name, issue=number)
    for name in order:
        if name in due and name not in used:
            take(name)
    return chosen


def default_gh_json(*args, limit=CALL_LIMIT, until=None):
    """GitHub JSON via baton.pr.gh; None on failure. The call and its retries end by UNTIL
    (`time.time()`)."""
    from . import pr as pr_helpers
    result = pr_helpers.gh(*args, limit=limit, until=until)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout) if result.stdout is not None else None
    except ValueError:
        return None


def list_due(config, names, *, gh_json=None, clock=None, stop=None, cache=None, log=None,
             call_limit=CALL_LIMIT, budget=PASS_BUDGET):
    """List each due repository. Stop between calls; bound each call and the pass; never write.
    A repository not listed in time, or after a stop, comes back failed."""
    clock = clock or time.monotonic
    cache = {} if cache is None else cache
    deadline = clock() + budget
    found = {}
    if gh_json is None:
        def gh_json(*args):   # one call and its retries get what is left of both bounds
            until = time.time() + min(call_limit, deadline - clock())
            return default_gh_json(*args, limit=call_limit, until=until)

    def bounded(*args):
        if clock() >= deadline or stop and stop():
            return None
        return gh_json(*args)

    cap = int(config.scheduling.max_pending_prs or 1)
    for name in names:
        if stop and stop():
            break
        if clock() >= deadline:
            found[name] = RepoListing(name=name, failed=True)
            continue
        from .config import repo_dirs
        repo = config.repo(name)
        state = repo_dirs(config.paths, repo).state
        listing = list_repository(bounded, name, state, milestone=repo.agent_milestone, owner=repo.owner,
                                  cap=cap, cache=cache, log=log)
        found[name] = listing
        if listing.failed and log:
            log(f'{name}: listing failed')
    for name in names:
        found.setdefault(name, RepoListing(name=name, failed=True))
    return found
