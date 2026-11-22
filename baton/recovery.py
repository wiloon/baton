"""Persisted recovery for live worker and verification retries (#125 follow-up).

A recovery record holds the granted action, failed vendor, picker exclusions, remaining
budget, state fingerprint, and advisor decision id. It is written before alternate
vendor selection so a deferral or restart cannot lose dispatch constraints.
"""
import hashlib
import json
from pathlib import Path

from . import notify, pick
from . import pr as pr_helpers
from .common import Exhausted, git
from .quota import atomic_json

# Test seam: raw CI log marker that must not reach advisor snapshots (PR #163 point 4).
LOG_EXCERPT_MARKER = "ADVISOR_VERIFY_LOG_EXCERPT_MARKER"

REQUIRED = frozenset({
    "action", "failed_vendor", "exclusions", "remaining_budget", "fingerprint",
    "role", "episode", "item", "reason", "decision_id",
})


class RecoveryUnreadable(ValueError):
    """A recovery record exists but cannot be read or validated."""


def path(env, episode, item):
    episode, item = str(episode), str(item)
    return Path(str(env.dirs.state)) / f"recovery-{episode}-{item}.json"


def _valid(record):
    if not isinstance(record, dict):
        return False
    if not REQUIRED <= set(record):
        return False
    if record["action"] not in ("retry_step", "retry_step_other_vendor"):
        return False
    if not isinstance(record["exclusions"], list):
        return False
    if not isinstance(record["remaining_budget"], dict):
        return False
    if not isinstance(record["fingerprint"], dict):
        return False
    return all(isinstance(record[key], str) for key in ("failed_vendor", "role", "episode", "item", "reason", "decision_id"))


def load(env, episode, item):
    location = path(env, episode, item)
    try:
        if not location.is_file():
            return None
        record = json.loads(location.read_text())
    except (OSError, ValueError) as error:
        raise RecoveryUnreadable(f"unreadable recovery {location.name}") from error
    if not _valid(record):
        raise RecoveryUnreadable(f"invalid recovery {location.name}")
    return record


def persist(env, record):
    if not _valid(record):
        raise ValueError("recovery record missing required fields")
    atomic_json(path(env, record["episode"], record["item"]), record)


def clear(env, episode, item):
    try:
        path(env, episode, item).unlink(missing_ok=True)
    except OSError as error:
        notify.log(f"could not clear recovery for {episode}/{item}: {error}")


def clear_episode(env, episode):
    """Drop every pending recovery for one task episode."""
    state = Path(str(env.dirs.state))
    prefix = f"recovery-{episode}-"
    for entry in state.glob(f"{prefix}*.json"):
        try:
            entry.unlink(missing_ok=True)
        except OSError as error:
            notify.log(f"could not clear {entry.name}: {error}")


def fingerprint_matches(record, current):
    """True when every stored fingerprint field still matches CURRENT."""
    stored = record.get("fingerprint") or {}
    if not isinstance(current, dict):
        return False
    for key, value in stored.items():
        if current.get(key) != value:
            return False
    return True


def pr_fingerprint(wt, pr, *, remote_head="", base="", feedback_digest="", round_s="", extra=None):
    """Bind recovery to local and remote PR state."""
    branch = git(wt, "branch", "--show-current") or ""
    local_head = git(wt, "rev-parse", "HEAD") or ""
    clean = not bool(git(wt, "status", "--porcelain"))
    data = {"pr": str(pr), "branch": branch, "local_head": local_head, "clean": clean,
            "remote_head": remote_head, "base": base, "feedback_digest": feedback_digest, "round": str(round_s)}
    if extra:
        data.update(extra)
    return data


def issue_fingerprint(wt, issue, *, branch="", base="", extra=None):
    branch = branch or git(wt, "branch", "--show-current") or ""
    local_head = git(wt, "rev-parse", "HEAD") or ""
    clean = not bool(git(wt, "status", "--porcelain"))
    data = {"issue": str(issue), "branch": branch, "local_head": local_head, "clean": clean, "base": base or ""}
    if extra:
        data.update(extra)
    return data


def spec_fingerprint(wt, issue, *, branch="", base="", issue_body="", comment_digest="", round_s="", adr_head=""):
    """Bind spec recovery to issue body, trusted comment, round, ADR head, and worktree (#125 point 6)."""
    data = issue_fingerprint(wt, issue, branch=branch, base=base)
    data["issue_body"] = feedback_digest(issue_body)
    data["comment_digest"] = comment_digest or ""
    data["round"] = str(round_s)
    data["adr_head"] = adr_head or ""
    return data


def mark_dispatched(env, record):
    """Note that the granted retry has been dispatched; keep the record until success or handoff."""
    if not record:
        return None
    updated = {**record, "dispatched": True}
    persist(env, updated)
    return updated


def feedback_digest(text):
    return hashlib.sha256((text or "").encode()).hexdigest()


def worktree_ready(wt, branch):
    """Inspect branch, HEAD, cleanliness and ff-only ancestry (tick point 7)."""
    from . import session
    if not Path(wt).is_dir():
        return False, "worktree missing"
    current = git(wt, "branch", "--show-current") or ""
    if current != branch:
        return False, f"branch is {current!r}, expected {branch!r}"
    if git(wt, "status", "--porcelain"):
        return False, "worktree has local changes"
    head = git(wt, "rev-parse", "HEAD") or ""
    if not head:
        return False, "HEAD unreadable"
    remote = git(wt, "rev-parse", f"origin/{branch}") if branch else ""
    if remote and head != remote:
        ok = session.run_process(["git", "-C", str(wt), "merge-base", "--is-ancestor", head, f"origin/{branch}"],
                                 capture_output=True).returncode == 0
        if not ok:
            return False, "local HEAD is not an ancestor of origin"
    return True, ""


def verify_status(*, repairs_used, max_repairs, failure=""):
    """Structured verification status without raw CI log excerpts (point 4)."""
    text = failure or ""
    if LOG_EXCERPT_MARKER in text:
        raise ValueError("raw verify log marker in failure text")
    if "Last 200 lines:" in text:
        text = text.split("Last 200 lines:")[0].rstrip()
    summary = ""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped and not stripped.startswith("["):
            summary = stripped[:240]
            break
    return (f"Verification: repairs {repairs_used} of {max_repairs}; "
            f"outcome failed; summary: {summary or 'unknown'}")


def offered_actions(entry, failed_vendor, vendors_list, *, pin_same=True):
    """Actions the evidence supports for a live worker failure."""
    if not entry or entry.mode != "live" or not entry.actions:
        return ()
    offered = []
    if "retry_step" in entry.actions:
        offered.append("retry_step")
    if "retry_step_other_vendor" in entry.actions:
        if any(v != failed_vendor for v in vendors_list):
            offered.append("retry_step_other_vendor")
    return tuple(offered)


def decision_id(env, reason, item):
    """The grant id written for this item and reason, if any."""
    from . import advisor
    try:
        return advisor.grant_path(env, reason, item).read_text().strip()
    except OSError:
        return ""


def persist_grant(env, result, *, role, failed_vendor, exclusions, budget, fingerprint, episode, item, reason):
    """Write the recovery record after the advisor grants a non-handoff action."""
    if result.handed_off or result.action == "handoff":
        return None
    record = {
        "action": result.action,
        "failed_vendor": failed_vendor,
        "exclusions": list(exclusions),
        "remaining_budget": dict(budget),
        "fingerprint": dict(fingerprint),
        "role": role,
        "episode": str(episode),
        "item": str(item),
        "reason": reason,
        "decision_id": decision_id(env, reason, item),
    }
    persist(env, record)
    return record


def select_vendor(env, record, *, mode, candidates, pr="", issue="", fixed_by="", other_model="", spec_followup=False,
                  pinned=False):
    """Pick the vendor for a persisted recovery action."""
    failed = record["failed_vendor"]
    exclusions = list(record["exclusions"])
    if record["action"] == "retry_step":
        if pinned or record["role"] == "screenshot_reviewer":
            return failed
        return failed
    exclude = ",".join(dict.fromkeys([*exclusions, failed]))
    return pick.choose(env, record["role"], failed, mode, candidates, pr=pr, issue=issue, exclude=exclude,
                       fixed_by=fixed_by, other_model=other_model, spec_followup=spec_followup,
                       pinned=pinned or record["role"] == "screenshot_reviewer")


def open_pr_head(pr):
    data = pr_helpers.view(pr, "headRefOid,state,baseRefName")
    if not data or data.get("state") != "OPEN":
        return None
    return data


def revalidate_pr(pr, fingerprint, wt=""):
    data = open_pr_head(pr)
    current = {}
    if data:
        current["remote_head"] = data.get("headRefOid") or ""
        current["base"] = data.get("baseRefName") or ""
    if wt:
        current.update(pr_fingerprint(wt, pr))
    elif not current:
        return False
    keys = [key for key in fingerprint if key in current]
    if not keys:
        return False
    return all(fingerprint.get(key) == current.get(key) for key in keys)


def revalidate_worker(fingerprint, wt, pr):
    """Re-read local worktree state and optional remote head before a worker retry."""
    if not wt:
        return False
    current = pr_fingerprint(wt, pr)
    for key in ("local_head", "clean", "branch", "pr"):
        if fingerprint.get(key) not in ("", None) and fingerprint.get(key) != current.get(key):
            return False
    remote = fingerprint.get("remote_head")
    if remote:
        data = open_pr_head(pr)
        if not data or data.get("headRefOid") != remote:
            return False
    return True


def revalidate_spec(fingerprint, wt, issue, *, branch, base, issue_body, comment_digest, round_s, adr_head):
    """Re-read spec binding fields before a worker retry."""
    if not wt:
        return False
    current = spec_fingerprint(wt, issue, branch=branch, base=base, issue_body=issue_body,
                               comment_digest=comment_digest, round_s=round_s, adr_head=adr_head)
    return fingerprint_matches({"fingerprint": fingerprint}, current)