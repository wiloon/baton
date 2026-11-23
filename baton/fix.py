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
import time

from . import agents, dispatch, e2e_gate, hooks as telemetry, notify, pick, pr as pr_helpers, pr_contract, prompts, recovery, vendors
from .common import Deferred, Exhausted, child_env, git, push, strip_markers

REPLY_CAP = 60000
PUSH_SUPERSEDED = 80
# The verify output a repair run gets in its prompt: the tail, where the errors are. The whole
# output stays in the failure file (#38: a Claude agent could not read the ctx dir with the shell).
FAILURE_PROMPT_CAP = 20000


def failure_excerpt(failure):
    if len(failure) <= FAILURE_PROMPT_CAP:
        return failure
    return f"[... first {len(failure) - FAILURE_PROMPT_CAP} characters cut; see the failure file ...]\n" + failure[-FAILURE_PROMPT_CAP:]


class Runner:
    @session.scoped
    def __init__(self, env=None, hooks=None, pr=0, round=""):
        env = session.resolve(env)
        self.env, self.round = env, round
        self.state, self.ctx = str(env.dirs.state), str(env.dirs.ctx)
        self.repo, self.self_dir = env.repo.gh_repo, Path(str(env.config.paths.self_dir))
        self.max_verify = int(env.config.scheduling.max_verify_fixes or 2)
        self.pr, self.reply, self.gate_result = pr, "", ""
        self.gate_applies = True   # dev.run clears it for a partial slice
        self.handed_off = False    # set once handoff() labels the PR; callers tell it apart from rc 1
        self.push_feedback = None
        self.ci_failed_head = ""
        self.push_caller = "ci" if round == "ci" else "review" if round else "standalone"
        self.last_fixer = ""
        self.attempt_authors = []
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

    def resume(self, pr, wt):
        episode = self.env.runtime.issue or pr
        try:
            saved = recovery.load(self.env, episode, pr)
        except recovery.RecoveryUnreadable:
            saved = None
        saved_reason = saved["reason"] if saved and saved["reason"].startswith("fix_") else "fix_run_failed"
        selected = recovery.resume(
            self.env, episode, pr, lambda reason: reason.startswith("fix_"),
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

    def finish_verified(self, pr, wt, fixer, *, prefix=""):
        url = f"https://github.com/{self.repo}/pull/{pr}"
        ref = (git(wt, "branch", "--show-current") or "HEAD") if fixer == "cursor" else "HEAD"
        return self.push_with_retry(pr, wt, ref, next_step="done", url=url, who=prefix, fixer=fixer,
                                    clear_cursor=fixer == "cursor")

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
        if binding["next"] == "handoff":
            reason = binding["handoff_reason"]
            title = (f"PR #{pr} has questions" if reason == "fix_needs_info"
                     else f"PR #{pr} fix failed ({binding['fixer']})")
            return self.handoff(pr, reason, title, f"{binding['outcome']} {url}")
        who = binding.get("who") or ""
        rc = self.refresh_checks(pr, who, url)
        if rc != 0:
            return rc
        if binding.get("description_rc"):
            self.description_discarded(pr, binding.get("fixer") or "fixer",
                                       binding.get("description_reason") or "")
        if binding.get("clear_cursor"):
            self.clear_cursor_request(pr)
        recovery.clear(self.env, self.push_episode(pr), pr)
        return 0

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
                if disposition == "ok" and owner_handoff.check_push_landed(binding) == "landed":
                    return self.continue_push(pr, pending, url)
                return self.handoff(pr, reason, title, f"{text}: could not confirm the landed push{note}. {url}")
        if initial:
            if self.push_feedback is None:   # a recovery step re-entered past fix_pr's own capture
                self.push_feedback = self.capture_push_feedback(pr)
            if self.env.repo.signoff:
                from .common import sign_off
                if not sign_off(wt):
                    return self.handoff(pr, reason, title, f"{text}: sign-off failed{note}. {url}")
            branch = owner_handoff.push_destination(wt, ref)
            commit = git(wt, "rev-parse", "HEAD")
            if not branch or not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
                return self.handoff(pr, reason, title, f"{text}: local commit or branch missing{note}. {url}")
            remote, unreadable_transient = owner_handoff.observe_remote(wt, branch)
            if remote is None and not unreadable_transient:
                return self.handoff(pr, reason, title, unreadable)
            binding = {
                "pr": str(pr), "issue": str(self.env.runtime.issue or ""), "commit": commit,
                "branch": branch, "ref": ref, "remote": remote, "upstream": upstream,
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
                if not owner_handoff.check_push_local(binding):
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
            status = owner_handoff.check_push_landed(binding)
        if status == "absent" and (initial or pending["attempts"] < owner_handoff.MAX_ATTEMPTS):
            # Push the saved commit to that destination; never force (#151).
            pushed = push(binding["wt"], f"{binding['commit']}:refs/heads/{binding['branch']}",
                          upstream=binding["upstream"], signoff=False)
            if pushed:
                status = "landed"
            else:
                # A lost response may still have updated the remote: look before retrying.
                status = owner_handoff.check_push_landed(binding)
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
        out = dispatch.dispatch(vendor, role, prompt, tag, str(wt), str(self.self_dir / "settings.json"), output,
                                env or self.env, self.hooks)
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
                out = self.agent(vendor, "fix", prompt, f"{kind}-{number}-verify-fix-{total_rounds + local_repairs}",
                                 wt, str(reply_file), self.env.with_runtime(fix_followup=False))
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
            if cursor_base and cursor_base != agent_head and "cursor" not in self.attempt_authors:
                self.add_author("cursor")
                vendors.record_author(self.state, pr, "cursor")
                vendors.record_contrib(self.state, pr, "cursor", str(wt), cursor_base, agent_head)
            if self.head(wt) != agent_head:
                if agents.ADAPTERS[fixer].RECORDS_AUTHOR_MODEL:
                    vendors.record_author_model(self.state, pr, fixer, model)
                self.add_author(fixer)
                vendors.record_author(self.state, pr, fixer)
                vendors.record_contrib(self.state, pr, fixer, str(wt), agent_head)
        except OSError:
            return False
        return True

    def refresh_checks(self, pr, who, url):
        rc, _, log = pr_helpers.apply_checks(pr, self.ctx, consume=False)
        if log:
            notify.log(log)
        if rc != 0:
            return self.handoff(pr, "fix_checks_refresh_failed", f"{who}PR #{pr}: Checks not refreshed",
                                f"The fix is pushed but the description may still list stale checks. {url}")
        return 0

    def recheck_without_commit(self, pr, who, url, description_changed=False):
        """(rc, changed) for a fix with no commit: a re-check the fixer gave still reaches the
        description, and the next review checks it. No re-check leaves the description alone,
        unless the fixer rewrote it: its own checks section must not reach the merge gate."""
        if not description_changed and not pr_helpers.fixer_gave_checks(self.ctx, pr):
            return 0, False
        rc, changed, log = pr_helpers.apply_checks(pr, self.ctx)
        if log:
            notify.log(log)
        if rc != 0:
            return self.handoff(pr, "fix_checks_refresh_failed", f"{who}PR #{pr}: Checks not refreshed",
                                f"The description may still list stale checks. {url}"), False
        return 0, changed

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

    # --- a Claude or Codex fixer ---
    def fix_pr(self, pr, wt, fixer, source, feedback, head="", *, owner_triggered=False):
        self.last_fixer = fixer
        url = f"https://github.com/{self.repo}/pull/{pr}"
        self.push_feedback = self.capture_push_feedback(pr)
        fix_file = Path(f"/tmp/baton-fix-{pr}.md")
        base_file = Path(self.state, f"cursor-fix-base-{pr}")
        cursor_base = base_file.read_text().strip() if base_file.is_file() else ""
        agent_head = self.head(wt)
        head = cursor_base or head or agent_head
        # A new adapter reads the live description and owns its own request.
        self.clear_cursor_request(pr, keep_base=True)
        self.fetch_main(wt)
        description_request = Path(f"{fix_file}.body")
        fix_file.unlink(missing_ok=True)
        pr_helpers.remove_body_request(description_request)
        Path(self.ctx, f"pr-{pr}-fix-checks.json").unlink(missing_ok=True)
        template = "codex-fix-prompt.md" if fixer == "codex" else "fix-prompt.md"
        description = Path(self.ctx, f"pr-{pr}-description.md")
        # Claude and Codex edit the description through /tmp; the runner applies it (#75).
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
        # A failed Codex run has no reply worth posting; its commits still reach the owner below.
        if not (fixer in ("cursor", "codex") and outcome.startswith("RESULT: failed")):
            if fix_file.is_file() and fix_file.stat().st_size:
                try:
                    rest = pr_helpers.split_checks(pr, strip_markers(fix_file.read_text()), self.ctx)
                except Exception:
                    rest = strip_markers(fix_file.read_text())
                marker = (f"<!-- baton-fix by={fixer} outcome=needs-info -->"
                          if outcome.startswith("RESULT: needs-info")
                          else f"<!-- baton-fix by={fixer} -->")
                if any(line.strip() and not line.startswith("RESULT: ") for line in rest.split("\n")):   # a checks-only reply has nothing to post; a bare marker would move the cutoff
                    self.comment(pr, f"{marker}\n{rest.rstrip(chr(10))}")
            self.hooks.fix(fixer, self.round or 0, out["reply"], str(fix_file))
        task_changed, description_rc, description_changed, description_reason = False, 0, False, ""
        if outcome.startswith("RESULT: fixed"):
            want = pr_helpers.task_wanted(out["reply"])
            has_request = description_request.exists() or description_request.is_symlink()
            description_rc, description_changed, description_reason, task_changed = pr_helpers.apply_body(
                pr, str(description_request), self.state, self.ctx, completion=want or None)
            Path(self.state, f"pr-body-base-{pr}").unlink(missing_ok=True)
            pr_helpers.remove_body_request(description_request)
            # A rejected body keeps the prior text; still apply a valid TASK: request.
            if not has_request or description_rc:
                rc, task_changed, log = pr_helpers.apply_task(pr, want, self.ctx)
                if log:
                    notify.log(log)
                if rc != 0 and (rc != 2 or description_rc != 2):
                    reason = "fix_task_write_failed" if rc == 1 else "fix_task_invalid"
                    if rc == 1:
                        return self.handoff(pr, reason, f"PR #{pr}: task completion change failed ({fixer})", url)
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
        if self.head(wt) == head:
            rc, checks_changed = self.recheck_without_commit(pr, "", url, description_changed)
            if rc != 0:
                return rc
            if description_rc != 0:
                self.description_discarded(pr, fixer, description_reason)
            if not task_changed and not checks_changed and not description_changed:
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
        return 0

    # --- a Cursor fixer ---
    def clear_cursor_request(self, pr, *, keep_base=False):
        for name in ("pr-body-base", "cursor-fix-task"):
            Path(self.state, f"{name}-{pr}").unlink(missing_ok=True)
        pr_helpers.remove_body_request(Path(self.ctx, f"pr-{pr}-body.md"))
        if not keep_base:
            Path(self.state, f"cursor-fix-base-{pr}").unlink(missing_ok=True)

    def cursor_fix(self, pr, wt, source, feedback, head="", *, owner_triggered=False):
        agent_head = self.head(wt)
        url = f"https://github.com/{self.repo}/pull/{pr}"
        branch = git(wt, "branch", "--show-current") or ""
        base_file = Path(self.state, f"cursor-fix-base-{pr}")
        baseline_file = Path(self.state, f"pr-body-base-{pr}")
        task_file = Path(self.state, f"cursor-fix-task-{pr}")
        if base_file.is_file():
            head = base_file.read_text().split("\n")[0]
        else:
            head = head or self.head(wt)
            base_file.write_text(head + "\n")
        request = Path(self.ctx, f"pr-{pr}-body.md")
        if task_file.is_file():
            want, _, feedback_digest = task_file.read_text().partition("\n")
            self.push_feedback = feedback_digest.strip() or self.capture_push_feedback(pr)
        elif baseline_file.is_file() and request.is_file():
            # Legacy pending body requests have no saved TASK intent.
            want = ""
            self.push_feedback = self.capture_push_feedback(pr)
        else:
            self.push_feedback = self.capture_push_feedback(pr)
            feedback_file = Path(self.ctx, f"pr-{pr}-cursor-fix-{time.strftime('%Y%m%d-%H%M%S')}.md")
            feedback_file.write_text(f"{feedback}\n\n<pr-description>\n{pr_helpers.body(pr) or ''}\n</pr-description>\n")
            self.fetch_main(wt)
            shots = Path(self.ctx, f"pr-{pr}-shots")
            session.run_process(["rm", "-rf", str(shots)])
            shots.mkdir(parents=True)
            self.clear_cursor_request(pr, keep_base=True)
            Path(self.ctx, f"pr-{pr}-fix-checks.json").unlink(missing_ok=True)
            out = self.agent("cursor", "fix", self.render(
                "cursor-fix-prompt.md", PR=str(pr), ISSUE=self.env.runtime.issue or "unknown", GH_REPO=self.repo,
                WORKTREE=str(wt), BRANCH=branch, SOURCE=source, FEEDBACK_FILE=str(feedback_file),
                FIX_SCOPE=fix_scope("cursor", owner_triggered),
                SHOTS_DIR=str(shots), PR_BODY_FILE=str(request)), f"pr-{pr}-cursor-fix", wt)
            outcome, reply_raw = out["outcome"], out["reply"] or ""
            self.reply = reply_raw
            want = pr_helpers.task_wanted(reply_raw)
            reply = "\n".join(line for line in strip_markers(reply_raw).split("\n") if not line.startswith("RESULT: ")).rstrip("\n")
            if reply_raw:
                reply = pr_helpers.split_checks(pr, reply, self.ctx).rstrip("\n")
            if reply.strip():
                marker = ("<!-- baton-fix by=cursor outcome=needs-info -->"
                          if outcome.startswith("RESULT: needs-info")
                          else "<!-- baton-fix by=cursor -->")
                self.comment(pr, f"{marker}\n{reply[:REPLY_CAP]}", f"PR #{pr}: posting Cursor's reply failed")
            if not outcome.startswith("RESULT: fixed"):
                if self.head(wt) != head:
                    if not self.record(pr, "cursor", wt, head, out["model"] or "unknown", ""):
                        return 1
                    return self.push_partial_fix(pr, wt, branch, url, outcome, "cursor", source, feedback, head)
                if outcome.startswith("RESULT: needs-info"):
                    return self.handoff(pr, "fix_needs_info", f"Cursor PR #{pr} has questions", url)
                if not self.record(pr, "cursor", wt, agent_head, "", ""):
                    return 1
                action, alt, _ = self.recover(pr, "fix_run_failed", f"Cursor PR #{pr} fix failed", f"{outcome} {url}",
                                              "cursor", wt=wt, head=head)
                if action == "handoff":
                    return 1
                return self.dispatch_fixer(pr, wt, alt, source, feedback, head)
            if request.exists() or request.is_symlink() or want:
                # Publish the fixed attempt after saving its checks, before any GitHub edit.
                temp = task_file.with_suffix(".tmp")
                temp.write_text(f"{want}\n{self.push_feedback or ''}\n")
                temp.replace(task_file)
        has_request = request.exists() or request.is_symlink()
        description_rc, body_changed, description_reason, task_changed = pr_helpers.apply_body(
            pr, str(request), self.state, self.ctx, completion=want or None)
        if description_rc == 1:
            notify.log(f"PR #{pr}: description update deferred after GitHub failure")
            return 75
        discarded = description_rc == 2
        if discarded:
            pr_helpers.remove_body_request(request)
            has_request = False
        if not has_request or description_rc:
            rc, task_changed, log = pr_helpers.apply_task(pr, want, self.ctx, baseline_file)
            if log:
                notify.log(log)
            if rc == 1:
                if discarded:
                    self.description_discarded(pr, "cursor", description_reason)
                notify.log(f"PR #{pr}: description update deferred after GitHub failure")
                return 75
            if rc != 0 and (rc != 2 or description_rc != 2):
                reason = "fix_task_write_failed" if rc == 1 else "fix_task_invalid"
                if rc == 1:
                    return self.handoff(pr, reason, f"PR #{pr}: task completion change failed (cursor)", url)
                start = agent_head if self.attempt_authors else head
                if not self.record(pr, "cursor", wt, start, "", ""):
                    return 1
                action, alt, _ = self.recover(pr, reason, f"PR #{pr}: task completion change failed (cursor)",
                                              f"Invalid task relation from cursor. {url}", "cursor", wt=wt, head=head,
                                              validation_error=log)
                if action == "handoff":
                    return 1
                self.clear_cursor_request(pr, keep_base=True)
                return self.dispatch_fixer(pr, wt, alt, source, correction(feedback, log), head)
        moved = self.head(wt) != head
        recorded_before_retry = False
        if not moved:
            rc, checks_changed = self.recheck_without_commit(pr, "Cursor ", url, body_changed)
            if rc != 0:
                return rc
            if discarded:
                self.description_discarded(pr, "cursor", description_reason)
            if not (body_changed or task_changed or checks_changed):
                action, alt, _ = self.recover(pr, "fix_no_commit", f"Cursor PR #{pr} needs you",
                                                f"Cursor's fix run made no commit. {url}", "cursor", wt=wt, head=head)
                if action == "handoff":
                    self.clear_cursor_request(pr)
                    return 1
                self.clear_cursor_request(pr, keep_base=True)
                return self.dispatch_fixer(pr, wt, alt, source, feedback, head)
        else:
            rc, failure, rounds = self.verify(wt, "origin/main", "cursor", "pr", pr)
            if rc != 0:
                start = agent_head if self.attempt_authors else head
                if not self.record(pr, "cursor", wt, start, "", ""):
                    return 1
                recorded_before_retry = True
                action, alt, remaining = self.recover(pr, "fix_verify_failed",
                                                      f"Cursor PR #{pr}: local CI verification failed",
                                                      recovery.verify_status(repairs_used=rounds, max_repairs=self.max_verify,
                                                                             failure=failure), "cursor", wt=wt,
                                                      repairs_used=rounds, head=head,
                                                      before_labels=lambda: self.verify_failed(pr, failure, rounds))
                if action == "handoff":
                    return 1
                vendor = alt if alt != "cursor" else "cursor"
                rc, failure, more = self.verify_retry(wt, self.verified_base, vendor, "pr", pr, repairs=remaining,
                                                repairs_before=rounds)
                rounds = more
                if rc != 0:
                    self.verify_failed(pr, failure, rounds)
                    return self.handoff(pr, "fix_verify_failed", f"Cursor PR #{pr}: local CI verification failed", url)
        try:
            vendors.record_author(self.state, pr, "cursor")
        except OSError:
            return 1
        if not recorded_before_retry:
            start = agent_head if self.attempt_authors else head
            self.add_author("cursor")
            vendors.record_contrib(self.state, pr, "cursor", str(wt), start)
        if moved:
            return self.push_with_retry(
                pr, wt, branch, next_step="done", url=url, description_rc=2 if discarded else 0,
                description_reason=description_reason, who="Cursor ", fixer="cursor",
                clear_cursor=True)
        self.clear_cursor_request(pr)
        episode = self.env.runtime.issue or str(pr)
        recovery.clear(self.env, str(episode), str(pr))
        return 0


    def dispatch_fixer(self, pr, wt, fixer, source, feedback, head):
        self.last_fixer = fixer
        scope = {"owner_triggered": self.owner_triggered}
        with self.dispatching(pr, wt):
            if fixer != "cursor":
                return self.fix_pr(pr, wt, fixer, source, feedback, head, **scope)
            rc = self.cursor_fix(pr, wt, source, feedback, head, **scope)
            if rc == 75:   # a short block reported by return value, e.g. a description update
                recovery.settle(self.env, self.env.runtime.issue or pr, pr, lambda: self.retry_binding(wt, pr))
        self.hooks.fix("cursor", self.round or 0, self.reply)
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
    action = "Handle every Must fix point. Fixing nits is optional." if fixer == "cursor" else "Handle every point."
    return f"{action} All review nits are in scope for the reply."


def fix_pr(runner, pr, wt, fixer, source, feedback, head="", *, owner_triggered=False):
    """Resume a saved fix step or dispatch the requested adapter."""
    from . import advisor

    runner.owner_triggered = owner_triggered
    runner.retry_context = {"fix_feedback": recovery.feedback_digest(feedback), "fix_source": source}
    # Reconcile a saved push before a worker grant can dispatch or resume verification.
    waiting = runner.waiting_push(pr)
    if waiting == "unreadable":
        return 1
    if waiting is not None:
        binding = waiting["continuation"]
        runner.last_fixer = binding["fixer"]
        return runner.push_with_retry(pr, wt, binding["ref"], next_step=binding["next"],
                                      url=f"https://github.com/{runner.repo}/pull/{pr}", pending=waiting)
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
        runner.last_fixer = fixer
        rc, failure, rounds = runner.verify_retry(wt, budget.get("base_sha") or "origin/main", fixer, "pr", pr,
                                                  repairs=budget["verify_repairs"],
                                            repairs_before=budget["repairs_used"])
        if rc:
            runner.verify_failed(pr, failure, rounds)
            return runner.handoff(pr, "fix_verify_failed", f"PR #{pr}: local CI verification failed", failure)
        rc = runner.finish_verified(pr, wt, fixer, prefix="Cursor " if fixer == "cursor" else "")
        runner.clear_cursor_request(pr)
        recovery.clear(runner.env, runner.env.runtime.issue or pr, pr)
        return rc
    if record["state"] == "progressed" and fixer != "cursor" and runner.head(wt) != head:
        return runner.finish_progressed(pr, wt, fixer, head, budget)
    if record["reason"] == "fix_task_invalid":
        feedback = correction(feedback, budget.get("validation_error", ""))
    if record["state"] != "progressed":   # a completed Cursor step reconciles from its saved request
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
