"""Read-only issue eligibility shared by the serve supervisor and the tick child (#291, ADR 0012).

The supervisor lists candidates and in-flight work; the child re-checks before it starts anything
and alone runs the writes, vendor probes and work locks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re

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
    """Whether the owner's latest ready-for-agent label event authorizes ISSUE."""
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


def pending_spec_issues(state: Path):
    found = set()
    for reason in SPEC_PENDING:
        for prefix in ("waiting", "retry"):
            for path in state.glob(f"handoff-{prefix}-*-{reason}.json"):
                issue = path.name.removeprefix(f"handoff-{prefix}-").removesuffix(f"-{reason}.json")
                if issue.isdecimal():
                    found.add(issue)
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


def slot_markers(state: Path):
    found = {}
    for path in state.glob("slot-deferred-*"):
        try:
            data = json.loads(path.read_text())
            found[path.name.removeprefix("slot-deferred-")] = f"{data['vendor']} {data['reason']}"
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return found


def counted_wip(prs, wip_numbers, state: Path):
    """agent-wip issues that hold a max_pending_prs slot (same formula as Tick.next_issue)."""
    branches = {p["headRefName"] for p in prs}
    with_pr = {n for n in wip_numbers if branches.intersection(names.branches(n))}
    with_owner = {names.issue_of(p["headRefName"]) for p in prs
                  if any(label.get("name") == "ready-for-human" for label in p.get("labels") or [])}
    pending = with_pr - with_owner
    pr_of = {names.issue_of(p["headRefName"]): str(p.get("number", "")) for p in prs
             if names.issue_of(p["headRefName"]) not in with_owner}
    markers = slot_markers(state)
    deferred = {n for n in pending if pr_of.get(n) and pr_of[n] in markers}
    return pending, deferred


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


def list_open_prs(gh_json, repo):
    prs = gh_json("pr", "list", "-R", repo, "--state", "open", "--limit", "100",
                  "--json", "number,headRefName,labels")
    return prs if isinstance(prs, list) else None


def list_wip_numbers(gh_json, repo):
    listed = gh_json("issue", "list", "-R", repo, "--state", "open", "--label", "agent-wip",
                     "--limit", "100", "--json", "number")
    if not isinstance(listed, list):
        return None
    return {str(item["number"]) for item in listed}


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
    waiting = pending_spec_issues(state)
    pending, deferred = counted_wip(prs, wip, state)
    out = []
    for candidate in ready:
        issue = str(candidate.number)
        if issue in waiting or any(names.issue_of(b) == issue for b in branches):
            continue
        if not cap_allows(candidate.priority, pending, deferred, cap):
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
        key = (name, "issue", str(issue)) if issue else (name, "no-new", "")
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


def default_gh_json(*args, limit=CALL_LIMIT):
    """GitHub JSON via baton.pr.gh with a per-call timeout; None on failure."""
    from . import pr as pr_helpers
    result = pr_helpers.gh(*args, limit=limit)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout) if result.stdout is not None else None
    except ValueError:
        return None


def list_due(config, names, *, gh_json=None, clock=None, stop=None, cache=None, log=None,
             call_limit=CALL_LIMIT, budget=PASS_BUDGET):
    """List each due repository. Stop between calls; bound the pass; never write."""
    import time
    gh_json = gh_json or default_gh_json
    clock = clock or time.monotonic
    cache = {} if cache is None else cache
    deadline = clock() + budget
    found = {}
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

        def bounded(*args):
            if clock() >= deadline:
                return None
            return gh_json(*args)

        listing = list_repository(bounded, name, state, milestone=repo.agent_milestone, owner=repo.owner,
                                  cap=cap, cache=cache, log=log)
        found[name] = listing
        if listing.failed and log:
            log(f'{name}: listing failed')
    for name in names:
        found.setdefault(name, RepoListing(name=name, failed=True))
    return found
