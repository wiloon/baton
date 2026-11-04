"""Fix runs for the bash runner (migration slice 4b, #275): `fix_pr`, `cursor_fix` and
`verify_with_fixes` of run.sh.

A fixer addresses feedback on a PR in its worktree; the runner posts the reply, applies the
description and task requests, verifies the commits (with repair runs), records who wrote
what, pushes, and refreshes the Manual checks. Every gate that stops hands the PR to the
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

from . import dispatch, e2e_gate, hooks as telemetry, notify, pr as pr_helpers, pr_contract, prompts, vendors
from .common import Deferred, child_env, git, strip_markers

REPLY_CAP = 60000


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
        self.hooks = hooks or telemetry.from_env(env, issue=env.runtime.issue, pr=pr, round=round)

    # --- the owner handoff every gate shares ---
    def label_human(self, pr):
        pr_helpers.gh("pr", "edit", str(pr), "-R", self.repo, "--add-label", "ready-for-human")

    def handoff(self, pr, reason, title, text):
        """Label the PR for the owner, record why, say so on Telegram; always rc 1."""
        self.label_human(pr)
        if reason:
            self.hooks.task("handoff", reason, "--reason", reason)
        notify.notify(title, text)
        return 1

    def comment(self, pr, body, failed=""):
        if pr_helpers.gh("pr", "comment", str(pr), "-R", self.repo, "--body", body).returncode != 0 and failed:
            notify.log(failed)

    def render(self, template, **pairs):
        return prompts.render(self.self_dir / template, list(pairs.items()), env=self.env)

    def agent(self, vendor, role, prompt, tag, wt, output=""):
        """One agent run; Deferred when the vendor cannot run now."""
        out = dispatch.dispatch(vendor, role, prompt, tag, str(wt), str(self.self_dir / "settings.json"), output,
                                self.env, self.hooks)
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

    def verify(self, wt, base, vendor, kind, number):
        """(rc, failure, rounds): rc 0 passed, 1 failed or refused, 75 deferred (Deferred)."""
        try:
            gate = self.e2e_gate(kind, number)
        except ValueError as error:
            failure = f"E2E gate: {error}"
            notify.log(f"{kind} #{number}: {failure}")
            return 1, failure, 0
        failure_file = Path(self.ctx, f"{kind}-{number}-verify-failure.txt")
        reply_file = Path(self.ctx, f"{kind}-{number}-verify-fix.md")
        attempt, failure = 0, ""

        def dirty():
            return bool(git(wt, "status", "--porcelain"))

        def event(result, started):
            self.hooks.task("verify", f"{kind}:{number}:{git(wt, 'rev-parse', 'HEAD')}:{result}",
                            "--ci-result", result, "--duration-ms", int(time.monotonic() - started) * 1000)
        while True:
            if dirty():
                failure = "Verification refused: uncommitted or untracked files remain."
                notify.log(f"{kind} #{number}: {failure}")
                return 1, failure, attempt
            started = time.monotonic()
            repo = self.env.repo
            done = session.run_process([str(self.self_dir / "verify.sh"), str(wt), base], text=True, capture_output=True,
                                  env=child_env(VERIFY_TAG=f"{kind}-{number}-verify-{attempt}", LOGS=str(self.env.dirs.logs),
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
                    return 0, failure, attempt
                failure = "CI checks passed but left uncommitted or untracked files."
            failure_file.write_text(failure + "\n")
            notify.log(f"{kind} #{number}: verify failed (attempt {attempt} of {self.max_verify})")
            event("failure", started)
            if attempt >= self.max_verify:
                return 1, failure, attempt
            attempt += 1
            head = git(wt, "rev-parse", "HEAD")
            reply_file.unlink(missing_ok=True)
            out = self.agent(vendor, "fix", self.render("verify-fix-prompt.md", VENDOR=vendor, WORKTREE=str(wt), BASE=base,
                                                        FAILURE_FILE=str(failure_file), REPLY_FILE=str(reply_file)),
                             f"{kind}-{number}-verify-fix-{attempt}", wt, str(reply_file))
            if not out["outcome"].startswith("RESULT: fixed") or dirty():
                notify.log(f"{kind} #{number}: {vendor} verification fix failed or left the tree dirty")
                return 1, failure, attempt
            if git(wt, "rev-parse", "HEAD") == head:   # fixed outside the repo: verify.sh still judges it
                notify.log(f"{kind} #{number}: {vendor} verification fix made no commit; checking again")
                continue
            self.hooks.task("ci_fix", f"{kind}:{number}:{attempt}:{git(wt, 'rev-parse', 'HEAD')}",
                            "--agent", vendor, "--round", attempt)

    def verify_failed(self, pr, failure, rounds):
        self.comment(pr, f"<!-- baton-verify-failed -->\nLocal CI verification failed before push after {rounds} of "
                         f"{self.max_verify} fix rounds.\n\n{failure[:REPLY_CAP]}", f"PR #{pr}: posting verification failure failed")

    # --- shared tail: after the commits are verified ---
    def record(self, pr, fixer, wt, head, model):
        """Who wrote what (author, author model, lines); False when the records failed."""
        try:
            if fixer == "codex":
                vendors.record_author_model(self.state, pr, fixer, model)
            vendors.record_author(self.state, pr, fixer)
            vendors.record_contrib(self.state, pr, fixer, str(wt), head)
        except OSError:
            return False
        return True

    def refresh_checks(self, pr, who, url):
        rc, _, log = pr_helpers.apply_checks(pr, self.ctx)
        if log:
            notify.log(log)
        if rc != 0:
            return self.handoff(pr, "", f"{who}PR #{pr}: Manual checks not refreshed",
                                f"The fix is pushed but the description may still list stale checks. {url}")
        return 0

    def recheck_without_commit(self, pr, who, url, description_changed=False):
        """(rc, changed) for a fix with no commit: a re-check the fixer gave still reaches the
        description, and the next review checks it. No re-check leaves the description alone,
        unless the fixer rewrote it: its own `## Manual checks` must not reach the merge gate."""
        if not description_changed and not pr_helpers.fixer_gave_checks(self.ctx, pr):
            return 0, False
        rc, changed, log = pr_helpers.apply_checks(pr, self.ctx)
        if log:
            notify.log(log)
        if rc != 0:
            return self.handoff(pr, "", f"{who}PR #{pr}: Manual checks not refreshed",
                                f"The description may still list stale checks. {url}"), False
        return 0, changed

    def head(self, wt):
        return git(wt, "rev-parse", "HEAD")

    def fetch_main(self, wt):
        # The fixer has no network, so its `ci:changed BASE=origin/main` would diff against a stale main.
        if session.run_process(["git", "-C", str(wt), "fetch", "-q", "origin", "main"]).returncode:
            notify.log(f"fetching origin/main for {wt} failed; the fixer uses the local copy")

    def description_not_applied(self, pr, fixer, url):
        return self.handoff(pr, "", f"PR #{pr}: description request not applied ({fixer})",
                            f"It was invalid or GitHub refused it; the commits, if any, are pushed. {url}")

    # --- a Claude or Codex fixer ---
    def fix_pr(self, pr, wt, fixer, source, feedback, head=""):
        url = f"https://github.com/{self.repo}/pull/{pr}"
        fix_file = Path(f"/tmp/baton-fix-{pr}.md")
        head = head or self.head(wt)
        self.fetch_main(wt)
        description_request = Path(f"{fix_file}.body")
        fix_file.unlink(missing_ok=True)
        description_request.unlink(missing_ok=True)
        Path(self.ctx, f"pr-{pr}-fix-checks.json").unlink(missing_ok=True)
        template = f"codex-fix-prompt.md" if fixer == "codex" else "fix-prompt.md"
        description = Path(self.ctx, f"pr-{pr}-description.md")
        if fixer == "codex":  # It has no GitHub access: the runner reads and writes the description for it.
            description.write_text(pr_helpers.body(pr) or "")
        out = self.agent(fixer, "fix", self.render(template, PR=str(pr), ISSUE=self.env.runtime.issue or "unknown",
                                                   GH_REPO=self.repo, WORKTREE=str(wt), SOURCE=source, FEEDBACK=feedback,
                                                   PR_DESCRIPTION_FILE=str(description)),
                         f"pr-{pr}-fix{f'-{self.round}' if self.round else ''}", wt, str(fix_file))
        outcome, model = out["outcome"], out["model"] or "unknown"
        # A failed Codex run has no reply worth posting; its commits still reach the owner below.
        if not (fixer == "codex" and outcome.startswith("RESULT: failed")):
            if fix_file.is_file() and fix_file.stat().st_size:
                try:
                    rest = pr_helpers.split_checks(pr, strip_markers(fix_file.read_text()), self.ctx)
                except Exception:
                    rest = strip_markers(fix_file.read_text())
                self.comment(pr, f"<!-- baton-fix by={fixer} -->\n{rest.rstrip(chr(10))}")
            self.hooks.fix(fixer, self.round or 0, out["reply"], str(fix_file))
        task_changed, description_rc, description_changed = False, 0, False
        if outcome.startswith("RESULT: fixed"):
            description_rc, description_changed = pr_helpers.apply_body(pr, str(description_request), self.state, self.ctx)
            Path(self.state, f"pr-body-base-{pr}").unlink(missing_ok=True)
            description_request.unlink(missing_ok=True)
            rc, task_changed, log = pr_helpers.apply_task(pr, out["reply"], self.ctx)
            if log:
                notify.log(log)
            if rc != 0:
                return self.handoff(pr, "", f"PR #{pr}: task completion change failed ({fixer})", url)
        elif self.head(wt) != head:
            # Anything but a fix goes to the owner, who must see the partial work ("commit
            # what is done" in the fix prompts): push it unverified. Only a fix is gated below.
            if not self.record(pr, fixer, wt, head, model):
                return 1
            if session.run_process(["git", "-C", str(wt), "push", "-q", "origin", "HEAD"]).returncode:
                outcome = "RESULT: failed pushing fix"
        if not outcome.startswith("RESULT: fixed"):
            if outcome.startswith("RESULT: needs-info"):
                return self.handoff(pr, "needs_info", f"PR #{pr} has questions", url)
            return self.handoff(pr, "fix_failed", f"PR #{pr} fix failed ({fixer})", f"{outcome} {url}")
        if self.head(wt) == head:
            rc, checks_changed = self.recheck_without_commit(pr, "", url, description_changed)
            if rc != 0:
                return rc
            if description_rc != 0:
                return self.description_not_applied(pr, fixer, url)
            if not task_changed and not checks_changed and not description_changed:
                return self.handoff(pr, "no_commit", f"PR #{pr} needs you", f"The fix run made no commit; stopping the loop. {url}")
        if self.head(wt) != head:
            rc, failure, rounds = self.verify(wt, "origin/main", fixer, "pr", pr)
            if rc != 0:
                self.verify_failed(pr, failure, rounds)
                return self.handoff(pr, "verify_failed", f"PR #{pr}: local CI verification failed", url)
            if not self.record(pr, fixer, wt, head, model if rounds == 0 else "unknown"):
                return 1
            if session.run_process(["git", "-C", str(wt), "push", "-q", "origin", "HEAD"]).returncode:
                return self.handoff(pr, "push_failed", f"PR #{pr}: pushing fix failed", url)
            rc = self.refresh_checks(pr, "", url)
            if rc != 0:
                return rc
            if description_rc != 0:
                return self.description_not_applied(pr, fixer, url)
        return 0

    # --- a Cursor fixer ---
    def cursor_fix(self, pr, wt, source, feedback):
        url = f"https://github.com/{self.repo}/pull/{pr}"
        branch = git(wt, "branch", "--show-current") or ""
        base_file = Path(self.state, f"cursor-fix-base-{pr}")
        if base_file.is_file():
            head = base_file.read_text().split("\n")[0]
        else:
            head = self.head(wt)
            base_file.write_text(head + "\n")
        feedback_file = Path(self.ctx, f"pr-{pr}-cursor-fix-{time.strftime('%Y%m%d-%H%M%S')}.md")
        feedback_file.write_text(f"{feedback}\n\n<pr-description>\n{pr_helpers.body(pr) or ''}\n</pr-description>\n")
        request = Path(self.ctx, f"pr-{pr}-body.md")
        reply_raw = ""
        if request.is_file() and Path(self.state, f"pr-body-base-{pr}").is_file():
            # Resume a validated request after a transient GitHub failure without another agent run.
            outcome = "RESULT: fixed"
        else:
            self.fetch_main(wt)
            shots = Path(self.ctx, f"pr-{pr}-shots")
            session.run_process(["rm", "-rf", str(shots)])
            shots.mkdir(parents=True)
            request.unlink(missing_ok=True)
            Path(self.ctx, f"pr-{pr}-fix-checks.json").unlink(missing_ok=True)
            out = self.agent("cursor", "fix", self.render(
                "cursor-fix-prompt.md", PR=str(pr), ISSUE=self.env.runtime.issue or "unknown", GH_REPO=self.repo,
                WORKTREE=str(wt), BRANCH=branch, SOURCE=source, FEEDBACK_FILE=str(feedback_file),
                SHOTS_DIR=str(shots), PR_BODY_FILE=str(request)), f"pr-{pr}-cursor-fix", wt)
            outcome, reply_raw = out["outcome"], out["reply"]
            self.reply = reply_raw
        reply = "\n".join(line for line in strip_markers(reply_raw).split("\n") if not line.startswith("RESULT: ")).rstrip("\n")
        # A resumed fix has no reply; its re-check was kept on the first pass.
        if reply_raw:
            try:
                reply = pr_helpers.split_checks(pr, reply, self.ctx).rstrip("\n")
            except Exception:
                pass
        if reply.strip():
            self.comment(pr, f"<!-- baton-fix by=cursor -->\n{reply[:REPLY_CAP]}", f"PR #{pr}: posting Cursor's reply failed")
        if not outcome.startswith("RESULT: fixed"):
            if outcome.startswith("RESULT: needs-info"):
                return self.handoff(pr, "needs_info", f"Cursor PR #{pr} has questions", url)
            return self.handoff(pr, "fix_failed", f"Cursor PR #{pr} fix failed", f"{outcome} {url}")
        rc, body_changed = pr_helpers.apply_body(pr, str(request), self.state, self.ctx)
        task_changed = False
        if rc == 0:
            rc, task_changed, log = pr_helpers.apply_task(pr, reply_raw, self.ctx)
            if log:
                notify.log(log)
        if rc == 1:
            notify.log(f"PR #{pr}: description update deferred after GitHub failure")
            return 75
        if rc != 0:
            return self.handoff(pr, "", f"Cursor PR #{pr}: invalid description request", url)
        moved = self.head(wt) != head
        if not moved:
            rc, checks_changed = self.recheck_without_commit(pr, "Cursor ", url)
            if rc != 0:
                return rc
            if not body_changed and not task_changed and not checks_changed:
                return self.handoff(pr, "no_commit", f"Cursor PR #{pr} needs you", f"Cursor's fix run made no commit. {url}")
        if moved:
            rc, failure, rounds = self.verify(wt, "origin/main", "cursor", "pr", pr)
            if rc != 0:
                self.verify_failed(pr, failure, rounds)
                return self.handoff(pr, "verify_failed", f"Cursor PR #{pr}: local CI verification failed", url)
        try:
            vendors.record_author(self.state, pr, "cursor")
        except OSError:
            return 1
        vendors.record_contrib(self.state, pr, "cursor", str(wt), head)
        if moved:
            if session.run_process(["git", "-C", str(wt), "push", "-q", "origin", branch]).returncode:
                return self.handoff(pr, "push_failed", f"Cursor PR #{pr}: push failed", url)
            if self.refresh_checks(pr, "Cursor ", url):
                return 1
        for path in (base_file, Path(self.state, f"pr-body-base-{pr}"), request):
            path.unlink(missing_ok=True)
        return 0


def fix_pr(runner, pr, wt, fixer, source, feedback, head=""):
    """The fix run for any fixer, with its fix telemetry; rc 0 done, 1 handed off, 75 deferred
    (Deferred when a vendor cannot run now)."""
    if fixer == "cursor":
        rc = runner.cursor_fix(pr, wt, source, feedback)
        runner.hooks.fix("cursor", runner.round or 0, runner.reply)
        return rc
    return runner.fix_pr(pr, wt, fixer, source, feedback, head)


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
        return result(fix_pr(runner, args.pr, args.wt, args.fixer, args.source, feedback, args.head))
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
