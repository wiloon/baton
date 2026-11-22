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
# Read retries wired in this slice; write retries stay direct handoff until a later slice.
RETRY_REASONS = frozenset({
    "review_comment_missing", "spec_comments_read_failed", "spec_pending_adr_read_failed",
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
    if waiting != expected or any(data.get(key) != value for key, value in expected.items()):
        raise RetryUnreadable(f"mismatched retry operation {path.name}")
    if not _binding_valid(reason, item, binding):
        raise RetryUnreadable(f"invalid continuation in {path.name}")
    attempts, due, scheduled = data.get("attempts"), data.get("due"), data.get("scheduled")
    if type(attempts) is not int or not (0 <= attempts <= MAX_ATTEMPTS):
        raise RetryUnreadable(f"out-of-range attempts in {path.name}")
    if (type(due) is not int or type(scheduled) is not int or not 0 <= scheduled <= int(clock())
            or due != scheduled + DELAYS[min(attempts, MAX_ATTEMPTS - 1)]):
        raise RetryUnreadable(f"out-of-range due in {path.name}")
    return data


def restore_retry(env, episode, reason, item, pending):
    """Keep a due attempt waiting when the authoritative read reports an owner pause."""
    atomic_json(retry_path(env, episode, reason, item), pending)


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
            after_labels, decision=None):
    """The one terminal handoff: outcomes, labels, telemetry, notify, log."""
    clear_episode(env, episode, item or number)
    recovery.clear_episode(env, episode)
    if kind == "pr":
        # Retire the continuation before publishing the handoff or releasing its grant.
        Path(env.dirs.state, f"review-pending-{number}").unlink(missing_ok=True)
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
    if not _binding_valid(reason, item, binding):
        return finish()
    waiting = {"reason": reason, "episode": episode, "item": item, "continuation": binding}
    delay = DELAYS[0]
    atomic_json(waiting_path(env, episode, reason, item), waiting)
    atomic_json(path, {**waiting, "attempts": 0, "scheduled": now, "due": now + delay})
    notify.log(f"handoff-retry {reason} scheduled in {delay}s attempt=0/{MAX_ATTEMPTS} episode={episode}")
    return Result(DEFER, handed_off=False, continuation=binding)


def perform(env, reason, *, kind, number, title, text, hooks, remove=(), item="", episode="",
            snapshot="", revalidate=None, after_labels=None, observed_actions=None,
            transient=False, continuation=None, resume=False):
    """Validate REASON, apply its rule or advisor action, and on handoff run labels/telemetry/notify.

    KIND is "pr" or "issue". REMOVE is labels to drop before adding ready-for-human. AFTER_LABELS
    receives the gh result when the caller must react to a failed swap. Recovery actions other
    than handoff are returned for the caller to execute; handoff side effects have already run.
    For eligible category-1 reads, TRANSIENT schedules a deferred attempt and RESUME consumes
    a due attempt (RETRY) or waits (DEFER). Review lookup also accepts a missing expected comment.
    Raises UnknownReason before any side effect when REASON is not in the table.
    """
    entry = REASONS.get(reason)
    if entry is None:
        raise UnknownReason(f"unlisted handoff reason: {reason!r}")
    episode = str(episode or item or number)

    def finish(decision=None):
        return _finish(env, reason, entry, kind=kind, number=number, title=title, text=text, hooks=hooks,
                       remove=remove, item=item, episode=episode, after_labels=after_labels,
                       decision=decision)

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
