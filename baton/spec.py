"""The spec review loop for the bash runner (migration slice 4d, #282): `spec_loop` and its
helpers of run.sh.

Claude writes issues and ADRs with the owner; a spec reviewer checks them first, read-only,
against main and docs/agents/principles.md. On "changes" a reviser resolves each point: it
edits the issue body (the runner applies it), commits ADR edits on docs/spec-issue-N (the
runner pushes them as a docs PR, merged once the reviewer accepts), and escalates what the
principles don't settle. Comments (the runner posts them; the markers are read back):
    <!-- baton-spec-review round=R by=V -->         the reviewer's review
    <!-- baton-spec-reply round=R [human] by=V -->  the reviser's reply
    <!-- baton-verdict: accept by=V -->             the accepted spec

The reviewer is never the vendor that wrote the latest revision, and the reviser never the
latest reviewer unless no other vendor can run, and then on a different model. `spec-paused-N` marks a spec run in flight; bash removes it unless the item
ends deferred (75), and a paused revision resumes with its edits and commits.

    loop --issue N   prints JSON for the bash caller, which keeps `defer_vendor`:
                     rc 0 accepted, 1 the owner or a failure took over, 75 deferred
    state N          "<kind> <round>" of the latest spec comment, or "none 0"
    plan N           the body of the latest accept/revise verdict comment
"""

from . import session
import argparse
import difflib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from . import advisor, agents, checks as check_section, claude_cli, dispatch, e2e_gate, fix, hooks as telemetry, merge, notify, pick, pr as pr_helpers, prompts, review, vendors
from .common import Deferred, changed, git, permission_denials, push, restore, strip_markers

REPLY_CAP = 60000
DENIED_NOTE = ("\n\nA previous attempt at this round ended with `RESULT: failed` after this command was denied: "
               "`{command}`. Only that command was refused. Finish the round with the other allowed commands "
               "and the Read, Edit and Write tools; the worktree keeps whatever that attempt left.\n")
VERDICT = re.compile(r"<!-- baton-verdict: (accept|revise)( by=(cursor|codex|claude))? -->")
STATE_RE = re.compile(r"<!-- baton-spec-(?P<k>review|reply) round=(?P<r>[0-9]+)(?P<h> human)?")
KIND_RE = r"<!-- baton-spec-%s round=[0-9]+( human)? by=(?P<v>[a-z]+) -->"
VERDICT_LINE = re.compile(r"^(SPEC|RESULT): ")
PENDING_ACCEPT = re.compile(
    r"^<!-- baton-spec-accept-pending pr=(?P<pr>\d+) -->\n(?P<verdict>.*)", re.DOTALL)


@session.scoped
def comments(env, issue):
    """The issue's comments, or None when gh fails or the payload is unreadable."""
    env = session.resolve(env)
    result = pr_helpers.gh("issue", "view", str(issue), "-R", env.repo.gh_repo, "--json", "comments")
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout).get("comments") or []
    except (ValueError, TypeError):
        return None


@session.scoped
def owner_bodies(env, issue):
    env = session.resolve(env)
    return [c.get("body") or "" for c in comments(env, issue) or ()
            if (c.get("author") or {}).get("login") == env.owner]


def pending_accept_in(listed, owner, login_re=None):
    """The latest valid pending-acceptance comment by OWNER that is not yet superseded by a
    later owner/cursor verdict with the same saved body: {pr, verdict, index}, or None."""
    found = None
    for i, c in enumerate(listed or ()):
        if (c.get("author") or {}).get("login") != owner:
            continue
        match = PENDING_ACCEPT.match(c.get("body") or "")
        if match and VERDICT.match(match["verdict"]):
            found = {"pr": match["pr"], "verdict": match["verdict"], "index": i}
    if not found:
        return None
    login_re = login_re or r"^(app/cursor|cursor(\[bot\])?)$"
    for i, c in enumerate(listed or ()):
        if i <= found["index"]:
            continue
        login = (c.get("author") or {}).get("login") or ""
        body = c.get("body") or ""
        if (login == owner or re.search(login_re, login)) and body == found["verdict"]:
            return None
    return found


@session.scoped
def accept_pending(env, issue):
    """Pending acceptance for ISSUE when comments are readable; None when absent, superseded,
    or unreadable."""
    env = session.resolve(env)
    listed = comments(env, issue)
    if listed is None:
        return None
    login_re = env.config.scheduling.cursor_login_re or r"^(app/cursor|cursor(\[bot\])?)$"
    return pending_accept_in(listed, env.owner, login_re)


@session.scoped
def cursor_plan(env, issue):
    """The body of the latest accept/revise verdict comment of the owner, Cursor or an agent
    posting for Cursor; empty when there is none, or while an acceptance is still pending (#122)."""
    env = session.resolve(env)
    login_re = env.config.scheduling.cursor_login_re or r"^(app/cursor|cursor(\[bot\])?)$"
    listed = comments(env, issue) or ()
    if pending_accept_in(listed, env.owner, login_re):
        return ""
    found = []
    for c in listed:
        login = (c.get("author") or {}).get("login") or ""
        body = c.get("body") or ""
        if (login == env.owner or re.search(login_re, login)) and VERDICT.match(body):
            found.append(body)
    return found[-1] if found else ""


@session.scoped
def spec_state(env, issue):
    """(kind, round) of the latest spec comment: review, reply, reply-human or none."""
    env = session.resolve(env)
    found = [m for body in owner_bodies(env, issue) if (m := STATE_RE.match(body))]
    if not found:
        return "none", 0
    last = found[-1]
    return last["k"] + ("-human" if last["h"] else ""), int(last["r"])


def pending_role(kind):
    return "reviser" if kind in ("review", "reply-human") else "spec_reviewer"


@session.scoped
def last_by(env, issue, kind):
    """The vendor of the latest spec review (KIND review) or reply, or ''."""
    env = session.resolve(env)
    found = [m for body in owner_bodies(env, issue) if (m := re.match(KIND_RE % kind, body))]
    return found[-1]["v"] if found else ""


@session.scoped
def last_model(env, issue, kind, round, vendor=""):
    """The actual model in the latest successful run of that round, from runner.log."""
    env = session.resolve(env)
    tag = f"issue-{issue}-spec-{round}" if kind != "reply" else f"issue-{issue}-spec-reply-{round}"
    log, found, repo = Path(str(env.config.paths.logs), "runner.log"), "", notify.label(env)
    if not log.is_file():
        return ""
    for line in log.read_text(errors="replace").splitlines():
        fields = line.split()
        if (len(fields) >= 6 and fields[2:4] == [repo, "end"]
                and fields[4] in (tag, f"{tag}-rerun") and fields[5] == "rc=0"):
            first = fields[6] if len(fields) > 6 else ""
            if first.startswith("claude_model="):
                found = "" if vendor == "codex" else first.split("=", 1)[1]
            else:
                # Codex end lines put the reported model after the outcome and usage.
                found = "" if vendor == "claude" else next(
                    (field.split("=", 1)[1] for field in fields[6:] if field.startswith("model=")), "")
    return found


def reply_text(reply):
    """The agent's reply without markers and verdict lines (untrusted text)."""
    return "\n".join(line for line in strip_markers(reply or "").split("\n") if not VERDICT_LINE.match(line)).rstrip("\n")


class Spec:
    @session.scoped
    def __init__(self, env, hooks, issue):
        env = session.resolve(env)
        self.env, self.hooks, self.issue = env, hooks, str(issue)
        self.state, self.ctx, self.repo = str(env.dirs.state), str(env.dirs.ctx), env.repo.gh_repo
        self.self_dir, self.repo_dir = Path(str(env.config.paths.self_dir)), str(env.dirs.clone)
        self.url = f"https://github.com/{self.repo}/issues/{issue}"
        self.wt = Path(str(env.dirs.worktrees), f"spec-{issue}")
        self.branch = f"docs/spec-issue-{issue}"
        self.pr, self.base = "", "origin/main"

    def path(self, name):
        return Path(self.state, f"{name}-{self.issue}")

    def render(self, template, **pairs):
        return prompts.render(template, list(pairs.items()), env=self.env)

    @staticmethod
    def followup(round, reviewer, last_vendor):
        """A later round mostly re-checks earlier points, so it runs on the cheaper model. Codex
        keeps its usual one after its own revision: the two must be different models."""
        return round > 1 and not (reviewer == "codex" and last_vendor == "codex")

    @staticmethod
    def reviser_followup(round, reviser, last_vendor):
        """Claude revising Claude's review, the last resort: round 1's review ran on the usual model,
        so the revision takes the other one. A later review already ran on the followup model."""
        return round == 1 and reviser == "claude" and last_vendor == "claude"

    def agent(self, vendor, role, prompt, tag, settings, output="", followup=False):
        env = self.env.with_runtime(spec_followup=followup)
        out = dispatch.dispatch(vendor, role, prompt, tag, str(self.wt), str(self.self_dir / settings), output, env,
                                self.hooks)
        if out["rc"] == 75:
            raise Deferred(vendor, out["defer"])
        return out

    def gh(self, *args):
        return pr_helpers.gh(*args, "-R", self.repo)

    def pick(self, role, candidates, last_vendor, other_model, *, spec_followup=False):
        vendor = pick.choose(self.env, role, "agent" if role == "spec_reviewer" else "pool", "contrib", candidates,
                             exclude=last_vendor, issue=self.issue, other_model=other_model,
                             spec_followup=spec_followup)
        self.started()
        return vendor

    announce = ""

    def started(self):
        """The loop's notice, once per run and only after a vendor was picked (Deferred raises first)."""
        if self.announce:
            notify.notify(self.announce, self.url)
            self.announce = ""

    # --- a revision starts from what the last one changed ---
    def mark_base(self, round):
        """Before a revision: spec-base-N keeps "ROUND HEAD" and spec-body-N the issue body,
        both from the revision's first start (a paused revision resumes with its commits)."""
        base = self.path("spec-base")
        if base.is_file() and base.read_text().split(" ", 1)[0].strip() == str(round):
            return
        body = self.gh("issue", "view", self.issue, "--json", "body", "-q", ".body")
        if body.returncode != 0:
            return
        self.path("spec-body").write_text(body.stdout)
        base.write_text(f"{round} {git(self.wt, 'rev-parse', 'HEAD') or ''}\n")

    def round_note(self, round):
        """The prompt note for that review round, or ''."""
        try:
            base_round, sha = self.path("spec-base").read_text().split()[:2]
        except (OSError, ValueError):
            return ""
        if not (round > 1 and int(base_round) == round - 1):
            return ""
        diff_file = Path(self.ctx, f"issue-{self.issue}-spec-{round}-body.diff")
        before = self.path("spec-body").read_text().splitlines(keepends=True)
        now = self.gh("issue", "view", self.issue, "--json", "body", "-q", ".body")
        after = now.stdout.splitlines(keepends=True) if now.returncode == 0 else []
        diff = "".join(difflib.unified_diff(before, after, "before", "after"))
        diff_file.write_text(diff)
        body_note = f"the issue body diff is `{diff_file}`" if diff else "the issue body is unchanged"
        return (f"Round {base_round}'s revision: {body_note}; its ADR edits are `git diff {sha}..HEAD` (empty when none). "
                "Earlier rounds already checked the rest of the issue and ADRs against the code: start from your earlier "
                "points and these changes, and read code only to verify a resolution or text the revision added.")

    # --- small steps ---
    def handoff(self, why, reason="spec_review_run_failed", *, snapshot="", revalidate=None, observed_actions=None):
        """The owner takes over; the loop resumes once they answer on the issue and remove
        ready-for-human. Returns the shared-entry Result (#125)."""
        from . import handoff as owner_handoff
        message = [why]

        def after_labels(swapped):
            if swapped.returncode != 0:
                # Nothing runs the task now, but agent-wip would say otherwise (#46).
                message[0] += (" Swapping agent-wip for ready-for-human failed too, so the issue still looks "
                               f"in progress: fix its labels by hand.{pr_helpers.why(swapped)}")
                notify.log(f"#{self.issue}: handoff label swap failed{pr_helpers.why(swapped)}")

        return owner_handoff.perform(
            self.env, reason, kind="issue", number=self.issue, remove=("agent-wip",),
            title=f"#{self.issue}: spec needs you",
            text=lambda: f"{message[0]} {self.url}",
            hooks=self.hooks,
            snapshot=snapshot, revalidate=revalidate, observed_actions=observed_actions,
            after_labels=after_labels)

    def worker_recovery(self, reason, vendor, detail, *, role="spec_reviewer"):
        """Snapshot and offered live actions for a spec worker failure (#125)."""
        from . import handoff as owner_handoff
        entry = owner_handoff.REASONS.get(reason)
        if not entry or entry.mode != "live" or not entry.actions:
            return {}
        offered = [a for a in ("retry_step", "retry_step_other_vendor") if a in entry.actions]
        if "retry_step_other_vendor" in offered and not any(v != vendor for v in pick.all_vendors(self.env)):
            offered = [a for a in offered if a != "retry_step_other_vendor"]
        if not offered:
            return {}
        snapshot = (f"Decision point: {reason}. Repository {self.repo}, issue #{self.issue}.\n"
                    f"Failed vendor: {vendor}; role: {role}.\nOutcome: {detail}\n"
                    "Actions: retry_step = one more attempt with the same vendor; "
                    "retry_step_other_vendor = one attempt with an eligible alternate; "
                    "handoff = the owner takes the issue.\n")
        return {"snapshot": snapshot, "revalidate": lambda: True, "observed_actions": tuple(offered)}

    def recover_worker(self, why, reason, vendor, *, role="spec_reviewer"):
        """Ask the advisor at a live spec worker failure; return (action, vendor)."""
        from . import handoff as owner_handoff
        from .common import Exhausted
        result = self.handoff(why, reason, **self.worker_recovery(reason, vendor, why, role=role))
        if result.handed_off:
            return owner_handoff.HANDOFF, vendor
        if result.action == "retry_step_other_vendor":
            try:
                vendor = pick.choose(self.env, role, vendor, "contrib", pick.all_vendors(self.env),
                                     issue=self.issue, exclude=vendor)
            except Exhausted as exhausted:
                self.handoff(exhausted.reason or why, reason)
                return owner_handoff.HANDOFF, vendor
            notify.log(f"#{self.issue}: advisor granted alternate {role} {vendor}: {result.decision.reason}")
        else:
            notify.log(f"#{self.issue}: advisor granted retry of {role} ({vendor}): {result.decision.reason}")
        return result.action, vendor

    def post(self, body, failed):
        posted = self.gh("issue", "comment", self.issue, "--body", body)
        if posted.returncode == 0:
            return True
        notify.log(f"#{self.issue}: {failed}{pr_helpers.why(posted)}")
        return False

    def worktree(self):
        if self.path("spec-paused").is_file() and self.wt.is_dir():
            return True   # Retain partial edits/commits from an interrupted revision.
        if self.wt.is_dir():
            return all(session.run_process(["git", "-C", str(self.wt), *args]).returncode == 0 for args in (
                ("reset", "-q", "--hard"), ("clean", "-qfd"), ("checkout", "-q", "-B", self.branch, self.base)))
        return session.run_process(["git", "-C", self.repo_dir, "worktree", "add", "-q", "-B", self.branch, str(self.wt),
                               self.base]).returncode == 0

    @staticmethod
    def unverified(vendor, last_vendor, model, other_model):
        """A Codex run after a Codex run must be a different, known model."""
        return vendor == "codex" and last_vendor == "codex" and (
            other_model in ("", "unknown") or model in ("", "unknown") or model == other_model)

    @staticmethod
    def mode_note(label, vendor, last_vendor, model, other_model):
        if vendor == "claude" and last_vendor == "claude":
            return f"{label} mode: same-vendor-different-model (claude after claude).\n\n"
        if vendor != "codex" or last_vendor != "codex":
            return ""
        return f"{label} mode: same-vendor-different-model (codex/{model} after codex/{other_model}).\n\n"

    def record(self, vendor, model):
        if not self.pr:
            return
        if agents.ADAPTERS[vendor].RECORDS_AUTHOR_MODEL:
            vendors.record_author_model(self.state, self.pr, vendor, model or "unknown")
        vendors.record_author(self.state, self.pr, vendor)

    def finish(self):
        for name in ("spec-paused", "spec-base", "spec-body", "spec-owner-body"):
            self.path(name).unlink(missing_ok=True)

    def keep_owner_body(self):
        """Before the first spec round: spec-owner-body-N keeps the body the owner wrote."""
        if self.path("spec-owner-body").is_file():
            return
        body = self.gh("issue", "view", self.issue, "--json", "body", "-q", ".body")
        if body.returncode == 0:
            self.path("spec-owner-body").write_text(body.stdout)

    def accept_problems(self):
        """What the issue body as revised breaks, one point per line; empty when an accept may
        stand (#25): the E2E gate must still parse, and the owner's manual checks stay."""
        body = self.gh("issue", "view", self.issue, "--json", "body", "-q", ".body")
        if body.returncode != 0:
            return ["The runner could not read the issue body to check it."]
        problems = []
        try:
            e2e_gate.parse(body.stdout, self.env.repo)
        except ValueError as exc:
            problems.append(f"The `## E2E gate` section must be exactly one line, with no prose after it: {exc}")
        try:
            owner = self.path("spec-owner-body").read_text()
        except OSError:
            owner = ""
        kept = set(check_section.lines(body.stdout))
        lost = [line for line in check_section.lines(owner) if line not in kept]
        if lost:
            problems.append(f"`## {check_section.HEADING}` must keep every check the owner wrote, word for word; these are "
                            "missing (never move them to `## Before release`):\n" + "\n".join(f"   {line}" for line in lost))
        return problems

    # --- the reviewer's round ---
    def limit_snapshot(self, round, reviewer, reply, context):
        """(snapshot, revalidate) for the spec_limit decision point (#17 / #125)."""
        n, env = self.issue, self.env
        state = spec_state(env, n)
        try:
            issue_text = Path(context).read_text()
        except OSError:
            issue_text = ""
        diff = git(self.wt, "diff", "--stat", "origin/main...HEAD") or "no ADR changes"
        snapshot = (f"Decision point: spec round limit. Issue #{n} of {self.repo} used {round} spec review rounds "
                    f"and the last reviewer ({reviewer}) still asks for changes.\n"
                    "Actions: extra_round = one more revision of the issue by an agent, then one more review; "
                    "handoff = the owner takes the spec.\n\n"
                    f'<last-review round="{round}" by="{reviewer}">\n{reply}\n</last-review>\n\n'
                    f"<adr-changes>\n{diff}\n</adr-changes>\n\n{issue_text}")
        return snapshot, (lambda: spec_state(env, n) == state)

    def at_limit(self, round, max_rounds, reviewer, reply, context, why):
        """True when the spec goes on for one advisor-granted round; otherwise hands off."""
        if round < max_rounds + self.extra:
            return True
        snap, check = self.limit_snapshot(round, reviewer, reply, context)
        result = self.handoff(why, "spec_limit", snapshot=snap, revalidate=check)
        if result.action == "extra_round":
            self.extra = 1
            notify.log(f"#{self.issue}: advisor granted one more spec round: {result.decision.reason}")
            return True
        return False

    def review_round(self, round, max_rounds, context):
        """"accept", "changes" or "stop" (the owner or a failure took over)."""
        n, wt, env = self.issue, self.wt, self.env
        if not hasattr(self, "extra"):
            self.extra = 1 if advisor.granted(env, "spec_limit", n) else 0   # granted by the advisor (#17)
        adr_note = "No ADR has been revised yet."
        if git(wt, "rev-list", "origin/main..HEAD"):
            adr_note = (f"Earlier rounds revised ADRs on this branch (docs PR #{self.pr or '?'}): "
                        "see `git diff origin/main...HEAD`.")
        head = git(wt, "rev-parse", "HEAD") or ""
        last_vendor = last_by(env, n, "reply")
        other_model = last_model(env, n, "reply", round - 1, last_vendor)
        reviewer = self.pick("spec_reviewer", pick.all_vendors(env), last_vendor, other_model)
        attempt = 0
        while True:
            tag = f"issue-{n}-spec-{round}" + (f"-retry{attempt}" if attempt else "")
            out = self.agent(reviewer, "spec_review", self.render(
                self.self_dir / "spec-review-prompt.md", VENDOR=reviewer, ISSUE=n, GH_REPO=self.repo, WORKTREE=str(wt),
                BRANCH=self.branch, ROUND=str(round), MAX_ROUNDS=str(max_rounds), CONTEXT_FILE=str(context),
                ADR_NOTE=adr_note, ROUND_NOTE=self.round_note(round)), tag, "review-settings.json",
                followup=self.followup(round, reviewer, last_vendor))
            outcome, model = out["outcome"], out["model"] or ""
            if self.unverified(reviewer, last_vendor, model, other_model):
                self.handoff("Codex spec reviewer model was not verified as different from the reviser model.",
                             "spec_reviewer_model_unverified")
                return "stop"
            mode_note = self.mode_note("Review", reviewer, last_vendor, model, other_model)
            if changed(wt, head):
                restore(wt, head)
                outcome = f"RESULT: failed {reviewer} changed files in a read-only spec review"
            reply = reply_text(out["reply"])
            self.hooks.verdict("spec_review", reviewer, head, round, outcome, out["reply"] or "")
            if outcome.startswith("SPEC: accept"):
                problems = self.accept_problems()
                if not problems:
                    return self.accept(reviewer, round, mode_note, reply)
                points = "\n".join(f"{i}. [decidable] {p}" for i, p in enumerate(problems, 1))
                if not self.post(f"<!-- baton-spec-review round={round} by={reviewer} -->\n"
                                 f"## Spec review ({reviewer}), round {round}: accept refused by the runner\n\n"
                                 f"{reviewer} accepted, but the issue body breaks the runner's rules:\n\n{points}\n\n"
                                 f"{mode_note}{reply[:REPLY_CAP]}", f"posting the refused accept of {reviewer} failed"):
                    return "stop"
                if not self.at_limit(round, max_rounds, reviewer, points, context,
                                     f"{reviewer} accepted, but the issue body breaks the runner's rules after {round} spec rounds."):
                    return "stop"
                return "changes"
            if outcome.startswith("SPEC: changes"):
                if not self.post(f"<!-- baton-spec-review round={round} by={reviewer} -->\n"
                                 f"## Spec review ({reviewer}), round {round}\n\n{mode_note}{reply[:REPLY_CAP]}",
                                 f"posting {reviewer}'s spec review failed"):
                    return "stop"
                if not self.at_limit(round, max_rounds, reviewer, reply[:REPLY_CAP], context,
                                     f"{reviewer} still has points after {round} spec rounds."):
                    return "stop"
                return "changes"
            if outcome.startswith("SPEC: reject"):
                self.gh("issue", "comment", n, "--body", f"<!-- baton-note by={reviewer} -->\n"
                        f"## Spec review ({reviewer}), round {round}: reject\n\n{reply[:REPLY_CAP]}")
                self.handoff(f"{reviewer} rejects the issue.", "spec_rejected")
                return "stop"
            action, reviewer = self.recover_worker(
                f"{reviewer}'s spec review failed: {outcome}", "spec_review_run_failed", reviewer)
            if action == "handoff":
                return "stop"
            attempt += 1

    def accept_verdict(self, reviewer, round, mode_note, reply):
        return (f"<!-- baton-verdict: accept by={reviewer} -->\n"
                f"## Spec review ({reviewer}), round {round}: accept\n\n{mode_note}{reply[:REPLY_CAP]}")

    def pr_state(self, pr):
        """OPEN / MERGED / CLOSED, or None when gh fails."""
        data = pr_helpers.view(pr, "state")
        return (data or {}).get("state") if data is not None else None

    def publish_accept(self, verdict):
        """Post the saved verdict and make the issue runnable. Keeps pending acceptance if the
        verdict write fails so a later tick can finish after the ADR PR is merged."""
        n = self.issue
        if not self.post(verdict, "posting the accept verdict failed"):
            return "stop"
        self.gh("issue", "edit", n, "--remove-label", "needs-triage", "--add-label", "ready-for-agent")
        merge.remove_worktree(self.repo_dir, self.wt)
        round_m = re.search(r"round (\d+): accept", verdict)
        notify.notify(f"#{n}: spec accepted (round {round_m.group(1) if round_m else '?'})", self.url)
        advisor.finish_episode(self.env, "issue", n, "accept")
        self.finish()
        return "accept"

    def merge_adr_and_publish(self, pr, verdict, *, reviewer=""):
        """CI + shared merge helper for an open ADR PR, then publish the verdict once MERGED."""
        n, who = self.issue, reviewer or "the reviewer"
        merger = merge.Merger(self.env, self.hooks, pr, n)
        ci_message = f"{who} accepted, but CI on the ADR PR #{pr} is not green."

        def stop(why, reason, **status):
            self.handoff(why, reason, **merger.observation(reason, acceptance=verdict, **status))
            return "stop"

        if not merger.ci_green(30):
            return stop(ci_message, "spec_adr_ci_before_merge_not_green", ci_failed=True)
        result = merger.merge_with_updates(30)
        if not result.get("ok"):
            if result.get("reason") == "ci_after_update_not_green":
                return stop(ci_message, "spec_adr_ci_after_update_not_green", ci_failed=True)
            return stop(f"{who} accepted, but merging the ADR PR #{pr} failed.{result.get('detail') or ''}",
                        "spec_adr_merge_failed", failure=result.get("reason") or "")
        if self.pr_state(pr) != "MERGED":
            return stop(f"{who} accepted, but ADR PR #{pr} is still open after the merge attempt.",
                        "spec_adr_merge_unconfirmed")
        notify.notify(f"#{n}: ADR PR #{pr} merged", f"https://github.com/{self.repo}/pull/{pr}")
        return self.publish_accept(verdict)

    def resume_pending_accept(self, pending):
        """Resume an accepted spec whose ADR merge was interrupted (#122)."""
        pr, verdict = pending["pr"], pending["verdict"]
        self.pr = pr
        state = self.pr_state(pr)
        if state is None:
            self.handoff(f"Could not read ADR PR #{pr} to resume acceptance.", "spec_pending_adr_read_failed")
            return False
        if state == "MERGED":
            return self.publish_accept(verdict) == "accept"
        if state == "CLOSED":
            self.handoff(f"ADR PR #{pr} was closed without merging; acceptance stays pending.", "spec_adr_closed")
            return False
        if state != "OPEN":
            reason = "spec_adr_state_invalid"
            merger = merge.Merger(self.env, self.hooks, pr, self.issue)
            self.handoff(f"ADR PR #{pr} state is unreadable ({state}); acceptance stays pending.",
                         reason, **merger.observation(reason, acceptance=verdict))
            return False
        return self.merge_adr_and_publish(pr, verdict) == "accept"

    def accept(self, reviewer, round, mode_note, reply):
        """Merge the ADR PR when there is one, post the verdict, and make the issue runnable."""
        n, pr = self.issue, self.pr
        verdict = self.accept_verdict(reviewer, round, mode_note, reply[:REPLY_CAP])
        if pr:
            pending = f"<!-- baton-spec-accept-pending pr={pr} -->\n{verdict}"
            if not self.post(pending, f"posting pending acceptance for ADR PR #{pr} failed"):
                self.handoff(f"{reviewer} accepted, but posting pending acceptance failed.",
                             "spec_pending_accept_post_failed")
                return "stop"
            return self.merge_adr_and_publish(pr, verdict, reviewer=reviewer)
        return self.publish_accept(verdict)

    # --- the reviser's round ---
    def revise_round(self, round, context):
        """"revised" or "stop" (the owner or a failure took over)."""
        n, wt, env = self.issue, self.wt, self.env
        out_base = f"/tmp/baton-spec-{n}-{round}"
        last_vendor = last_by(env, n, "review")
        other_model = last_model(env, n, "review", round, last_vendor)
        claude_followup = self.reviser_followup(round, "claude", last_vendor)
        reviser = self.pick("reviser", list(env.config.quota.vendor_pool), last_vendor, other_model,
                            spec_followup=claude_followup)
        self.mark_base(round)
        attempt = 0
        while True:
            for stale in Path("/tmp").glob(f"baton-spec-{n}-{round}.*"):
                stale.unlink()
            template = self.self_dir / ("codex-spec-prompt.md" if reviser == "codex" else "spec-prompt.md")
            prompt = self.render(
                template, ISSUE=n, GH_REPO=self.repo, WORKTREE=str(wt), BRANCH=self.branch, ROUND=str(round),
                CONTEXT_FILE=str(context), OUT=out_base)
            followup = reviser == "claude" and claude_followup
            tag = f"issue-{n}-spec-reply-{round}" + (f"-retry{attempt}" if attempt else "")
            out = self.agent(reviser, "spec", prompt, tag, "spec-settings.json", out_base, followup=followup)
            denied = permission_denials(out.get("log", "")) if out["outcome"].startswith("RESULT: failed") else []
            if denied:
                # A denied command is the runner's setup, not a question for the owner: rerun once (#53).
                notify.log(f"#{n}: spec revision round {round} failed after a denied command ({denied[0]}); rerunning once")
                for suffix in (".body", ".comment"):
                    Path(f"{out_base}{suffix}").unlink(missing_ok=True)
                out = self.agent(reviser, "spec", prompt + DENIED_NOTE.format(command=denied[0]), f"{tag}-rerun",
                                 "spec-settings.json", out_base, followup=followup)
                denied = permission_denials(out.get("log", "")) if out["outcome"].startswith("RESULT: failed") else []
            outcome, model = out["outcome"], out["model"] or ""
            if (self.unverified(reviser, last_vendor, model, other_model)
                    or (reviser == last_vendor == "claude" and not claude_cli.distinct_models(model, other_model))):
                self.handoff(f"{reviser.capitalize()} spec reviser model was not verified as different from the reviewer model.",
                             "spec_reviser_model_unverified")
                return "stop"
            mode_note = self.mode_note("Revision", reviser, last_vendor, model, other_model)
            comment, body = Path(f"{out_base}.comment"), Path(f"{out_base}.body")
            if git(wt, "status", "--porcelain"):
                outcome = f"RESULT: failed uncommitted changes left in {wt}"
            if re.match(r"SPEC: (revised|human)", outcome) and not (comment.is_file() and comment.stat().st_size):
                outcome = f"RESULT: failed no reply in {comment}"
            if outcome.startswith("SPEC: revised"):
                human = ""
                break
            if outcome.startswith("SPEC: human"):
                human = " human"
                break
            why = f" A command was denied ({denied[0]}): check the spec role's permissions." if denied else ""
            action, reviser = self.recover_worker(
                f"{reviser} spec run failed: {outcome}{why}", "spec_revision_run_failed", reviser, role="reviser")
            if action == "handoff":
                return "stop"
            attempt += 1
        if body.is_file() and body.stat().st_size:
            body.write_text(check_section.canonicalize(strip_markers(body.read_text())).rstrip("\n") + "\n")
            edited = self.gh("issue", "edit", n, "--body-file", str(body))
            if edited.returncode != 0:
                self.handoff(f"Applying {reviser} revised issue body failed ({body}).{pr_helpers.why(edited)}",
                             "spec_body_edit_failed")
                return "stop"
        if git(wt, "rev-list", f"{self.base}..HEAD"):
            try:
                self.record(reviser, model)
            except OSError:
                return "stop"
            if not push(wt, self.branch, upstream=True, signoff=self.env.repo.signoff):
                self.handoff(f"Pushing {self.branch} failed.", "spec_adr_push_failed")
                return "stop"
            self.base = f"origin/{self.branch}"
            if not self.pr:
                created = self.gh("pr", "create", "--head", self.branch, "--base", "main",
                                  "--title", f"docs(adr): spec revisions for #{n}", "--body",
                                  f"ADR revisions from the spec review of #{n} ({reviser} revises). The runner merges this "
                                  "once a spec reviewer accepts the spec; the issue stays open for implementation.\n\n"
                                  f"## {check_section.HEADING}\nnone")
                if created.returncode != 0:
                    self.handoff(f"Opening the ADR PR for {self.branch} failed.{pr_helpers.why(created)}",
                                 "spec_adr_open_failed")
                    return "stop"
                self.pr = created.stdout.strip().rsplit("/", 1)[-1]
        try:
            self.record(reviser, model)
        except OSError:
            return "stop"
        text = strip_markers(comment.read_text()).encode()[:REPLY_CAP].decode(errors="ignore").rstrip("\n")
        adr = f"\nADR revisions: #{self.pr}\n" if self.pr else ""
        if not self.post(f"<!-- baton-spec-reply round={round}{human} by={reviser} -->\n"
                         f"## Spec reply ({reviser}), round {round}\n{adr}\n{mode_note}{text}",
                         f"posting {reviser} spec reply failed"):
            return "stop"
        for leftover in Path("/tmp").glob(f"baton-spec-{n}-{round}.*"):
            leftover.unlink()
        if human:
            self.handoff(f"{reviser} has questions only you can answer. Reply on the issue, then remove ready-for-human.",
                         "spec_questions")
            return "stop"
        return "revised"

    # --- the loop ---
    def loop(self):
        """True once the spec reviewer accepted (verdict comment posted, any ADR PR merged),
        False when the owner or a failure took over. Deferred when a vendor cannot run now."""
        n, env = self.issue, self.env
        listed = comments(env, n)
        if listed is None:
            self.handoff("Could not read issue comments to check pending acceptance.", "spec_comments_read_failed")
            return False
        pending = pending_accept_in(listed, env.owner)
        if pending:
            # Resume the ADR merge without a worktree, vendor pick or agent dispatch (#122).
            self.gh("issue", "edit", n, "--remove-label", "ready-for-human", "--add-label", "agent-wip")
            return self.resume_pending_accept(pending)
        kind, round = spec_state(env, n)
        if pending_role(kind) == "reviser":
            step = "revise"   # the reviewer asked for changes, or the owner answered
        elif kind == "reply":
            step, round = "review", round + 1   # a reviser revised; the spec is reviewed again
        else:
            step, round = "review", 1
            self.keep_owner_body()
        max_rounds = max(int(env.config.scheduling.max_spec_rounds or 3), round + 1)   # an owner answer earns another round
        if git(self.repo_dir, "rev-parse", "-q", "--verify", f"origin/{self.branch}") is not None:
            self.base = f"origin/{self.branch}"
        if not self.worktree():
            notify.log(f"spec worktree for #{n} failed")
            return False
        self.gh("issue", "edit", n, "--remove-label", "ready-for-human", "--add-label", "agent-wip")
        # Sent by started() once a vendor is picked: a deferred tick (no vendor can run now) repeats every
        # timer interval, and announcing each one floods the owner with notices of work that never ran.
        self.announce = f"#{n}: spec review (round {round}, {'revision' if step == 'revise' else step})"
        while True:
            self.hooks.round = round
            self.path("spec-paused").write_text(f"{n}\n")
            found = self.gh("pr", "list", "--state", "open", "--head", self.branch, "--json", "number",
                            "--jq", ".[0].number // empty")
            self.pr = found.stdout.strip() if found.returncode == 0 else ""
            context = Path(self.ctx, f"issue-{n}-spec-{round}-{step}-{time.strftime('%Y%m%d-%H%M%S')}.md")
            context.write_text(review.issue_context(env, n))
            if step == "review":
                result = self.review_round(round, max_rounds, context)
                if result == "changes":
                    step = "revise"
                    continue
                return result == "accept"
            if self.revise_round(round, context) != "revised":
                return False
            step, round = "review", round + 1


@session.scoped
def run(args, env=None):
    env = session.resolve(env)
    env = env.with_runtime(issue=args.issue)
    hooks = telemetry.from_env(env, issue=args.issue)
    try:
        return fix.result(0 if Spec(env, hooks, args.issue).loop() else 1)
    except Deferred as deferred:
        return fix.result(75, defer_vendor=deferred.vendor, defer=deferred.reason)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("loop").add_argument("--issue", required=True)
    sub.add_parser("state").add_argument("issue")
    sub.add_parser("plan").add_argument("issue")
    args = parser.parse_args(argv)
    env = session.resolve()
    if args.command == "state":
        print(*spec_state(env, args.issue))
        return 0
    if args.command == "plan":
        print(cursor_plan(env, args.issue))
        return 0
    out = run(args, env)
    print(json.dumps(out, ensure_ascii=False))
    return out["rc"]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"spec: {exc}", file=sys.stderr)
        sys.exit(1)
