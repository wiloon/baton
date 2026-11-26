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

from . import advisor, after_merge, check_verdicts, checkpoint, checks, ci, design_findings, fix, hooks as telemetry, names, notify, pick, pr as pr_helpers, pr_contract, statefile
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
        baseline = "\n\n".join(context)
        ci_log = ""
        if self.env.repo.advisor_ci_log_excerpts and status in ("failed", "pending"):
            ci_log = self.failed_check_log(data.get("statusCheckRollup"))
        snapshot = advisor.augment_snapshot(self.env, baseline, ci_log_excerpt=ci_log)
        return {"snapshot": snapshot, "observed_actions": ("retry_step",) if retry else ()}

    def failed_check_log(self, rollup, chars=3000):
        """The bounded tail of the failed check run's log for the observed head; "" when none (#125).

        The runs come from the head's own check rollup, so the log belongs to that head."""
        for check in rollup if isinstance(rollup, list) else ():
            if not isinstance(check, dict) or str(check.get("conclusion") or "").upper() in (
                    "", "SUCCESS", "NEUTRAL", "SKIPPED"):
                continue
            found = re.search(r"/actions/runs/(\d+)", str(check.get("detailsUrl") or ""))
            if not found:
                continue
            result = pr_helpers.gh("run", "view", found.group(1), "-R", self.repo, "--log-failed")
            if result.returncode == 0 and result.stdout.strip():
                return f"{check.get('name') or 'check'} (run {found.group(1)}):\n{result.stdout[-chars:]}"
        return ""

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
        self.hooks.task("ci_fix", f"pr:{pr}:{head}", "--agent", runner.last_fixer or fixer, "--head-sha", head)
        return 0

    # --- the task behind the PR ---
    def finish_merged_task(self):
        """Idempotent recovery for a partial slice, including owner merges: keep an open issue
        runnable without reusing a merged branch or worktree next time. False: retry later."""
        pr, state_dir = self.pr, self.state
        advisor.finish_episode(self.env, "pr", pr, "merged")
        if Path(state_dir, f"task-merged-pr-{pr}").exists():
            return True
        data = pr_helpers.view(pr, "headRefName,body")
        if data is None:
            return False
        try:
            issue, completion = pr_contract.relation(data)
        except ValueError:
            return True
        head_branch = data.get("headRefName") or ""
        if not re.fullmatch(r"\d+", issue):
            return True
        history = pr_helpers.issue_history(self.repo, issue)
        if history is None:
            return False
        newer = [item["number"] for item in history if item["number"] > int(pr)]
        if newer:
            notify.log(f"PR #{pr}: superseded by PR #{max(newer)}; task left alone")
            Path(state_dir, f"task-merged-pr-{pr}").touch()
            return True
        gh = lambda *args: pr_helpers.gh(*args, "-R", self.repo)
        state = gh("issue", "view", issue, "--json", "state", "--jq", ".state")
        if state.returncode != 0:
            return False
        if completion == "partial" and state.stdout.strip() == "OPEN":
            if any((item.get("state") or "").lower() == "open" and item["number"] != int(pr) for item in history):
                return True   # An open PR on the issue's agent branches still owns the task.
            if names.issue_of(head_branch):
                wt = names.worktree_for(str(self.env.dirs.worktrees), head_branch)
            else:
                _, wt = names.for_issue(str(self.env.dirs.worktrees), issue)
            if wt.is_dir():
                if git(wt, "status", "--porcelain") or session.run_process(
                        ["git", "-C", str(wt), "merge-base", "--is-ancestor", "HEAD", "origin/main"]).returncode != 0:
                    notify.log(f"PR #{pr}: retained unmerged or dirty worktree {wt}")
                    return False
                if not remove_worktree(str(self.env.dirs.clone), wt):
                    return False
            if gh("issue", "edit", issue, "--remove-label", "agent-wip", "--add-label", "ready-for-agent").returncode != 0:
                return False
            # This slice is done: close its dev grants with their outcome so the next slice
            # starts clean (the issue episode stays open until the issue is, but its grants do not).
            advisor.finish_episode(self.env, "issue", issue, "merged")
            Path(state_dir, f"last-slice-{issue}").write_text(f"{pr}\n")
            Path(state_dir, "chain-next").touch()   # this repository's: only its lock holder clears it
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
        if screenshot.get("design_unusable"):
            return None
        verdicts = check_verdicts.parse(screenshot.get("comment") or "", len(listed))
        if verdicts is None:
            return None
        # A check an open design finding blocks is never verified, whatever its line says (#96).
        held = design_findings.blocked(screenshot.get("design") or ())
        return [item for item, verdict in zip(listed, verdicts) if verdict != "verified" or item in held]

    def orphaned_design(self, screenshot, body):
        """The open design findings whose check no current check carries (the checks changed after
        the review that raised them, or the section is now none): the owner decides."""
        listed = checks.items(body or "")
        return [item for item in (screenshot or {}).get("design") or ()
                if any(check not in listed for check in item["checks"])]

    def owed(self, left, body):
        """What a hand-off shows the owner: the checks left after a usable screenshot review,
        else every current check (of every checks section when the body is not well-formed)."""
        return left if left is not None else checks.recovery_items(body or "")

    def left_text(self, left, owed, design=()):
        """The hand-off list; a check an open design finding blocks carries the finding's text."""
        if not owed:
            return ""
        lead = "The screenshot review left these checks for you" if left is not None else "Checks for you"

        def line(item):
            found = [f for f in design if item in f["checks"]]
            return f"\n- {item}" + "".join(f" — design finding {f['text']}{design_findings.answer_text(f)}"
                                            + (f" — owner — design disagreement: reviewer: {f['disagree']['reviewer']}; "
                                               f"fixer: {f['disagree']['fixer']}" if f.get("disagree") else "")
                                            for f in found)
        return f"{lead}:" + "".join(line(item) for item in owed)

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

    # --- the ADR branch update: pinned, verified and bounded (#151, section 6) ---
    UPDATE_WINDOW = 60       # seconds, wall clock, from the save that precedes the request
    REQUEST_WINDOW = 30      # the request and any repeat get only the first half
    update_count = 0
    validated_head = ""
    update_reason = ""

    def update_file(self):
        return Path(self.state, f"adr-update-pr-{self.pr}.json")

    def has_update(self):
        path = self.update_file()
        return path.exists() or path.is_symlink()

    def save_update(self, record):
        path = self.update_file()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record) + "\n")
        os.replace(tmp, path)

    def clear_update(self):
        statefile.unlink_unless_directory(self.update_file())   # a damaged directory stays, contents and all

    def load_update(self, accepted):
        """None when no update is saved; the record when whole and in range; False when the file
        is not a regular file, unreadable, malformed or out of range (fail closed)."""
        path = self.update_file()
        if not self.has_update():
            return None
        try:
            if path.is_symlink() or not path.is_file():
                return False
            record = json.loads(path.read_text())
            if (not isinstance(record, dict) or not isinstance(record.get("head"), str)
                    or not re.fullmatch(r"[0-9a-f]{40}", record["head"])
                    or not accepted or record.get("accepted", record["head"]) != accepted
                    or type(record.get("count")) is not int):
                return False
            if set(record) == {"accepted", "head", "count"}:
                return record if 1 <= record["count"] <= 3 and record["head"] != accepted else False
            fields = {"head", "base", "count", "deadline", "response"}
            if (set(record) - {"accepted"} != fields
                    or not isinstance(record["base"], str) or not record["base"]
                    or not 0 <= record["count"] <= 2
                    or (record["count"] == 0 and record["head"] != accepted)
                    or type(record["deadline"]) not in (int, float)
                    or record["response"] not in (None, "accepted", "inconclusive")
                    or not 0 <= record["deadline"] <= time.time() + self.UPDATE_WINDOW):
                return False
            return record
        except (OSError, ValueError):
            return False

    def bounded(self, deadline, command):
        """COMMAND under the time left until DEADLINE, killed then; None when none is left."""
        budget = pr_helpers.deadline_budget(deadline)
        if not budget:
            return None
        done = session.run_process(["timeout", "-s", "KILL", str(budget), *command],
                                   capture_output=True, text=True, check=False)
        return done if time.time() < deadline else None

    def verify_updated_head(self, saved, new, base, deadline):
        """True when NEW is a merge commit of SAVED and a head already on BASE, with the tree of a
        clean merge of the two. A call that fails, or cannot start in time, is False."""
        clone = ["git", "-C", str(self.env.dirs.clone)]

        def run(*args):
            done = self.bounded(deadline, [*clone, *args])
            return done if done is not None and done.returncode == 0 else None

        if not run("fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}",
                   f"+refs/pull/{self.pr}/head:refs/remotes/origin/baton-pr-{self.pr}"):
            return False
        parents = run("rev-list", "--parents", "-n", "1", new)
        fields = parents.stdout.split() if parents else []
        if len(fields) != 3 or fields[0] != new or fields[1] != saved:
            return False
        if not run("merge-base", "--is-ancestor", fields[2], f"refs/remotes/origin/{base}"):
            return False
        merged, tree = run("merge-tree", "--write-tree", saved, fields[2]), run("rev-parse", f"{new}^{{tree}}")
        return bool(merged and tree and merged.stdout.split()[:1] == tree.stdout.split()[:1] != [])

    def read_head(self, deadline):
        """The PR's head within DEADLINE; '' when unreadable. One call, no inner repeat."""
        done = pr_helpers.gh("pr", "view", str(self.pr), "-R", self.repo, "--json", "headRefOid",
                             until=deadline, repeat=lambda result: False)
        if time.time() >= deadline or done.returncode != 0:
            return ""
        try:
            head = json.loads(done.stdout).get("headRefOid")
            return head if isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head) else ""
        except (ValueError, AttributeError):
            return ""

    def settle_update(self, record):
        """Poll for the head the saved update produces until its one deadline. A new head is
        returned only after it is verified; None, with `update_reason` saying why."""
        saved, deadline = record["head"], record["deadline"]
        self.update_reason = "update_deadline"
        while deadline - time.time() > 0:
            sha = self.read_head(deadline)
            if sha and sha != saved:
                verified = self.verify_updated_head(saved, sha, record["base"], deadline)
                if time.time() >= deadline:
                    return None
                if verified:
                    return sha
                self.update_reason = "update_unverified"
                return None
            time.sleep(max(0, min(2, deadline - time.time())))
        return None

    def finish_update(self, record):
        new = self.settle_update(record)
        if new:
            return self.confirm_update(record, new)
        return self.update_failure(self.update_reason)

    def update_failure(self, reason):
        return {"ok": False, "reason": reason, "detail": {
            "update_deadline": " the branch update was not confirmed before its deadline",
            "update_unverified": " the updated head is not a clean merge of the saved head and the base",
        }[reason]}

    def confirm_update(self, record, head):
        # Atomically replace the in-flight phase before entering CI or attempting the merge.
        self.save_update({"accepted": record.get("accepted", record["head"]),
                          "head": head, "count": record["count"] + 1})
        return head

    def pinned_update(self, accepted, saved, base, count):
        """One conditional update from SAVED: the verified new head, or a failure dict."""
        record = {"accepted": accepted, "head": saved, "base": base, "count": count,
                  "deadline": time.time() + self.UPDATE_WINDOW, "response": None}
        self.save_update(record)

        def refused(result):
            status = re.search(r"\bHTTP ([1-5]\d\d)\b", result.stderr or "")
            return result.returncode != 0 and status is not None and status[1].startswith("4")

        def secondary(result):
            return bool(refused(result) and re.search(r"secondary rate limit", result.stderr, re.IGNORECASE))

        sent = pr_helpers.gh("api", "-X", "PUT", f"repos/{self.repo}/pulls/{self.pr}/update-branch",
                             "-f", f"expected_head_sha={saved}", repeat=secondary,
                             until=record["deadline"] - self.UPDATE_WINDOW + self.REQUEST_WINDOW)
        if refused(sent):
            # Refused: this request was not accepted. It counts as landed only if the head already passes.
            sha = self.read_head(record["deadline"])
            verified = sha and sha != saved and self.verify_updated_head(saved, sha, base, record["deadline"])
            if time.time() >= record["deadline"]:
                return self.update_failure("update_deadline")
            if verified:
                return self.confirm_update(record, sha)
            return {"ok": False, "reason": "update_failed", "detail": pr_helpers.why(sent)}
        record["response"] = "accepted" if sent.returncode == 0 else "inconclusive"
        self.save_update(record)
        return self.finish_update(record)

    def settle_pending_update(self, accepted):
        """A saved update from an earlier run: None when there is none, else the verified head or a
        failure dict. The deadline is checked before any read of the PR."""
        record = self.load_update(accepted)
        if record is None:
            return None
        if record is False:
            return {"ok": False, "reason": "update_state_invalid", "detail": " the saved branch update is damaged"}
        self.update_count = record["count"]
        if "deadline" not in record:
            return record["head"]
        head = self.finish_update(record)
        if isinstance(head, str):
            self.update_count += 1
        return head

    def merge_with_updates(self, minutes, *, before_merge=None, pinned=None):
        """Merge the PR, updating a behind branch at most three times (#122).

        BEFORE_MERGE: optional callable() -> gate-style dict. Called after a confirmed
        update and its CI wait, before each merge attempt. A result whose action is not
        "merge" stops further attempts.

        PINNED: the originally accepted head for an ADR PR (#151, section 6). The update is then the conditional,
        verified and bounded API call, and every merge carries --match-head-commit with a
        validated head.

        Returns {"ok": True} or {"ok": False, "reason": ..., "detail": ..., "gate": ...}.
        """
        pr, repo = str(self.pr), self.repo
        updates, match_sha = self.update_count, self.approved_head or None
        validated = self.validated_head or pinned
        if pinned is not None:
            if not validated:
                return {"ok": False, "reason": "sha_unavailable", "detail": " the accepted head is unavailable"}
            match_sha = validated
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
                if pinned is not None:
                    base = (pr_helpers.view(pr, "baseRefName") or {}).get("baseRefName") or ""
                    if not validated or not base:
                        return {"ok": False, "reason": "sha_unavailable",
                                "detail": " the validated head or the base branch is unavailable"}
                    new_sha = self.pinned_update(pinned, validated, base, updates)
                    if isinstance(new_sha, dict):
                        return new_sha
                    validated = self.validated_head = new_sha
                else:
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
                if pinned is not None:
                    self.merge_head = match_sha
                return {"ok": True}
            if pinned is not None and pr_helpers.transient(merged):
                # a lost answer on the pinned ADR path goes to the saved retry budget, not a re-poll
                return {"ok": False, "reason": "merge_failed", "detail": pr_helpers.why(merged), "transient": True}
            data = self.pr_merge_state()   # carried into the next iteration, never re-read
            if (data or {}).get("mergeStateStatus") != "BEHIND":
                return {"ok": False, "reason": "merge_failed", "detail": pr_helpers.why(merged),
                        "transient": pr_helpers.transient(merged)}
            match_sha = (validated or None) if pinned is not None else self.approved_head or None

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
        the checks section is none or the screenshot review verified every check (SCREENSHOT:
        the reviewed checks and the round's screenshot review comment, #92); otherwise label it
        ready-for-human."""
        pr, url = self.pr, self.url
        if self.env.config.scheduling.auto_merge is not True or self.env.repo.auto_merge is not True:
            where = "" if self.env.config.scheduling.auto_merge is not True else f" for {self.repo}"
            body = pr_helpers.body(pr)
            left = self.left_for_owner(screenshot, body)
            owed = self.owed(left, body)
            listed = self.left_text(left, owed, (screenshot or {}).get("design") or ())
            self.handoff("auto_merge_off", f"PR #{pr} review passed",
                         f"Auto-merge is off{where}; ready for you." + (f" {listed}\n{url}" if listed else f" {url}"))
            if body is None or not pr_helpers.checks_none(body):
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
            orphans = self.orphaned_design(screenshot, body)
            if orphans:
                self.handoff("manual_checks", f"PR #{pr} needs a manual check",
                             "Review and CI passed, but open design findings no longer match the PR's checks:"
                             + "".join(f"\n- {item['id']}: {item['text']}" for item in orphans) + f"\n{url}")
                return
            left = self.left_for_owner(screenshot, body)
            gate = pr_helpers.gate(pr, cleared=screenshot["checks"] if left == [] else None,
                                   carried=(screenshot or {}).get("design"))
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
            listed = self.left_text(left, owed, (screenshot or {}).get("design") or ())
            listed += "".join(f"\n- open design finding {item['id']}: {item['text']}"
                              for item in self.orphaned_design(screenshot, live_body))
            self.handoff("manual_checks", f"PR #{pr} needs a manual check",
                         "Review and CI passed." + (f" {listed}\n{url}" if listed else f" {url}"))
            notify.send_shots(pr, note="Checks for you" if left is None else "", left=owed)
            return

        def recheck():
            """After a branch update, re-run checks clearance and metadata preflight (ADR 0006)."""
            nonlocal body, left
            try:
                body = pr_helpers.body(pr)
                left = self.left_for_owner(screenshot, body)
                return pr_helpers.gate(pr, cleared=screenshot["checks"] if left == [] else None,
                                       carried=(screenshot or {}).get("design"))
            except Exception:
                return {"action": "manual_checks"}

        result = self.merge_with_updates(45, before_merge=recheck)
        if not result.get("ok"):
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
                listed = self.left_text(left, owed, (screenshot or {}).get("design") or ())
                self.handoff("manual_checks", f"PR #{pr} needs a manual check",
                             "Review and CI passed." + (f" {listed}\n{url}" if listed else f" {url}"))
                notify.send_shots(pr, note="Checks for you" if left is None else "", left=owed)
                return
            detail = result.get("detail") or ""
            self.handoff("merge_failed", f"PR #{pr} merge failed", f"{url}{detail}",
                         **self.observation("merge_failed", failure=reason))
            return
        self.hooks.task("merged", "merged")
        notify.notify(f"PR #{pr} merged", url)
        # A partial PR keeps its issue open; release it for the next slice below.
        if remove_worktree(str(self.env.dirs.clone), wt):
            notify.log(f"cleaned up worktree {wt}")
        if left == []:   # merged on the screenshot review: the owner can still look
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
