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
import subprocess
import sys
import time

from . import agents, dispatch, e2e_gate, hooks as telemetry, notify, pick, pr as pr_helpers, pr_contract, prompts, recovery, vendors
from .common import Deferred, Exhausted, child_env, git, push, strip_markers

REPLY_CAP = 60000
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
        self.last_fixer = ""
        self.attempt_authors = []
        self.retry_context = {}
        self.retry_start = None
        self.required_retry_label = ""
        self.verification_progress = None
        self.hooks = hooks or telemetry.from_env(env, issue=env.runtime.issue, pr=pr, round=round)

    # --- the owner handoff every gate shares ---
    def handoff(self, pr, reason, title, text, **recovery_kw):
        """Label the PR for the owner, record why, say so on Telegram; rc 1 on handoff."""
        from . import handoff as owner_handoff
        episode = self.env.runtime.issue or str(pr)
        result = owner_handoff.perform(
            self.env, reason, kind="pr", number=pr,
            title=title, text=text, hooks=self.hooks, item=str(pr), episode=str(episode), **recovery_kw)
        if result.handed_off:
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
        if not push(wt, ref, signoff=self.env.repo.signoff):
            return self.handoff(pr, "fix_push_failed", f"PR #{pr}: pushing fix failed", url)
        return self.refresh_checks(pr, prefix, url)

    def add_author(self, vendor):
        if vendor not in self.attempt_authors:
            self.attempt_authors.append(vendor)

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
        rc, _, log = pr_helpers.apply_checks(pr, self.ctx)
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
    def fix_pr(self, pr, wt, fixer, source, feedback, head=""):
        self.last_fixer = fixer
        url = f"https://github.com/{self.repo}/pull/{pr}"
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
                                                   GH_REPO=self.repo, WORKTREE=str(wt), SOURCE=source, FEEDBACK=feedback,
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
                self.comment(pr, f"<!-- baton-fix by={fixer} -->\n{rest.rstrip(chr(10))}")
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
        pushed = not outcome.startswith("RESULT: fixed") and self.head(wt) != head
        if pushed:
            # Anything but a fix goes to the owner, who must see the partial work ("commit
            # what is done" in the fix prompts): push it unverified. Only a fix is gated below.
            if not self.record(pr, fixer, wt, agent_head, model, cursor_base):
                return 1
            if not push(wt, "HEAD", signoff=self.env.repo.signoff):
                return self.handoff(pr, "fix_push_failed", f"PR #{pr}: pushing fix failed", url)
        if not outcome.startswith("RESULT: fixed"):
            if outcome.startswith("RESULT: needs-info"):
                return self.handoff(pr, "fix_needs_info", f"PR #{pr} has questions", url)
            action, fixer, _ = self.recover(pr, "fix_run_failed", f"PR #{pr} fix failed ({fixer})",
                                              f"{outcome} {url}", fixer, wt=wt, head=head, pushed=pushed)
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
            rc = self.finish_verified(pr, wt, fixer)
            if rc != 0:
                return rc
            if description_rc != 0:
                self.description_discarded(pr, fixer, description_reason)
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

    def cursor_fix(self, pr, wt, source, feedback, head=""):
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
            want = task_file.read_text().split("\n")[0]
        elif baseline_file.is_file() and request.is_file():
            # Legacy pending body requests have no saved TASK intent.
            want = ""
        else:
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
                SHOTS_DIR=str(shots), PR_BODY_FILE=str(request)), f"pr-{pr}-cursor-fix", wt)
            outcome, reply_raw = out["outcome"], out["reply"] or ""
            self.reply = reply_raw
            want = pr_helpers.task_wanted(reply_raw)
            reply = "\n".join(line for line in strip_markers(reply_raw).split("\n") if not line.startswith("RESULT: ")).rstrip("\n")
            if reply_raw:
                reply = pr_helpers.split_checks(pr, reply, self.ctx).rstrip("\n")
            if reply.strip():
                self.comment(pr, f"<!-- baton-fix by=cursor -->\n{reply[:REPLY_CAP]}", f"PR #{pr}: posting Cursor's reply failed")
            if not outcome.startswith("RESULT: fixed"):
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
                temp.write_text(want + "\n")
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
            if not push(wt, branch, signoff=self.env.repo.signoff):
                return self.handoff(pr, "fix_push_failed", f"Cursor PR #{pr}: push failed", url)
            if self.refresh_checks(pr, "Cursor ", url):
                return 1
        if discarded and moved:
            self.description_discarded(pr, "cursor", description_reason)
        self.clear_cursor_request(pr)
        episode = self.env.runtime.issue or str(pr)
        recovery.clear(self.env, str(episode), str(pr))
        return 0


    def dispatch_fixer(self, pr, wt, fixer, source, feedback, head):
        self.last_fixer = fixer
        with self.dispatching(pr, wt):
            if fixer != "cursor":
                return self.fix_pr(pr, wt, fixer, source, feedback, head)
            rc = self.cursor_fix(pr, wt, source, feedback, head)
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


def fix_pr(runner, pr, wt, fixer, source, feedback, head=""):
    """Resume a saved fix step or dispatch the requested adapter."""
    from . import advisor

    runner.retry_context = {"fix_feedback": recovery.feedback_digest(feedback), "fix_source": source}
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
        rc = fix_pr(runner, args.pr, args.wt, args.fixer, args.source, feedback, args.head)
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
