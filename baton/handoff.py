"""Shared task handoff entry (#125, ADR 0008): one place for ready-for-human.

Every task handoff in review, fix, merge, spec, dev and tick goes through `perform`. It validates
the reason against the code table, applies the rule or advisor action for that reason, and on a
terminal handoff performs the label change, telemetry and notification. Callers keep their own
label removals and stop semantics. Cursor triage and after-merge tracker issues stay outside.
"""
from dataclasses import dataclass

from . import advisor, notify, pr as pr_helpers

HANDOFF = advisor.HANDOFF
READY = "ready-for-human"

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

def capped_mode(global_mode, point_mode):
    """ADR 0008: the global mode is a ceiling on the decision point's mode."""
    if point_mode not in RANK:
        return "off"
    if global_mode not in RANK:
        return "off"
    return point_mode if RANK[point_mode] <= RANK[global_mode] else global_mode


def label_ready(env, kind, number, remove=()):
    """Add ready-for-human on a PR or issue; REMOVE labels first. Returns the gh CompletedProcess."""
    opts = []
    for name in remove:
        opts.extend(["--remove-label", name])
    opts.extend(["--add-label", READY])
    cmd = "pr" if kind == "pr" else "issue"
    return pr_helpers.gh(cmd, "edit", str(number), "-R", env.repo.gh_repo, *opts)


def perform(env, reason, *, kind, number, title, text, hooks, remove=(),
            snapshot="", revalidate=None, after_labels=None, observed_actions=None):
    """Validate REASON, apply its rule or advisor action, and on handoff run labels/telemetry/notify.

    KIND is "pr" or "issue". REMOVE is labels to drop before adding ready-for-human. AFTER_LABELS
    receives the gh result when the caller must react to a failed swap. Recovery actions other
    than handoff are returned for the caller to execute; handoff side effects have already run.
    Raises UnknownReason before any side effect when REASON is not in the table.
    """
    entry = REASONS.get(reason)
    if entry is None:
        raise UnknownReason(f"unlisted handoff reason: {reason!r}")

    def finish(decision=None):
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

    # The observation caller supplies only the actions the current evidence supports.
    observing = entry.mode == "observe" and observed_actions is not None
    if entry.category in (1, 4) or (not observing and "extra_round" not in entry.actions):
        return finish()
    offered = tuple(a for a in entry.actions if a in observed_actions) if observing else ("extra_round",)
    cfg = advisor.settings(env)
    mode = capped_mode(cfg.mode if advisor.enabled(env) else "off", entry.mode)
    if mode == "off":
        return finish()
    decision = advisor.advise(env, reason, number, snapshot, offered, task_kind=kind,
                             revalidate=revalidate, rule=HANDOFF, mode=mode,
                             category=entry.category if observing else None)
    if decision.action == HANDOFF:
        return finish(decision)
    return Result(decision.action, decision, False)
