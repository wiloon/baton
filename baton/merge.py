"""Merge, or hand a reviewed PR to the owner (migration slice 4c, #280): `merge_or_handoff`,
`ci_green`, `ci_fix_round`, `finish_merged_task` and `remove_worktree` of run.sh.

The order of `merge_or_handoff` is a contract: CI green (one repair round), the gate, the
merge, the notification, the worktree, the screenshots, `agent-wip`, the fetch,
`finish_merged_task`, `after_merge`. Every stop hands the PR to the owner the same way: a
label, a telemetry handoff, a Telegram message.

CLI for the bash callers that stay (the tick's recovery, the spec round, the cleanup loops):
    ci-green PR MINUTES    exit 0 when CI is green (telemetry recorded)
    finish-task PR         exit 1 when the recovery should be retried
    remove-worktree WT     exit 0 when the worktree and its branch are gone
"""

from . import session
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from . import after_merge, check_verdicts, checks, ci, fix, hooks as telemetry, names, notify, pick, pr as pr_helpers
from .common import Deferred, git


def remove_worktree(repo_dir, wt):
    """Drop the worktree and its local branch. The remote branch is deleted by GitHub on
    merge (delete_branch_on_merge)."""
    branch = git(wt, "branch", "--show-current") or ""
    if session.run_process(["git", "-C", repo_dir, "worktree", "remove", "--force", str(wt)]).returncode != 0 or not branch:
        return False
    return session.run_process(["git", "-C", repo_dir, "branch", "-D", branch], stdout=subprocess.DEVNULL).returncode == 0


class Merger:
    @session.scoped
    def __init__(self, env, hooks, pr, issue=""):
        env = session.resolve(env)
        self.env, self.hooks, self.pr, self.issue = env, hooks, pr, issue
        self.state, self.ctx, self.repo = str(env.dirs.state), str(env.dirs.ctx), env.repo.gh_repo
        self.url = f"https://github.com/{self.repo}/pull/{pr}"

    def label(self, *options):
        pr_helpers.gh("pr", "edit", str(self.pr), "-R", self.repo, *options)

    def handoff(self, reason, title, text):
        self.label("--add-label", "ready-for-human")
        self.hooks.task("handoff", reason, "--reason", reason)
        notify.notify(title, text)

    # --- CI ---
    def ci_green(self, minutes):
        """CI passed, or the PR triggers no checks (docs-only paths); baton.ci. The result
        goes to the optional CI metadata hook."""
        before = ""
        if self.hooks.enabled:
            result = pr_helpers.gh("pr", "view", str(self.pr), "-R", self.repo, "--json", "headRefOid", "--jq", ".headRefOid",
                                   limit=15)
            before = result.stdout.strip() if result.returncode == 0 else ""
        started = time.monotonic()
        result = ci.green(str(self.pr), minutes)
        self.hooks.ci(self.pr, result["rc"], before, result["no_checks"],
                      duration_ms=int((time.monotonic() - started) * 1000))
        return result["green"]

    def ci_fix_round(self, wt):
        """One repair for a failed code-check head (#243). baton.ci decides: infrastructure
        and ambiguous failures, and a head repaired once already, are left for the owner.
        Returns 0 when a repair was pushed. A repair that cannot run now hands the head off."""
        pr = self.pr
        found = ci.classify(str(pr), self.state, list(self.env.repo.ci_fixable_checks))
        if found is None or not re.fullmatch(r"[a-f0-9]{40,64}", found["head"]):
            return 1
        head = found["head"]
        try:
            fixer = pick.choose(self.env, "fixer", "agent", "contrib", pick.all_vendors(self.env), pr=str(pr),
                                issue=self.issue)
            notify.log(f"PR #{pr}: code-only CI failure on {head}; one repair round by {fixer}")
            rc = fix.fix_pr(fix.Runner(self.env, self.hooks, pr, "ci"), pr, wt, fixer, "CI code checks", found["feedback"])
        except Deferred:
            rc = 75
        if rc == 75:
            notify.log(f"PR #{pr}: CI repair deferred; handing off this head")
            return 1
        Path(self.state, f"ci-fix-{pr}-{head}").touch()   # one repair round for this PR head, even across ticks
        if rc != 0:
            return rc
        self.hooks.task("ci_fix", f"pr:{pr}:{head}", "--agent", fixer, "--head-sha", head)
        return 0

    # --- the task behind the PR ---
    def finish_merged_task(self):
        """Idempotent recovery for a partial slice, including owner merges: keep an open issue
        runnable without reusing a merged branch or worktree next time. False: retry later."""
        pr, state_dir = self.pr, self.state
        if Path(state_dir, f"task-merged-pr-{pr}").exists():
            return True
        try:
            issue = pr_helpers.relation(pr)
        except ValueError:
            return True
        completion, issue = issue["completion"], issue["issue"]
        if not re.fullmatch(r"\d+", issue):
            return True
        gh = lambda *args: pr_helpers.gh(*args, "-R", self.repo)
        state = gh("issue", "view", issue, "--json", "state", "--jq", ".state")
        if state.returncode != 0:
            return False
        if completion == "partial" and state.stdout.strip() == "OPEN":
            branch, wt = names.for_issue(str(self.env.dirs.worktrees), issue)
            open_prs = gh("pr", "list", "--state", "open", "--head", branch, "--json", "number", "--jq", "length")
            if open_prs.returncode != 0:
                return False
            if open_prs.stdout.strip() != "0":
                return True   # A later slice already owns the task.
            if wt.is_dir():
                if git(wt, "status", "--porcelain") or session.run_process(
                        ["git", "-C", str(wt), "merge-base", "--is-ancestor", "HEAD", "origin/main"]).returncode != 0:
                    notify.log(f"PR #{pr}: retained unmerged or dirty worktree {wt}")
                    return False
                if not remove_worktree(str(self.env.dirs.clone), wt):
                    return False
            if gh("issue", "edit", issue, "--remove-label", "agent-wip", "--add-label", "ready-for-agent").returncode != 0:
                return False
            Path(state_dir, f"last-slice-{issue}").write_text(f"{pr}\n")
            Path(self.env.config.paths.state, "chain-next").touch()   # global: the next pass may be any repository's
            notify.log(f"PR #{pr}: partial slice merged; #{issue} ready for its next slice")
        elif state.stdout.strip() == "CLOSED":
            # Closed is the done state; a leftover ready-for-agent would only mislead.
            if gh("issue", "edit", issue, "--remove-label", "agent-wip", "--remove-label", "ready-for-agent").returncode != 0:
                return False
        else:
            return True   # A closing keyword may still be awaiting GitHub's state transition.
        Path(state_dir, f"task-merged-pr-{pr}").touch()
        return True

    def after_merge(self):
        try:
            after_merge.follow_ups(str(self.pr), self.issue)
            after_merge.owner_actions(str(self.pr), self.issue)
            after_merge.deferred_checks(str(self.pr), self.issue)
        except Exception as exc:
            notify.log(f"PR #{self.pr}: after-merge failed: {exc}")
            notify.log(f"PR #{self.pr}: after-merge filing failed")

    # --- the screenshot review's verdicts (#92, ADR 0006) ---
    def left_for_owner(self, screenshot, body):
        """The PR's checks the screenshot review did not verify; None when there is no usable
        review: none ran, the body is not well-formed or lists no check, the description's
        checks changed since it ran, or its verdicts are unreadable."""
        if not screenshot or body is None:
            return None
        listed = checks.items(body)
        if not listed or listed != check_verdicts.items(screenshot.get("checks") or ""):
            return None
        verdicts = check_verdicts.parse(screenshot.get("comment") or "", len(listed))
        if verdicts is None:
            return None
        return [item for item, verdict in zip(listed, verdicts) if verdict != "verified"]

    def remainder(self):
        return Path(self.state, f"deferred-checks-items-pr-{self.pr}")

    def keep_deferred(self, source, left):
        """With manual_checks = "defer", only the checks LEFT go to the tracking issue (#48). The
        file records the SOURCE checks they were computed from too, so the tracker trusts it
        only while the merged description still lists them."""
        self.remainder().write_text(json.dumps({"source": source, "left": left}) + "\n")

    def drop_deferred(self):
        self.remainder().unlink(missing_ok=True)

    def owed(self, left, body):
        """What a hand-off shows the owner: the checks left after a usable screenshot review,
        else every current check (of every checks section when the body is not well-formed)."""
        return left if left is not None else checks.recovery_items(body or "")

    def left_text(self, left, owed):
        if not owed:
            return ""
        lead = "The screenshot review left these checks for you" if left is not None else "Checks for you"
        return f"{lead}:" + "".join(f"\n- {item}" for item in owed)

    # --- update a behind branch, then merge (#122) ---
    def pr_merge_state(self):
        """mergeStateStatus and headRefOid, or None when gh fails."""
        return pr_helpers.view(self.pr, "mergeStateStatus,headRefOid")

    def confirm_new_head(self, before, deadline=60):
        """Poll until headRefOid is nonempty and different from BEFORE, or DEADLINE seconds elapse."""
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            data = pr_helpers.view(self.pr, "headRefOid")
            sha = (data or {}).get("headRefOid") or ""
            if sha and sha != before:
                return sha
            time.sleep(2)
        return ""

    def merge_with_updates(self, minutes, *, before_merge=None):
        """Merge the PR, updating a behind branch at most three times (#122).

        BEFORE_MERGE: optional callable() -> gate-style dict. Called after a confirmed
        update and its CI wait, before each merge attempt. A result whose action is not
        "merge" stops further attempts.

        Returns {"ok": True} or {"ok": False, "reason": ..., "detail": ..., "gate": ...}.
        """
        pr, repo = str(self.pr), self.repo
        updates, match_sha = 0, None
        data = self.pr_merge_state()
        while True:
            status = (data or {}).get("mergeStateStatus")
            if status == "BEHIND":
                if updates >= 3:
                    return {"ok": False, "reason": "behind_exhausted", "detail": ""}
                before = (data or {}).get("headRefOid") or ""
                if not before:
                    return {"ok": False, "reason": "sha_unavailable",
                            "detail": " mergeStateStatus is BEHIND but headRefOid is unavailable"}
                updated = pr_helpers.gh("pr", "update-branch", pr, "-R", repo)
                if updated.returncode != 0:
                    return {"ok": False, "reason": "update_failed", "detail": pr_helpers.why(updated)}
                new_sha = self.confirm_new_head(before)
                if not new_sha:
                    return {"ok": False, "reason": "head_unchanged",
                            "detail": " branch head did not change after update-branch"}
                updates += 1
                match_sha = new_sha
                if not self.ci_green(minutes):
                    return {"ok": False, "reason": "ci_not_green", "detail": ""}
                if before_merge is not None:
                    gate = before_merge()
                    if gate is not None and gate.get("action") != "merge":
                        return {"ok": False, "reason": "gate", "detail": "", "gate": gate}
            args = ["pr", "merge", pr, "-R", repo, "--merge"]
            if match_sha:
                args.extend(["--match-head-commit", match_sha])
            merged = pr_helpers.gh(*args)
            if merged.returncode == 0:
                return {"ok": True}
            data = self.pr_merge_state()   # carried into the next iteration, never re-read
            if (data or {}).get("mergeStateStatus") != "BEHIND":
                return {"ok": False, "reason": "merge_failed", "detail": pr_helpers.why(merged)}
            match_sha = None

    # --- the review passed ---
    def merge_or_handoff(self, wt, screenshot=None):
        """Merge when auto-merge is on for the runner and for this repository, CI is green and
        the checks section is none, the screenshot review verified every check (SCREENSHOT:
        the reviewed checks and the round's screenshot review comment, #92), or the repository
        defers checks (#48); otherwise label it ready-for-human."""
        pr, url = self.pr, self.url
        deferred = self.env.repo.manual_checks == "defer"
        if self.env.config.scheduling.auto_merge is not True or self.env.repo.auto_merge is not True:
            where = "" if self.env.config.scheduling.auto_merge is not True else f" for {self.repo}"
            body = pr_helpers.body(pr)
            left = self.left_for_owner(screenshot, body)
            owed = self.owed(left, body)
            listed = self.left_text(left, owed)
            self.handoff("auto_merge_off", f"PR #{pr} review passed",
                         f"Auto-merge is off{where}; ready for you." + (f" {listed}\n{url}" if listed else f" {url}"))
            if not deferred and (body is None or not pr_helpers.checks_none(body)):
                notify.send_shots(pr, note="Checks for you" if left is None else "", left=owed)
            return
        if not self.ci_green(45):
            if self.ci_fix_round(wt) != 0 or not self.ci_green(45):
                self.handoff("ci_not_green", f"PR #{pr}: CI not green", f"Review passed but CI failed or timed out. {url}")
                return
        body, left = None, None
        try:
            body = pr_helpers.body(pr)
            left = self.left_for_owner(screenshot, body)
            gate = pr_helpers.gate(pr, defer=deferred, cleared=screenshot["checks"] if left == [] else None)
        except Exception:
            gate = {"action": "manual_checks"}
        if gate.get("action") == "queue_review":
            notify.log(f"PR #{pr}: merge preflight blocked: {gate['reason']}")
            self.label("--add-label", "agent-review")
            notify.notify(f"PR #{pr}: metadata review queued", f"{gate['reason']} {url}")
            return
        if gate.get("action") != "merge":   # the checks, or no answer: the owner decides
            live_body = gate.get("body")
            left = self.left_for_owner(screenshot, live_body)
            owed = self.owed(left, live_body if live_body is not None else body)
            listed = self.left_text(left, owed)
            self.handoff("manual_checks", f"PR #{pr} needs a manual check",
                         "Review and CI passed." + (f" {listed}\n{url}" if listed else f" {url}"))
            notify.send_shots(pr, note="Checks for you" if left is None else "", left=owed)
            return
        if deferred:   # the remainder belongs to this attempt, never to an earlier one
            if left is None:
                self.drop_deferred()
            else:
                self.keep_deferred(checks.items(body), left)

        def recheck():
            """After a branch update, re-run checks clearance and metadata preflight (ADR 0006)."""
            nonlocal body, left
            try:
                body = pr_helpers.body(pr)
                left = self.left_for_owner(screenshot, body)
                return pr_helpers.gate(pr, defer=deferred, cleared=screenshot["checks"] if left == [] else None)
            except Exception:
                return {"action": "manual_checks"}

        result = self.merge_with_updates(45, before_merge=recheck)
        if not result.get("ok"):
            self.drop_deferred()
            reason = result.get("reason")
            if reason == "ci_not_green":
                self.handoff("ci_not_green", f"PR #{pr}: CI not green",
                             f"Review passed but CI failed or timed out after updating the branch. {url}")
                return
            if reason == "gate":
                gate = result.get("gate") or {"action": "manual_checks"}
                if gate.get("action") == "queue_review":
                    notify.log(f"PR #{pr}: merge preflight blocked: {gate['reason']}")
                    self.label("--add-label", "agent-review")
                    notify.notify(f"PR #{pr}: metadata review queued", f"{gate['reason']} {url}")
                    return
                live_body = gate.get("body")
                left = self.left_for_owner(screenshot, live_body)
                owed = self.owed(left, live_body if live_body is not None else body)
                listed = self.left_text(left, owed)
                self.handoff("manual_checks", f"PR #{pr} needs a manual check",
                             "Review and CI passed." + (f" {listed}\n{url}" if listed else f" {url}"))
                notify.send_shots(pr, note="Checks for you" if left is None else "", left=owed)
                return
            detail = result.get("detail") or ""
            self.handoff("merge_failed", f"PR #{pr} merge failed", f"{url}{detail}")
            return
        if deferred:   # a recheck may have refreshed left against the live body
            if left is None:
                self.drop_deferred()
            else:
                self.keep_deferred(checks.items(body) if body is not None else [], left)
        self.hooks.task("merged", "merged")
        notify.notify(f"PR #{pr} merged", url)
        # A partial PR keeps its issue open; release it for the next slice below.
        if remove_worktree(str(self.env.dirs.clone), wt):
            notify.log(f"cleaned up worktree {wt}")
        if left == [] and not deferred:   # merged on the screenshot review: the owner can still look
            notify.send_shots(pr, note="merged after screenshot review", left=checks.items(body))
        session.run_process(["rm", "-rf", str(Path(self.ctx, f"pr-{pr}-shots"))])
        if self.issue:
            pr_helpers.gh("issue", "edit", self.issue, "-R", self.repo, "--remove-label", "agent-wip")
        session.run_process(["git", "-C", str(self.env.dirs.clone), "fetch", "-q", "--prune", "origin"])
        if not self.finish_merged_task():
            notify.log(f"PR #{pr}: task recovery will retry")
        self.after_merge()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    green = sub.add_parser("ci-green")
    green.add_argument("pr")
    green.add_argument("minutes", type=int)
    sub.add_parser("finish-task").add_argument("pr")
    sub.add_parser("remove-worktree").add_argument("wt")
    args = parser.parse_args(argv)
    env = session.resolve()
    if args.command == "remove-worktree":
        return 0 if remove_worktree(str(env.dirs.clone), args.wt) else 1
    merger = Merger(env, telemetry.from_env(env, issue=env.runtime.issue, pr=args.pr), args.pr, env.runtime.issue)
    if args.command == "ci-green":
        return 0 if merger.ci_green(args.minutes) else 1
    return 0 if merger.finish_merged_task() else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"merge: {exc}", file=sys.stderr)
        sys.exit(1)
