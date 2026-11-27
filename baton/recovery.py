"""Persisted recovery for live worker and verification retries (#125 follow-up).

A recovery record holds the granted action, failed vendor, picker exclusions, remaining
budget, state fingerprint, and advisor decision id. It is written before alternate
vendor selection so a deferral or restart cannot lose dispatch constraints.
"""
import contextlib
import hashlib
import json
from pathlib import Path
import re

from . import notify, pick, tiers, vendors
from . import pr as pr_helpers
from .common import Deferred, Exhausted, git
from .config import ROLE_ROUTE
from .quota import atomic_json

REQUIRED = frozenset({
    "action", "failed_vendor", "exclusions", "remaining_budget", "fingerprint",
    "role", "episode", "item", "reason", "decision_id", "state", "pinned",
})
# granted: not dispatched yet. dispatched: the step may have run. progressed: a short block
# interrupted a step that had already changed the saved inputs, which are checkpointed.
STATES = ("granted", "dispatched", "progressed")


class RecoveryUnreadable(ValueError):
    """A recovery record exists but cannot be read or validated."""


def path(env, episode, item):
    episode, item = str(episode), str(item)
    return Path(str(env.dirs.state)) / f"recovery-{episode}-{item}.json"


def _valid(record):
    from .handoff import REASONS

    if not isinstance(record, dict):
        return False
    if not REQUIRED <= set(record):
        return False
    if not all(isinstance(record[key], str) for key in ("failed_vendor", "role", "episode", "item", "reason", "decision_id")):
        return False
    if record["action"] not in ("retry_step", "retry_step_other_vendor"):
        return False
    if record["state"] not in STATES or not isinstance(record["pinned"], bool):
        return False
    entry = REASONS.get(record["reason"])
    if not entry or entry.mode != "live" or record["action"] not in entry.actions:
        return False
    if not isinstance(record["exclusions"], list) or not all(isinstance(v, str) for v in record["exclusions"]):
        return False
    budget = record["remaining_budget"]
    if not isinstance(budget, dict):
        return False
    if record["reason"] in ("fix_verify_failed", "dev_verify_failed"):
        if any(type(budget.get(key)) is not int or budget[key] < 0 for key in ("verify_repairs", "repairs_used")):
            return False
        if record["reason"] == "dev_verify_failed" and not isinstance(budget.get("body"), str):
            return False
    if not isinstance(record["fingerprint"], dict) or not record["fingerprint"]:
        return False
    return True


def load(env, episode, item):
    location = path(env, episode, item)
    try:
        record = json.loads(location.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise RecoveryUnreadable(f"unreadable recovery {location.name}") from error
    if (not _valid(record) or record["episode"] != str(episode) or record["item"] != str(item)):
        raise RecoveryUnreadable(f"invalid recovery {location.name}")
    return record


def pending_fix(env, episode, item):
    """The vendor of a saved fix retry, "" when none (a review record is not one). An unreadable
    record answers "agent", so the fix resume can hand it off."""
    try:
        record = load(env, episode, item)
    except RecoveryUnreadable:
        return "agent"
    if not record or record["role"] != "fixer" or not record["reason"].startswith("fix_"):
        return ""
    return record.get("selected_vendor") or record["failed_vendor"]


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


def clear_item(env, item):
    for entry in Path(env.dirs.state).glob(f"recovery-*-{item}.json"):
        try:
            entry.unlink(missing_ok=True)
        except OSError as error:
            notify.log(f"could not clear {entry.name}: {error}")


def fingerprint_matches(record, current):
    """The saved fingerprint must equal the current binding exactly (extra or missing fields differ)."""
    return bool(current) and record.get("fingerprint") == current


def pr_fingerprint(wt, pr, *, remote_head="", base="", round_s="", extra=None):
    """Bind recovery to local and remote PR state."""
    branch = git(wt, "branch", "--show-current") or ""
    local_head = git(wt, "rev-parse", "HEAD") or ""
    clean = git(wt, "status", "--porcelain") == ""
    data = {"pr": str(pr), "branch": branch, "local_head": local_head, "clean": clean,
            "remote_head": remote_head, "base": base, "round": str(round_s)}
    if extra:
        data.update(extra)
    return data


def issue_fingerprint(wt, issue, *, base=""):
    branch = git(wt, "branch", "--show-current") or ""
    local_head = git(wt, "rev-parse", "HEAD") or ""
    clean = git(wt, "status", "--porcelain") == ""
    return {"issue": str(issue), "branch": branch, "local_head": local_head, "clean": clean, "base": base or ""}


def spec_fingerprint(wt, issue, *, base="", issue_body="", comment_digest="", round_s="", adr_head=""):
    """Bind spec recovery to issue body, trusted comment, round, ADR head, and worktree (#125 point 6)."""
    data = issue_fingerprint(wt, issue, base=base)
    data["issue_body"] = feedback_digest(issue_body)
    data["comment_digest"] = comment_digest or ""
    data["round"] = str(round_s)
    data["adr_head"] = adr_head or ""
    return data


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
    if git(wt, "status", "--porcelain") != "":
        return False, "worktree has local changes"
    head = git(wt, "rev-parse", "HEAD") or ""
    if not head:
        return False, "HEAD unreadable"
    remote = git(wt, "rev-parse", f"origin/{branch}") if branch else ""
    if not remote:
        return False, "origin branch unreadable"
    if head != remote:
        ok = session.run_process(["git", "-C", str(wt), "merge-base", "--is-ancestor", head, f"origin/{branch}"],
                                 capture_output=True).returncode == 0
        if not ok:
            return False, "local HEAD is not an ancestor of origin"
    return True, ""


def verify_status(*, repairs_used, max_repairs, failure=""):
    """Structured verification status without raw CI log excerpts (point 4)."""
    text = failure or ""
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


def retry_candidates(env, role, candidates, *, mode, pr="", issue="", fixed_by="", exclusions=()):
    """Static eligibility; temporary quota and cooldown blocks belong to the picker."""
    allowed = vendors._role_eligible(role, env=env)
    eligible = [v for v in candidates if v in allowed and v not in exclusions]
    if mode == "review":
        check = vendors.screenshot_allowed if role == "screenshot_reviewer" else vendors.review_allowed
        route = "screenshot_review" if role == "screenshot_reviewer" else "review"
        tier = tiers.step(env, route, issue or env.runtime.issue, pr or env.runtime.pr)
        eligible = [v for v in eligible if check(str(env.dirs.state), pr, v,
                                                env.config.models.route(route, v, tier=tier).model, fixed_by)]
    return eligible


def offered_actions(entry, failed_vendor, candidates, *, pinned=False, local=False):
    if not entry:
        return ()
    return tuple(action for action in entry.actions if
                 (action == "retry_step" and (local or failed_vendor in candidates))
                 or (action == "retry_step_other_vendor" and not pinned and not local
                     and any(v != failed_vendor for v in candidates)))


def decision_id(env, reason, item):
    """The grant id written for this item and reason, if any."""
    from . import advisor
    try:
        return advisor.grant_path(env, reason, item).read_text().strip()
    except OSError:
        return ""


def persist_grant(env, result, *, role, failed_vendor, exclusions, budget, fingerprint, episode, item, reason,
                  pinned=False):
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
        "state": "granted",
        "pinned": bool(pinned),
    }
    persist(env, record)
    return record


def select_vendor(env, record, *, mode, candidates, pr="", issue="", fixed_by="", other_model="",
                  spec_followup=False, pinned=False):
    """Select from the role's candidates with strict recovery constraints.

    A spec role's saved exclusions are its independence preference (the other phase's vendor): in an
    other-vendor retry they stay candidates, and the picker applies the first pick's last-resort rule."""
    role, failed = record["role"], record["failed_vendor"]
    pinned = record["pinned"]   # the first pick's constraint, not the saved alternate's
    exclusions = [v for v in record["exclusions"] if v]
    same = record["action"] == "retry_step"
    soft = []
    if not same:
        if role in ("spec_reviewer", "reviser"):
            soft = [v for v in exclusions if v != failed]
        exclusions.append(failed)
    # One read of the step's tier for the eligibility check and the pick.
    env = tiers.carry(env, ROLE_ROUTE.get(role, role), issue or env.runtime.issue, pr or env.runtime.pr)
    eligible = retry_candidates(env, role, candidates, mode=mode, pr=pr, issue=issue,
                                fixed_by=fixed_by, exclusions=exclusions)
    if soft and eligible:   # with no independent vendor at all, the retry hands off
        eligible = retry_candidates(env, role, candidates, mode=mode, pr=pr, issue=issue, fixed_by=fixed_by,
                                    exclusions=[v for v in exclusions if v not in soft])
    state = str(env.dirs.state)
    fallback = same and mode == "review" and failed in vendors.review_excluded(state, pr, fixed_by)
    selected = record.get("selected_vendor")
    if selected and not same:
        eligible = [v for v in eligible if v == selected]
    if same and not fallback:
        eligible = [v for v in eligible if v == failed]
    if not eligible or (same and failed not in eligible) or (pinned and not same):
        raise Exhausted(role, "No eligible vendor for the saved retry.")
    vendor = pick.choose(env, role, failed if same else selected or "", mode, eligible, pr=pr, issue=issue,
                           exclude=" ".join(dict.fromkeys(filter(None, exclusions))), fixed_by=fixed_by,
                           other_model=other_model, spec_followup=spec_followup, pinned=same)
    if vendor not in eligible or (same and vendor != failed):
        raise Exhausted(role, "Picker returned a vendor outside the saved retry constraints.")
    return vendor


def open_pr_head(pr):
    fields = "headRefOid,state,baseRefName,headRefName,body,comments,labels,reviews"
    try:
        data = pr_helpers.view(pr, fields)
    except (ValueError, TypeError):
        return None
    if (not isinstance(data, dict) or data.get("state") != "OPEN"
            or not isinstance(data.get("headRefOid"), str) or not re.fullmatch(r"[a-f0-9]{40,64}", data["headRefOid"])
            or not isinstance(data.get("baseRefName"), str) or not data["baseRefName"]
            or not isinstance(data.get("headRefName"), str) or not data["headRefName"]
            or not isinstance(data.get("body"), str)
            or any(not isinstance(data.get(key), list) or any(not isinstance(row, dict) for row in data[key])
                   for key in ("comments", "labels", "reviews"))
            or any(not isinstance(row.get("body"), str) for row in data["comments"])):
        return None
    return data


def worker_fingerprint(wt, pr, *, round_s="", extra=None, head_ref="", required_label=""):
    data = open_pr_head(pr)
    if not data or head_ref and data.get("headRefName") != head_ref:
        return None
    names = {label.get("name") for label in data["labels"]}
    if "ready-for-human" in names or required_label and required_label not in names:
        return None
    current = pr_fingerprint(wt, pr, remote_head=data["headRefOid"], base=data["baseRefName"],
                             round_s=round_s, extra=extra)
    if not current["clean"] or not current["branch"] or not current["local_head"]:
        return None
    evidence = {key: data.get(key) for key in ("headRefName", "body", "comments", "labels", "reviews")}
    current["pr_evidence"] = feedback_digest(json.dumps(evidence, sort_keys=True))
    current["head_ref"] = data.get("headRefName")
    feedback = [comment for comment in data["comments"] if not comment["body"].startswith("<!-- baton-fix by=")]
    current["feedback"] = feedback_digest(json.dumps([feedback, data.get("reviews")], sort_keys=True))
    return current


def resume(env, episode, item, match, binding, handoff, *, role=None, local=False, **picker):
    """Load before the normal step. A deferral leaves the action and budget intact.

    ROLE keeps a record of another role (a spec reviewer's for the reviser) from being taken over."""
    try:
        record = load(env, episode, item)
        if record is None or not match(record["reason"]) or role and record["role"] != role:
            return None
        return resolve(env, record, binding, local=local(record) if callable(local) else local, **picker)
    except (RecoveryUnreadable, Exhausted) as error:
        handoff(str(error))
        return False


def resolve(env, record, binding, *, local=False, **picker):
    from . import advisor
    if not advisor.enabled(env) or advisor.settings(env).mode != "live":
        raise RecoveryUnreadable("Live recovery is disabled.")
    if not fingerprint_matches(record, binding()):
        raise RecoveryUnreadable("Saved recovery inputs changed or could not be read.")
    if not record["decision_id"] or record["decision_id"] != decision_id(env, record["reason"], record["item"]):
        raise RecoveryUnreadable("Saved recovery grant could not be confirmed.")
    if record["state"] == "dispatched":
        raise RecoveryUnreadable("A saved retry was already dispatched and its outcome is unknown.")
    if record["state"] == "progressed":   # a completed step: continue, never pick or replay it
        return record, record.get("selected_vendor") or record["failed_vendor"]
    vendor = record["failed_vendor"] if local else select_vendor(env, record, **picker)
    if not local and not fingerprint_matches(record, binding()):
        raise RecoveryUnreadable("Saved recovery inputs changed during selection.")
    if not local and not record.get("selected_vendor"):
        record = {**record, "selected_vendor": vendor}
        persist(env, record)
    return record, vendor


def verification_progress(env, episode, item, vendor):
    """Reserve repair runs before dispatch and advance the saved local head after each run."""
    record = load(env, episode, item)
    if not record:
        return None
    budget = record["remaining_budget"]

    def checkpoint(used, remaining, head):
        budget["repairs_used"] = used
        budget["verify_repairs"] = remaining
        record["fingerprint"]["local_head"] = head
        persist(env, record)

    return checkpoint


# Fields that hold what the owner or the world supplied; a change there is never our own progress.
FOREIGN = frozenset({"feedback", "issue_evidence", "issue_body", "comment_digest", "base", "head_ref", "branch"})


def _readable(env, episode, item):
    try:
        return load(env, episode, item)
    except RecoveryUnreadable:
        return None


def update_budget(env, episode, item, **fields):
    """Checkpoint facts about the granted step (for example a finished worker) on the record."""
    record = _readable(env, episode, item)
    if record:
        record["remaining_budget"].update(fields)
        persist(env, record)


@contextlib.contextmanager
def dispatching(env, episode, item, binding, *, role=None):
    """Consume the grant before the step runs; settle it when a short block interrupts the step.

    A restart that finds "dispatched" cannot tell whether the step ran, so it hands off. A
    deferral without progress gives the grant back; one after progress (a commit, our own reply)
    checkpoints the new inputs so the normal resume continues instead of handing off."""
    record = _readable(env, episode, item)
    if not record or role and record["role"] != role:
        yield
        return
    if record["state"] == "granted":   # dispatched: an enclosing step already consumed it
        persist(env, {**record, "state": "dispatched"})
    try:
        yield
    except Deferred:
        settle(env, episode, item, binding)
        raise


def settle(env, episode, item, binding):
    """A short block interrupted the dispatched step: give the grant back or checkpoint its progress."""
    fresh = _readable(env, episode, item)
    if fresh and fresh["state"] in ("dispatched", "progressed"):
        current = binding() or {}
        saved = fresh["fingerprint"]
        changed = {key for key in {*current, *saved} if current.get(key) != saved.get(key)}
        if current and changed and not changed & FOREIGN:
            fresh.update(state="progressed", fingerprint=current)
        elif fresh["state"] == "dispatched":
            fresh["state"] = "granted"
        persist(env, fresh)


def request(env, episode, item, reason, role, vendor, detail, fingerprint, binding, handoff,
            *, exclusions=(), budget=None, local=False, **picker):
    """Ask once, save the grant before selection, and re-read before dispatch."""
    from . import advisor
    from . import handoff as owner_handoff

    if not advisor.enabled(env):
        handoff()
        return None
    candidates = [] if local else retry_candidates(
        env, role, picker["candidates"], mode=picker["mode"], pr=picker.get("pr", ""), issue=picker.get("issue", ""),
        fixed_by=picker.get("fixed_by", ""), exclusions=exclusions)
    actions = offered_actions(owner_handoff.REASONS.get(reason), vendor, candidates,
                              pinned=picker.get("pinned", False), local=local)
    if not actions:
        handoff()
        return None
    snapshot = (f"Decision point: {reason}. Repository {env.repo.gh_repo}, item #{item}.\n"
                f"Failed vendor: {vendor or 'none'}; role: {role}.\nOutcome: {detail}\n")
    result = handoff(snapshot=snapshot, observed_actions=actions,
                     revalidate=lambda: fingerprint is not None and fingerprint_matches(
                         {"fingerprint": fingerprint}, binding()))
    if getattr(result, "action", "handoff") == "handoff":
        return None
    try:
        record = persist_grant(env, result, role=role, failed_vendor=vendor, exclusions=exclusions,
                               budget=budget or {}, fingerprint=fingerprint, episode=episode,
                               item=item, reason=reason, pinned=picker.get("pinned", False))
        return resolve(env, record, binding, local=local, **picker)
    except (RecoveryUnreadable, Exhausted) as error:
        handoff(text=str(error))
        return None
