"""Fix runs for the bash runner (migration slice 4b, #275): `fix_pr`, `cursor_fix` and
`verify_with_fixes` of run.sh.

A fixer addresses feedback on a PR in its worktree; the runner posts the reply, applies the
description and task requests, verifies the commits (with repair runs), records who wrote
what, pushes, and refreshes the checks. Every gate that stops hands the PR to the
owner the same way: label ready-for-human, a telemetry handoff, a Telegram message.

Prints JSON for the bash caller, which keeps `defer_vendor` (it exits the item's subshell):
    rc            0 done, 1 handed to the owner or failed, 75 deferred
    defer_vendor  with `defer`: the vendor to rest and why; empty unless rc is 75
    failure, rounds   (verify) the last check's output and the repair runs made
"""

from . import session
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from . import agents, design_findings, dispatch, e2e_gate, hooks as telemetry, notify, pick, pr as pr_helpers, pr_contract, prompts, recovery, tiers, vendors
from .common import Deferred, Exhausted, child_env, git, push, strip_markers

REPLY_CAP = 60000
PUSH_SUPERSEDED = 80
UNCONFIRMED_DESCRIPTION = "The runner could not confirm whether the description edit landed"
# The verify output a repair run gets in its prompt: the tail, where the errors are. The whole
# output stays in the failure file (#38: a Claude agent could not read the ctx dir with the shell).
FAILURE_PROMPT_CAP = 20000


def failure_excerpt(failure):
    if len(failure) <= FAILURE_PROMPT_CAP:
        return failure
    return f"[... first {len(failure) - FAILURE_PROMPT_CAP} characters cut; see the failure file ...]\n" + failure[-FAILURE_PROMPT_CAP:]


class Runner:
    # Design findings (#96): the review loop sets these on the runner it hands to fix_pr.
    loop_id = ""        # the review loop's id, carried by every fix comment it posts
    design_ids = ()     # the open design findings the fixer must answer
    design_only = False  # no Must fix in the feedback: only design findings
    design_reply = ()   # the reply's `## Design findings` lines, read by fix_pr

    @session.scoped
    def __init__(self, env=None, hooks=None, pr=0, round=""):
        env = session.resolve(env)
        env = tiers.carry(env, "fix", env.runtime.issue, pr or env.runtime.pr)
        self.env, self.round = env, round
        self.state, self.ctx = str(env.dirs.state), str(env.dirs.ctx)
        self.repo, self.self_dir = env.repo.gh_repo, Path(str(env.config.paths.self_dir))
        self.max_verify = int(env.config.scheduling.max_verify_fixes or 2)
        self.pr, self.reply, self.gate_result = pr, "", ""
        self.gate_applies = True   # dev.run clears it for a partial slice
        self.handed_off = False    # set once handoff() labels the PR; callers tell it apart from rc 1
        self.push_feedback = None
        self.changed_input = ""
        self.ci_failed_head = ""
        self.push_caller = "ci" if round == "ci" else "review" if round else "standalone"
        self.last_fixer = ""
        self.attempt_authors = []
        self.prior_author = ""   # the earlier reply producer whose commits this attempt carries
        self.retry_context = {}
        self.retry_start = None
        self.required_retry_label = ""
        self.owner_triggered = False   # fix_pr sets it; dispatch_fixer keeps the owner scope on retries
        self.verification_progress = None
        self.hooks = hooks or telemetry.from_env(env, issue=env.runtime.issue, pr=pr, round=round)

    # --- the owner handoff every gate shares ---
    def handoff(self, pr, reason, title, text, *, resume=False, transient=False, continuation=None, **recovery_kw):
        """Label the PR for the owner, record why, say so on Telegram; rc 1 on handoff."""
        from . import handoff as owner_handoff
        episode = self.env.runtime.issue or str(pr)
        result = owner_handoff.perform(
            self.env, reason, kind="pr", number=pr,
            title=title, text=text, hooks=self.hooks, item=str(pr), episode=str(episode),
            resume=resume, transient=transient, continuation=continuation, **recovery_kw)
        if result.action == owner_handoff.DEFER:
            raise Deferred("handoff-retry", reason, lends_slot=False)
        if result.action == owner_handoff.RETRY:
            return result
        if result.handed_off:
            if reason == owner_handoff.FIX_PUSH:
                self.push_flag(pr).unlink(missing_ok=True)
            self.handed_off = True
            recovery.clear(self.env, str(episode), str(pr))
            return 1
        return result

    def retry_binding(self, wt, pr):
        return recovery.worker_fingerprint(wt, pr, round_s=self.round, extra=self.retry_context,
                                           required_label=self.required_retry_label)

    def retry_picker(self, pr):
        return dict(mode="contrib", candidates=pick.all_vendors(self.env), pr=str(pr),
                    issue=self.env.runtime.issue)

    def recover(self, pr, reason, title, detail, vendor, *, wt, repairs_used=0, head="",
                validation_error="", pushed=False, before_labels=None):
        episode = self.env.runtime.issue or str(pr)
        budget = {"verify_repairs": max(0, self.max_verify - repairs_used),
                  "repairs_used": repairs_used, "authors": self.attempt_authors, "head": head,
                  "base_sha": getattr(self, "verified_base", ""),
                  "validation_error": validation_error}
        def binding():
            current = self.retry_binding(wt, pr)
            start = self.retry_start
            if not start or not current:
                return None
            heads = {start["remote_head"]}
            if pushed:
                heads.add(current["local_head"])
            if current["remote_head"] not in heads or any(
                current[key] != start[key] for key in ("base", "head_ref", "feedback")
            ):
                return None
            return current

        result = recovery.request(
            self.env, episode, pr, reason, "fixer", vendor, detail, binding(), binding,
            lambda text=detail, **kw: self.handoff(
                pr, reason, title, text, **({"before_labels": before_labels} if before_labels else {}), **kw),
            budget=budget, local=reason == "fix_verify_failed",
            exclusions=getattr(self, "retry_exclusions", ()), **self.retry_picker(pr))
        return (result[0]["action"], result[1], budget["verify_repairs"]) if result else ("handoff", vendor, 0)

    def resume(self, pr, wt, *, verify_only=False):
        episode = self.env.runtime.issue or pr
        try:
            saved = recovery.load(self.env, episode, pr)
        except recovery.RecoveryUnreadable:
            saved = None
        saved_reason = saved["reason"] if saved and saved["reason"].startswith("fix_") else "fix_run_failed"
        selected = recovery.resume(
            self.env, episode, pr,
            lambda reason: reason == "fix_verify_failed" if verify_only else reason.startswith("fix_"),
            lambda: self.retry_binding(wt, pr),
            lambda text: self.handoff(pr, saved_reason, f"PR #{pr} needs you", text),
            role="fixer", local=lambda record: record["reason"] == "fix_verify_failed", **self.retry_picker(pr))
        if selected:
            self.attempt_authors = list(selected[0]["remaining_budget"].get("authors", ()))
        return selected

    def dispatching(self, pr, wt):
        """Consume a saved grant before the step it allows runs (see recovery.dispatching)."""
        return recovery.dispatching(self.env, self.env.runtime.issue or pr, pr,
                                    lambda: self.retry_binding(wt, pr), role="fixer")

    def verify_retry(self, wt, base, vendor, kind, number, *, repairs, repairs_before, binding=None):
        episode = self.env.runtime.issue or number
        self.verification_progress = recovery.verification_progress(self.env, episode, number, vendor)
        binding = binding or (lambda: self.retry_binding(wt, number))
        try:
            with recovery.dispatching(self.env, episode, number, binding):
                return self.verify(wt, base, vendor, kind, number, repairs=repairs, repairs_before=repairs_before)
        finally:
            self.verification_progress = None

    def pr_head_branch(self, pr):
        """The PR's headRefName from GitHub, or '' when unreadable."""
        try:
            data = pr_helpers.view(pr, "headRefName")
        except ValueError:
            return ""
        branch = data.get("headRefName") if isinstance(data, dict) else ""
        return branch.strip() if isinstance(branch, str) else ""

    def finish_verified(self, pr, wt, fixer, *, prefix="", description_rc=0, description_reason=""):
        url = f"https://github.com/{self.repo}/pull/{pr}"
        reply = agents.ADAPTERS[fixer].FIX_RESUMABLE_VIA_REPLY
        return self.push_with_retry(pr, wt, "HEAD", next_step="done", url=url, who=prefix, fixer=fixer,
                                    description_rc=description_rc, description_reason=description_reason,
                                    clear_cursor=reply)

    def add_author(self, vendor):
        if vendor not in self.attempt_authors:
            self.attempt_authors.append(vendor)

    def push_episode(self, pr):
        return self.env.runtime.issue or str(pr)

    def capture_push_feedback(self, pr):
        from .review import owner_feedback_digest, read_feedback_history
        history = read_feedback_history(self.repo, pr, pr_helpers.gh)
        return owner_feedback_digest(self.env.owner, history) if history is not None else None

    def push_disposition(self, pr, binding):
        """Revalidate the saved operation before touching the remote or running its continuation."""
        def disposition(data):
            if not isinstance(data, dict):
                return "retry"
            if data.get("state") in ("CLOSED", "MERGED"):
                return "superseded"
            labels = data.get("labels")
            if data.get("state") != "OPEN" or not isinstance(labels, list) or any(
                    not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in labels):
                return "retry"
            return "paused" if any(label["name"] == "ready-for-human" for label in labels) else "ok"

        try:
            status = disposition(pr_helpers.view(pr, "state,labels"))
        except ValueError:
            return "retry"
        if status != "ok":
            return status
        if issue := binding["issue"]:
            result = pr_helpers.gh("issue", "view", issue, "-R", self.repo, "--json", "state,labels")
            try:
                data = json.loads(result.stdout) if result.returncode == 0 else None
            except ValueError:
                data = None
            status = disposition(data)
            if status != "ok":
                return status
        feedback = self.capture_push_feedback(pr)
        if feedback is None:
            return "retry"
        return "ok" if feedback == binding["feedback"] else "changed"

    def push_flag(self, pr):
        """Says a fix push is waiting, so a lost record fails closed instead of rerunning the worker."""
        return Path(self.state, f"fix-push-waiting-{pr}")

    def waiting_push(self, pr):
        """A saved fix push for this PR, or None; hands off when the record is damaged or missing."""
        from . import handoff as owner_handoff
        try:
            found = owner_handoff.load_retry(self.env, self.push_episode(pr), owner_handoff.FIX_PUSH, str(pr))
            if found and (found["continuation"]["issue"] or str(pr)) != self.push_episode(pr):
                raise owner_handoff.RetryUnreadable("the saved issue does not match the push operation")
        except owner_handoff.RetryUnreadable as exc:
            self.handoff(pr, owner_handoff.FIX_PUSH, f"PR #{pr}: pushing fix failed",
                         f"Could not read the saved push operation: {exc}")
            return "unreadable"
        if found is None and self.push_flag(pr).exists():
            self.handoff(pr, owner_handoff.FIX_PUSH, f"PR #{pr}: pushing fix failed",
                         "The saved push operation is missing; not running the fixer again.")
            return "unreadable"
        return found

    def finish_push(self, pr, binding, url):
        """Continuation after a landed or freshly successful fix push (#151)."""
        from . import handoff as owner_handoff
        if binding["next"] == "handoff":
            reason = binding["handoff_reason"]
            title = (f"PR #{pr} has questions" if reason == "fix_needs_info"
                     else f"PR #{pr} fix failed ({binding['fixer']})")
            return self.handoff(pr, reason, title, f"{binding['outcome']} {url}")
        who = binding.get("who") or ""
        waiting = self.waiting_meta(pr, owner_handoff.FIX_CHECKS)
        if waiting == "unreadable":
            return 1
        return self.refresh_checks(
            pr, who, url, consume=False, pending=waiting, fixer=binding.get("fixer") or "",
            description_rc=binding.get("description_rc") or 0,
            description_reason=binding.get("description_reason") or "",
            clear_cursor=bool(binding.get("clear_cursor")))

    def acknowledge_push(self, pr):
        """Retire a push after the caller has durably advanced."""
        from . import handoff as owner_handoff
        flag = self.push_flag(pr)
        if flag.exists():
            pr_helpers.checks_file(self.ctx, pr).unlink(missing_ok=True)
            flag.unlink(missing_ok=True)
        owner_handoff.clear_retry(self.env, self.push_episode(pr), owner_handoff.FIX_PUSH, str(pr))

    def continue_push(self, pr, pending, url):
        from . import handoff as owner_handoff
        if pending.get("phase") == "finished":
            return 0
        binding = pending["continuation"]
        branch = binding["branch"]
        session.run_process(
            ["git", "-C", binding["wt"], "fetch", "-q", "origin",
             f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            capture_output=True, text=True)
        if binding["next"] == "recover":
            return self.recover_pushed_fix(pr, binding, url)
        rc = self.finish_push(pr, binding, url)
        if rc == 0:
            owner_handoff.restore_retry(self.env, self.push_episode(pr), owner_handoff.FIX_PUSH, str(pr),
                                        {**pending, "phase": "finished"})
        else:
            self.acknowledge_push(pr)
        return rc

    def push_partial_fix(self, pr, wt, ref, url, outcome, fixer, source, feedback, head):
        if outcome.startswith("RESULT: needs-info"):
            return self.push_with_retry(pr, wt, ref, next_step="handoff", url=url, outcome=outcome,
                                        handoff_reason="fix_needs_info", fixer=fixer)
        start = self.retry_start
        context = {
            "source": source, "feedback": feedback, "head": head, "owner_triggered": self.owner_triggered,
            "authors": self.attempt_authors,
            "start": {key: start[key] for key in ("base", "head_ref", "feedback", "remote_head")} if start else None,
        }
        return self.push_with_retry(pr, wt, ref, next_step="recover", url=url, outcome=outcome,
                                    handoff_reason="fix_run_failed", fixer=fixer, recovery_context=context)

    def recover_pushed_fix(self, pr, binding, url):
        wt, fixer = binding["wt"], binding["fixer"]
        context = binding["recovery"]
        source, feedback, head = context["source"], context["feedback"], context["head"]
        self.last_fixer = fixer
        self.attempt_authors = list(context["authors"])
        self.retry_start = context["start"]
        self.owner_triggered = context["owner_triggered"]
        self.retry_context = {"fix_feedback": recovery.feedback_digest(feedback), "fix_source": source}
        episode = self.push_episode(pr)
        try:
            saved = recovery.load(self.env, episode, pr)
        except recovery.RecoveryUnreadable as error:
            rc = self.handoff(pr, "fix_run_failed", f"PR #{pr} fix failed ({fixer})", str(error))
        else:
            # Selection may have deferred after recovery saved its grant for this push.
            if (saved and saved["role"] == "fixer" and saved["reason"] == "fix_run_failed"
                    and saved["failed_vendor"] == fixer
                    and saved["fingerprint"].get("local_head") == binding["commit"]):
                selected = self.resume(pr, wt)
                rc = 1 if not selected else 0
                if selected:
                    _, fixer = selected
            else:
                action, fixer, _ = self.recover(
                    pr, "fix_run_failed", f"PR #{pr} fix failed ({fixer})", f"{binding['outcome']} {url}",
                    fixer, wt=wt, head=head, pushed=True)
                rc = 1 if action == "handoff" else 0
        self.acknowledge_push(pr)
        if rc:
            return rc
        return self.dispatch_fixer(pr, wt, fixer, source, feedback, head)

    def push_with_retry(self, pr, wt, ref, *, next_step, url, upstream=False, outcome="",
                        handoff_reason="", description_rc=0, description_reason="", who="",
                        fixer="", clear_cursor=False, recovery_context=None, pending=None):
        """Save before pushing, or consume a due attempt before reconciling (#151)."""
        from . import handoff as owner_handoff
        episode = self.push_episode(pr)
        reason = owner_handoff.FIX_PUSH
        title = f"PR #{pr}: pushing fix failed"
        text = f"PR #{pr}: pushing fix failed"
        initial = pending is None
        if not initial:
            binding = pending["continuation"]
            next_step, outcome = binding["next"], binding["outcome"]
        note = f" (worker: {outcome})" if outcome else ""
        unreadable = f"{text}: could not observe the remote ref{note}. {url}"
        if not initial:
            if not pending.get("phase") and int(owner_handoff.clock()) < pending["due"]:
                raise Deferred("handoff-retry", reason, lends_slot=False)
            if binding["caller"] != self.push_caller or binding["wt"] != str(wt):
                return self.handoff(pr, reason, title, f"{text}: the saved caller or worktree changed{note}. {url}")
            disposition = self.push_disposition(pr, binding)
            if disposition == "superseded":
                from . import advisor
                advisor.finish_episode(self.env, "pr", pr, "superseded")
                Path(self.state, f"review-pending-{pr}").unlink(missing_ok=True)
                self.acknowledge_push(pr)
                return PUSH_SUPERSEDED
            if disposition == "paused":
                raise Deferred("handoff-retry", reason, lends_slot=False)
            if disposition == "changed":
                return self.handoff(pr, reason, title, f"{text}: owner feedback changed{note}. {url}")
            if pending.get("phase"):
                if disposition == "ok" and owner_handoff.check_push_landed(binding, allow_detached=True) == "landed":
                    return self.continue_push(pr, pending, url)
                return self.handoff(pr, reason, title, f"{text}: could not confirm the landed push{note}. {url}")
        if initial:
            if self.push_feedback is None:   # a recovery step re-entered past fix_pr's own capture
                self.push_feedback = self.capture_push_feedback(pr)
            branch = self.pr_head_branch(pr)
            if not branch:
                return self.handoff(pr, reason, title, f"{text}: could not read PR head branch{note}. {url}")
            current = git(wt, "branch", "--show-current")
            if current is None:
                return self.handoff(pr, reason, title, f"{text}: local commit or branch missing{note}. {url}")
            if current and current != branch:
                return self.handoff(pr, reason, title, f"{text}: local inputs changed{note}. {url}")
            if self.env.repo.signoff:
                from .common import sign_off
                if not sign_off(wt):
                    return self.handoff(pr, reason, title, f"{text}: sign-off failed{note}. {url}")
            commit = git(wt, "rev-parse", "HEAD")
            if not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
                return self.handoff(pr, reason, title, f"{text}: local commit or branch missing{note}. {url}")
            remote, unreadable_transient = owner_handoff.observe_remote(wt, branch)
            if remote is None and not unreadable_transient:
                return self.handoff(pr, reason, title, unreadable)
            binding = {
                "pr": str(pr), "issue": str(self.env.runtime.issue or ""), "commit": commit,
                "branch": branch, "ref": ref if ref in ("HEAD", branch) else "HEAD", "remote": remote, "upstream": upstream,
                "wt": str(wt), "next": next_step, "outcome": outcome,
                "handoff_reason": handoff_reason, "description_rc": description_rc,
                "description_reason": description_reason or "", "who": who, "fixer": fixer,
                "clear_cursor": clear_cursor, "feedback": self.push_feedback,
                "caller": self.push_caller, "ci_head": self.ci_failed_head,
            }
            if recovery_context is not None:
                binding["recovery"] = recovery_context
            self.push_flag(pr).write_text(str(self.env.runtime.issue or ""))
            pending = owner_handoff.prepare_retry(self.env, episode, reason, str(pr), binding)
            if pending is None:
                return self.handoff(pr, reason, title, f"{text}: the push inputs could not be saved{note}. {url}")
            self.clear_task_write(pr)
            Path(self.state, f"slot-deferred-{pr}").unlink(missing_ok=True)
        elif pending["attempts"] < owner_handoff.MAX_ATTEMPTS:
            # Keep the pre-resume attempts: resume already consumed one on disk (#151).
            result = self.handoff(pr, reason, title, f"{text}{note}. {url}", resume=True)
            if result == 1:
                return 1
            binding = result.continuation
            if disposition != "ok":
                if pending["attempts"] + 1 < owner_handoff.MAX_ATTEMPTS:
                    raise Deferred("handoff-retry", reason, lends_slot=False)
                return self.handoff(pr, reason, title, f"{text}: could not verify the owner inputs{note}. {url}")
            if binding["remote"] is None:
                if not owner_handoff.check_push_local(binding, allow_detached=True):
                    return self.handoff(pr, reason, title, f"{text}: local inputs changed{note}. {url}")
                # Nothing was pushed yet, so what origin holds now is the pre-push state.
                remote, unreadable_transient = owner_handoff.observe_remote(wt, binding["branch"])
                if remote is not None:
                    consumed = owner_handoff.load_retry(self.env, episode, reason, str(pr))
                    binding = owner_handoff.save_push_baseline(self.env, episode, str(pr), consumed, remote)
                    pending = {**pending, "continuation": binding}
                elif not unreadable_transient:
                    return self.handoff(pr, reason, title, unreadable)

        binding = pending["continuation"] if initial else binding
        pushed = None
        if binding["remote"] is None:
            status = "retry"   # origin could not be read yet; retry before any push
        else:
            status = owner_handoff.check_push_landed(binding, allow_detached=True)
        if status == "absent" and (initial or pending["attempts"] < owner_handoff.MAX_ATTEMPTS):
            # Push the saved commit to that destination; never force (#151).
            pushed = push(binding["wt"], f"{binding['commit']}:refs/heads/{binding['branch']}",
                          upstream=binding["upstream"], signoff=False)
            if pushed:
                status = "landed"
            else:
                # A lost response may still have updated the remote: look before retrying.
                status = owner_handoff.check_push_landed(binding, allow_detached=True)
                if status != "landed" and not pushed.transient:
                    detail = pushed.detail or "permanent push failure"
                    return self.handoff(pr, reason, title, f"{text}: {detail}{note}. {url}")
                if status == "absent":
                    status = "retry"
        if status == "landed":
            if not initial and disposition != "ok":
                return self.handoff(pr, reason, title, f"{text}: could not verify the owner inputs{note}. {url}")
            landed = {**owner_handoff.load_retry(self.env, episode, reason, str(pr)), "phase": "landed"}
            owner_handoff.restore_retry(self.env, episode, reason, str(pr), landed)
            return self.continue_push(pr, landed, url)
        if status in ("changed", "failed"):
            detail = "inputs changed or the remote is unreadable"
            return self.handoff(pr, reason, title, f"{text}: {detail}{note}. {url}")
        if status == "retry":
            attempts = pending["attempts"] + (0 if initial else 1)
            if attempts < owner_handoff.MAX_ATTEMPTS:
                now = int(owner_handoff.clock())
                owner_handoff.restore_retry(
                    self.env, episode, reason, str(pr),
                    {**pending, "attempts": attempts, "scheduled": now,
                     "due": now + owner_handoff.DELAYS[attempts]})
                raise Deferred("handoff-retry", reason, lends_slot=False)
        detail = getattr(pushed, "detail", "") or status
        return self.handoff(pr, reason, title, f"{text}: {detail}{note}. {url}")

    def comment(self, pr, body, failed=""):
        posted = pr_helpers.gh("pr", "comment", str(pr), "-R", self.repo, "--body", body)
        if posted.returncode != 0 and failed:
            notify.log(f"{failed}{pr_helpers.why(posted)}")

    def render(self, template, **pairs):
        return prompts.render(self.self_dir / template, list(pairs.items()), env=self.env)

    def agent(self, vendor, role, prompt, tag, wt, output="", env=None):
        """One agent run; Deferred when the vendor cannot run now."""
        extra = {"continues_step": True} if getattr(self, "_continues_step", False) else {}
        out = dispatch.dispatch(vendor, role, prompt, tag, str(wt), str(self.self_dir / "settings.json"), output,
                                env or self.env, self.hooks, **extra)
        if out["rc"] == 75:
            raise Deferred(vendor, out["defer"])
        return out

    # --- verify, with repair runs ---
    def e2e_gate(self, kind, number):
        """The gate hook's arguments the task's issue asks for, "" for none; ValueError when
        they can't be read or are malformed. A PR's issue is in its task marker. A partial slice
        doesn't run it: the gate is about the whole issue, so the slice that completes it does."""
        if not self.gate_applies:
            return ""
        def view(what, target):
            done = pr_helpers.gh(what, "view", str(target), "-R", self.repo, "--json", "body", "--jq", ".body")
            if done.returncode != 0:
                raise ValueError(f"reading {what} {target} failed")
            return done.stdout
        issue = number
        if kind != "issue":
            marker = pr_contract.MARKER.search(view("pr", number))
            if not marker or marker.group(2) == "partial":
                return ""
            issue = marker.group(1)
        return e2e_gate.parse(view("issue", issue), self.env.repo)

    def verify(self, wt, base, vendor, kind, number, *, repairs=None, repairs_before=0):
        """(rc, failure, rounds): rc 0 passed, 1 failed or refused, 75 deferred (Deferred).

        REPAIRS caps how many verification-fix runs may follow a red check. REPAIRS_BEFORE
        counts repairs already consumed earlier in the episode (#125 point 12).
        """
        self.verified_base = git(wt, "rev-parse", base) or base   # a retry verifies the same commit
        try:
            gate = self.e2e_gate(kind, number)
        except ValueError as error:
            failure = f"E2E gate: {error}"
            notify.log(f"{kind} #{number}: {failure}")
            return 1, failure, 0
        failure_file = Path(self.ctx, f"{kind}-{number}-verify-failure.txt")
        # Under /tmp, the one place outside the worktree guard.sh lets a fixer write (#38).
        reply_file = Path("/tmp", f"baton-{self.repo.replace('/', '-')}-{kind}-{number}-verify-fix.md")
        local_repairs, failure = 0, ""
        budget = self.max_verify if repairs is None else int(repairs)
        total_rounds = int(repairs_before)

        def dirty():
            return git(wt, "status", "--porcelain") != ""

        def event(result, started):
            self.hooks.task("verify", f"{kind}:{number}:{git(wt, 'rev-parse', 'HEAD')}:{result}",
                            "--ci-result", result, "--duration-ms", int(time.monotonic() - started) * 1000)
        while True:
            if dirty():
                failure = "Verification refused: uncommitted or untracked files remain."
                notify.log(f"{kind} #{number}: {failure}")
                return 1, failure, total_rounds + local_repairs
            started = time.monotonic()
            repo = self.env.repo
            tag_round = total_rounds + local_repairs
            done = session.run_process([str(self.self_dir / "verify.sh"), str(wt), base], text=True, capture_output=True,
                                  env=child_env(VERIFY_TAG=f"{kind}-{number}-verify-{tag_round}", LOGS=str(self.env.dirs.logs),
                                                RUNNER_LOG=str(self.env.config.paths.logs / "runner.log"), RUNNER_LOG_LABEL=notify.label(self.env),
                                                E2E_GATE=gate, CI_COMMAND=repo.ci_command.replace("{base}", base),
                                                VERIFY_HOOK=str(repo.verify_hook or ""), GATE_HOOK=str(repo.gate_hook or "")))
            failure = (done.stdout + done.stderr).rstrip("\n") if done.returncode else done.stdout.rstrip("\n")
            if done.returncode == 0:
                if not dirty():
                    failure_file.unlink(missing_ok=True)
                    event("pass", started)
                    self.gate_result = next((line for line in done.stdout.splitlines() if line.startswith("E2E gate passed:")), "")
                    if self.gate_result and kind != "issue":
                        self.comment(number, f"<!-- baton-e2e-gate -->\n{self.gate_result}, on {git(wt, 'rev-parse', '--short', 'HEAD')}. "
                                     "The runner ran this.", f"PR #{number}: posting the E2E gate result failed")
                    return 0, failure, total_rounds + local_repairs
                failure = "CI checks passed but left uncommitted or untracked files."
            failure_file.write_text(failure + "\n")
            notify.log(f"{kind} #{number}: verify failed (repair {local_repairs} of {budget} in this call)")
            event("failure", started)
            if local_repairs >= budget:
                return 1, failure, total_rounds + local_repairs
            if not self.env.config.allows(vendor, "fixer"):
                return 1, failure, total_rounds + local_repairs
            local_repairs += 1
            head = git(wt, "rev-parse", "HEAD")
            reply_file.unlink(missing_ok=True)
            prompt = self.render("verify-fix-prompt.md", VENDOR=vendor, WORKTREE=str(wt), BASE=base,
                                 FAILURE_FILE=str(failure_file), REPLY_FILE=str(reply_file), FAILURE=failure_excerpt(failure))
            if self.verification_progress:
                self.verification_progress(total_rounds + local_repairs, budget - local_repairs, head)
            try:
                self._continues_step = True
                try:
                    out = self.agent(vendor, "fix", prompt,
                                     f"{kind}-{number}-verify-fix-{total_rounds + local_repairs}",
                                     wt, str(reply_file), self.env.with_runtime(fix_followup=False))
                finally:
                    self._continues_step = False
            except Deferred:
                if self.verification_progress and not dirty() and git(wt, "rev-parse", "HEAD") == head:
                    self.verification_progress(total_rounds + local_repairs - 1, budget - local_repairs + 1, head)
                raise
            current_head = git(wt, "rev-parse", "HEAD")
            if self.verification_progress and current_head and not dirty():
                if kind == "pr" and current_head != head:
                    if not self.record(number, vendor, wt, head, "unknown", ""):
                        return 1, "Recording verification repair authorship failed.", total_rounds + local_repairs
                self.verification_progress(total_rounds + local_repairs, budget - local_repairs, current_head)
            if not out["outcome"].startswith("RESULT: fixed") or dirty():
                notify.log(f"{kind} #{number}: {vendor} verification fix failed or left the tree dirty")
                return 1, failure, total_rounds + local_repairs
            if current_head == head:   # fixed outside the repo: verify.sh still judges it
                notify.log(f"{kind} #{number}: {vendor} verification fix made no commit; checking again")
                continue
            self.hooks.task("ci_fix", f"{kind}:{number}:{total_rounds + local_repairs}:{git(wt, 'rev-parse', 'HEAD')}",
                            "--agent", vendor, "--round", total_rounds + local_repairs)

    def verify_failed(self, pr, failure, rounds):
        self.comment(pr, f"<!-- baton-verify-failed -->\nLocal CI verification failed before push after {rounds} of "
                         f"{self.max_verify} fix rounds.\n\n{failure[:REPLY_CAP]}", f"PR #{pr}: posting verification failure failed")

    # --- shared tail: after the commits are verified ---
    def record(self, pr, fixer, wt, agent_head, model, cursor_base):
        """Who wrote what (author, author model, lines); False when the records failed."""
        try:
            prior = self.prior_author or agents.LEGACY_REPLY_VENDOR   # a saved legacy base is Cursor's
            if cursor_base and cursor_base != agent_head and prior not in self.attempt_authors:
                self.add_author(prior)
                vendors.record_author(self.state, pr, prior)
                vendors.record_contrib(self.state, pr, prior, str(wt), cursor_base, agent_head)
            if self.head(wt) != agent_head:
                if agents.ADAPTERS[fixer].RECORDS_AUTHOR_MODEL:
                    vendors.record_author_model(self.state, pr, fixer, model)
                self.add_author(fixer)
                vendors.record_author(self.state, pr, fixer)
                vendors.record_contrib(self.state, pr, fixer, str(wt), agent_head)
        except OSError:
            return False
        return True

    def waiting_meta(self, pr, reason):
        """A saved checks or task write for this PR, or None; hands off when damaged or missing."""
        from . import handoff as owner_handoff
        try:
            found = owner_handoff.load_retry(self.env, self.push_episode(pr), reason, str(pr))
        except owner_handoff.RetryUnreadable as exc:
            self.handoff(pr, reason, f"PR #{pr}: metadata write failed",
                         f"Could not read the saved operation: {exc}")
            return "unreadable"
        return found

    def check_meta_write(self, pr, binding, *, pending_description=None):
        """Revalidate a saved checks or task write before rewriting or continuing (#151)."""
        def disposition(data):
            if not isinstance(data, dict):
                return "retry"
            if data.get("state") in ("CLOSED", "MERGED"):
                return "superseded"
            labels = data.get("labels")
            if data.get("state") != "OPEN" or not isinstance(labels, list) or any(
                    not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in labels):
                return "retry"
            return "paused" if any(label["name"] == "ready-for-human" for label in labels) else "ok"

        try:
            data = pr_helpers.view(pr, "state,labels,headRefOid,body")
        except ValueError:
            return "retry"
        if data is None:
            return "retry"
        status = disposition(data)
        if status != "ok":
            return status
        if issue := binding["issue"]:
            result = pr_helpers.gh("issue", "view", issue, "-R", self.repo, "--json", "state,labels")
            try:
                issue_data = json.loads(result.stdout) if result.returncode == 0 else None
            except ValueError:
                issue_data = None
            status = disposition(issue_data)
            if status != "ok":
                return status
        if binding["feedback"]:
            feedback = self.capture_push_feedback(pr)
            if feedback is None:
                return "retry"
            if feedback != binding["feedback"]:
                self.changed_input = "the owner feedback"
                return "changed"
        head = data.get("headRefOid")
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
            return "retry"
        if head != binding["head"]:
            self.changed_input = "the PR head"
            return "changed"
        body = (data.get("body") or "").rstrip("\n")
        if body == binding["intended"]:
            return "landed"
        if body == binding["starting"]:
            return "absent"
        if pending_description and body == pending_description:
            # Section 5: a failed description edit that landed after all is the new starting body.
            return "absent" if binding["intended"] != pending_description else "landed"
        self.changed_input = "the PR description"
        return "changed"

    def capture_head(self, pr):
        """The PR head SHA to bind a metadata write to; None when it can't be read."""
        try:
            viewed = pr_helpers.view(pr, "headRefOid")
        except ValueError:
            return None
        head = viewed.get("headRefOid") if isinstance(viewed, dict) else None
        return head if isinstance(head, str) and re.fullmatch(r"[0-9a-f]{40}", head) else None

    def run_meta_write(self, pr, reason, title, text, binding, pending, initial):
        """Reconcile, edit and settle a saved description write; one loop for checks and TASK: edits (#151).

        Returns "landed", or the result code to return (a handoff, or PUSH_SUPERSEDED);
        raises Deferred when a later attempt is saved or the PR is paused.
        """
        from . import handoff as owner_handoff
        episode = self.push_episode(pr)

        def reconcile():
            return self.check_meta_write(pr, binding)

        status = reconcile()
        edited = None
        if status == "absent":
            edited = pr_helpers.gh(
                "pr", "edit", str(pr), "-R", self.repo, "--body-file", "-",
                input=binding["intended"] + "\n", reconcile=reconcile)
            if edited.returncode == 0:
                status = "landed"
            else:
                status = reconcile()   # landed, superseded, paused and changed settle it as they are
                if status in ("absent", "retry"):
                    if not pr_helpers.transient(edited):
                        return self.handoff(pr, reason, title, f"{text}{pr_helpers.why(edited)}")
                    status = "retry"
        if status == "landed":
            return "landed"
        if status == "superseded":
            owner_handoff.clear_retry(self.env, episode, reason, str(pr))
            from . import advisor
            advisor.finish_episode(self.env, "pr", pr, "superseded")
            return PUSH_SUPERSEDED
        if status == "paused":
            owner_handoff.restore_retry(self.env, episode, reason, str(pr), pending)
            raise Deferred("handoff-retry", reason, lends_slot=False)
        if status in ("changed", "failed"):
            return self.handoff(pr, reason, title, f"{text} Inputs changed or the PR is unreadable.")
        attempts = pending["attempts"] + (0 if initial else 1)
        if status == "retry" and attempts < owner_handoff.MAX_ATTEMPTS:
            now = int(owner_handoff.clock())
            owner_handoff.restore_retry(
                self.env, episode, reason, str(pr),
                {**pending, "attempts": attempts, "scheduled": now,
                 "due": now + owner_handoff.DELAYS[attempts]})
            raise Deferred("handoff-retry", reason, lends_slot=False)
        return self.handoff(pr, reason, title, text + (pr_helpers.why(edited) if edited is not None else ""))

    def refresh_checks(self, pr, who, url, *, consume=False, pending=None, description_rc=0,
                       description_reason="", clear_cursor=False, fixer=""):
        """Save before refreshing checks, or consume a due attempt (#151)."""
        from . import handoff as owner_handoff
        reason = owner_handoff.FIX_CHECKS
        title = f"{who}PR #{pr}: Checks not refreshed"
        text = (f"The fix is pushed but the description may still list stale checks. {url}"
                if not consume else f"The description may still list stale checks. {url}")
        episode = self.push_episode(pr)
        initial = pending is None
        if initial:
            file = pr_helpers.checks_file(self.ctx, pr)
            try:
                checks = json.loads(file.read_text())["checks"]
            except (OSError, ValueError, KeyError, TypeError):
                checks = None
            current = pr_helpers.body(pr)
            if current is None:
                return self.handoff(pr, reason, title, text)
            intended = pr_contract.set_checks(current, checks).rstrip("\n")
            if intended == current:
                if consume:
                    file.unlink(missing_ok=True)
                if description_rc:
                    self.description_discarded(pr, fixer or self.last_fixer or "fixer",
                                               description_reason or "")
                if clear_cursor:
                    self.clear_reply_request(pr, fixer or self.last_fixer or agents.LEGACY_REPLY_VENDOR)
                recovery.clear(self.env, episode, pr)
                return 0
            if self.push_feedback is None:
                self.push_feedback = self.capture_push_feedback(pr)
            head = self.capture_head(pr)
            if head is None:
                return self.handoff(pr, reason, title, f"{text} The PR head could not be read.")
            binding = {
                "pr": str(pr), "issue": str(self.env.runtime.issue or ""),
                "starting": current, "intended": intended, "head": head,
                "feedback": self.push_feedback or "", "fixer": fixer or self.last_fixer or "fixer",
                "who": who, "consume": consume,
                "description_rc": description_rc, "description_reason": description_reason or "",
                "clear_cursor": bool(clear_cursor),
            }
            pending = owner_handoff.prepare_retry(self.env, episode, reason, str(pr), binding)
            if pending is None:
                return self.handoff(pr, reason, title, f"{text} The checks inputs could not be saved.")
            self.clear_task_write(pr)
        else:
            binding = pending["continuation"]
            if pending.get("phase") == "landed":
                self.checks_written = True
                return self.finish_checks(pr, binding, resumed=True)
            result = self.handoff(pr, reason, title, text, resume=True)
            if result == 1:
                return 1
            binding = result.continuation
            # Keep the pre-resume attempts: resume already consumed one on disk (#151).

        outcome = self.run_meta_write(pr, reason, title, text, binding, pending, initial)
        if outcome != "landed":
            return outcome
        if binding["consume"]:
            pr_helpers.checks_file(self.ctx, pr).unlink(missing_ok=True)
        notify.log(f"PR #{pr}: checks refreshed after the fix")
        self.checks_written = True
        landed = {**owner_handoff.load_retry(self.env, episode, reason, str(pr)), "phase": "landed"}
        owner_handoff.restore_retry(self.env, episode, reason, str(pr), landed)
        return self.finish_checks(pr, binding, resumed=not initial)

    def finish_checks(self, pr, binding, *, resumed):
        """Continuation after a landed checks refresh (#151)."""
        from . import handoff as owner_handoff
        if binding["description_rc"]:
            self.description_discarded(pr, binding["fixer"], binding["description_reason"])
        fixer = binding["fixer"]
        if binding["clear_cursor"] or (resumed and binding["consume"]
                                       and fixer in agents.ADAPTERS
                                       and agents.ADAPTERS[fixer].FIX_RESUMABLE_VIA_REPLY):
            self.clear_reply_request(pr, fixer)
        owner_handoff.clear_retry(self.env, self.push_episode(pr), owner_handoff.FIX_CHECKS, str(pr))
        recovery.clear(self.env, self.push_episode(pr), pr)
        return 0

    def recheck_without_commit(self, pr, who, url, description_changed=False, *, fixer,
                               description_rc=0, description_reason=""):
        """(rc, changed) for a fix with no commit: a re-check the fixer gave still reaches the
        description, and the next review checks it. No re-check leaves the description alone,
        unless the fixer rewrote it: its own checks section must not reach the merge gate."""
        if not description_changed and not pr_helpers.fixer_gave_checks(self.ctx, pr):
            if description_rc:
                self.description_discarded(pr, fixer, description_reason)
            return 0, False
        self.checks_written = False
        rc = self.refresh_checks(pr, who, url, consume=True, fixer=fixer,
                                 description_rc=description_rc, description_reason=description_reason)
        if rc != 0:
            return rc, False
        return 0, self.checks_written

    def head(self, wt):
        return git(wt, "rev-parse", "HEAD")

    def fetch_main(self, wt):
        # The fixer has no network, so its `ci:changed BASE=origin/main` would diff against a stale main.
        if session.run_process(["git", "-C", str(wt), "fetch", "-q", "origin", "main"]).returncode:
            notify.log(f"fetching origin/main for {wt} failed; the fixer uses the local copy")

    def description_discarded(self, pr, fixer, reason):
        """Log and best-effort comment a discarded description request; never fails the fix."""
        detail = reason or "It was invalid or GitHub refused it"
        notify.log(f"PR #{pr}: description request not applied ({fixer}) | {detail}")
        self.comment(
            pr,
            f"<!-- baton-description-discarded by={fixer} -->\n"
            f"Discarded the PR description request from {fixer}.\n\n{detail}",
        )

    def clear_description_write(self, pr):
        from . import handoff as owner_handoff
        owner_handoff.clear_retry(self.env, self.push_episode(pr), owner_handoff.FIX_DESCRIPTION, str(pr))

    def clear_task_write(self, pr):
        from . import handoff as owner_handoff
        owner_handoff.clear_retry(self.env, self.push_episode(pr), owner_handoff.FIX_TASK, str(pr))

    def write_description(self, pr, request, *, want="", fixer, resume=None, pending=None, baseline=None,
                          wt=""):
        """Apply a description (+ optional TASK:) under section 5 (#151).

        Cursor waits under the shared category-1 budget; Claude and Codex discard on a confirmed
        miss. Exhaustion and permanent failures discard rather than hand off. Returns
        (rc, changed, reason, task_changed, discarded). Raises Deferred while Cursor waits.
        """
        from . import handoff as owner_handoff
        reason = owner_handoff.FIX_DESCRIPTION
        episode = self.push_episode(pr)
        budgeted = agents.ADAPTERS[fixer].FIX_RESUMABLE_VIA_REPLY
        if pending is None:
            pending = self.waiting_meta(pr, reason)
            if pending == "unreadable":
                return 1, False, "", False, False
        initial = pending is None
        if not initial:
            binding = pending["continuation"]
            if pending.get("phase") == "landed":
                task_changed = bool(binding["want"] and binding["want"] != binding.get("original_completion"))
                return 0, True, "", task_changed, False
            now = int(owner_handoff.clock())
            if pending["attempts"] >= owner_handoff.MAX_ATTEMPTS:
                # A restart after attempt two: no third try (#151).
                detail = binding.get("last_reason") or UNCONFIRMED_DESCRIPTION
                if request:
                    pr_helpers.remove_body_request(Path(request))
                self.clear_description_write(pr)
                return 0, False, detail, False, True
            if now < pending["due"]:
                raise Deferred("handoff-retry", reason, lends_slot=False)
            notify.log(f"handoff-retry {reason} attempt={pending['attempts'] + 1}/{owner_handoff.MAX_ATTEMPTS} "
                       f"episode={episode}")
            resume = binding.get("resume") or resume
            outcome = self._run_description_write(pr, binding, pending, initial=False)
            return self._settle_description(pr, request, binding, outcome, resume=resume)
        path = Path(request) if request else None
        if path is None or (not path.exists() and not path.is_symlink()):
            return 0, False, "", False, False
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            detail = "description request must be a non-empty regular file and cannot be a symlink"
            pr_helpers.remove_body_request(path)
            return 0, False, detail, False, True
        current = pr_helpers.view(pr, "headRefName,body")
        if current is None:
            detail = "GitHub could not confirm whether the description edit landed"
            if budgeted:
                # Unreadable before the first save: nothing to replay; discard.
                pr_helpers.remove_body_request(path)
                return 0, False, detail, False, True
            pr_helpers.remove_body_request(path)
            return 0, False, detail, False, True
        before = (current.get("body") or "").rstrip("\n")
        original = pr_helpers._original_body(baseline, before) if baseline is not None else before
        try:
            relation_source = {**current, "body": original} if want else current
            issue, original_completion = pr_contract.relation(relation_source)
            if not re.fullmatch(r"\d+", issue) or original_completion not in ("partial", "complete"):
                detail = "cannot determine the PR's issue and task completion"
                pr_helpers.remove_body_request(path)
                return 0, False, detail, False, True
            if want and pr_contract.relation(current)[0] != issue:
                detail = "the PR's associated issue changed since the fix began"
                pr_helpers.remove_body_request(path)
                return 0, False, detail, False, True
            prepared = pr_helpers._prepare_request(path.read_text(), issue, original_completion, want or None)
        except ValueError as error:
            pr_helpers.remove_body_request(path)
            return 0, False, str(error), False, True
        intended = prepared.rstrip("\n")
        task_changed = bool(want and want != original_completion)
        # Match apply_body: a relation-only edit is not a description text change.
        measured_from = original
        if task_changed:
            try:
                measured_from = pr_contract.retarget(original, issue, want).rstrip("\n")
            except ValueError:
                pass
        text_changed = intended != measured_from
        if intended == before:
            pr_helpers.remove_body_request(path)
            Path(self.state, f"pr-body-base-{pr}").unlink(missing_ok=True)
            return 0, text_changed, "", task_changed, False
        if self.push_feedback is None:
            self.push_feedback = self.capture_push_feedback(pr)
        head = self.capture_head(pr)
        if head is None:
            detail = "The PR head could not be read."
            if budgeted:
                pr_helpers.remove_body_request(path)
                return 0, False, detail, False, True
            pr_helpers.remove_body_request(path)
            return 0, False, detail, False, True
        binding = {
            "pr": str(pr), "issue": str(self.env.runtime.issue or issue or ""),
            "starting": original, "intended": intended, "head": head,
            "feedback": self.push_feedback or "", "fixer": fixer, "who": "",
            "want": want or "", "original_completion": original_completion,
            "text_changed": text_changed,
            "description_rc": 0, "description_reason": "", "clear_cursor": False,
            "wt": str(wt or (resume or {}).get("wt") or ""),
            "resume": resume or {"head": head, "agent_head": head, "model": "",
                                 "cursor_base": "", "description_changed": False},
            "last_reason": "",
        }
        if budgeted:
            pending = owner_handoff.prepare_retry(self.env, episode, reason, str(pr), binding)
            if pending is None:
                pr_helpers.remove_body_request(path)
                return 0, False, "The description inputs could not be saved.", False, True
        outcome = self._run_description_write(pr, binding, pending, initial=True, prepared=prepared)
        return self._settle_description(pr, request, binding, outcome, resume=binding["resume"],
                                        task_changed=task_changed, text_changed=text_changed)

    def _run_description_write(self, pr, binding, pending, *, initial, prepared=None):
        """Reconcile and optionally rewrite a saved description; Cursor may defer (#151)."""
        from . import handoff as owner_handoff
        reason = owner_handoff.FIX_DESCRIPTION
        episode = self.push_episode(pr)
        budgeted = agents.ADAPTERS[binding["fixer"]].FIX_RESUMABLE_VIA_REPLY

        def reconcile():
            return self.check_meta_write(pr, binding)

        original = pending
        if pending is not None and not initial and budgeted:
            # Consume first: a crash after this point must not repeat the attempt (#151).
            now = int(owner_handoff.clock())
            used = pending["attempts"] + 1
            pending = {**pending, "attempts": used, "scheduled": now,
                       "due": now + owner_handoff.DELAYS[min(used, owner_handoff.MAX_ATTEMPTS - 1)]}
            owner_handoff.restore_retry(self.env, episode, reason, str(pr), pending)
        self.changed_input = ""

        # Initial attempt just read the body while building the binding; resume revalidates first.
        status = "absent" if initial else reconcile()
        edited = None
        unconfirmed = status == "retry"
        if status == "absent":
            body_text = prepared if prepared is not None else binding["intended"] + "\n"
            if not body_text.endswith("\n"):
                body_text += "\n"
            # A real file path (not stdin) keeps the tick and apply_body test doubles working.
            with tempfile.NamedTemporaryFile(
                    "w", dir=self.ctx, prefix=f"pr-{pr}-description.", delete=False) as file:
                file.write(body_text)
            try:
                edited = pr_helpers.gh(
                    "pr", "edit", str(pr), "-R", self.repo, "--body-file", file.name,
                    reconcile=reconcile if pending is not None
                    else (lambda: self._description_after_failure(pr, binding)))
            finally:
                Path(file.name).unlink(missing_ok=True)
            if edited.returncode == 0:
                status = "landed"
            else:
                status = reconcile() if pending is not None else self._description_after_failure(pr, binding)
                unconfirmed = status == "retry"
                if status in ("absent", "retry"):
                    if not pr_helpers.transient(edited):
                        if unconfirmed:
                            return {"status": "discard", "detail": UNCONFIRMED_DESCRIPTION, "unconfirmed": True}
                        return {"status": "discard",
                                "detail": f"GitHub could not update the PR description{pr_helpers.why(edited)}"}
                    status = "retry"
        if status == "landed":
            return {"status": "landed"}
        if status == "superseded":
            owner_handoff.clear_retry(self.env, episode, reason, str(pr))
            from . import advisor
            advisor.finish_episode(self.env, "pr", pr, "superseded")
            return {"status": "superseded"}
        if status == "paused":
            if pending is not None:
                owner_handoff.restore_retry(self.env, episode, reason, str(pr), original)
            raise Deferred("handoff-retry", reason, lends_slot=False)
        if status == "changed":
            return {"status": "discard",
                    "detail": f"{self.changed_input or 'the PR description'} changed during the edit"}
        if status == "retry" and not budgeted:
            if unconfirmed or edited is None:
                return {"status": "discard", "detail": UNCONFIRMED_DESCRIPTION, "unconfirmed": True}
            return {"status": "discard",
                    "detail": f"GitHub could not update the PR description{pr_helpers.why(edited)}"}
        attempts = (pending or {}).get("attempts", 0)
        if status == "retry" and pending is not None and attempts < owner_handoff.MAX_ATTEMPTS:
            notify.log(f"PR #{pr}: description update deferred after GitHub failure")
            if initial:
                now = int(owner_handoff.clock())
                owner_handoff.restore_retry(
                    self.env, episode, reason, str(pr),
                    {**pending, "scheduled": now, "due": now + owner_handoff.DELAYS[0]})
            raise Deferred("handoff-retry", reason, lends_slot=False)
        if unconfirmed or edited is None:
            return {"status": "discard", "detail": UNCONFIRMED_DESCRIPTION, "unconfirmed": True}
        return {"status": "discard",
                "detail": f"GitHub could not update the PR description{pr_helpers.why(edited)}"}

    def _description_after_failure(self, pr, binding):
        """Classify the body after a failed description edit with no saved retry yet."""
        try:
            viewed = pr_helpers.view(pr, "body")
        except ValueError:
            return "retry"
        if viewed is None:
            return "retry"
        body = (viewed.get("body") or "").rstrip("\n")
        if body == binding["intended"]:
            return "landed"
        if body == binding["starting"]:
            return "absent"
        return "changed"

    def _settle_description(self, pr, request, binding, outcome, *, resume=None, task_changed=None,
                            text_changed=None):
        """Finish a description attempt: clear state, discard, or report progress."""
        status = outcome["status"]
        if status == "discard" and outcome.get("unconfirmed") and self._description_after_failure(
                pr, binding) == "landed":
            # The edit landed after all: count it and skip the discard (section 5).
            outcome = {"status": "landed"}
            status = "landed"
        if status == "superseded":
            if request:
                pr_helpers.remove_body_request(Path(request))
            Path(self.state, f"pr-body-base-{pr}").unlink(missing_ok=True)
            return PUSH_SUPERSEDED, False, "", False, False
        if status == "landed":
            self.clear_description_write(pr)
            if agents.ADAPTERS[binding["fixer"]].FIX_RESUMABLE_VIA_REPLY:
                # A reply fixer keeps its request and baseline until the fix completes, so a crash
                # replays the landed edit as progress (clear_cursor_request removes them).
                pass
            else:
                if request:
                    pr_helpers.remove_body_request(Path(request))
                Path(self.state, f"pr-body-base-{pr}").unlink(missing_ok=True)
            if task_changed is None:
                task_changed = bool(binding.get("want") and binding["want"] != binding.get("original_completion"))
            if text_changed is None:
                text_changed = binding.get("text_changed")
            if text_changed is None:
                text_changed = True
            return 0, bool(text_changed), "", task_changed, False
        detail = outcome.get("detail") or "GitHub could not update the PR description"
        self.clear_description_write(pr)
        if request:
            pr_helpers.remove_body_request(Path(request))
        # Keep pr-body-base so a following TASK: write can detect an issue change (#151).
        return 0, False, detail, False, True

    def write_task(self, pr, url, *, want="", pending=None, fixer="", who="", baseline=None,
                   description_rc=0, description_reason="", resume=None):
        """Save before applying a TASK: change, or consume a due attempt (#151).

        RESUME is what the caller still has to do once the edit lands (saved with the write).
        Returns (rc, changed, log) like pr.apply_task: rc 1 is a handoff or deferral (Deferred),
        2/3 are validation failures the caller may recover from.
        """
        from . import handoff as owner_handoff
        reason = owner_handoff.FIX_TASK
        title = f"PR #{pr}: task completion change failed ({fixer or self.last_fixer or 'fixer'})"
        text = url
        episode = self.push_episode(pr)
        if pending is None:
            pending = self.waiting_meta(pr, reason)   # a saved write continues instead of starting over
            if pending == "unreadable":
                return 1, False, ""
        initial = pending is None
        if initial:
            if not want:
                return 0, False, ""
            current = pr_helpers.view(pr, "headRefName,body")
            if current is None:
                return self.handoff(pr, reason, title, text), False, ""
            before = (current.get("body") or "").rstrip("\n")
            original = pr_helpers._original_body(baseline, before) if baseline is not None else before
            try:
                issue, completion = pr_contract.relation(current)
                original_issue, original_completion = pr_contract.relation({**current, "body": original})
                if original_issue != issue:
                    return 3, False, f"PR #{pr}: the associated issue changed since the fix began"
                if not re.fullmatch(r"\d+", issue):
                    return 2, False, ""
                if completion == want:
                    return 0, original_completion != want, ""
                try:
                    intended = pr_contract.retarget(before + "\n", issue, want).rstrip("\n")
                except ValueError:
                    return 3, False, ""
            except ValueError:
                return 2, False, ""
            if intended == before:
                return 0, False, ""
            if self.push_feedback is None:
                self.push_feedback = self.capture_push_feedback(pr)
            head = self.capture_head(pr)
            if head is None:
                return self.handoff(pr, reason, title, f"{text} The PR head could not be read."), False, ""
            binding = {
                "pr": str(pr), "issue": str(self.env.runtime.issue or issue or ""),
                "starting": before, "intended": intended, "head": head, "want": want,
                "feedback": self.push_feedback or "", "fixer": fixer or self.last_fixer or "fixer",
                "who": who, "description_rc": description_rc, "description_reason": description_reason or "",
                "clear_cursor": False, "resume": resume,
            }
            if self.retry_context:
                binding["retry_context"] = self.retry_context
            pending = owner_handoff.prepare_retry(self.env, episode, reason, str(pr), binding)
            if pending is None:
                return self.handoff(pr, reason, title, f"{text} The task inputs could not be saved."), False, ""
        else:
            binding = pending["continuation"]
            if pending.get("phase") == "landed":
                return 0, True, ""
            result = self.handoff(pr, reason, title, text, resume=True)
            if result == 1:
                return 1, False, ""
            binding = result.continuation

        outcome = self.run_meta_write(pr, reason, title, text, binding, pending, initial)
        if outcome != "landed":
            return outcome, False, ""
        landed = {**owner_handoff.load_retry(self.env, episode, reason, str(pr)), "phase": "landed"}
        owner_handoff.restore_retry(self.env, episode, reason, str(pr), landed)
        log = f"PR #{pr}: task completion change at the fixer's request; the next review checks it"
        notify.log(log)
        return 0, True, log

    # --- an artifact-protocol fixer ---
    def fix_pr(self, pr, wt, fixer, source, feedback, head="", *, owner_triggered=False):
        self.last_fixer = fixer
        url = f"https://github.com/{self.repo}/pull/{pr}"
        self.push_feedback = self.capture_push_feedback(pr)
        fix_file = Path(f"/tmp/baton-fix-{pr}.md")
        agent_head = self.head(wt)
        head = self.claim_fix(pr, fixer, head, agent_head)
        cursor_base = head if self.prior_author else ""   # the earlier reply producer's commits
        # A new adapter reads the live description and owns its own request.
        self.clear_cursor_request(pr, keep_base=True)
        self.fetch_main(wt)
        description_request = Path(f"{fix_file}.body")
        fix_file.unlink(missing_ok=True)
        pr_helpers.remove_body_request(description_request)
        Path(self.ctx, f"pr-{pr}-fix-checks.json").unlink(missing_ok=True)
        adapter = agents.ADAPTERS[fixer]
        template = adapter.FIX_TEMPLATE
        description = Path(self.ctx, f"pr-{pr}-description.md")
        # Artifact fixers edit the description through /tmp; the runner applies it (#75).
        # The adapter's FIX_TEMPLATE tells it how to reach that file.
        description.write_text(pr_helpers.body(pr) or "")
        shots_note, env = "", None
        if self.env.repo.screenshot_review:   # the next review looks at fresh screenshots (#50)
            shots = Path(self.ctx, f"pr-{pr}-shots")
            session.run_process(["rm", "-rf", str(shots)])
            shots.mkdir(parents=True)
            shots_note = (f" Then, when that section is not `none`, re-shoot every item into `{shots}` (the runner "
                          "cleared it) the way the repository's agent docs describe, named `NN-<slug>-<variant>.png` in "
                          "list order: the next review looks at them. Delete the screenshot script and leave the "
                          "worktree clean.")
            env = self.env.with_runtime(shots_dir=str(shots))
        out = self.agent(fixer, "fix", self.render(template, PR=str(pr), ISSUE=self.env.runtime.issue or "unknown",
                                                   GH_REPO=self.repo, WORKTREE=str(wt), SOURCE=source,
                                                   FIX_SCOPE=fix_scope(fixer, owner_triggered), FEEDBACK=feedback,
                                                   PR_DESCRIPTION_FILE=str(description), SHOTS_NOTE=shots_note),
                         f"pr-{pr}-fix{f'-{self.round}' if self.round else ''}", wt, str(fix_file), env)
        outcome, model = out["outcome"], out["model"] or "unknown"
        # Suppress failed replies from adapters with denied GitHub access. Partial commits still reach the owner.
        if not (adapter.GITHUB_ACCESS == "denied" and outcome.startswith("RESULT: failed")):
            if fix_file.is_file() and fix_file.stat().st_size:
                try:
                    rest = pr_helpers.split_checks(pr, strip_markers(fix_file.read_text()), self.ctx)
                except Exception:
                    rest = strip_markers(fix_file.read_text())
                self.design_reply = rest
                loop = f" loop={self.loop_id} round={self.round}" if self.loop_id and str(self.round).isdecimal() else ""
                marker = (f"<!-- baton-fix by={fixer}{loop} outcome=needs-info -->"
                          if outcome.startswith("RESULT: needs-info")
                          else f"<!-- baton-fix by={fixer}{loop} -->")
                if any(line.strip() and not line.startswith("RESULT: ") for line in rest.split("\n")):   # a checks-only reply has nothing to post; a bare marker would move the cutoff
                    self.comment(pr, f"{marker}\n{rest.rstrip(chr(10))}")
            self.hooks.fix(fixer, self.round or 0, out["reply"], str(fix_file))
        task_changed, description_rc, description_changed, description_reason = False, 0, False, ""
        if outcome.startswith("RESULT: fixed"):
            want = pr_helpers.task_wanted(out["reply"])
            has_request = description_request.exists() or description_request.is_symlink()
            resume = {"agent_head": agent_head, "head": head, "model": model, "cursor_base": cursor_base,
                      "description_changed": False}
            if has_request:
                rc, description_changed, description_reason, task_changed, discarded = self.write_description(
                    pr, str(description_request), want=want or "", fixer=fixer, resume=resume,
                    baseline=Path(self.state, f"pr-body-base-{pr}"), wt=str(wt))
                if rc == PUSH_SUPERSEDED:
                    return rc
                if rc == 1:
                    return 1
                description_rc = 2 if discarded else 0
                resume = {**resume, "description_changed": bool(description_changed)}
            Path(self.state, f"pr-body-base-{pr}").unlink(missing_ok=True)
            # A rejected body keeps the prior text; still apply a valid TASK: request.
            if not has_request or description_rc:
                rc, task_changed, log = self.write_task(
                    pr, url, want=want, fixer=fixer, description_rc=description_rc,
                    description_reason=description_reason, resume=resume)
                if rc == PUSH_SUPERSEDED:
                    return rc
                if rc != 0 and (rc != 2 or description_rc != 2):
                    reason = "fix_task_write_failed" if rc == 1 else "fix_task_invalid"
                    if rc == 1:
                        return 1
                    if not self.record(pr, fixer, wt, agent_head, model, cursor_base):
                        return 1
                    action, fixer, _ = self.recover(pr, reason, f"PR #{pr}: task completion change failed ({fixer})",
                                                    f"Invalid task relation from {fixer}. {url}", fixer, wt=wt, head=head,
                                                    validation_error=log)
                    if action == "handoff":
                        return 1
                    return self.dispatch_fixer(pr, wt, fixer, source, correction(feedback, log), head)
        if not outcome.startswith("RESULT: fixed") and self.head(wt) != head:
            # Publish partial work before handing off or asking recovery to run another fixer.
            if not self.record(pr, fixer, wt, agent_head, model, cursor_base):
                return 1
            return self.push_partial_fix(pr, wt, "HEAD", url, outcome, fixer, source, feedback, head)
        if not outcome.startswith("RESULT: fixed"):
            if outcome.startswith("RESULT: needs-info"):
                return self.handoff(pr, "fix_needs_info", f"PR #{pr} has questions", url)
            action, fixer, _ = self.recover(pr, "fix_run_failed", f"PR #{pr} fix failed ({fixer})",
                                            f"{outcome} {url}", fixer, wt=wt, head=head)
            if action == "handoff":
                return 1
            return self.dispatch_fixer(pr, wt, fixer, source, feedback, head)
        return self.finish_fix(pr, wt, fixer, source, feedback, head, agent_head=agent_head, model=model,
                               cursor_base=cursor_base, task_changed=task_changed,
                               description_rc=description_rc, description_changed=description_changed,
                               description_reason=description_reason)

    def rejection_only(self):
        """A no-commit fix is no hand-off when the feedback had only design findings (no Must fix)
        and the reply answers every one of them `rejected`: the next screenshot review judges it."""
        return bool(self.design_only and design_findings.rejects_all(self.design_ids, self.design_reply or ""))

    def capped_reply(self, reply):
        """REPLY under REPLY_CAP with its `## Design findings` section whole and only the rest cut;
        None when that section alone exceeds the cap."""
        if len(reply) <= REPLY_CAP:
            return reply
        rest, section = design_findings.split_section(reply)
        if not section:
            return reply[:REPLY_CAP]
        if len(section) > REPLY_CAP:
            return None
        head = rest.rstrip("\n")[:max(0, REPLY_CAP - len(section) - 2)].rstrip("\n")
        return f"{head}\n\n{section}" if head else section

    def reply_rejection_only(self, pr, fixer):
        """rejection_only for a reply-protocol fix; a resume that ran no agent reads the answers saved
        before the description was applied."""
        if self.design_reply:
            return self.rejection_only()
        try:
            saved = json.loads(Path(self.state, f"{fixer}-fix-design-{pr}").read_text())
            return bool(self.design_only and set(saved["ids"]) == set(self.design_ids)
                        and design_findings.rejects_all(saved["ids"], saved["reply"]))
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def finish_fix(self, pr, wt, fixer, source, feedback, head, *, agent_head, model, cursor_base,
                   task_changed, description_rc, description_changed, description_reason):
        """The steps after a fixed run's description and TASK: writes: checks, verify, push (#151)."""
        url = f"https://github.com/{self.repo}/pull/{pr}"
        base_file = Path(self.state, f"cursor-fix-base-{pr}")
        if self.head(wt) == head:
            rc, checks_changed = self.recheck_without_commit(
                pr, "", url, description_changed, fixer=fixer,
                description_rc=description_rc, description_reason=description_reason)
            if rc != 0:
                return rc
            if (not task_changed and not checks_changed and not description_changed
                    and not self.rejection_only()):
                action, fixer, _ = self.recover(pr, "fix_no_commit", f"PR #{pr} needs you",
                                                f"The fix run made no commit; stopping the loop. {url}", fixer, wt=wt, head=head)
                if action == "handoff":
                    return 1
                return self.dispatch_fixer(pr, wt, fixer, source, feedback, head)
        if self.head(wt) != head:
            rc, failure, rounds = self.verify(wt, "origin/main", fixer, "pr", pr)
            if not self.record(pr, fixer, wt, agent_head, model if rounds == 0 else "unknown", cursor_base):
                return 1
            if rc != 0:
                action, fixer, remaining = self.recover(pr, "fix_verify_failed",
                                                        f"PR #{pr}: local CI verification failed",
                                                        recovery.verify_status(repairs_used=rounds, max_repairs=self.max_verify,
                                                                               failure=failure), fixer, wt=wt,
                                                        repairs_used=rounds, head=head,
                                                        before_labels=lambda: self.verify_failed(pr, failure, rounds))
                if action == "handoff":
                    return 1
                rc, failure, more = self.verify_retry(wt, self.verified_base, fixer, "pr", pr, repairs=remaining,
                                                repairs_before=rounds)
                rounds = more
                if rc != 0:
                    self.verify_failed(pr, failure, rounds)
                    return self.handoff(pr, "fix_verify_failed", f"PR #{pr}: local CI verification failed", url)
            rc = self.push_with_retry(
                pr, wt, "HEAD", next_step="done", url=url, description_rc=description_rc,
                description_reason=description_reason, fixer=fixer)
            if rc != 0:
                return rc
        base_file.unlink(missing_ok=True)
        episode = self.env.runtime.issue or str(pr)
        recovery.clear(self.env, str(episode), str(pr))
        self.clear_task_write(pr)
        return 0

    # --- a reply-protocol fixer ---
    def clear_reply_request(self, pr, fixer, *, keep_base=False):
        """Drop the shared body request and the producer's saved task and fix base."""
        Path(self.state, f"pr-body-base-{pr}").unlink(missing_ok=True)
        Path(self.state, f"{fixer}-fix-task-{pr}").unlink(missing_ok=True)
        Path(self.state, f"{fixer}-fix-design-{pr}").unlink(missing_ok=True)
        if not keep_base:
            Path(self.state, f"{fixer}-fix-base-{pr}").unlink(missing_ok=True)
        pr_helpers.remove_body_request(Path(self.ctx, f"pr-{pr}-body.md"))

    # --- the runner-owned record of which attempt owns the shared request files ---
    def read_owner(self, pr):
        try:
            data = json.loads(Path(self.state, f"pr-{pr}-fix-owner").read_text())
        except (OSError, ValueError):
            return None
        if (isinstance(data, dict) and data.get("fixer") in agents.ADAPTERS
                and isinstance(data.get("base"), str) and data["base"]):
            return data
        return None

    def write_owner(self, pr, fixer, base):
        path = Path(self.state, f"pr-{pr}-fix-owner")
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"fixer": fixer, "base": base}))
        temp.replace(path)

    def clear_owner(self, pr):
        Path(self.state, f"pr-{pr}-fix-owner").unlink(missing_ok=True)

    def read_base(self, pr, fixer):
        try:
            return Path(self.state, f"{fixer}-fix-base-{pr}").read_text().split("\n")[0].strip()
        except OSError:
            return ""

    def claim_fix(self, pr, fixer, head, agent_head):
        """Publish this attempt as the owner of the shared fix files and return its fix base.

        A different earlier producer's request, description baseline, saved checks and base files
        are discarded first, but its original base carries over to the new fixer.
        """
        request = Path(self.ctx, f"pr-{pr}-body.md")
        pair = Path(self.state, f"pr-body-base-{pr}").is_file() and request.is_file()
        owner = self.read_owner(pr)
        producer, base = "", ""
        if owner:
            producer, base = owner["fixer"], owner["base"]
        elif pair:   # a request from before owner records is Cursor's
            producer = agents.LEGACY_REPLY_VENDOR
            base = self.read_base(pr, agents.LEGACY_REPLY_VENDOR) or (agent_head if fixer == agents.LEGACY_REPLY_VENDOR else head or agent_head)
        if not base:
            base = self.read_base(pr, fixer) or self.read_base(pr, agents.LEGACY_REPLY_VENDOR) or head or agent_head
        self.prior_author = ""
        if producer and producer != fixer:
            for name, adapter in agents.ADAPTERS.items():
                if adapter.FIX_RESUMABLE_VIA_REPLY:
                    self.clear_reply_request(pr, name)
            Path(self.ctx, f"pr-{pr}-fix-checks.json").unlink(missing_ok=True)
            if agents.ADAPTERS[producer].FIX_RESUMABLE_VIA_REPLY:
                self.prior_author = producer
        elif not producer and self.read_base(pr, agents.LEGACY_REPLY_VENDOR):
            self.prior_author = agents.LEGACY_REPLY_VENDOR
        self.write_owner(pr, fixer, base)
        return base

    def clear_cursor_request(self, pr, *, keep_base=False):
        """Compatibility name for Cursor's pending reply request (shared body files included)."""
        self.clear_reply_request(pr, "cursor", keep_base=keep_base)

    def cursor_fix(self, pr, wt, source, feedback, head="", *, owner_triggered=False):
        return self.reply_fix(pr, wt, "cursor", source, feedback, head, owner_triggered=owner_triggered)

    def reply_fix(self, pr, wt, fixer, source, feedback, head="", *, owner_triggered=False):
        adapter = agents.ADAPTERS[fixer]
        name = adapter.DISPLAY_NAME
        agent_head = self.head(wt)
        url = f"https://github.com/{self.repo}/pull/{pr}"
        base_file = Path(self.state, f"{fixer}-fix-base-{pr}")
        baseline_file = Path(self.state, f"pr-body-base-{pr}")
        task_file = Path(self.state, f"{fixer}-fix-task-{pr}")
        design_file = Path(self.state, f"{fixer}-fix-design-{pr}")
        head = self.claim_fix(pr, fixer, head, agent_head)
        base_file.write_text(head + "\n")
        request = Path(self.ctx, f"pr-{pr}-body.md")
        if task_file.is_file():
            want, _, feedback_digest = task_file.read_text().partition("\n")
            self.push_feedback = feedback_digest.strip() or self.capture_push_feedback(pr)
        elif baseline_file.is_file() and request.is_file() and fixer == agents.LEGACY_REPLY_VENDOR:
            # Legacy pending body requests have no saved TASK intent (Cursor only).
            want = ""
            self.push_feedback = self.capture_push_feedback(pr)
        else:
            branch = self.pr_head_branch(pr)
            if not branch:
                from . import handoff as owner_handoff
                return self.handoff(pr, owner_handoff.FIX_PUSH, f"PR #{pr}: pushing fix failed",
                                    f"PR #{pr}: pushing fix failed: could not read PR head branch. {url}")
            self.push_feedback = self.capture_push_feedback(pr)
            feedback_file = Path(self.ctx, f"pr-{pr}-{fixer}-fix-{time.strftime('%Y%m%d-%H%M%S')}.md")
            feedback_file.write_text(f"{feedback}\n\n<pr-description>\n{pr_helpers.body(pr) or ''}\n</pr-description>\n")
            self.fetch_main(wt)
            shots = Path(self.ctx, f"pr-{pr}-shots")
            session.run_process(["rm", "-rf", str(shots)])
            shots.mkdir(parents=True)
            if fixer != agents.LEGACY_REPLY_VENDOR:
                # A later Cursor round must not resume the request this run supersedes.
                Path(self.state, f"{agents.LEGACY_REPLY_VENDOR}-fix-task-{pr}").unlink(missing_ok=True)
            self.clear_reply_request(pr, fixer, keep_base=True)
            Path(self.ctx, f"pr-{pr}-fix-checks.json").unlink(missing_ok=True)
            out = self.agent(fixer, "fix", self.render(
                adapter.FIX_TEMPLATE, PR=str(pr), ISSUE=self.env.runtime.issue or "unknown", GH_REPO=self.repo,
                WORKTREE=str(wt), BRANCH=branch, SOURCE=source, FEEDBACK_FILE=str(feedback_file),
                FIX_SCOPE=fix_scope(fixer, owner_triggered),
                SHOTS_DIR=str(shots), PR_BODY_FILE=str(request)), f"pr-{pr}-{fixer}-fix", wt,
                env=self.env.with_runtime(shots_dir=str(shots), pr_body_file=str(request)))
            outcome, reply_raw = out["outcome"], out["reply"] or ""
            self.reply = reply_raw
            want = pr_helpers.task_wanted(reply_raw)
            reply = "\n".join(line for line in strip_markers(reply_raw).split("\n") if not line.startswith("RESULT: ")).rstrip("\n")
            if reply_raw:
                reply = pr_helpers.split_checks(pr, reply, self.ctx).rstrip("\n")
            self.design_reply = reply
            if reply.strip():
                loop = f" loop={self.loop_id} round={self.round}" if self.loop_id and str(self.round).isdecimal() else ""
                marker = (f"<!-- baton-fix by={fixer}{loop} outcome=needs-info -->"
                          if outcome.startswith("RESULT: needs-info")
                          else f"<!-- baton-fix by={fixer}{loop} -->")
                posted = self.capped_reply(reply)
                if posted is None:   # the design answers alone do not fit: post nothing that parses as a partial answer
                    if not self.record(pr, fixer, wt, agent_head, "", ""):
                        return 1
                    action, alt, _ = self.recover(pr, "fix_run_failed", f"{name} PR #{pr} fix failed",
                                                  f"design answers exceed the reply cap. {url}", fixer, wt=wt, head=head)
                    if action == "handoff":
                        return 1
                    return self.dispatch_fixer(pr, wt, alt, source, feedback, head)
                self.comment(pr, f"{marker}\n{posted}", f"PR #{pr}: posting {name}'s reply failed")
            if not outcome.startswith("RESULT: fixed"):
                if self.head(wt) != head:
                    if not self.record(pr, fixer, wt, head, out["model"] or "unknown", ""):
                        return 1
                    return self.push_partial_fix(pr, wt, "HEAD", url, outcome, fixer, source, feedback, head)
                if outcome.startswith("RESULT: needs-info"):
                    return self.handoff(pr, "fix_needs_info", f"{name} PR #{pr} has questions", url)
                if not self.record(pr, fixer, wt, agent_head, "", ""):
                    return 1
                action, alt, _ = self.recover(pr, "fix_run_failed", f"{name} PR #{pr} fix failed", f"{outcome} {url}",
                                              fixer, wt=wt, head=head)
                if action == "handoff":
                    return 1
                return self.dispatch_fixer(pr, wt, alt, source, feedback, head)
            if request.exists() or request.is_symlink() or want:
                # Keep the validated design answers for a resume that runs no agent.
                if self.design_only and self.design_ids:
                    design_file.write_text(json.dumps({"ids": list(self.design_ids), "reply": reply}))
                # Publish the fixed attempt after saving its checks, before any GitHub edit.
                temp = task_file.with_suffix(".tmp")
                temp.write_text(f"{want}\n{self.push_feedback or ''}\n")
                temp.replace(task_file)
        has_request = request.exists() or request.is_symlink()
        resume = {"head": head, "agent_head": agent_head, "description_changed": False,
                  "model": "", "cursor_base": ""}
        body_changed, description_reason, task_changed, discarded = False, "", False, False
        if has_request:
            rc, body_changed, description_reason, task_changed, discarded = self.write_description(
                pr, str(request), want=want or "", fixer=fixer, resume=resume,
                baseline=baseline_file, wt=str(wt))
            if rc == PUSH_SUPERSEDED:
                return rc
            if rc == 1:
                return 1
            resume = {**resume, "description_changed": bool(body_changed)}
            if discarded:
                has_request = False
        if not has_request or discarded:
            rc, task_changed, log = self.write_task(
                pr, url, want=want, fixer=fixer, baseline=baseline_file,
                description_rc=2 if discarded else 0, description_reason=description_reason, resume=resume)
            if rc == PUSH_SUPERSEDED:
                return rc
            if rc == 1:
                return 1
            if rc != 0 and (rc != 2 or not discarded):
                reason = "fix_task_invalid"
                start = agent_head if (self.attempt_authors or self.prior_author) else head
                if not self.record(pr, fixer, wt, start, "", head if self.prior_author else ""):
                    return 1
                action, alt, _ = self.recover(pr, reason, f"PR #{pr}: task completion change failed ({fixer})",
                                              f"Invalid task relation from {fixer}. {url}", fixer, wt=wt, head=head,
                                              validation_error=log)
                if action == "handoff":
                    return 1
                self.clear_reply_request(pr, fixer, keep_base=True)
                return self.dispatch_fixer(pr, wt, alt, source, correction(feedback, log), head)
        return self.finish_reply_fix(
            pr, wt, fixer, "HEAD", url, source, feedback, head, agent_head=agent_head,
            body_changed=body_changed, task_changed=task_changed, discarded=discarded,
            description_reason=description_reason)

    def resume_description_write(self, pr, wt, url, source, feedback, pending):
        """Resume a Cursor description edit, then the rest of the fix (#151 section 5)."""
        binding = pending["continuation"]
        fixer = binding["fixer"]
        saved = binding["resume"]
        self.last_fixer = fixer
        self.push_feedback = binding["feedback"] or None
        request = Path(self.ctx, f"pr-{pr}-body.md")
        rc, body_changed, description_reason, task_changed, discarded = self.write_description(
            pr, str(request) if request.exists() or request.is_symlink() else "",
            want=binding.get("want") or "", fixer=fixer, resume=saved, pending=pending)
        if rc == PUSH_SUPERSEDED:
            return rc
        if rc == 1:
            return 1
        if discarded:
            # After a discard, a valid TASK: change is its own write (#151 section 5).
            # The discard comment waits for the checks/push continuation, as today.
            resume = {**saved, "description_changed": False}
            baseline = Path(self.state, f"pr-body-base-{pr}")
            if not baseline.is_file():
                # Retarget against the body captured with the description, so an owner
                # issue correction during the wait is still detected.
                temp = baseline.with_suffix(".tmp")
                temp.write_text(binding["starting"] + "\n")
                temp.replace(baseline)
            task_rc, task_changed, log = self.write_task(
                pr, url, want=binding.get("want") or "", fixer=fixer, baseline=baseline,
                description_rc=2, description_reason=description_reason, resume=resume)
            if task_rc == PUSH_SUPERSEDED:
                return task_rc
            if task_rc == 1:
                return 1
            if task_rc != 0:
                start = saved["agent_head"] if self.attempt_authors else saved["head"]
                if not self.record(pr, fixer, wt, start, saved.get("model") or "", saved.get("cursor_base") or ""):
                    return 1
                action, alt, _ = self.recover(
                    pr, "fix_task_invalid", f"PR #{pr}: task completion change failed ({fixer})",
                    f"Invalid task relation from {fixer}. {url}", fixer, wt=wt, head=saved["head"],
                    validation_error=log)
                if action == "handoff":
                    return 1
                self.clear_reply_request(pr, fixer, keep_base=True)
                return self.dispatch_fixer(pr, wt, alt, source, correction(feedback, log), saved["head"])
        else:
            saved = {**saved, "description_changed": bool(body_changed)}
        return self.finish_reply_fix(
            pr, wt, fixer, "HEAD", url, source, feedback, saved["head"], agent_head=saved["agent_head"],
            body_changed=saved["description_changed"], task_changed=task_changed, discarded=discarded,
            description_reason=description_reason)

    def resume_task_write(self, pr, wt, url, source, feedback, pending):
        from . import handoff as owner_handoff
        binding = pending["continuation"]
        fixer = binding["fixer"]
        saved = binding.get("resume")
        if saved is None:
            # Older Cursor TASK records stored the base only in the sidecar.
            try:
                head = Path(self.state, f"cursor-fix-base-{pr}").read_text().strip()
            except (OSError, ValueError):
                head = ""
            if not re.fullmatch(r"[0-9a-f]{40}", head):
                return self.handoff(pr, owner_handoff.FIX_TASK, f"Cursor PR #{pr}: task completion change failed",
                                    "The saved Cursor fix base is missing or damaged.")
            saved = {"head": head, "agent_head": self.head(wt), "description_changed": False}
        rc, task_changed, _ = self.write_task(pr, url, pending=pending, fixer=fixer)
        if rc != 0:
            return rc
        if not agents.ADAPTERS[fixer].FIX_RESUMABLE_VIA_REPLY:
            return self.finish_fix(
                pr, wt, fixer, source, feedback, saved["head"], agent_head=saved["agent_head"],
                model=saved["model"], cursor_base=saved["cursor_base"], task_changed=task_changed,
                description_rc=binding["description_rc"], description_changed=saved["description_changed"],
                description_reason=binding["description_reason"])
        return self.finish_reply_fix(
            pr, wt, fixer, "HEAD", url, source, feedback, saved["head"], agent_head=saved["agent_head"],
            body_changed=saved["description_changed"], task_changed=task_changed, discarded=binding["description_rc"] == 2,
            description_reason=binding["description_reason"])

    def finish_cursor_fix(self, pr, wt, branch, url, source, feedback, head, *, agent_head,
                          body_changed, task_changed, discarded, description_reason):
        return self.finish_reply_fix(
            pr, wt, agents.LEGACY_REPLY_VENDOR, branch, url, source, feedback, head, agent_head=agent_head,
            body_changed=body_changed, task_changed=task_changed, discarded=discarded,
            description_reason=description_reason)

    def finish_reply_fix(self, pr, wt, fixer, branch, url, source, feedback, head, *, agent_head,
                         body_changed, task_changed, discarded, description_reason):
        name = agents.ADAPTERS[fixer].DISPLAY_NAME
        who = f"{name} "
        moved = self.head(wt) != head
        recorded_before_retry = False
        if not moved:
            rc, checks_changed = self.recheck_without_commit(
                pr, who, url, body_changed, fixer=fixer,
                description_rc=2 if discarded else 0, description_reason=description_reason)
            if rc != 0:
                return rc
            if not (body_changed or task_changed or checks_changed or self.reply_rejection_only(pr, fixer)):
                action, alt, _ = self.recover(pr, "fix_no_commit", f"{name} PR #{pr} needs you",
                                                f"{name}'s fix run made no commit. {url}", fixer, wt=wt, head=head)
                if action == "handoff":
                    self.clear_reply_request(pr, fixer)
                    return 1
                self.clear_reply_request(pr, fixer, keep_base=True)
                return self.dispatch_fixer(pr, wt, alt, source, feedback, head)
        else:
            rc, failure, rounds = self.verify(wt, "origin/main", fixer, "pr", pr)
            if rc != 0:
                start = agent_head if (self.attempt_authors or self.prior_author) else head
                if not self.record(pr, fixer, wt, start, "", head if self.prior_author else ""):
                    return 1
                recorded_before_retry = True
                action, alt, remaining = self.recover(pr, "fix_verify_failed",
                                                      f"{name} PR #{pr}: local CI verification failed",
                                                      recovery.verify_status(repairs_used=rounds, max_repairs=self.max_verify,
                                                                             failure=failure), fixer, wt=wt,
                                                      repairs_used=rounds, head=head,
                                                      before_labels=lambda: self.verify_failed(pr, failure, rounds))
                if action == "handoff":
                    return 1
                rc, failure, more = self.verify_retry(wt, self.verified_base, alt, "pr", pr, repairs=remaining,
                                                repairs_before=rounds)
                rounds = more
                if rc != 0:
                    self.verify_failed(pr, failure, rounds)
                    return self.handoff(pr, "fix_verify_failed", f"{name} PR #{pr}: local CI verification failed", url)
        try:
            prior = self.prior_author
            if (not recorded_before_retry and prior and head != agent_head
                    and prior not in self.attempt_authors):
                # The earlier producer's commits (original base..agent_head) are its own.
                self.add_author(prior)
                vendors.record_author(self.state, pr, prior)
                vendors.record_contrib(self.state, pr, prior, str(wt), head, agent_head)
            vendors.record_author(self.state, pr, fixer)
        except OSError:
            return 1
        if not recorded_before_retry:
            start = agent_head if self.attempt_authors else head
            self.add_author(fixer)
            vendors.record_contrib(self.state, pr, fixer, str(wt), start)
        if moved:
            return self.push_with_retry(
                pr, wt, branch, next_step="done", url=url, description_rc=2 if discarded else 0,
                description_reason=description_reason, who=who, fixer=fixer,
                clear_cursor=True)
        self.clear_reply_request(pr, fixer)
        episode = self.env.runtime.issue or str(pr)
        recovery.clear(self.env, str(episode), str(pr))
        self.clear_task_write(pr)
        return 0


    def dispatch_fixer(self, pr, wt, fixer, source, feedback, head):
        self.last_fixer = fixer
        scope = {"owner_triggered": self.owner_triggered}
        with self.dispatching(pr, wt):
            if not agents.ADAPTERS[fixer].FIX_RESUMABLE_VIA_REPLY:
                return self.fix_pr(pr, wt, fixer, source, feedback, head, **scope)
            rc = self.reply_fix(pr, wt, fixer, source, feedback, head, **scope)
            if rc == 75:   # a short block reported by return value, e.g. a description update
                recovery.settle(self.env, self.env.runtime.issue or pr, pr, lambda: self.retry_binding(wt, pr))
        self.hooks.fix(fixer, self.round or 0, self.reply)
        return rc

    def finish_progressed(self, pr, wt, fixer, head, budget):
        """Continue a completed worker step (its commits and reply exist) with its verification."""
        self.last_fixer = fixer
        rc, failure, rounds = self.verify_retry(
            wt, budget.get("base_sha") or "origin/main", fixer, "pr", pr,
            repairs=budget.get("verify_repairs", self.max_verify), repairs_before=budget.get("repairs_used", 0))
        if rc:
            self.verify_failed(pr, failure, rounds)
            return self.handoff(pr, "fix_verify_failed", f"PR #{pr}: local CI verification failed", failure)
        if not self.record(pr, fixer, wt, head, "unknown", ""):
            return 1
        rc = self.finish_verified(pr, wt, fixer)
        if rc == 0:
            recovery.clear(self.env, self.env.runtime.issue or pr, pr)
        return rc


def correction(feedback, error):
    return f"{feedback}\n\nCorrect the task relation in the previous attempt's reply or description. " \
           f"Retain the existing commits. Validation error: {error or 'invalid task relation'}"


def fix_scope(fixer, owner_triggered):
    if owner_triggered:
        return ("The owner's words set the scope for the edits and the reply. Any `<findings>` block is context only. "
                "Handle only the requests the owner makes, including the whole review if the owner asks for it. "
                "Include only those requests in your reply.")
    if agents.ADAPTERS[fixer].FIX_SCOPE_POLICY == "must_fix_nits_optional":
        action = "Handle every Must fix point. Fixing nits is optional."
    else:
        action = "Handle every point."
    return f"{action} All review nits are in scope for the reply."


def fix_pr(runner, pr, wt, fixer, source, feedback, head="", *, owner_triggered=False):
    """Resume a saved fix step or dispatch the requested adapter; keep the owner record only on a deferral."""
    rc = _fix_pr(runner, pr, wt, fixer, source, feedback, head, owner_triggered=owner_triggered)
    if rc != 75:   # the attempt is finalized; a deferral keeps the owner for its re-entry
        from . import handoff as owner_handoff
        pending = owner_handoff.operation_episodes(
            runner.env, (owner_handoff.FIX_PUSH, *owner_handoff.FIX_META_WRITES))
        if str(pr) not in pending and not Path(runner.state, f"fix-push-waiting-{pr}").exists():
            # Shared and producer files go before the record that names the producer.
            owner = runner.read_owner(pr)
            for name in {fixer, owner["fixer"] if owner else fixer}:
                runner.clear_reply_request(pr, name)
        runner.clear_owner(pr)
    return rc


def _fix_pr(runner, pr, wt, fixer, source, feedback, head="", *, owner_triggered=False):
    from . import advisor

    runner.owner_triggered = owner_triggered
    runner.retry_context = {"fix_feedback": recovery.feedback_digest(feedback), "fix_source": source}
    # Reconcile a saved push or metadata write before a worker grant can dispatch.
    from . import handoff as owner_handoff
    waiting = runner.waiting_push(pr)
    if waiting == "unreadable":
        return 1
    if waiting is not None:
        runner.clear_task_write(pr)
        binding = waiting["continuation"]
        runner.last_fixer = binding["fixer"]
        return runner.push_with_retry(pr, wt, binding["ref"], next_step=binding["next"],
                                      url=f"https://github.com/{runner.repo}/pull/{pr}", pending=waiting)
    url = f"https://github.com/{runner.repo}/pull/{pr}"
    waiting_description = runner.waiting_meta(pr, owner_handoff.FIX_DESCRIPTION)
    if waiting_description == "unreadable":
        return 1
    if (isinstance(waiting_description, dict)
            and waiting_description["continuation"]["fixer"] != fixer):
        # Another fixer never applies a pending description it did not write: drop it.
        runner.clear_description_write(pr)
        for name, adapter in agents.ADAPTERS.items():
            if adapter.FIX_RESUMABLE_VIA_REPLY and name != fixer:
                runner.clear_reply_request(pr, name, keep_base=True)
        waiting_description = None
    if waiting_description is not None:
        return runner.resume_description_write(pr, wt, url, source, feedback, waiting_description)
    waiting_checks = runner.waiting_meta(pr, owner_handoff.FIX_CHECKS)
    if waiting_checks == "unreadable":
        return 1
    if (isinstance(waiting_checks, dict) and waiting_checks.get("phase") != "landed"
            and waiting_checks["continuation"].get("fixer") not in (fixer, None, "")):
        # A switched fixer never applies the former producer's pending checks.
        owner_handoff.clear_retry(runner.env, runner.push_episode(pr), owner_handoff.FIX_CHECKS, str(pr))
        pr_helpers.checks_file(runner.ctx, pr).unlink(missing_ok=True)
        waiting_checks = None
    if waiting_checks is not None:
        runner.clear_task_write(pr)
        binding = waiting_checks["continuation"]
        runner.last_fixer = binding.get("fixer") or runner.last_fixer
        return runner.refresh_checks(
            pr, binding.get("who") or "", url, consume=binding.get("consume", True),
            pending=waiting_checks, fixer=binding.get("fixer") or "")
    waiting_task = runner.waiting_meta(pr, owner_handoff.FIX_TASK)
    if waiting_task == "unreadable":
        return 1
    if (isinstance(waiting_task, dict) and waiting_task.get("phase") != "landed"
            and waiting_task["continuation"]["fixer"] != fixer):
        # A switched fixer never replays the former producer's pending task write.
        runner.clear_task_write(pr)
        waiting_task = None
    task = {}
    if waiting_task is not None:
        task = waiting_task["continuation"]
        runner.last_fixer = task["fixer"]
        runner.push_feedback = task["feedback"] or None
        # The review loop can resume metadata writes without reloading the worker prompt.
        runner.retry_context = task.get("retry_context", runner.retry_context)
        pending = runner.resume(pr, wt, verify_only=True) if waiting_task.get("phase") == "landed" else None
        if pending is False:
            return 1
        if not pending:
            if advisor.enabled(runner.env):
                runner.retry_start = runner.retry_binding(wt, pr)
            return runner.resume_task_write(pr, wt, url, source, feedback, waiting_task)
    else:
        pending = runner.resume(pr, wt)
    if pending is False:
        return 1
    if advisor.enabled(runner.env):
        runner.retry_start = runner.retry_binding(wt, pr)
    if not pending:
        return runner.dispatch_fixer(pr, wt, fixer, source, feedback, head)
    record, fixer = pending
    budget = record["remaining_budget"]
    head = budget.get("head") or head
    if record["reason"] == "fix_verify_failed":
        runner.last_fixer = task.get("fixer", fixer)
        rc, failure, rounds = runner.verify_retry(wt, budget.get("base_sha") or "origin/main", fixer, "pr", pr,
                                                  repairs=budget["verify_repairs"],
                                            repairs_before=budget["repairs_used"])
        if rc:
            runner.verify_failed(pr, failure, rounds)
            return runner.handoff(pr, "fix_verify_failed", f"PR #{pr}: local CI verification failed", failure)
        last = runner.last_fixer
        prefix = f"{agents.ADAPTERS[last].DISPLAY_NAME} " if agents.ADAPTERS[last].FIX_RESUMABLE_VIA_REPLY else ""
        rc = runner.finish_verified(
            pr, wt, last, prefix=prefix,
            description_rc=task.get("description_rc", 0), description_reason=task.get("description_reason", ""))
        runner.clear_reply_request(pr, last)
        # An artifact fixer can finish with a base saved by an earlier Cursor attempt.
        runner.clear_cursor_request(pr)
        recovery.clear(runner.env, runner.env.runtime.issue or pr, pr)
        return rc
    if (record["state"] == "progressed" and not agents.ADAPTERS[fixer].FIX_RESUMABLE_VIA_REPLY
            and runner.head(wt) != head):
        return runner.finish_progressed(pr, wt, fixer, head, budget)
    if record["reason"] == "fix_task_invalid":
        feedback = correction(feedback, budget.get("validation_error", ""))
    if record["state"] != "progressed":   # a completed reply step reconciles from its saved request
        runner.clear_cursor_request(pr, keep_base=True)
    return runner.dispatch_fixer(pr, wt, fixer, source, feedback, head)


def result(rc, **extra):
    return {"rc": rc, "defer_vendor": "", "defer": "", **extra}


@session.scoped
def run(command, args, env=None, feedback=""):
    env = session.resolve(env)
    runner = Runner(env, pr=args.pr if command != "verify" else (args.number if args.kind == "pr" else 0),
                    round=env.runtime.round if env.runtime.round != "0" else "")
    try:
        if command == "verify":
            rc, failure, rounds = runner.verify(args.wt, args.base, args.vendor, args.kind, args.number)
            return result(rc, failure=failure, rounds=rounds)
        rc = fix_pr(runner, args.pr, args.wt, args.fixer, args.source, feedback, args.head,
                    owner_triggered=getattr(args, "owner_triggered", False))
        if rc == 0:
            runner.acknowledge_push(args.pr)
        return result(rc, fixer=runner.last_fixer, authors=runner.attempt_authors)
    except Deferred as deferred:
        return result(75, defer_vendor=deferred.vendor, defer=deferred.reason)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fix = sub.add_parser("pr", help="feedback on stdin")
    for name in ("pr", "wt", "fixer", "source"):
        fix.add_argument(f"--{name}", required=True)
    fix.add_argument("--head", default="")
    fix.add_argument("--owner-triggered", action="store_true", help="limit the fix to the owner's requests")
    check = sub.add_parser("verify")
    for name in ("wt", "base", "vendor", "kind", "number"):
        check.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    out = run(args.command, args, feedback=sys.stdin.read() if args.command == "pr" else "")
    print(json.dumps(out, ensure_ascii=False))
    return out["rc"]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"fix: {exc}", file=sys.stderr)
        sys.exit(1)
