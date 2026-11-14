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

from . import dispatch, e2e_gate, fix, hooks as telemetry, md, merge, notify, pick, pr as pr_helpers, prompts, review, vendors
from .common import Deferred, changed, git, push, restore, strip_markers

REPLY_CAP = 60000
VERDICT = re.compile(r"<!-- baton-verdict: (accept|revise)( by=(cursor|codex|claude))? -->")
STATE_RE = re.compile(r"<!-- baton-spec-(?P<k>review|reply) round=(?P<r>[0-9]+)(?P<h> human)?")
KIND_RE = r"<!-- baton-spec-%s round=[0-9]+( human)? by=(?P<v>[a-z]+) -->"
VERDICT_LINE = re.compile(r"^(SPEC|RESULT): ")


@session.scoped
def comments(env, issue):
    """The issue's comments, or None when gh fails."""
    env = session.resolve(env)
    result = pr_helpers.gh("issue", "view", str(issue), "-R", env.repo.gh_repo, "--json", "comments")
    return (json.loads(result.stdout).get("comments") or []) if result.returncode == 0 else None


@session.scoped
def owner_bodies(env, issue):
    env = session.resolve(env)
    return [c.get("body") or "" for c in comments(env, issue) or ()
            if (c.get("author") or {}).get("login") == env.owner]


@session.scoped
def cursor_plan(env, issue):
    """The body of the latest accept/revise verdict comment of the owner, Cursor or an agent
    posting for Cursor; empty when there is none."""
    env = session.resolve(env)
    login_re = env.config.scheduling.cursor_login_re or r"^(app/cursor|cursor(\[bot\])?)$"
    found = []
    for c in comments(env, issue) or ():
        login = (c.get("author") or {}).get("login") or ""
        if (login == env.owner or re.search(login_re, login)) and VERDICT.match(c.get("body") or ""):
            found.append(c["body"])
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


@session.scoped
def last_by(env, issue, kind):
    """The vendor of the latest spec review (KIND review) or reply, or ''."""
    env = session.resolve(env)
    found = [m for body in owner_bodies(env, issue) if (m := re.match(KIND_RE % kind, body))]
    return found[-1]["v"] if found else ""


@session.scoped
def last_model(env, issue, kind, round):
    """The actual Codex model in the successful run of that round, from runner.log."""
    env = session.resolve(env)
    tag = f"issue-{issue}-spec-{round}" if kind != "reply" else f"issue-{issue}-spec-reply-{round}"
    log, found, repo = Path(str(env.config.paths.logs), "runner.log"), "", notify.label(env)
    if not log.is_file():
        return ""
    for line in log.read_text(errors="replace").splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[2:6] == [repo, "end", tag, "rc=0"]:
            for field in fields[6:]:
                if field.startswith("model="):
                    found = field[len("model="):]
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
        env = self.env.with_runtime(spec_followup=True) if followup else self.env
        out = dispatch.dispatch(vendor, role, prompt, tag, str(self.wt), str(self.self_dir / settings), output, env,
                                self.hooks)
        if out["rc"] == 75:
            raise Deferred(vendor, out["defer"])
        return out

    def gh(self, *args):
        return pr_helpers.gh(*args, "-R", self.repo)

    def pick(self, role, candidates, last_vendor, other_model):
        return pick.choose(self.env, role, "agent" if role == "spec_reviewer" else "pool", "contrib", candidates,
                           exclude=last_vendor, issue=self.issue, other_model=other_model)

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
    def handoff(self, why, reason="spec_failed"):
        """The owner takes over; the loop resumes once they answer on the issue and remove
        ready-for-human."""
        self.gh("issue", "edit", self.issue, "--remove-label", "agent-wip", "--add-label", "ready-for-human")
        self.hooks.task("handoff", reason, "--reason", reason)
        notify.notify(f"#{self.issue}: spec needs you", f"{why} {self.url}")

    def post(self, body, failed):
        if self.gh("issue", "comment", self.issue, "--body", body).returncode == 0:
            return True
        notify.log(f"#{self.issue}: {failed}")
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
            not other_model or not model or model == other_model or model == "unknown")

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
        if vendor == "codex":
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
        kept = {line.strip() for line in md.section("Manual checks", body.stdout).splitlines()}
        lost = [line.strip() for line in md.section("Manual checks", owner).splitlines()
                if line.strip() and line.strip() not in kept]
        if lost:
            problems.append("`## Manual checks` must keep every check the owner wrote, word for word; these are "
                            "missing (never move them to `## Before release`):\n" + "\n".join(f"   {line}" for line in lost))
        return problems

    # --- the reviewer's round ---
    def review_round(self, round, max_rounds, context):
        """"accept", "changes" or "stop" (the owner or a failure took over)."""
        n, wt, env = self.issue, self.wt, self.env
        adr_note = "No ADR has been revised yet."
        if git(wt, "rev-list", "origin/main..HEAD"):
            adr_note = (f"Earlier rounds revised ADRs on this branch (docs PR #{self.pr or '?'}): "
                        "see `git diff origin/main...HEAD`.")
        head = git(wt, "rev-parse", "HEAD") or ""
        last_vendor = last_by(env, n, "reply")
        other_model = last_model(env, n, "reply", round - 1)
        reviewer = self.pick("spec_reviewer", pick.all_vendors(env), last_vendor, other_model)
        out = self.agent(reviewer, "spec_review", self.render(
            self.self_dir / "spec-review-prompt.md", VENDOR=reviewer, ISSUE=n, GH_REPO=self.repo, WORKTREE=str(wt),
            BRANCH=self.branch, ROUND=str(round), MAX_ROUNDS=str(max_rounds), CONTEXT_FILE=str(context),
            ADR_NOTE=adr_note, ROUND_NOTE=self.round_note(round)), f"issue-{n}-spec-{round}", "review-settings.json",
            followup=self.followup(round, reviewer, last_vendor))
        outcome, model = out["outcome"], out["model"] or ""
        if self.unverified(reviewer, last_vendor, model, other_model):
            self.handoff("Codex spec reviewer model was not verified as different from the reviser model.")
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
            if round >= max_rounds:
                self.handoff(f"{reviewer} accepted, but the issue body breaks the runner's rules after {round} spec rounds.",
                             "spec_limit")
                return "stop"
            return "changes"
        if outcome.startswith("SPEC: changes"):
            if not self.post(f"<!-- baton-spec-review round={round} by={reviewer} -->\n"
                             f"## Spec review ({reviewer}), round {round}\n\n{mode_note}{reply[:REPLY_CAP]}",
                             f"posting {reviewer}'s spec review failed"):
                return "stop"
            if round >= max_rounds:
                self.handoff(f"{reviewer} still has points after {round} spec rounds.", "spec_limit")
                return "stop"
            return "changes"
        if outcome.startswith("SPEC: reject"):
            self.gh("issue", "comment", n, "--body", f"<!-- baton-note by={reviewer} -->\n"
                    f"## Spec review ({reviewer}), round {round}: reject\n\n{reply[:REPLY_CAP]}")
            self.handoff(f"{reviewer} rejects the issue.", "spec_rejected")
            return "stop"
        self.handoff(f"{reviewer}'s spec review failed: {outcome}")
        return "stop"

    def accept(self, reviewer, round, mode_note, reply):
        """Merge the ADR PR when there is one, post the verdict, and make the issue runnable."""
        n, pr = self.issue, self.pr
        if pr:
            if not merge.Merger(self.env, self.hooks, pr, n).ci_green(30):
                self.handoff(f"{reviewer} accepted, but CI on the ADR PR #{pr} is not green.")
                return "stop"
            if self.gh("pr", "merge", pr, "--merge").returncode != 0:
                self.handoff(f"{reviewer} accepted, but merging the ADR PR #{pr} failed.")
                return "stop"
            notify.notify(f"#{n}: ADR PR #{pr} merged", f"https://github.com/{self.repo}/pull/{pr}")
        if not self.post(f"<!-- baton-verdict: accept by={reviewer} -->\n"
                         f"## Spec review ({reviewer}), round {round}: accept\n\n{mode_note}{reply[:REPLY_CAP]}",
                         f"posting {reviewer}'s accept failed"):
            return "stop"
        self.gh("issue", "edit", n, "--remove-label", "needs-triage", "--add-label", "ready-for-agent")
        merge.remove_worktree(self.repo_dir, self.wt)
        notify.notify(f"#{n}: spec accepted (round {round})", self.url)
        self.finish()
        return "accept"

    # --- the reviser's round ---
    def revise_round(self, round, context):
        """"revised" or "stop" (the owner or a failure took over)."""
        n, wt, env = self.issue, self.wt, self.env
        out_base = f"/tmp/baton-spec-{n}-{round}"
        for stale in Path("/tmp").glob(f"baton-spec-{n}-{round}.*"):
            stale.unlink()
        last_vendor = last_by(env, n, "review")
        other_model = last_model(env, n, "review", round)
        reviser = self.pick("reviser", list(env.config.quota.vendor_pool), last_vendor, other_model)
        self.mark_base(round)
        template = self.self_dir / (f"codex-spec-prompt.md" if reviser == "codex" else "spec-prompt.md")
        out = self.agent(reviser, "spec", self.render(
            template, ISSUE=n, GH_REPO=self.repo, WORKTREE=str(wt), BRANCH=self.branch, ROUND=str(round),
            CONTEXT_FILE=str(context), OUT=out_base), f"issue-{n}-spec-reply-{round}", "spec-settings.json", out_base,
            followup=self.reviser_followup(round, reviser, last_vendor))
        outcome, model = out["outcome"], out["model"] or ""
        if self.unverified(reviser, last_vendor, model, other_model):
            self.handoff("Codex spec reviser model was not verified as different from the reviewer model.")
            return "stop"
        mode_note = self.mode_note("Revision", reviser, last_vendor, model, other_model)
        comment, body = Path(f"{out_base}.comment"), Path(f"{out_base}.body")
        if git(wt, "status", "--porcelain"):
            outcome = f"RESULT: failed uncommitted changes left in {wt}"
        if re.match(r"SPEC: (revised|human)", outcome) and not (comment.is_file() and comment.stat().st_size):
            outcome = f"RESULT: failed no reply in {comment}"
        if outcome.startswith("SPEC: revised"):
            human = ""
        elif outcome.startswith("SPEC: human"):
            human = " human"
        else:
            self.handoff(f"{reviser} spec run failed: {outcome}")
            return "stop"
        if body.is_file() and body.stat().st_size:
            body.write_text(strip_markers(body.read_text()).rstrip("\n") + "\n")
            if self.gh("issue", "edit", n, "--body-file", str(body)).returncode != 0:
                self.handoff(f"Applying {reviser} revised issue body failed ({body}).")
                return "stop"
        if git(wt, "rev-list", f"{self.base}..HEAD"):
            try:
                self.record(reviser, model)
            except OSError:
                return "stop"
            if not push(wt, self.branch, upstream=True, signoff=self.env.repo.signoff):
                self.handoff(f"Pushing {self.branch} failed.")
                return "stop"
            self.base = f"origin/{self.branch}"
            if not self.pr:
                created = self.gh("pr", "create", "--head", self.branch, "--base", "main",
                                  "--title", f"docs(adr): spec revisions for #{n}", "--body",
                                  f"ADR revisions from the spec review of #{n} ({reviser} revises). The runner merges this "
                                  "once a spec reviewer accepts the spec; the issue stays open for implementation.\n\n"
                                  "## Manual checks\nnone")
                if created.returncode != 0:
                    self.handoff(f"Opening the ADR PR for {self.branch} failed.")
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
                         "needs_info")
            return "stop"
        return "revised"

    # --- the loop ---
    def loop(self):
        """True once the spec reviewer accepted (verdict comment posted, any ADR PR merged),
        False when the owner or a failure took over. Deferred when a vendor cannot run now."""
        n, env = self.issue, self.env
        kind, round = spec_state(env, n)
        if kind in ("review", "reply-human"):
            step = "revise"   # the reviewer asked for changes, or the owner answered
        elif kind == "reply":
            step, round = "review", round + 1   # a reviser revised; the spec is reviewed again
        else:
            step, round = "review", 1
            self.keep_owner_body()
        max_rounds = max(int(env.config.scheduling.max_spec_rounds or 6), round + 1)   # an owner answer earns another round
        if git(self.repo_dir, "rev-parse", "-q", "--verify", f"origin/{self.branch}") is not None:
            self.base = f"origin/{self.branch}"
        if not self.worktree():
            notify.log(f"spec worktree for #{n} failed")
            return False
        self.gh("issue", "edit", n, "--remove-label", "ready-for-human", "--add-label", "agent-wip")
        notify.notify(f"#{n}: spec review (round {round}, {'revision' if step == 'revise' else step})", self.url)
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
