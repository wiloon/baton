"""The review loop for the bash runner (migration slice 4c, #280): `run_review`,
`cursor_review`, `review_loop` and the review context of run.sh.

One reviewer run reads the PR's context file and returns REVIEW: pass or REVIEW: changes;
on changes a fixer is picked by contribution and baton.fix runs, then the next round
reviews. A pass goes to baton.merge. The `review-pending-N` checkpoint (baton.checkpoint,
a bash file) records the round and step after every change, so a deferred review resumes
where it stopped, on another vendor when its own cannot run.

Prints JSON for the bash caller, which keeps `defer_vendor` (it exits the item's subshell):
    rc            0 done or handed to the owner, 75 deferred
    defer_vendor  with `defer`: the vendor to rest and why; empty unless a vendor must rest
"""

from . import session
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

from . import checkpoint, dispatch, fix, hooks as telemetry, merge, notify, pick, pr as pr_helpers, prompts, vendors
from .common import Deferred, changed, git, restore, strip_markers

REPLY_CAP = 60000
REVIEWED = ("REVIEW: pass", "REVIEW: changes")
MARKER_LINE = re.compile(r"^(REVIEW|RESULT): ")


# --- what the reviewer reads: the issue, the PR and their comments --------------------------
def jq_str(value):
    """A value in a jq string interpolation: null is "null"."""
    return "null" if value is None else str(value)


def cut(text, limit):
    text = text or ""
    return text if len(text) <= limit else f"{text[:limit]}\n[cut: {len(text) - limit} more characters]"


def paginated(args):
    """The JSON values `gh api --paginate` prints (one array per page), flattened."""
    result = pr_helpers.gh(*args)
    if result.returncode != 0:
        return []
    out, decoder, text, i = [], json.JSONDecoder(), result.stdout, 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        value, i = decoder.raw_decode(text, i)
        out.extend(value if isinstance(value, list) else [value])
    return out


@session.scoped
def issue_context(env, issue):
    """The issue and its comments: only the owner's, Cursor's and the agents' are kept."""
    env = session.resolve(env)
    owner, limit = env.owner, int(env.config.scheduling.review_context_max_chars or 20000)
    login_re = env.config.scheduling.cursor_login_re or r"^(app/cursor|cursor(\[bot\])?)$"
    result = pr_helpers.gh("issue", "view", str(issue), "-R", env.repo.gh_repo, "--json", "title,body,comments")
    if result.returncode != 0:
        return ""
    data = json.loads(result.stdout)
    lines = [f'<issue number="{issue}">\n# {jq_str(data.get("title"))}\n\n{cut(data.get("body"), limit)}\n</issue>']
    for comment in data.get("comments") or ():
        login = (comment.get("author") or {}).get("login") or ""
        if login == owner or re.search(login_re, login):
            lines.append(f'<issue-comment author="{login}">\n{cut(comment.get("body"), limit)}\n</issue-comment>')
    return "".join(f"{line}\n" for line in lines)


@session.scoped
def review_context(env, pr, issue):
    """The issue, then the PR with its comments, reviews and inline comments. Context files
    keep only the comments that count (public repo: others dropped); each body is cut."""
    env = session.resolve(env)
    owner, repo, limit = env.owner, env.repo.gh_repo, int(env.config.scheduling.review_context_max_chars or 20000)
    login_re = env.config.scheduling.cursor_login_re or r"^(app/cursor|cursor(\[bot\])?)$"

    def kept(login):
        return login == owner or bool(re.search(login_re, login))
    text = issue_context(env, issue) if re.fullmatch(r"\d+", str(issue or "")) else ""
    result = pr_helpers.gh("pr", "view", str(pr), "-R", repo, "--json", "title,body,comments,reviews")
    lines = []
    if result.returncode == 0:
        data = json.loads(result.stdout)
        lines.append(f'<pr number="{pr}">\n# {jq_str(data.get("title"))}\n\n{cut(data.get("body"), limit)}\n</pr>')
        for review in data.get("reviews") or ():
            login = (review.get("author") or {}).get("login") or ""
            if review.get("body") != "" and kept(login):
                lines.append(f'<pr-review author="{login}" state="{jq_str(review.get("state"))}">\n'
                             f'{cut(review.get("body"), limit)}\n</pr-review>')
        for comment in data.get("comments") or ():
            login = (comment.get("author") or {}).get("login") or ""
            if kept(login):
                lines.append(f'<pr-comment author="{login}">\n{cut(comment.get("body"), limit)}\n</pr-comment>')
    for comment in paginated(["api", f"repos/{repo}/pulls/{pr}/comments", "--paginate"]):
        login = (comment.get("user") or {}).get("login") or ""
        if kept(login):
            line = comment.get("line") if comment.get("line") is not None else comment.get("original_line")
            lines.append(f'<inline-comment author="{login}" at="{jq_str(comment.get("path"))}:{jq_str(line)}">\n'
                         f'{cut(comment.get("body"), limit)}\n</inline-comment>')
    return text + "".join(f"{line}\n" for line in lines)


def is_follow_up(repo, issue):
    if not re.fullmatch(r"\d+", str(issue or "")):
        return False
    result = pr_helpers.gh("issue", "view", str(issue), "-R", repo, "--json", "labels")
    try:
        return any(label.get("name") == "follow-up" for label in json.loads(result.stdout).get("labels") or ())
    except ValueError:
        return False


def nit_rule(repo, issue):
    """How the reviewer treats nits. A nit on the PR's own code is fixed in it or dropped
    (P-2); after_merge also filters those by diff hunk. A PR doing a follow-up issue files no
    further follow-up: each nit is fixed in it or dropped."""
    if is_follow_up(repo, issue):
        return (f"This PR does follow-up issue #{issue}, and nothing it leaves is filed again. Put each nit worth "
                "doing under Must fix, drop the rest, and write \"none\" under Nits.")
    return ("A nit on lines this PR adds or changes is its own code: when it is worth doing, list it "
            "under Must fix so the PR fixes it before merge; otherwise leave it out. Under Nits list only nits on code "
            "the PR leaves untouched. They never block: the runner files them as one follow-up issue after the merge, "
            "so make each one self-contained, with file:line.")


def agent_review(repo, owner, pr, round):
    """The body of the reviewer agent's comment for that round; "" when there is none."""
    result = pr_helpers.gh("pr", "view", str(pr), "-R", repo, "--json", "comments")
    if result.returncode != 0:
        return ""
    marker = f"<!-- baton-review round={round} "
    bodies = [c.get("body") or "" for c in json.loads(result.stdout).get("comments") or ()
              if (c.get("author") or {}).get("login") == owner and (c.get("body") or "").startswith(marker)]
    return bodies[-1].rstrip("\n") if bodies else ""


def human_feedback(repo, owner, pr):
    """The owner's reviews, comments and inline comments since the last agent fix. Public
    repo: only the owner's words reach an agent as instructions."""
    since = ""
    result = pr_helpers.gh("pr", "view", str(pr), "-R", repo, "--json", "comments")
    data = json.loads(result.stdout) if result.returncode == 0 else {}
    fixes = [c.get("createdAt") or "" for c in data.get("comments") or ()
             if (c.get("author") or {}).get("login") == owner and (c.get("body") or "").startswith("<!-- baton-fix")]
    since = max(fixes, default="")
    result = pr_helpers.gh("pr", "view", str(pr), "-R", repo, "--json", "reviews,comments")
    out = ""
    if result.returncode == 0:
        data = json.loads(result.stdout)
        items = [f"Review ({r.get('submittedAt')}):\n{r['body']}" for r in data.get("reviews") or ()
                 if (r.get("author") or {}).get("login") == owner and r.get("body")
                 and not r["body"].startswith("<!-- baton") and (r.get("submittedAt") or "") > since]
        items += [f"Comment ({c.get('createdAt')}):\n{c['body']}" for c in data.get("comments") or ()
                  if (c.get("author") or {}).get("login") == owner and not (c.get("body") or "").startswith("<!-- baton")
                  and (c.get("createdAt") or "") > since]
        out = "\n\n---\n\n".join(items) + "\n"
    for comment in paginated(["api", f"repos/{repo}/pulls/{pr}/comments", "--paginate"]):
        if (comment.get("user") or {}).get("login") == owner and (comment.get("created_at") or "") > since:
            line = comment.get("line") if comment.get("line") is not None else comment.get("original_line")
            out += f"\n\n---\n\nInline comment on {jq_str(comment.get('path'))}:{jq_str(line)}:\n{comment.get('body')}\n"
    return out.rstrip("\n")


# --- one review round and the loop ------------------------------------------------------
class Review:
    @session.scoped
    def __init__(self, env, hooks, pr, wt, issue="", fixed_by=""):
        env = session.resolve(env)
        self.env, self.hooks, self.pr, self.wt = env, hooks, pr, wt
        self.state, self.ctx, self.repo = str(env.dirs.state), str(env.dirs.ctx), env.repo.gh_repo
        self.owner, self.self_dir = env.owner, Path(str(env.config.paths.self_dir))
        self.issue, self.fixed_by = issue, fixed_by
        self.max_rounds = int(env.config.scheduling.max_review_rounds or 2)
        self.url = f"https://github.com/{self.repo}/pull/{pr}"
        self.checkpoint = Path(self.state, f"review-pending-{pr}")
        self.values = {}

    # --- small steps every gate shares ---
    def label(self, *options):
        pr_helpers.gh("pr", "edit", str(self.pr), "-R", self.repo, *options)

    def handoff(self, reason, title, text):
        self.label("--add-label", "ready-for-human")
        self.hooks.task("handoff", reason, "--reason", reason)
        notify.notify(title, text)

    def render(self, template, **pairs):
        return prompts.render(self.self_dir / template, list(pairs.items()), env=self.env)

    def review_followup(self, round, reviewer):
        """A later round mostly re-checks the fix commits, so it runs on the cheaper model. Codex keeps
        its usual one on code Codex wrote: the two must be different models."""
        return round > 1 and not (reviewer == "codex" and "codex" in vendors.review_excluded(self.state, self.pr, self.fixed_by))

    def agent(self, vendor, role, prompt, tag, settings="", output="", env=None):
        """One agent run in the worktree; Deferred when the vendor cannot run now."""
        out = dispatch.dispatch(vendor, role, prompt, tag, str(self.wt), settings, output, env or self.env, self.hooks)
        if out["rc"] == 75:
            raise Deferred(vendor, out["defer"])
        return out

    def vendors(self):
        return pick.all_vendors(self.env)

    def review_vendor(self, current):
        """The reviewer with the fewest lines in the PR. When every vendor is excluded it stays
        as it is, so review_allowed hands the PR to the owner."""
        return pick.choose(self.env, "reviewer", current, "review", self.vendors(), pr=str(self.pr),
                           issue=self.issue, fixed_by=self.fixed_by)

    def review_allowed(self, vendor, model=""):
        """Persisted authors survive restarts and role changes: a vendor that wrote the PR does
        not review it. The PR goes to the owner otherwise."""
        if vendors.review_allowed(self.state, self.pr, vendor, model, self.fixed_by):
            return True
        self.label("--add-label", "ready-for-human")
        self.hooks.task("handoff", "review_failed", "--pr", str(self.pr), "--reason", "review_failed")
        notify.notify(f"PR #{self.pr} needs you",
                      f"Reviewer {vendor}/{model or 'unknown'} may have written the code under review; refusing self-review.")
        return False

    def seed(self):
        """Who wrote the PR and how many lines each (PRs from before they were recorded)."""
        try:
            if vendors.seed_authors(self.state, self.pr, self.repo, self.env.config.scheduling.cursor_branch_re,
                                    self.env.config.scheduling.cursor_login_re) != 0:
                return False
        except OSError:
            return False
        return True

    def seed_contrib(self):
        for commit in vendors.seed_contrib(self.state, self.pr, str(self.wt)):
            notify.log(f"PR #{self.pr}: commit {commit} has no known vendor; not counted")

    # --- one reviewer run ---
    def cursor_review(self, round, note, context):
        """Cursor reviews a PR the owner wrote with Claude (principle P-4). Read-only, so the
        runner posts its review as <!-- baton-review round=N by=cursor -->.
        Returns (outcome, reply)."""
        pr, wt = self.pr, self.wt
        head = git(wt, "rev-parse", "HEAD")
        out = self.agent("cursor", "review", self.render(
            "cursor-review-prompt.md", PR=str(pr), GH_REPO=self.repo, WORKTREE=str(wt), ROUND=str(round),
            MAX_ROUNDS=str(self.max_rounds), ROUND_NOTE=note, CONTEXT_FILE=str(context),
            NIT_RULE=nit_rule(self.repo, self.issue)), f"pr-{pr}-cursor-review-{round}")
        outcome, reply = out["outcome"], out["reply"] or ""
        if changed(wt, head):
            restore(wt, head)
            return "RESULT: failed Cursor changed files in a read-only review", reply
        if not outcome.startswith(REVIEWED):
            return outcome, reply
        posted = "\n".join(line for line in strip_markers(reply).split("\n") if not MARKER_LINE.match(line)).rstrip("\n")
        comment = pr_helpers.gh("pr", "comment", str(pr), "-R", self.repo, "--body",
                                f"<!-- baton-review round={round} by=cursor -->\n{posted[:REPLY_CAP]}")
        return (outcome if comment.returncode == 0 else "RESULT: failed posting Cursor's review"), reply

    def run_review(self, round, note, reviewer="pool"):
        """One reviewer run; returns OUTCOME. REVIEWER is cursor, a pool vendor, or pool (the
        pick decides). The code-review skill (two sub-agents) only pays off on a big diff.
        Deferred when the reviewer cannot run now."""
        pr, wt = self.pr, self.wt
        self.hooks.round = round
        head = git(wt, "rev-parse", "HEAD") or ""
        if not self.seed():
            return "RESULT: failed recording PR authors"
        if (reviewer != "cursor" or reviewer not in self.vendors()
                or vendors.vendor_blocked(str(self.env.config.paths.state), "cursor")):
            reviewer = self.review_vendor(reviewer)
        expected = (self.env.config.models.model_codex_review or "") if reviewer == "codex" else ""
        if not self.review_allowed(reviewer, expected):
            return "RESULT: failed reviewer model may be a PR author"
        numstat = git(wt, "diff", "--numstat", "origin/main...HEAD") or ""
        lines = sum(int(f) for row in numstat.splitlines() for f in row.split()[:2] if f.isdecimal())
        # The code-review skill is the Matt skills plugin's; without the plugin, review in one pass.
        if lines > int(self.env.config.scheduling.review_skill_min_lines or 800) and self.env.skills:
            skill = f"The diff is {lines} lines: run the `mattpocock-skills:code-review` skill on it and use its findings."
        else:
            skill = (f"The diff is {lines} lines: review it yourself; don't run the `mattpocock-skills:code-review` "
                     "skill or start sub-agents.")
        if reviewer == "codex":
            skill = ("Review the diff yourself, following AGENTS.md; no Claude plugins or hooks are available, "
                     "so don't read the `code-review` skill.")
        context = Path(self.ctx, f"pr-{pr}-review-{round}-{time.strftime('%Y%m%d-%H%M%S')}.md")
        text = review_context(self.env, pr, self.issue)
        try:
            pr_helpers.preflight(pr)
        except ValueError as error:
            text += (f"\n<runner-preflight>\n{error}\nReport this as Must fix; description-only changes use the "
                     "runner artifact.\n</runner-preflight>\n")
        context.write_text(text)
        if reviewer == "cursor":
            outcome, reply = self.cursor_review(round, note, context)
            self.hooks.verdict("review", "cursor", head, round, outcome, reply)
            return outcome
        review_file = Path(f"/tmp/baton-review-{pr}-{round}.md")
        review_file.unlink(missing_ok=True)
        template = "codex-review-prompt.md" if reviewer == "codex" else "review-prompt.md"
        out = self.agent(reviewer, "review", self.render(
            template, PR=str(pr), ISSUE=self.issue or "unknown", GH_REPO=self.repo, WORKTREE=str(wt), ROUND=str(round),
            MAX_ROUNDS=str(self.max_rounds), ROUND_NOTE=note, SKILL_STEP=skill, CONTEXT_FILE=str(context),
            NIT_RULE=nit_rule(self.repo, self.issue)), f"pr-{pr}-review-{round}",
            str(self.self_dir / "review-settings.json"), str(review_file),
            self.env.with_runtime(review_followup=True) if self.review_followup(round, reviewer) else None)
        outcome, model = out["outcome"], out["model"] or ""
        if not self.review_allowed(reviewer, model):
            return "RESULT: failed actual reviewer model may be a PR author"
        review_note = ""
        if reviewer == "codex" and "codex" in vendors.review_excluded(self.state, pr, self.fixed_by):
            mode = vendors.codex_review_mode(self.state, pr, model)
            review_note = f"Review mode: {mode} (codex/{model}).\n\n"
        if outcome.startswith(REVIEWED):
            if review_file.is_file() and review_file.stat().st_size:
                body = strip_markers(review_file.read_text()).rstrip("\n")
                posted = pr_helpers.gh("pr", "comment", str(pr), "-R", self.repo, "--body",
                                       f"<!-- baton-review round={round} by={reviewer} -->\n{review_note}{body}")
                if posted.returncode != 0:
                    outcome = "RESULT: failed posting review"
            else:
                outcome = "RESULT: failed no review artifact"
        self.hooks.verdict("review", reviewer, head, round, outcome, out["reply"] or "", str(review_file))
        return outcome

    # --- the checkpoint ---
    def save(self):
        checkpoint.write(self.checkpoint, {"pr": self.pr, "wt": self.wt, "ISSUE": self.issue, **self.values,
                                           "fixed_by": self.fixed_by})

    def drop(self):
        self.checkpoint.unlink(missing_ok=True)

    # --- the loop ---
    def loop(self, note, fixer="agent", reviewer="pool"):
        """Review, fix, review again, at most MAX_REVIEW_ROUNDS reviews. Each fix goes to the
        vendor that changed the fewest lines of the PR and can run now; a fixed REVIEWER (Cursor
        on owner PRs) never fixes. A review after a fix excludes only the fixer (fixed_by says
        round 1 reviews such a fix). A checkpoint resumes the step, on another vendor when its
        own cannot run. Returns 0 (done or handed to the owner) or 75 (deferred; Deferred is
        raised when a vendor must rest)."""
        pr, wt = self.pr, self.wt
        round, step, fix_head = 1, "review", ""
        saved = checkpoint.read(self.checkpoint)
        if saved:
            note, fixer, reviewer = saved.get("note", ""), saved.get("fixer", ""), saved.get("reviewer", "")
            round = int(saved["round"]) if saved.get("round", "").isdecimal() else 1
            step, fix_head = saved.get("step") or "review", saved.get("fix_head", "")
            self.fixed_by, self.issue = saved.get("fixed_by", ""), saved.get("ISSUE", "")
        if self.seed():
            self.seed_contrib()
        self.label("--remove-label", "ready-for-human")
        while True:
            self.hooks.round = round
            if round == 1:
                rnote = note
            else:
                rnote = (f"Round {round}, the last one: only check that every Must fix item of the round {round - 1} review "
                         f"comment (marker `<!-- baton-review round={round - 1}`) is resolved or convincingly answered in "
                         "the author's latest `<!-- baton-fix` comment, and that the fix commits added no new must-fix "
                         "problem. Code the fixes did not touch was reviewed already: anything new you notice there is a Nit."
                         + (f" Start from the fix commits (`git diff {fix_head}..HEAD`) and read other code only where they need it. "
                            "Don't read the full PR diff, AGENTS.md or the principles again unless a fix commit needs them."
                            if fix_head else ""))
            self.values = dict(note=note, fixer=fixer, reviewer=reviewer, round=round, step=step, fix_head=fix_head)
            self.save()
            if step == "review":
                # A disabled or resting Cursor is re-picked like any pool vendor.
                if (reviewer != "cursor" or reviewer not in self.vendors()
                        or vendors.vendor_blocked(str(self.env.config.paths.state), "cursor")):
                    reviewer = self.review_vendor(reviewer)
                    self.values["reviewer"] = reviewer
                    self.save()
                outcome = self.run_review(round, rnote, reviewer)
                if outcome.startswith("REVIEW: pass"):
                    break
                if not outcome.startswith("REVIEW: changes"):
                    self.handoff("review_failed", f"PR #{pr} review failed ({reviewer})", f"{outcome} {self.url}")
                    self.drop()
                    return 0
                if round >= self.max_rounds:
                    self.handoff("review_limit", f"PR #{pr} needs you",
                                 f"Still has must-fix items after {round} review rounds. {self.url}")
                    self.drop()
                    return 0
            if step != "fix":
                fix_head = git(wt, "rev-parse", "HEAD") or ""
            step = "fix"
            self.values.update(step=step, fix_head=fix_head)
            self.save()
            feedback = agent_review(self.repo, self.owner, pr, round)
            # The next round's reviewer must stay free: a fixed reviewer, or a sole pool vendor.
            pool = self.env.config.quota.vendor_pool
            exclude = "cursor" if reviewer == "cursor" else ("" if len(pool) > 1 else pool[0])
            fixer = pick.choose(self.env, "fixer", fixer, "contrib", self.vendors(), pr=str(pr), exclude=exclude,
                                issue=self.issue)
            self.values["fixer"] = fixer
            self.save()
            runner = fix.Runner(self.env, self.hooks, pr, str(round))
            rc = fix.fix_pr(runner, pr, wt, fixer, f"the reviewer agent (round {round})", feedback, fix_head)
            if rc == 75:
                return 75
            if rc != 0:
                self.drop()
                return 0
            self.fixed_by = fixer
            round, step = round + 1, "review"
        self.drop()
        merge.Merger(self.env, self.hooks, pr, self.issue).merge_or_handoff(wt)
        return 0


@session.scoped
def run(args, env=None, note=""):
    """The loop for one PR; the JSON for the bash caller."""
    env = session.resolve(env)
    env = env.with_runtime(issue=args.issue)
    hooks = telemetry.from_env(env, issue=args.issue, pr=args.pr)
    review = Review(env, hooks, args.pr, args.wt, args.issue, args.fixed_by)
    try:
        return fix.result(review.loop(note, args.fixer, args.reviewer))
    except Deferred as deferred:
        return fix.result(75, defer_vendor=deferred.vendor, defer=deferred.reason)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    loop = sub.add_parser("loop", help="the round-1 note on stdin")
    for name in ("pr", "wt"):
        loop.add_argument(f"--{name}", required=True)
    loop.add_argument("--fixer", default="agent")
    loop.add_argument("--reviewer", default="pool")
    loop.add_argument("--fixed-by", default="")
    loop.add_argument("--issue", default="")
    sub.add_parser("feedback").add_argument("pr")
    sub.add_parser("issue-context").add_argument("issue")
    args = parser.parse_args(argv)
    env = session.resolve()
    if args.command == "feedback":
        print(human_feedback(env.repo.gh_repo, env.owner, args.pr))
        return 0
    if args.command == "issue-context":
        sys.stdout.write(issue_context(env, args.issue))
        return 0
    out = run(args, env, sys.stdin.read())
    print(json.dumps(out, ensure_ascii=False))
    return out["rc"]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"review: {exc}", file=sys.stderr)
        sys.exit(1)
