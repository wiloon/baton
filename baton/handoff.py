"""Shared task handoff entry (#125, ADR 0008): one place for ready-for-human.

Every task handoff in review, fix, merge, spec, dev and tick goes through `perform`. It validates
the reason against the code table, applies the rule or advisor action for that reason, and on a
terminal handoff performs the label change, telemetry and notification. Callers keep their own
label removals and stop semantics. Cursor triage and after-merge tracker issues stay outside.

Category-1 reasons never call the advisor. After `pr.gh`'s per-call backoff, a recognized
transient failure schedules at most two later attempts (due after 60s then 300s). The attempt is
consumed before it runs so a restart cannot reset the budget. Permanent failures and exhaustion
hand off. Deferrals yield the tick without sleeping.
"""
import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import advisor, notify
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
RETRY_REASONS = frozenset({"review_comment_missing", "spec_comments_read_failed", "spec_pending_adr_read_failed"})

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
    "fix_description_write_failed": Reason(1, "rule"),
    "fix_task_write_failed": Reason(1, "rule"),
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

# Actions the runner already executes live at a decision point (#17 / this slice).
LIVE_ACTIONS = frozenset({"extra_round"})


def capped_mode(global_mode, point_mode):
    """ADR 0008: the global mode is a ceiling on the decision point's mode."""
    if point_mode not in RANK:
        return "off"
    if global_mode not in RANK:
        return "off"
    return point_mode if RANK[point_mode] <= RANK[global_mode] else global_mode


def retry_path(env, episode, reason):
    """Repository state keyed by task episode and reason (#125 category-1 budget)."""
    return Path(str(env.dirs.state)) / f"handoff-retry-{episode}-{reason}.json"


class RetryUnreadable(Exception):
    """A retry record exists but cannot be read or parsed; callers must fail closed."""


def load_retry(env, episode, reason):
    """Return the saved retry dict, None when absent, or raise RetryUnreadable when corrupt."""
    path = retry_path(env, episode, reason)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RetryUnreadable(f"unreadable retry record {path.name}") from exc
    if not isinstance(data, dict):
        raise RetryUnreadable(f"invalid retry record {path.name}")
    return data


def save_retry(env, episode, reason, data):
    atomic_json(retry_path(env, episode, reason), data)


def clear_retry(env, episode, reason):
    retry_path(env, episode, reason).unlink(missing_ok=True)


def label_ready(env, kind, number, remove=()):
    """Add ready-for-human on a PR or issue; REMOVE labels first. Returns the gh CompletedProcess."""
    opts = []
    for name in remove:
        opts.extend(["--remove-label", name])
    opts.extend(["--add-label", READY])
    cmd = "pr" if kind == "pr" else "issue"
    return pr_helpers.gh(cmd, "edit", str(number), "-R", env.repo.gh_repo, *opts)


def _finish(env, reason, entry, *, kind, number, title, text, hooks, remove, item, episode, close_outcomes,
            after_labels, decision=None):
    """The one terminal handoff: outcomes, labels, telemetry, notify, log."""
    clear_episode(env, episode)
    for point in close_outcomes:
        advisor.outcome(env, point, item, "handoff")
    result = label_ready(env, kind, number, remove=remove)
    if after_labels:
        after_labels(result)
    hooks.task("handoff", reason, "--reason", reason)
    body = (text() if callable(text) else text) + (advisor.handoff_note(decision) if decision else "")
    notify.notify(title, body)
    notify.log(f"handoff {reason} category={entry.category}")
    return Result(HANDOFF, decision, True)


def clear_episode(env, episode):
    """Drop every pending category-1 retry of EPISODE (a terminal handoff ends them all)."""
    for path in Path(str(env.dirs.state)).glob(f"handoff-retry-{episode}-*.json"):
        path.unlink(missing_ok=True)


def _category1(env, reason, entry, *, kind, number, title, text, hooks, remove, item, episode,
               close_outcomes, after_labels, transient, continuation, resume):
    """Rule-decided recovery: schedule, consume a due attempt, or hand off (#125)."""
    now = int(clock())

    def finish():
        return _finish(env, reason, entry, kind=kind, number=number, title=title, text=text, hooks=hooks,
                       remove=remove, item=item, episode=episode, close_outcomes=close_outcomes,
                       after_labels=after_labels)

    try:
        pending = load_retry(env, episode, reason)
    except RetryUnreadable as exc:
        notify.log(f"handoff-retry {reason} unreadable episode={episode}: {exc}")
        return finish()
    attempts = int(pending.get("attempts") or 0) if pending else 0

    if resume:
        if pending is None or attempts >= MAX_ATTEMPTS:
            return finish()
        due = int(pending.get("due") or 0)
        if now < due:
            return Result(DEFER, handed_off=False, continuation=pending.get("continuation"))
        attempts += 1
        pending = {**pending, "attempts": attempts}
        if attempts < MAX_ATTEMPTS:
            pending["due"] = now + DELAYS[attempts]
        save_retry(env, episode, reason, pending)
        notify.log(f"handoff-retry {reason} attempt={attempts}/{MAX_ATTEMPTS} episode={episode}")
        return Result(RETRY, handed_off=False, continuation=pending.get("continuation"))

    # A saved entry that was never resumed (its caller forgot) must not loop forever: hand off.
    if not transient or (pending is not None and attempts == 0) or attempts >= MAX_ATTEMPTS:
        return finish()

    delay = DELAYS[attempts]
    save_retry(env, episode, reason, {
        "attempts": attempts,
        "due": now + delay,
        "continuation": continuation or (pending or {}).get("continuation") or {},
        "reason": reason,
        "episode": episode,
    })
    notify.log(f"handoff-retry {reason} scheduled in {delay}s attempt={attempts}/{MAX_ATTEMPTS} episode={episode}")
    return Result(DEFER, handed_off=False, continuation=continuation)


def perform(env, reason, *, kind, number, title, text, hooks, remove=(), item="", episode="",
            snapshot="", revalidate=None, close_outcomes=(), after_labels=None,
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
                       remove=remove, item=item, episode=episode, close_outcomes=close_outcomes, after_labels=after_labels,
                       decision=decision)

    if reason in RETRY_REASONS:
        return _category1(env, reason, entry, kind=kind, number=number, title=title, text=text,
                          hooks=hooks, remove=remove, item=item, episode=episode,
                          close_outcomes=close_outcomes, after_labels=after_labels,
                          transient=transient, continuation=continuation, resume=resume)

    if entry.category == 1:
        return finish()

    # Category 4 never calls the advisor. Category 2/3 may, when the effective mode allows
    # and this slice already executes the offered action (extra_round). Other recoveries fall
    # through to the rule (handoff) until a later slice wires them.
    if entry.category == 4 or not entry.actions:
        return finish()

    offered = tuple(a for a in entry.actions if a in LIVE_ACTIONS)
    if not offered or advisor.granted(env, reason, item):
        return finish()

    cfg = advisor.settings(env)
    mode = capped_mode(cfg.mode if advisor.enabled(env) else "off", entry.mode)
    if mode == "off":
        return finish()
    decision = advisor.advise(env, reason, item, snapshot, offered, episode=episode or str(item),
                             revalidate=revalidate, rule=HANDOFF, mode=mode)
    if decision.action == HANDOFF:
        return finish(decision)
    return Result(decision.action, decision, False)
