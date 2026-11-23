"""Shared task handoff entry (#125, ADR 0008): one place for ready-for-human.

Every task handoff in review, fix, merge, spec, dev and tick goes through `perform`. It validates
the reason against the code table, applies the rule or advisor action for that reason, and on a
terminal handoff performs the label change, telemetry and notification. Callers keep their own
label removals and stop semantics. Cursor triage and after-merge tracker issues stay outside.

Category-1 reasons never call the advisor. After `pr.gh`'s per-call backoff, a recognized
transient failure schedules at most two later attempts (due after 60s then 300s). The attempt is
consumed before it runs so a restart cannot reset the budget. Permanent failures and exhaustion
hand off. Deferrals yield the tick without sleeping (#151).
"""
import hashlib
import json
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from . import advisor, notify, recovery
from . import pr as pr_helpers
from .quota import atomic_json

HANDOFF = advisor.HANDOFF
DEFER = "defer"
RETRY = "retry"
READY = "ready-for-human"
# After gh backoff: at most two later attempts, due after these delays (seconds).
DELAYS = (60, 300)
MAX_ATTEMPTS = len(DELAYS)
clock = time.time
# Category-1 reads and comment posts share the budget.
REVIEW_POST = "review_comment_post_failed"
SCREENSHOT_POST = "screenshot_comment_post_failed"
SPEC_REVIEW_POST = "spec_review_comment_post_failed"
SPEC_REPLY_POST = "spec_reply_post_failed"
SPEC_PENDING_ACCEPT_POST = "spec_pending_accept_post_failed"
SPEC_ACCEPT_POST = "spec_accept_post_failed"
FIX_PUSH = "fix_push_failed"
DEV_PUSH = "dev_push_failed"
SPEC_ADR_PUSH = "spec_adr_push_failed"
COMMENT_POSTS = (REVIEW_POST, SCREENSHOT_POST)
SPEC_POSTS = (SPEC_REVIEW_POST, SPEC_REPLY_POST)
SPEC_ACCEPT_POSTS = (SPEC_PENDING_ACCEPT_POST, SPEC_ACCEPT_POST)
# Spec comment posts and accept/pending-accept posts share tick discovery.
SPEC_WRITE_POSTS = (*SPEC_POSTS, *SPEC_ACCEPT_POSTS)
# Dev and ADR pushes are issue-scoped; fix pushes are PR-scoped.
ISSUE_PUSHES = (DEV_PUSH, SPEC_ADR_PUSH)
PUSH_REASONS = frozenset({FIX_PUSH, *ISSUE_PUSHES})
RETRY_REASONS = frozenset({
    "review_comment_missing", "spec_comments_read_failed", "spec_pending_adr_read_failed",
    *COMMENT_POSTS, *SPEC_WRITE_POSTS, *PUSH_REASONS,
})

# Categories: 1 rule-decided, 2 judgement, 3 needs investigation, 4 owner-only.
RANK = {"off": 0, "observe": 1, "live": 2}


@dataclass(frozen=True)
class Reason:
    category: int
    mode: str                    # rule | live | observe | direct
    actions: tuple[str, ...] = ()  # recovery actions; handoff is always offered


@dataclass(frozen=True)
class Result:
    action: str
    decision: object | None = None
    handed_off: bool = False
    continuation: dict | None = None


class UnknownReason(ValueError):
    """A handoff reason that is not in the table; never apply labels for it."""


class RetryUnreadable(Exception):
    """A retry record exists but cannot be read or parsed; callers must fail closed."""


# Every identifier is a distinct reason. Modes follow the #125 table; global advisor mode caps them.
REASONS = {
    "review_limit": Reason(2, "live", ("extra_round",)),
    "spec_limit": Reason(2, "live", ("extra_round",)),
    "review_run_failed": Reason(2, "live", ("retry_step", "retry_step_other_vendor")),
    "screenshot_run_failed": Reason(2, "live", ("retry_step", "retry_step_other_vendor")),
    "spec_review_run_failed": Reason(2, "live", ("retry_step", "retry_step_other_vendor")),
    "spec_revision_run_failed": Reason(2, "live", ("retry_step", "retry_step_other_vendor")),
    "fix_run_failed": Reason(2, "live", ("retry_step", "retry_step_other_vendor")),
    "fix_no_commit": Reason(2, "live", ("retry_step", "retry_step_other_vendor")),
    "fix_task_invalid": Reason(2, "live", ("retry_step", "retry_step_other_vendor")),
    "dev_run_failed": Reason(2, "live", ("retry_step", "retry_step_other_vendor")),
    "dev_result_invalid": Reason(2, "live", ("retry_step", "retry_step_other_vendor")),
    "dev_task_invalid": Reason(2, "live", ("retry_step", "retry_step_other_vendor")),
    "ci_before_merge_not_green": Reason(2, "observe", ("retry_step",)),
    "ci_after_update_not_green": Reason(2, "observe", ("retry_step",)),
    "spec_adr_ci_before_merge_not_green": Reason(2, "observe", ("retry_step",)),
    "spec_adr_ci_after_update_not_green": Reason(2, "observe", ("retry_step",)),
    "review_comment_missing": Reason(1, "rule"),
    "review_comment_post_failed": Reason(1, "rule"),
    "screenshot_comment_post_failed": Reason(1, "rule"),
    "spec_review_comment_post_failed": Reason(1, "rule"),
    "spec_reply_post_failed": Reason(1, "rule"),
    "spec_comments_read_failed": Reason(1, "rule"),
    "spec_pending_adr_read_failed": Reason(1, "rule"),
    "spec_pending_accept_post_failed": Reason(1, "rule"),
    "spec_accept_post_failed": Reason(1, "rule"),
    "fix_checks_refresh_failed": Reason(1, "rule"),
    "fix_task_write_failed": Reason(1, "rule"),
    "fix_run_unfinished": Reason(1, "rule"),
    "spec_body_edit_failed": Reason(1, "rule"),
    "spec_adr_open_failed": Reason(1, "rule"),
    "dev_push_failed": Reason(1, "rule"),
    "fix_push_failed": Reason(1, "rule"),
    "spec_adr_push_failed": Reason(1, "rule"),
    "review_author_state_failed": Reason(3, "live", ("retry_step",)),
    "dev_verify_failed": Reason(3, "live", ("retry_step",)),
    "fix_verify_failed": Reason(3, "live", ("retry_step",)),
    "fix_worktree_diverged": Reason(3, "live", ("retry_step",)),
    "merge_failed": Reason(3, "observe", ("retry_step",)),
    "spec_adr_merge_failed": Reason(3, "observe", ("retry_step",)),
    "spec_adr_merge_unconfirmed": Reason(3, "observe", ("retry_step",)),
    "spec_adr_state_invalid": Reason(3, "observe", ("retry_step",)),
    "reviewer_unavailable": Reason(4, "direct"),
    "screenshot_reviewer_unavailable": Reason(4, "direct"),
    "reviewer_role_denied": Reason(4, "direct"),
    "screenshot_reviewer_role_denied": Reason(4, "direct"),
    "review_independence_denied": Reason(4, "direct"),
    "screenshot_independence_denied": Reason(4, "direct"),
    "spec_reviewer_model_unverified": Reason(4, "direct"),
    "spec_reviser_model_unverified": Reason(4, "direct"),
    "auto_merge_off": Reason(4, "direct"),
    "owner_review_paths": Reason(4, "direct"),
    "manual_checks": Reason(4, "direct"),
    "spec_rejected": Reason(4, "direct"),
    "spec_adr_closed": Reason(4, "direct"),
    "fix_needs_info": Reason(4, "direct"),
    "dev_blocked": Reason(4, "direct"),
    "spec_questions": Reason(2, "live", ("answer_spec_questions",)),
}

def capped_mode(global_mode, point_mode):
    """ADR 0008: the global mode is a ceiling on the decision point's mode."""
    if point_mode not in RANK:
        return "off"
    if global_mode not in RANK:
        return "off"
    return point_mode if RANK[point_mode] <= RANK[global_mode] else global_mode


def operation_path(env, episode, reason, item, prefix):
    episode, item = str(episode), str(item or episode)
    key = episode if item == episode else f"{episode}-{item}"
    return Path(str(env.dirs.state)) / f"handoff-{prefix}-{key}-{reason}.json"


def retry_path(env, episode, reason, item=""):
    return operation_path(env, episode, reason, item, "retry")


def waiting_path(env, episode, reason, item=""):
    return operation_path(env, episode, reason, item, "waiting")


def _read_record(path):
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise RetryUnreadable(f"retry path is not a file {path.name}")
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise RetryUnreadable(f"invalid retry record {path.name}")
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RetryUnreadable(f"unreadable retry record {path.name}") from exc


def body_digest(body):
    return hashlib.sha256(body.encode()).hexdigest()


def _comment_post_valid(item, binding, *, marker):
    """A trusted review or screenshot comment and the inputs it was produced from (#151)."""
    body = binding.get("body")
    reviewer, round = binding.get("reviewer"), binding.get("round")
    if (reviewer not in ("claude", "codex", "cursor") or type(round) is not int or round <= 0
            or not isinstance(body, str)
            or not body.startswith(f"<!-- {marker} round={round} by={reviewer} -->\n")):
        return False
    try:
        if binding.get("digest") != body_digest(body):
            return False
    except UnicodeEncodeError:
        return False
    boundary = binding.get("comment_ids")
    return (binding.get("pr") == item
            and isinstance(boundary, list) and all(type(value) is int and value > 0 for value in boundary)
            and len(set(boundary)) == len(boundary)
            and all(isinstance(binding.get(key), str) for key in
                    ("head", "base", "issue", "description", "feedback", "outcome", "author", "run_id"))
            and bool(binding["base"]) and bool(binding["author"])
            and bool(re.fullmatch(r"[0-9a-f]{40}", binding["head"]))
            and bool(re.fullmatch(r"[0-9a-f]{64}", binding["description"]))
            and bool(re.fullmatch(r"[0-9a-f]{64}", binding["feedback"]))
            and binding["outcome"].startswith(("REVIEW: pass", "REVIEW: changes"))
            and (binding["issue"] == "" or binding["issue"].isascii() and binding["issue"].isdecimal()))


def _spec_comment_post_valid(item, binding, *, reason):
    """A trusted spec review or reply comment and the inputs it was produced from (#151)."""
    body, reviewer, round = binding.get("body"), binding.get("reviewer"), binding.get("round")
    if reviewer not in ("claude", "codex", "cursor") or type(round) is not int or round <= 0 or not isinstance(body, str):
        return False
    if reason == SPEC_REVIEW_POST:
        if not body.startswith(f"<!-- baton-spec-review round={round} by={reviewer} -->\n"):
            return False
        if binding.get("next") != "revise":
            return False
    else:
        if not re.match(rf"<!-- baton-spec-reply round={round}( human)? by={re.escape(reviewer)} -->\n", body):
            return False
        if binding.get("next") not in ("review", "human"):
            return False
    try:
        if binding.get("digest") != body_digest(body):
            return False
    except UnicodeEncodeError:
        return False
    boundary = binding.get("comment_ids")
    return (binding.get("issue") == item
            and isinstance(boundary, list) and all(type(value) is int and value > 0 for value in boundary)
            and len(set(boundary)) == len(boundary)
            and all(isinstance(binding.get(key), str) for key in
                    ("author", "issue_body", "adr_head", "feedback", "reply", "limit_why", "pr"))
            and bool(binding["author"]) and bool(binding["limit_why"])
            and bool(re.fullmatch(r"[0-9a-f]{64}", binding["issue_body"]))
            and bool(re.fullmatch(r"[0-9a-f]{64}", binding["feedback"]))
            and (binding["adr_head"] == "" or bool(re.fullmatch(r"[0-9a-f]{40}", binding["adr_head"])))
            and (binding["pr"] == "" or binding["pr"].isdecimal())
            and type(binding.get("max_rounds")) is int and binding["max_rounds"] > 0)


def _spec_accept_post_valid(item, binding, *, reason):
    """A trusted accept or pending-accept comment and the inputs it was produced from (#151)."""
    body, reviewer, round = binding.get("body"), binding.get("reviewer"), binding.get("round")
    if reviewer not in ("claude", "codex", "cursor") or type(round) is not int or round <= 0 or not isinstance(body, str):
        return False
    verdict = binding.get("verdict")
    if not isinstance(verdict, str) or not verdict.startswith(f"<!-- baton-verdict: accept by={reviewer} -->\n"):
        return False
    if f"round {round}: accept" not in verdict.split("\n", 2)[1]:
        return False
    if reason == SPEC_PENDING_ACCEPT_POST:
        pr = binding.get("pr")
        if not isinstance(pr, str) or not pr.isdecimal():
            return False
        head = binding.get("adr_head")
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
            return False
        expected = f"<!-- baton-spec-accept-pending pr={pr} head={head} -->\n{verdict}"
        if body != expected or binding.get("next") != "merge":
            return False
    else:
        if body != verdict or binding.get("next") != "transition":
            return False
        pr = binding.get("pr")
        if not isinstance(pr, str) or (pr != "" and not pr.isdecimal()):
            return False
        head = binding.get("adr_head")
        # Older records may omit the head. Reconciliation requires a matching revision for any saved PR.
        if not isinstance(head, str) or (head != "" and not re.fullmatch(r"[0-9a-f]{40}", head)):
            return False
        if head and not pr:
            return False
    try:
        if binding.get("digest") != body_digest(body):
            return False
    except UnicodeEncodeError:
        return False
    boundary = binding.get("comment_ids")
    return (binding.get("issue") == item
            and isinstance(boundary, list) and all(type(value) is int and value > 0 for value in boundary)
            and len(set(boundary)) == len(boundary)
            and all(isinstance(binding.get(key), str) for key in ("author", "issue_body", "feedback"))
            and bool(binding["author"])
            and bool(re.fullmatch(r"[0-9a-f]{64}", binding["issue_body"]))
            and bool(re.fullmatch(r"[0-9a-f]{64}", binding["feedback"])))


def _push_core_valid(binding):
    """Shared fields for a saved post-sign-off push to a destination ref (#151)."""
    commit, branch, remote = binding.get("commit"), binding.get("branch"), binding.get("remote")
    ref = binding.get("ref")
    return (isinstance(commit, str) and bool(re.fullmatch(r"[0-9a-f]{40}", commit))
            and isinstance(branch, str) and bool(branch)
            and ref in ("HEAD", branch)
            and "remote" in binding
            and (remote is None or isinstance(remote, str))
            and (not remote or bool(re.fullmatch(r"[0-9a-f]{40}", remote)))
            and type(binding.get("upstream")) is bool
            and isinstance(binding.get("wt"), str) and bool(binding["wt"])
            and isinstance(binding.get("feedback"), str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", binding["feedback"])))


def _fix_push_binding_valid(item, binding):
    next_step = binding.get("next")
    if next_step not in ("done", "handoff", "recover"):
        return False
    if (binding.get("pr") != item or not isinstance(binding.get("issue"), str)
            or (binding["issue"] and not binding["issue"].isdecimal())):
        return False
    caller, ci_head = binding.get("caller"), binding.get("ci_head")
    if caller == "ci":
        if not isinstance(ci_head, str) or not re.fullmatch(r"[0-9a-f]{40}", ci_head):
            return False
    elif caller not in ("standalone", "review") or ci_head != "":
        return False
    description_rc = binding.get("description_rc")
    if (type(description_rc) is not int or description_rc not in (0, 1, 2)
            or not isinstance(binding.get("description_reason"), str)
            or not isinstance(binding.get("fixer"), str) or not binding["fixer"]
            or (binding["fixer"] == "cursor" and description_rc == 1)):
        return False
    if next_step in ("handoff", "recover"):
        if binding.get("handoff_reason") not in ("fix_run_failed", "fix_needs_info"):
            return False
        if not isinstance(binding.get("outcome"), str) or not binding["outcome"]:
            return False
        if (binding["outcome"].startswith("RESULT: fixed")
                or binding["outcome"].startswith("RESULT: needs-info") != (binding["handoff_reason"] == "fix_needs_info")):
            return False
        if description_rc != 0 or binding["description_reason"]:
            return False
    elif (binding.get("handoff_reason") != "" or binding.get("outcome") != ""
          or bool(description_rc) != bool(binding["description_reason"])):
        return False
    if next_step == "recover":
        if binding["handoff_reason"] != "fix_run_failed" or not _push_recovery_valid(binding.get("recovery")):
            return False
    elif "recovery" in binding:
        return False
    return (isinstance(binding.get("who"), str)
            and type(binding.get("clear_cursor")) is bool)


def _dev_push_binding_valid(item, binding):
    attempts = binding.get("attempts")
    return (binding.get("issue") == item
            and binding.get("vendor") in ("claude", "codex", "cursor")
            and isinstance(binding.get("title"), str) and bool(binding["title"])
            and isinstance(binding.get("body_text"), str)
            and isinstance(binding.get("base"), str) and bool(re.fullmatch(r"[0-9a-f]{40}", binding["base"]))
            and isinstance(binding.get("model"), str)
            and isinstance(binding.get("gate_result"), str)
            and isinstance(attempts, list)
            and all(isinstance(row, list) and len(row) == 4
                    and all(isinstance(cell, str) for cell in row) for row in attempts))


def _spec_adr_push_binding_valid(item, binding):
    boundary = binding.get("comment_ids")
    return (binding.get("issue") == item
            and binding.get("reviser") in ("claude", "codex", "cursor")
            and type(binding.get("round")) is int and binding["round"] > 0
            and isinstance(binding.get("issue_body"), str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", binding["issue_body"]))
            and isinstance(binding.get("reply"), str)
            and isinstance(binding.get("mode_note"), str)
            and isinstance(binding.get("model"), str)
            and binding.get("human") in ("", " human")
            and binding.get("next") in ("review", "human")
            and type(binding.get("max_rounds")) is int and binding["max_rounds"] > 0
            and isinstance(binding.get("limit_why"), str) and bool(binding["limit_why"])
            and isinstance(binding.get("pr"), str)
            and (binding["pr"] == "" or binding["pr"].isdecimal())
            and isinstance(boundary, list)
            and all(type(value) is int and value > 0 for value in boundary)
            and len(set(boundary)) == len(boundary)
            and isinstance(binding.get("author"), str) and bool(binding["author"]))


def _push_binding_valid(item, binding, *, reason):
    """A push of a known post-sign-off commit to a destination ref (#151)."""
    if not _push_core_valid(binding):
        return False
    if reason == FIX_PUSH:
        return _fix_push_binding_valid(item, binding)
    if reason == DEV_PUSH:
        return _dev_push_binding_valid(item, binding)
    if reason == SPEC_ADR_PUSH:
        return _spec_adr_push_binding_valid(item, binding)
    return False


def _push_recovery_valid(context):
    if (not isinstance(context, dict)
            or not all(isinstance(context.get(key), str) for key in ("source", "feedback", "head"))
            or type(context.get("owner_triggered")) is not bool
            or not isinstance(context.get("authors"), list)
            or not all(isinstance(author, str) for author in context["authors"])
            or "start" not in context):
        return False
    start = context["start"]
    return start is None or (isinstance(start, dict) and all(
        isinstance(start.get(key), str) for key in ("base", "head_ref", "feedback", "remote_head")))


def _binding_valid(reason, item, binding):
    if not isinstance(binding, dict):
        return False
    if reason == "spec_comments_read_failed":
        return binding == {}
    if reason == "spec_pending_adr_read_failed":
        if binding == {"step": "discovery"}:
            return True
        return (binding.get("step") == "acceptance"
                and isinstance(binding.get("pr"), str) and binding["pr"].isdecimal()
                and isinstance(binding.get("verdict"), str) and bool(binding["verdict"])
                and isinstance(binding.get("issue_body"), str)
                and isinstance(binding.get("adr_head"), str)
                and (binding["adr_head"] == "" or bool(re.fullmatch(r"[0-9a-f]{40}", binding["adr_head"]))))
    if reason == REVIEW_POST:
        return _comment_post_valid(item, binding, marker="baton-review")
    if reason == SCREENSHOT_POST:
        return _comment_post_valid(item, binding, marker="baton-screenshot-review")
    if reason in SPEC_POSTS:
        return _spec_comment_post_valid(item, binding, reason=reason)
    if reason in SPEC_ACCEPT_POSTS:
        return _spec_accept_post_valid(item, binding, reason=reason)
    if reason in PUSH_REASONS:
        return _push_binding_valid(item, binding, reason=reason)
    return (binding.get("pr") == item and type(binding.get("round")) is int and binding["round"] > 0
            and all(isinstance(binding.get(key), str) for key in
                    ("head", "base", "digest", "issue", "screenshot_digest"))
            and bool(binding["base"]) and bool(re.fullmatch(r"[0-9a-f]{40}", binding["head"]))
            and bool(re.fullmatch(r"[0-9a-f]{64}", binding["digest"]))
            and (binding["issue"] == "" or binding["issue"].isdecimal())
            and (binding["screenshot_digest"] == "" or bool(re.fullmatch(r"[0-9a-f]{64}", binding["screenshot_digest"]))))


def load_retry(env, episode, reason, item=""):
    """Return a waiting operation's budget, or raise if either saved record is damaged or missing."""
    episode, item = str(episode), str(item or episode)
    path = retry_path(env, episode, reason, item)
    data = _read_record(path)
    waiting = _read_record(waiting_path(env, episode, reason, item))
    if data is None and waiting is None:
        return None
    if not isinstance(data, dict) or not isinstance(waiting, dict):
        raise RetryUnreadable(f"incomplete retry operation {path.name}")
    binding = data.get("continuation")
    expected = {"episode": episode, "item": item, "reason": reason, "continuation": binding}
    if any(data.get(key) != value for key, value in expected.items()):
        raise RetryUnreadable(f"mismatched retry operation {path.name}")
    if waiting != expected:
        # A due fix push can learn its baseline before the retry copy is replaced.
        observed = waiting.get("continuation")
        if (reason not in PUSH_REASONS or not isinstance(binding, dict) or not isinstance(observed, dict)
                or "remote" not in binding or binding["remote"] is not None or observed.get("remote") is None
                or type(data.get("attempts")) is not int or data["attempts"] < 1
                or waiting != {**expected, "continuation": {**binding, "remote": observed["remote"]}}):
            raise RetryUnreadable(f"mismatched retry operation {path.name}")
        binding = observed
        data = {**data, "continuation": binding}
    if not _binding_valid(reason, item, binding):
        raise RetryUnreadable(f"invalid continuation in {path.name}")
    if "confirmed" in data and (reason not in (*SPEC_POSTS, SPEC_ACCEPT_POST) or data["confirmed"] is not True):
        raise RetryUnreadable(f"invalid confirmed-post phase in {path.name}")
    if "phase" in data:
        finished_ok = (reason == FIX_PUSH and binding.get("next") == "done") or reason in ISSUE_PUSHES
        if (reason not in PUSH_REASONS or data["phase"] not in ("landed", "finished")
                or binding["remote"] is None
                or (data["phase"] == "finished" and not finished_ok)):
            raise RetryUnreadable(f"invalid push phase in {path.name}")
    attempts, due, scheduled = data.get("attempts"), data.get("due"), data.get("scheduled")
    if type(attempts) is not int or not (0 <= attempts <= MAX_ATTEMPTS):
        raise RetryUnreadable(f"out-of-range attempts in {path.name}")
    reads = data.get("reads")
    if "reads" in data and (reason not in SPEC_POSTS or data.get("confirmed") is not True
                            or type(reads) is not int or not 0 <= reads <= MAX_ATTEMPTS):
        raise RetryUnreadable(f"out-of-range reads in {path.name}")
    index = min(attempts if reads is None else reads, MAX_ATTEMPTS - 1)
    if (type(due) is not int or type(scheduled) is not int or not 0 <= scheduled <= int(clock())
            or due != scheduled + DELAYS[index]):
        raise RetryUnreadable(f"out-of-range due in {path.name}")
    return data


def restore_retry(env, episode, reason, item, pending):
    """Update the budget while keeping the saved operation unchanged."""
    atomic_json(retry_path(env, episode, reason, item), pending)


def save_push_baseline(env, episode, item, pending, remote, reason=FIX_PUSH):
    """Record the first observed remote in both copies before issuing any push."""
    binding = {**pending["continuation"], "remote": remote}
    waiting = {"episode": episode, "item": item, "reason": reason, "continuation": binding}
    atomic_json(waiting_path(env, episode, reason, item), waiting)
    restore_retry(env, episode, reason, item, {**pending, "continuation": binding})
    return binding


def prepare_retry(env, episode, reason, item, binding):
    """Save a new operation with its first deadline, before a write or after a failed read."""
    episode, item = str(episode), str(item)
    if not _binding_valid(reason, item, binding):
        return None
    waiting = {"reason": reason, "episode": episode, "item": item, "continuation": binding}
    now = int(clock())
    pending = {**waiting, "attempts": 0, "scheduled": now, "due": now + DELAYS[0]}
    atomic_json(waiting_path(env, episode, reason, item), waiting)
    restore_retry(env, episode, reason, item, pending)
    notify.log(f"handoff-retry {reason} scheduled in {DELAYS[0]}s attempt=0/{MAX_ATTEMPTS} episode={episode}")
    return pending


def comment_post_episodes(env, reason=None):
    """Find review or screenshot comment-post operations by PR number."""
    return operation_episodes(env, (reason,) if reason else COMMENT_POSTS)


def operation_episodes(env, reasons):
    found = {}
    for name in reasons:
        for path in Path(env.dirs.state).glob(f"handoff-*-{name}.json"):
            match = re.fullmatch(rf"handoff-(?:waiting|retry)-([0-9]+)(?:-([0-9]+))?-{re.escape(name)}\.json",
                                 path.name)
            if match:
                episode, item = match.groups()
                found.setdefault(item or episode, set()).add(episode)
    return found


def observe_remote(wt, branch):
    """(sha, transient): the SHA at origin/BRANCH, '' when absent, None when unreadable;
    transient says whether an unreadable ref looks like a transport failure."""
    from . import session
    from .common import push_transient
    done = session.run_process(
        ["git", "-C", str(wt), "ls-remote", "origin", f"refs/heads/{branch}"],
        capture_output=True, text=True)
    if done.returncode != 0:
        return None, push_transient((done.stderr or "") + (done.stdout or ""))
    line = (done.stdout or "").strip().splitlines()
    return (line[0].split()[0] if line else ""), False


def push_destination(wt, ref):
    """The branch name a push of REF updates on origin."""
    from .common import git
    if ref == "HEAD":
        return git(wt, "branch", "--show-current") or ""
    return ref


def check_push_local(binding):
    from .common import git
    wt = binding["wt"]
    return (git(wt, "rev-parse", "HEAD") == binding["commit"]
            and git(wt, "branch", "--show-current") == binding["branch"])


def check_push_landed(binding):
    """Whether origin already holds the saved post-sign-off commit (#151)."""
    if not check_push_local(binding):
        return "changed"
    current, transient = observe_remote(binding["wt"], binding["branch"])
    if current is None:
        return "retry" if transient else "failed"
    if current == binding["commit"]:
        return "landed"
    if current == binding["remote"]:
        return "absent"
    return "changed"


def _clear_path(path):
    try:
        path.unlink(missing_ok=True)
    except IsADirectoryError:
        path.rename(path.with_name(f"damaged-{uuid4().hex}-{path.name}"))


def clear_retry(env, episode, reason, item=""):
    for path in (retry_path(env, episode, reason, item), waiting_path(env, episode, reason, item)):
        try:
            _clear_path(path)
        except OSError as exc:
            notify.log(f"could not clear {path.name}: {exc}")
    if reason == DEV_PUSH:
        Path(env.dirs.state, f"dev-push-waiting-{episode}").unlink(missing_ok=True)
    elif reason == SPEC_ADR_PUSH:
        Path(env.dirs.state, f"spec-adr-push-waiting-{episode}").unlink(missing_ok=True)


def clear_episode(env, episode, item=""):
    """Drop this item's pending operations when handoff ends its episode."""
    for reason in RETRY_REASONS:
        clear_retry(env, episode, reason, item)


def label_ready(env, kind, number, remove=()):
    """Add ready-for-human on a PR or issue; REMOVE labels first. Returns the gh CompletedProcess."""
    opts = []
    for name in remove:
        opts.extend(["--remove-label", name])
    opts.extend(["--add-label", READY])
    cmd = "pr" if kind == "pr" else "issue"
    return pr_helpers.gh(cmd, "edit", str(number), "-R", env.repo.gh_repo, *opts)


def _finish(env, reason, entry, *, kind, number, title, text, hooks, remove, item, episode,
            after_labels, decision=None, before_labels=None):
    """The one terminal handoff: outcomes, labels, telemetry, notify, log."""
    finishing_push = (kind == "pr" and reason in ("fix_run_failed", "fix_needs_info", "fix_checks_refresh_failed")
                      and Path(env.dirs.state, f"fix-push-waiting-{number}").exists())
    if not finishing_push:
        if kind == "pr":
            # Retire the continuation before publishing the handoff or releasing its grant.
            Path(env.dirs.state, f"review-pending-{number}").unlink(missing_ok=True)
            Path(env.dirs.state, f"fix-push-waiting-{number}").unlink(missing_ok=True)
        clear_episode(env, episode, item or number)
        recovery.clear_episode(env, episode)
    if before_labels:
        before_labels()   # the comment that explains the failure goes up before the label
    result = label_ready(env, kind, number, remove=remove)
    if after_labels:
        after_labels(result)
    advisor.finish_episode(env, kind, number, "handoff")
    hooks.task("handoff", reason, "--reason", reason)
    body = (text() if callable(text) else text) + f" Reason {reason} (category {entry.category})."
    if decision:
        body += advisor.handoff_note(decision)
    notify.notify(title, body)
    notify.log(f"handoff {reason} category={entry.category}")
    if finishing_push:
        Path(env.dirs.state, f"review-pending-{number}").unlink(missing_ok=True)
        Path(env.dirs.state, f"fix-push-waiting-{number}").unlink(missing_ok=True)
        clear_episode(env, episode, item or number)
    return Result(HANDOFF, decision, True)


def _category1(env, reason, entry, *, kind, number, title, text, hooks, remove, item, episode,
               after_labels, transient, continuation, resume):
    """Rule-decided recovery: schedule, consume a due attempt, or hand off (#151)."""
    now = int(clock())
    item = str(item or number)
    path = retry_path(env, episode, reason, item)

    def finish():
        return _finish(env, reason, entry, kind=kind, number=number, title=title, text=text, hooks=hooks,
                       remove=remove, item=item, episode=episode,
                       after_labels=after_labels)

    try:
        pending = load_retry(env, episode, reason, item)
    except RetryUnreadable as exc:
        notify.log(f"handoff-retry {reason} unreadable episode={episode}: {exc}")
        return finish()

    if resume:
        if pending is None or pending["attempts"] >= MAX_ATTEMPTS:
            return finish()
        if now < pending["due"]:
            return Result(DEFER, handed_off=False, continuation=pending.get("continuation"))
        attempts = pending["attempts"] + 1
        updated = {**pending, "attempts": attempts}
        if attempts < MAX_ATTEMPTS:
            updated.update(scheduled=now, due=now + DELAYS[attempts])
        atomic_json(path, updated)
        notify.log(f"handoff-retry {reason} attempt={attempts}/{MAX_ATTEMPTS} episode={episode}")
        return Result(RETRY, handed_off=False, continuation=updated.get("continuation"))

    # A saved entry that was never resumed (its caller forgot) must not loop forever: hand off.
    if not transient or (pending is not None and pending["attempts"] == 0) or (
            pending is not None and pending["attempts"] >= MAX_ATTEMPTS):
        return finish()

    if pending is not None:
        return Result(DEFER, handed_off=False, continuation=pending["continuation"])

    binding = continuation if continuation is not None else {}
    if prepare_retry(env, episode, reason, item, binding) is None:
        return finish()
    return Result(DEFER, handed_off=False, continuation=binding)


def perform(env, reason, *, kind, number, title, text, hooks, remove=(), item="", episode="",
            snapshot="", revalidate=None, after_labels=None, observed_actions=None,
            transient=False, continuation=None, resume=False, fallback=False, before_labels=None):
    """Validate REASON, apply its rule or advisor action, and on handoff run labels/telemetry/notify.

    KIND is "pr" or "issue". REMOVE is labels to drop before adding ready-for-human. AFTER_LABELS
    receives the gh result when the caller must react to a failed swap. Recovery actions other
    than handoff are returned for the caller to execute; handoff side effects have already run.
    For eligible category-1 reads, TRANSIENT schedules a deferred attempt and RESUME consumes
    a due attempt (RETRY) or waits (DEFER). Review lookup also accepts a missing expected comment.
    FALLBACK lets the caller apply its normal rule when no grant is made. BEFORE_LABELS runs
    just before the terminal handoff labels the item (the explaining comment goes first).
    Raises UnknownReason before any side effect when REASON is not in the table.
    """
    entry = REASONS.get(reason)
    if entry is None:
        raise UnknownReason(f"unlisted handoff reason: {reason!r}")
    episode = str(episode or item or number)

    def finish(decision=None):
        if fallback:
            return Result(HANDOFF, decision, False)
        return _finish(env, reason, entry, kind=kind, number=number, title=title, text=text, hooks=hooks,
                       remove=remove, item=item, episode=episode, after_labels=after_labels,
                       decision=decision, before_labels=before_labels)

    if reason in RETRY_REASONS:
        return _category1(env, reason, entry, kind=kind, number=number, title=title, text=text,
                          hooks=hooks, remove=remove, item=item, episode=episode,
                          after_labels=after_labels, transient=transient,
                          continuation=continuation, resume=resume)

    # Callers that have not opted in keep today's handoff. Observation always passes the
    # actions the current evidence supports; live worker/investigation retries do the same
    # once wired. The #17 round limits still offer extra_round without that opt-in.
    if entry.category in (1, 4) or not entry.actions:
        return finish()
    if observed_actions is not None:
        offered = tuple(a for a in entry.actions if a in observed_actions)
        if not offered and entry.mode != "observe":
            return finish()
    elif entry.mode == "live" and "extra_round" in entry.actions:
        offered = ("extra_round",)
    else:
        prior = advisor.saved_attempt(env, reason, number, kind)
        return finish(prior)
    cfg = advisor.settings(env)
    mode = capped_mode(cfg.mode if advisor.enabled(env) else "off", entry.mode)
    if mode == "off":
        return finish(advisor.saved_attempt(env, reason, number, kind))
    typed = entry.mode == "observe" or any(action != "extra_round" for action in offered)
    decision = advisor.advise(env, reason, number, snapshot, offered, task_kind=kind,
                             revalidate=revalidate, rule=HANDOFF, mode=mode,
                             category=entry.category if typed else None)
    if decision.action == HANDOFF:
        return finish(decision)
    return Result(decision.action, decision, False)
