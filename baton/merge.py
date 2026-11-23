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
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from . import advisor, after_merge, check_verdicts, checkpoint, checks, ci, fix, hooks as telemetry, names, notify, pick, pr as pr_helpers
from .common import Deferred, git


def remove_worktree(repo_dir, wt):
    """Drop the worktree and its local branch. The remote branch is deleted by GitHub on
    merge (delete_branch_on_merge)."""
    branch = git(wt, "branch", "--show-current") or ""
    if session.run_process(["git", "-C", repo_dir, "worktree", "remove", "--force", str(wt)]).returncode != 0 or not branch:
        return False
    return session.run_process(["git", "-C", repo_dir, "branch", "-D", branch], stdout=subprocess.DEVNULL).returncode == 0


HANDED_OFF = 2


def checks_status(rollup):
    """Classify GitHub's check runs and commit status contexts for the observed head."""
    if not isinstance(rollup, list) or not rollup or any(not isinstance(check, dict) for check in rollup):
        return "unavailable"
    states = [(str(check.get("status") or check.get("state") or "").upper(),
               str(check.get("conclusion") or "").upper()) for check in rollup]
    pending = {"PENDING", "QUEUED", "IN_PROGRESS", "REQUESTED", "WAITING", "EXPECTED"}
    settled = {"SUCCESS", "NEUTRAL", "SKIPPED"}
    if any((conclusion and conclusion not in settled) or state in {"ERROR", "FAILURE"}
           for state, conclusion in states):
        return "failed"
    if any(state not in pending | settled and (state != "COMPLETED" or not conclusion)
           for state, conclusion in states):
        return "unavailable"
    return "pending" if any(state in pending for state, _ in states) else "settled"


class Merger:
    @session.scoped
    def __init__(self, env, hooks, pr, issue=""):
        env = session.resolve(env)
        self.env, self.hooks, self.pr, self.issue = env.with_runtime(issue=issue), hooks, pr, issue
        self.state, self.ctx, self.repo = str(env.dirs.state), str(env.dirs.ctx), env.repo.gh_repo
        self.url = f"https://github.com/{self.repo}/pull/{pr}"
        self.last_ci = ("", 0, 0)
        self.merge_head = ""
        self.approved_head = ""

    def label(self, *options):
        pr_helpers.gh("pr", "edit", str(self.pr), "-R", self.repo, *options)

    def handoff(self, reason, title, text, **observation):
        from . import handoff as owner_handoff
        owner_handoff.perform(
            self.env, reason, kind="pr", number=self.pr,
            title=title, text=text, hooks=self.hooks,
            item=self.pr, episode=self.issue or self.pr, **observation)

    def observation(self, reason, *, ci_failed=False, acceptance="", failure=""):
        """Read the current PR for an observation point. An offer never authorizes a write."""
        if not advisor.enabled(self.env):
            return {}
        try:
            data = pr_helpers.view(self.pr, "state,headRefOid,baseRefName,mergeStateStatus,statusCheckRollup,"
                                    "title,body,additions,deletions,changedFiles,comments") or {}
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        state = data.get("state")
        head = data.get("headRefOid")
        status = checks_status(data.get("statusCheckRollup"))
        context = [
            f"Decision point: {reason}. Repository {self.repo}, issue {self.issue}, PR {self.pr}.",
            f"PR state: {state}; current head: {head}; base: {data.get('baseRefName')}.",
            f"Merge status: {data.get('mergeStateStatus')}; CI status: {status}.",
        ]
        if ci_failed:
            checked_head, rc, minutes = self.last_ci
            same_head = bool(checked_head and checked_head == head
                             and (not self.merge_head or checked_head == self.merge_head))
            retry = same_head and state == "OPEN" and (
                status == "pending" or (rc == 124 and status != "failed"))
            context.append(f"CI wait head: {checked_head}; exit: {rc}; poll budget: {minutes} minutes.")
        else:
            # A reconciliation read is safe for a merged or ambiguous PR as well. An ordinary
            # merge could only resume at the same eligible head, after rerunning all gates.
            retry = state != "CLOSED"
            context.append(f"Previous merge head: {self.merge_head}; step outcome: {failure or reason}.")
        context.append(f"Diff size: {data.get('changedFiles')} files, +{data.get('additions')}"
                       f"/-{data.get('deletions')}.")
        comments = data.get("comments")
        comments = comments if isinstance(comments, list) else []
        for prefix in ("<!-- baton-review ", "<!-- baton-screenshot-review ", "<!-- baton-fix "):
            feedback = next((c["body"] for c in reversed(comments)
                             if isinstance(c, dict) and isinstance(c.get("author"), dict)
                             and c["author"].get("login") == self.env.owner
                             and isinstance(c.get("body"), str) and c["body"].startswith(prefix)), "")
            if feedback:
                context.append(feedback)
        if acceptance:
            context.append(acceptance)
        context.append(f"PR title: {data.get('title') or ''}\n{data.get('body') or ''}")
        return {"snapshot": "\n\n".join(context), "observed_actions": ("retry_step",) if retry else ()}

    # --- CI ---
    def ci_green(self, minutes):
        """CI passed, or the PR triggers no checks (docs-only paths); baton.ci. The result
        goes to the optional CI metadata hook."""
        before = ""
        if self.hooks.enabled or advisor.enabled(self.env):
            result = pr_helpers.gh("pr", "view", str(self.pr), "-R", self.repo, "--json", "headRefOid", "--jq", ".headRefOid",
                                   limit=15)
            before = result.stdout.strip() if result.returncode == 0 else ""
        started = time.monotonic()
        result = ci.green(str(self.pr), minutes)
        self.last_ci = (before if re.fullmatch(r"[a-f0-9]{40,64}", before) else "", result["rc"], minutes)
        self.hooks.ci(self.pr, result["rc"], before, result["no_checks"],
                      duration_ms=int((time.monotonic() - started) * 1000))
        return result["green"]

    def ci_fix_round(self, wt):
        """One repair for a failed code-check head (#243). baton.ci decides: infrastructure
        and ambiguous failures, and a head repaired once already, are left for the owner.
        Returns 0 when a repair was pushed, HANDED_OFF when the repair run already handed the PR to
        the owner, and 1 when no repair is possible. A repair that cannot run now hands the head off."""
        pr = self.pr
        runner = fix.Runner(self.env, self.hooks, pr, "ci")
        waiting = runner.waiting_push(pr)   # before any CI read or vendor choice (#151)
        if waiting == "unreadable":
            return HANDED_OFF if runner.handed_off else 1
        if waiting is not None:
            binding = waiting["continuation"]
            if binding["caller"] != "ci":
                runner.handoff(pr, "fix_push_failed", f"PR #{pr}: pushing fix failed",
                               "The saved push does not belong to a CI repair.")
                return HANDED_OFF
            head = binding["ci_head"]
        else:
            found = ci.classify(str(pr), self.state, list(self.env.repo.ci_fixable_checks))
            if found is None or not re.fullmatch(r"[a-f0-9]{40}", found["head"]):
                return 1
            head = found["head"]
            if self.approved_head and head != self.approved_head:
                return 1
        runner.ci_failed_head = head
        try:
            if waiting is not None:
                # Resume the saved push without picking another fixer (#151).
                notify.log(f"PR #{pr}: resuming the CI repair push for {head}")
                fixer = waiting["continuation"].get("fixer") or "agent"
                rc = fix.fix_pr(runner, pr, wt, fixer, "CI code checks", "")
            else:
                fixer = pick.choose(self.env, "fixer", "agent", "contrib", pick.all_vendors(self.env),
                                    pr=str(pr), issue=self.issue)
                notify.log(f"PR #{pr}: code-only CI failure on {head}; one repair round by {fixer}")
                rc = fix.fix_pr(runner, pr, wt, fixer, "CI code checks", found["feedback"])
        except Deferred as deferred:
            # Category-1 push waits keep the repair; a vendor deferral still hands the head off (#151).
            if deferred.vendor == "handoff-retry":
                raise
            rc = 75
        if rc == 75:
            notify.log(f"PR #{pr}: CI repair deferred; handing off this head")
            return 1
        if rc == fix.PUSH_SUPERSEDED:
            return HANDED_OFF
        Path(self.state, f"ci-fix-{pr}-{head}").touch()   # one repair round for this PR head, even across ticks
        if rc != 0:
            # Only a real handoff by the repair run ends here; any other failure (e.g. recording
            # authorship) falls through to the merge gate's own CI handoff.
            return HANDED_OFF if runner.handed_off else 1
        if completed := runner.waiting_push(pr):
            if completed == "unreadable":
                return HANDED_OFF
            repaired_head = completed["continuation"]["commit"]
            if self.approved_head:
                path = Path(self.state, f"review-pending-{pr}")
                saved = checkpoint.read(path)
                checkpoint.write(path, {**saved, "merge_head": repaired_head})
                self.approved_head = repaired_head
            runner.acknowledge_push(pr)
        self.hooks.task("ci_fix", f"pr:{pr}:{head}", "--agent", fixer, "--head-sha", head)
        return 0

    # --- the task behind the PR ---
    def finish_merged_task(self):
        """Idempotent recovery for a partial slice, including owner merges: keep an open issue
        runnable without reusing a merged branch or worktree next time. False: retry later."""
        pr, state_dir = self.pr, self.state
        advisor.finish_episode(self.env, "pr", pr, "merged")
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
        updates, match_sha = 0, self.approved_head or None
        data = self.pr_merge_state()
        while True:
            self.merge_head = (data or {}).get("headRefOid") or ""
            if self.approved_head and self.merge_head != self.approved_head:
                return {"ok": False, "reason": "head_changed", "detail": " The approved head changed."}
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
                if self.approved_head:
                    self.approved_head = new_sha
                self.merge_head = new_sha
                if not self.ci_green(minutes):
                    return {"ok": False, "reason": "ci_after_update_not_green", "detail": ""}
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
            match_sha = self.approved_head or None

    # --- owner-review paths (#131) ---
    def owner_paths_touched(self):
        """The PR's paths (old and new names of a rename) that match the repository's
        owner_review_paths; [] when it sets none or the PR touches none; None when the file
        list can't be read."""
        globs = self.env.repo.owner_review_paths
        if not globs:
            return []
        listed = pr_helpers.gh("api", "--paginate", f"repos/{self.repo}/pulls/{self.pr}/files",
                               "--jq", '.[] | .filename, (.previous_filename // empty)')
        if listed.returncode != 0:
            return None
        paths = sorted({line.strip() for line in listed.stdout.splitlines() if line.strip()})
        return [p for p in paths if any(fnmatchcase(p, g) for g in globs)]

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
        waiting = fix.Runner(self.env, self.hooks, pr, "ci").waiting_push(pr)
        if waiting == "unreadable":
            return
        if waiting or not self.ci_green(45):
            repaired = self.ci_fix_round(wt)
            if repaired == HANDED_OFF:
                return
            if repaired != 0 or not self.ci_green(45):
                reason = "ci_before_merge_not_green"
                self.handoff(reason, f"PR #{pr}: CI not green",
                             f"Review passed but CI failed or timed out. {url}",
                             **self.observation(reason, ci_failed=True))
                return
        touched = self.owner_paths_touched()
        if touched != []:
            # Reserved for the owner: never an advisor decision point (#131, #125).
            what = ("the changed files could not be read" if touched is None
                    else "it touches owner-review paths: " + ", ".join(touched))
            self.handoff("owner_review_paths", f"PR #{pr} review and CI passed",
                         f"Not auto-merged: {what}. Ready for you. {url}")
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
            if reason == "ci_after_update_not_green":
                self.handoff("ci_after_update_not_green", f"PR #{pr}: CI not green",
                             f"Review passed but CI failed or timed out after updating the branch. {url}",
                             **self.observation(reason, ci_failed=True))
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
            self.handoff("merge_failed", f"PR #{pr} merge failed", f"{url}{detail}",
                         **self.observation("merge_failed", failure=reason))
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
