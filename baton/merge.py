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
import os
from pathlib import Path
import re
import subprocess
import sys

from . import after_merge, ci, fix, hooks as telemetry, names, notify, pick, pr as pr_helpers
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
            result = session.run_process(["timeout", "15", "gh", "pr", "view", str(self.pr), "-R", self.repo,
                                     "--json", "headRefOid", "--jq", ".headRefOid"], text=True, capture_output=True)
            before = result.stdout.strip() if result.returncode == 0 else ""
        result = ci.green(str(self.pr), minutes)
        self.hooks.ci(self.pr, result["rc"], before, result["no_checks"])
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
        except Exception as exc:
            notify.log(f"PR #{self.pr}: after-merge failed: {exc}")
            notify.log(f"PR #{self.pr}: after-merge filing failed")

    # --- the review passed ---
    def merge_or_handoff(self, wt):
        """Merge when AUTO_MERGE is on, CI is green and "## Manual checks" is none; otherwise
        label it ready-for-human."""
        pr, url = self.pr, self.url
        if self.env.config.scheduling.auto_merge is not True:
            self.handoff("auto_merge_off", f"PR #{pr} review passed", f"Auto-merge is off; ready for you. {url}")
            body = pr_helpers.body(pr)
            if body is None or not pr_helpers.checks_none(body):
                notify.send_shots(pr)
            return
        if not self.ci_green(45):
            if self.ci_fix_round(wt) != 0 or not self.ci_green(45):
                self.handoff("ci_not_green", f"PR #{pr}: CI not green", f"Review passed but CI failed or timed out. {url}")
                return
        try:
            gate = pr_helpers.gate(pr)
        except Exception:
            gate = {"action": "manual_checks"}
        if gate.get("action") == "queue_review":
            notify.log(f"PR #{pr}: merge preflight blocked: {gate['reason']}")
            self.label("--add-label", "agent-review")
            notify.notify(f"PR #{pr}: metadata review queued", f"{gate['reason']} {url}")
            return
        if gate.get("action") != "merge":   # manual checks, or no answer: the owner decides
            self.handoff("manual_checks", f"PR #{pr} needs a manual check", f"Review and CI passed. {url}")
            notify.send_shots(pr)
            return
        if pr_helpers.gh("pr", "merge", str(pr), "-R", self.repo, "--merge").returncode != 0:
            self.handoff("merge_failed", f"PR #{pr} merge failed", url)
            return
        self.hooks.task("merged", "merged")
        notify.notify(f"PR #{pr} merged", url)
        # A partial PR keeps its issue open; release it for the next slice below.
        if remove_worktree(str(self.env.dirs.clone), wt):
            notify.log(f"cleaned up worktree {wt}")
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
