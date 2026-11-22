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

from . import advisor, agents, check_verdicts, checkpoint, checks as check_section, dispatch, fix, handoff as owner_handoff, hooks as telemetry, merge, notify, pick, pr as pr_helpers, prompts, vendors
from .common import Deferred, Exhausted, changed, git, restore, strip_markers

REPLY_CAP = 60000
ALREADY = "handed_off"   # fail_reason: review_allowed already handed the PR over
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


def agent_comment(repo, owner, pr, marker, round):
    """The body of the owner-authored comment with MARKER (baton-review or baton-screenshot-review)
    for that round, the latest one; "" when there is none."""
    result = pr_helpers.gh("pr", "view", str(pr), "-R", repo, "--json", "comments")
    if result.returncode != 0:
        return ""
    prefix = f"<!-- {marker} round={round} "
    bodies = [c.get("body") or "" for c in json.loads(result.stdout).get("comments") or ()
              if (c.get("author") or {}).get("login") == owner and (c.get("body") or "").startswith(prefix)]
    return bodies[-1].rstrip("\n") if bodies else ""


def agent_review(repo, owner, pr, round):
    """The body of the reviewer agent's comment for that round; "" when there is none."""
    return agent_comment(repo, owner, pr, "baton-review", round)


def must_fix_first(text):
    """Return the marker and Must fix section, other sections, and bare findings."""
    head, _, rest = text.partition("\n")
    kept, section, on = [], [], False
    for line in rest.split("\n"):
        if line.startswith("## "):
            on = line.startswith("## Must fix")
        (section if on else kept).append(line)
    findings = "\n".join(section[1:]).strip()
    if findings.lower() in ("none", "none."):
        findings = ""
    return "\n".join([head, *section]), "".join(f"\n{line}" for line in kept), findings


def combined_feedback(code, screenshot, limit):
    """The code review comment, a blank line, then the screenshot review comment, within LIMIT
    characters. When it overflows each comment gets half of the room, an unused share goes to the
    longer one, and a cut comment keeps its Must fix section first."""
    joined = f"{code}\n\n{screenshot}"
    if len(joined) <= limit:
        return joined
    code_parts, shot_parts = must_fix_first(code), must_fix_first(screenshot)
    code_findings, shot_findings = code_parts[2], shot_parts[2]
    if limit <= 2:
        return (code_findings or shot_findings or code)[:limit]
    room = limit - 2
    first = min(len(code), room // 2)
    second = min(len(screenshot), room - first)
    first = min(len(code), room - second)
    if len(code_findings) + len(shot_findings) <= room:
        first = max(len(code_findings), min(first, room - len(shot_findings)))
        second = room - first

    def fit(text, parts, size):
        if len(text) <= size:
            return text
        priority, rest, findings = parts
        if findings and len(priority) > size:
            return findings[:size]
        note = "\n[cut]"
        if len(priority) + len(note) <= size:
            return (priority + rest)[:size - len(note)] + note
        return (priority + rest)[:size]

    return f"{fit(code, code_parts, first)}\n\n{fit(screenshot, shot_parts, second)}"


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
        self.fail_reason = ""   # the handoff reason of the last failed run; ALREADY when it handed off itself

    def failed(self, reason, outcome):
        """Classify a runner-generated failure where it is produced (#125)."""
        self.fail_reason = reason
        return outcome

    # --- small steps every gate shares ---
    def label(self, *options):
        pr_helpers.gh("pr", "edit", str(self.pr), "-R", self.repo, *options)

    def handoff(self, reason, title, text, *, snapshot="", revalidate=None):
        """Terminal owner handoff through the shared entry (#125). Returns the entry Result."""
        return owner_handoff.perform(
            self.env, reason, kind="pr", number=self.pr,
            title=title, text=text, hooks=self.hooks, snapshot=snapshot, revalidate=revalidate)

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
        """The reviewer with the fewest lines in the PR. When every vendor is excluded, Exhausted
        ends the step with a ready-for-human handoff."""
        return pick.choose(self.env, "reviewer", current, "review", self.vendors(), pr=str(self.pr),
                           issue=self.issue, fixed_by=self.fixed_by, pinned=current == "cursor")

    def review_allowed(self, vendor, model=""):
        """Persisted authors survive restarts and role changes: a vendor that wrote the PR does
        not review it. The PR goes to the owner otherwise."""
        cfg = self.env.config
        if vendor not in cfg.declared() or not cfg.allows(vendor, "reviewer"):
            self.handoff("reviewer_role_denied", f"PR #{self.pr} needs you",
                         f"Reviewer {vendor} is not configured for code review.")
            return False
        if vendors.review_allowed(self.state, self.pr, vendor, model, self.fixed_by):
            return True
        self.handoff("review_independence_denied", f"PR #{self.pr} needs you",
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
            NIT_RULE=nit_rule(self.repo, self.issue)), f"pr-{pr}-cursor-review-{round}",
            env=self.env.with_runtime(review_followup=self.review_followup(round, "cursor")))
        outcome, reply = out["outcome"], out["reply"] or ""
        if changed(wt, head):
            restore(wt, head)
            return "RESULT: failed Cursor changed files in a read-only review", reply
        if not outcome.startswith(REVIEWED):
            return outcome, reply
        posted = "\n".join(line for line in strip_markers(reply).split("\n") if not MARKER_LINE.match(line)).rstrip("\n")
        comment = pr_helpers.gh("pr", "comment", str(pr), "-R", self.repo, "--body",
                                f"<!-- baton-review round={round} by=cursor -->\n{posted[:REPLY_CAP]}")
        if comment.returncode != 0:
            return self.failed("review_comment_post_failed", "RESULT: failed posting Cursor's review"), reply
        return outcome, reply

    def run_review(self, round, note, reviewer=None):
        """One reviewer run; returns OUTCOME. Use the loop's selected reviewer when supplied.
        The code-review skill (two sub-agents) only pays off on a big diff.
        Deferred when the reviewer cannot run now."""
        pr, wt = self.pr, self.wt
        self.hooks.round = round
        head = git(wt, "rev-parse", "HEAD") or ""
        if not self.seed():
            return self.failed("review_author_state_failed", "RESULT: failed recording PR authors")
        if reviewer is None:
            reviewer = self.review_vendor("pool")
        followup = self.review_followup(round, reviewer)
        expected = self.env.config.models.route("review", reviewer, followup=followup).model
        if not self.review_allowed(reviewer, expected):
            return self.failed(ALREADY, "RESULT: failed reviewer model may be a PR author")
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
            MAX_ROUNDS=str(self.max_rounds), ROUND_NOTE=note, SKILL_STEP=skill,
            CONTEXT_FILE=str(context),
            NIT_RULE=nit_rule(self.repo, self.issue)), f"pr-{pr}-review-{round}",
            str(self.self_dir / "review-settings.json"), str(review_file),
            self.env.with_runtime(review_followup=followup))
        outcome, model = out["outcome"], out["model"] or ""
        if not self.review_allowed(reviewer, model):
            return self.failed(ALREADY, "RESULT: failed actual reviewer model may be a PR author")
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
                    outcome = self.failed("review_comment_post_failed", "RESULT: failed posting review")
            else:
                outcome = self.failed("review_run_failed", "RESULT: failed no review artifact")
        self.hooks.verdict("review", reviewer, head, round, outcome, out["reply"] or "", str(review_file))
        return outcome

    # --- the screenshot review (#67): its own run after a successful code review ---
    def screenshot_enabled(self, reviewer):
        """Whether to check the PR body for screenshot review after this code review."""
        return (self.env.repo.screenshot_review
                and not agents.ADAPTERS[reviewer].SKIP_SCREENSHOT_AFTER_REVIEW
                and "owner" not in vendors.review_excluded(self.state, self.pr))

    def screenshot_vendor(self):
        """Claude or Codex, by the code review's exclusions; Cursor never looks at screenshots."""
        return pick.choose(self.env, "screenshot_reviewer", "", "review", self.vendors(), pr=str(self.pr),
                           issue=self.issue, fixed_by=self.fixed_by)

    def shots_step(self, shots):
        """The screenshot reviewer's look at the manual-check screenshots (#50): every valid PNG,
        or one Must fix when there are none."""
        if not shots:
            return (f"This PR has `## {check_section.HEADING}` but there are no screenshots: report one Must fix asking the "
                    "author to shoot every manual-check item the way the repository's agent docs describe.")
        listed = "".join(f"\n   - `{path}`" for path in shots)
        return ("Look at every one of the screenshots of this PR's manual checks (Claude: the Read tool; "
                f"Codex: view_image):{listed}\n"
                "   Compare each with the manual checks above and the issue.")

    def run_screenshot_review(self, round, checks):
        """One screenshot reviewer run and its comment; returns OUTCOME. Deferred when the reviewer
        cannot run now. It never hands the PR over: the loop does, once, on a failed outcome."""
        pr, wt = self.pr, self.wt
        self.hooks.round = round
        head = git(wt, "rev-parse", "HEAD") or ""
        vendor = self.screenshot_vendor()
        cfg = self.env.config
        if vendor not in cfg.declared() or not cfg.allows(vendor, "screenshot_reviewer"):
            return self.failed("screenshot_reviewer_role_denied", "RESULT: failed screenshot reviewer not role-allowed")
        route = self.env.config.models.route("screenshot_review", vendor)
        if not vendors.screenshot_allowed(self.state, pr, vendor, route.model, self.fixed_by):
            return self.failed("screenshot_independence_denied", "RESULT: failed screenshot reviewer model may be a PR author")
        limit = int(self.env.config.scheduling.review_context_max_chars or 20000)
        context = Path(self.ctx, f"pr-{pr}-screenshot-review-{round}-{time.strftime('%Y%m%d-%H%M%S')}.md")
        context.write_text(issue_context(self.env, self.issue) if re.fullmatch(r"\d+", str(self.issue or "")) else "")
        review_file = Path(f"/tmp/baton-screenshot-review-{pr}-{round}.md")
        review_file.unlink(missing_ok=True)
        delivery = ("Include the full review in your final message, above the verdict line." if vendor == "codex"
                    else f"Write the comment to `{review_file}` with the Write tool.")
        out = self.agent(vendor, "screenshot_review", self.render(
            "screenshot-review-prompt.md", PR=str(pr), ISSUE=self.issue or "unknown", GH_REPO=self.repo,
            WORKTREE=str(wt), ROUND=str(round), MAX_ROUNDS=str(self.max_rounds), CONTEXT_FILE=str(context),
            MANUAL_CHECKS=cut(check_verdicts.numbered(checks), limit), SHOTS_STEP=self.shots_step(notify.shots(pr, self.ctx)), DELIVERY=delivery),
            f"pr-{pr}-screenshot-review-{round}", str(self.self_dir / "review-settings.json"), str(review_file), self.env)
        outcome, model = out["outcome"], out["model"] or ""
        if not vendors.screenshot_allowed(self.state, pr, vendor, model, self.fixed_by):
            return self.failed("screenshot_independence_denied",
                               "RESULT: failed actual screenshot reviewer model may be a PR author")
        if outcome.startswith(REVIEWED):
            if review_file.is_file() and review_file.stat().st_size:
                body = strip_markers(review_file.read_text()).rstrip("\n")
                mode = (vendors.codex_review_mode(self.state, pr, model)
                        if vendor == "codex" and "codex" in vendors.review_excluded(self.state, pr, self.fixed_by) else "")
                review_note = f"Review mode: {mode} (codex/{model}).\n\n" if mode else ""
                posted = pr_helpers.gh("pr", "comment", str(pr), "-R", self.repo, "--body",
                                       f"<!-- baton-screenshot-review round={round} by={vendor} -->\n{review_note}{body}")
                if posted.returncode != 0:
                    outcome = self.failed("screenshot_comment_post_failed", "RESULT: failed posting screenshot review")
            else:
                outcome = self.failed("screenshot_run_failed", "RESULT: failed no screenshot review artifact")
        self.hooks.verdict("screenshot_review", vendor, head, round, outcome, out["reply"] or "", str(review_file))
        return outcome

    # --- the checkpoint ---
    def save(self):
        checkpoint.write(self.checkpoint, {"pr": self.pr, "wt": self.wt, "ISSUE": self.issue, **self.values,
                                           "fixed_by": self.fixed_by})

    def drop(self):
        self.checkpoint.unlink(missing_ok=True)

    # --- the round limit: the advisor may grant one more round (#17 / #125) ---
    def limit_snapshot(self, round, reviewer, fixer):
        """(snapshot, revalidate) for the review_limit decision point."""
        pr, wt = self.pr, self.wt
        head = git(wt, "rev-parse", "HEAD") or ""
        numstat = git(wt, "diff", "--numstat", "origin/main...HEAD") or ""
        files = len(numstat.splitlines())
        lines = sum(int(f) for row in numstat.splitlines() for f in row.split()[:2] if f.isdecimal())
        reviews = "\n\n".join(f'<review round="{r}">\n{agent_review(self.repo, self.owner, pr, r)}\n</review>'
                              for r in range(1, round + 1))
        snapshot = (f"Decision point: review round limit. PR #{pr} of {self.repo} used all {round} review rounds "
                    f"(max_review_rounds = {self.max_rounds}) and the last review still has must-fix items.\n"
                    f"Last reviewer: {reviewer}; last fixer: {self.fixed_by or fixer or 'none'}. "
                    f"Diff: {files} files, {lines} lines changed.\n"
                    "Actions: extra_round = one more fix by a fixer the vendor rules pick, then one more review; "
                    "handoff = the owner takes the PR.\n\n"
                    f"{reviews}\n\n{review_context(self.env, pr, self.issue)}")

        def still_current():
            data = pr_helpers.view(pr, "headRefOid,state")
            return bool(data) and data.get("state") == "OPEN" and data.get("headRefOid") == head
        return snapshot, still_current

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
        code_verdict = screenshot = ""   # the round's code verdict, and "1" when it had a screenshot review
        checks = ""
        extra = 0   # the advisor's one extra round (#17), kept in the checkpoint of the episode it was granted for
        saved = checkpoint.read(self.checkpoint)
        if saved:
            note, fixer, reviewer = saved.get("note", ""), saved.get("fixer", ""), saved.get("reviewer", "")
            round = int(saved["round"]) if saved.get("round", "").isdecimal() else 1
            step, fix_head = saved.get("step") or "review", saved.get("fix_head", "")
            code_verdict, screenshot = saved.get("code_verdict", ""), saved.get("screenshot", "")
            checks = saved.get("screenshot_checks", "")
            extra = 1 if saved.get("extra") == "1" else 0
            self.fixed_by, self.issue = saved.get("fixed_by", ""), saved.get("ISSUE", "")
        cfg = self.env.config
        if reviewer not in ("pool",) and (reviewer not in cfg.declared() or not cfg.allows(reviewer, "reviewer")):
            reviewer = "pool"
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
            self.values = dict(note=note, fixer=fixer, reviewer=reviewer, round=round, step=step, fix_head=fix_head,
                               code_verdict=code_verdict, screenshot=screenshot, screenshot_checks=checks,
                               extra="1" if extra else "")
            self.save()
            if step in ("review", "screenshot"):
                verdict = code_verdict
                if step == "review":
                    try:
                        reviewer = self.review_vendor(reviewer)
                    except Exhausted as exhausted:
                        self.handoff("reviewer_unavailable", f"PR #{pr} needs you",
                                     exhausted.reason or "No eligible reviewer for this PR.")
                        self.drop()
                        return 0
                    self.values["reviewer"] = reviewer
                    self.save()
                    self.fail_reason = ""
                    outcome = self.run_review(round, rnote, reviewer)
                    if not outcome.startswith(REVIEWED):
                        if self.fail_reason != ALREADY:   # a refusal handed off where it was decided
                            self.handoff(self.fail_reason or "review_run_failed", f"PR #{pr} review failed ({reviewer})",
                                         f"{outcome} {self.url}")
                        self.drop()
                        return 0
                    verdict = "pass" if outcome.startswith("REVIEW: pass") else "changes"
                if step == "screenshot" or self.screenshot_enabled(reviewer):
                    body = pr_helpers.body(pr)
                    # If the body read fails on resume, keep the checks saved with the code verdict.
                    if body is not None:
                        if check_section.items(body):
                            checks = check_section.read(body)
                        else:
                            checks = ""
                if checks:
                    step = "screenshot"
                    code_verdict = verdict
                    self.values.update(step=step, code_verdict=code_verdict, screenshot_checks=checks)
                    self.save()
                    self.fail_reason = ""
                    try:
                        outcome = self.run_screenshot_review(round, checks)
                    except Exhausted as exhausted:
                        outcome = self.failed("screenshot_reviewer_unavailable",
                                              exhausted.reason or "No screenshot reviewer available.")
                    if not outcome.startswith(REVIEWED):
                        self.handoff(self.fail_reason or "screenshot_run_failed", f"PR #{pr} screenshot review failed",
                                     f"{outcome} {self.url}")
                        self.drop()
                        return 0
                    screenshot = "1"
                    if outcome.startswith("REVIEW: changes"):
                        verdict = "changes"
                if verdict == "pass":
                    break
                if round >= self.max_rounds + extra:
                    snap, check = self.limit_snapshot(round, reviewer, fixer)
                    result = self.handoff(
                        "review_limit", f"PR #{pr} needs you",
                        f"Still has must-fix items after {round} review rounds. {self.url}",
                        snapshot=snap, revalidate=check)
                    if result.action == "extra_round":
                        extra = 1
                        self.values["extra"] = "1"
                        notify.log(f"PR #{pr}: advisor granted one more round: {result.decision.reason}")
                    else:
                        self.drop()
                        return 0
            if step != "fix":
                fix_head = git(wt, "rev-parse", "HEAD") or ""
            step = "fix"
            self.values.update(step=step, fix_head=fix_head, code_verdict=code_verdict, screenshot=screenshot,
                               screenshot_checks="")
            self.save()
            if screenshot:
                # A split review: the fixer reads both comments, or none (an incomplete one is no feedback).
                code = agent_comment(self.repo, self.owner, pr, "baton-review", round)
                shots = agent_comment(self.repo, self.owner, pr, "baton-screenshot-review", round)
                if not (code and shots):
                    self.handoff("review_comment_missing", f"PR #{pr} review comment missing",
                                 f"A review comment of round {round} is unavailable. {self.url}")
                    self.drop()
                    return 0
                feedback = combined_feedback(code, shots, int(self.env.config.scheduling.review_context_max_chars or 20000))
            else:
                feedback = agent_review(self.repo, self.owner, pr, round)
            # The next round's reviewer must stay free: a fixed reviewer, or a sole pool vendor.
            pool = self.env.config.quota.vendor_pool
            exclude = "cursor" if reviewer == "cursor" else ("" if len(pool) > 1 else pool[0])
            fixer = pick.choose(self.env, "fixer", fixer, "contrib", self.vendors(), pr=str(pr), exclude=exclude,
                                issue=self.issue)
            self.values["fixer"] = fixer
            self.save()
            runner = fix.Runner(self.env.with_runtime(fix_followup=round > 1), self.hooks, pr, str(round))
            rc = fix.fix_pr(runner, pr, wt, fixer, f"the reviewer agent (round {round})", feedback, fix_head)
            if rc == 75:
                return 75
            if rc != 0:
                if not runner.handed_off:   # e.g. authorship recording failed: the owner still needs one handoff
                    runner.handoff(pr, "fix_task_write_failed", f"PR #{pr} needs you",
                                   f"The fix run of round {round} failed before a handoff. {self.url}")
                self.drop()
                return 0
            self.fixed_by = fixer
            round, step = round + 1, "review"
            code_verdict = screenshot = checks = ""
        self.drop()
        # The last round's screenshot verdicts decide which manual checks still need the owner (#92).
        shots = ({"checks": checks, "comment": agent_comment(self.repo, self.owner, pr, "baton-screenshot-review", round)}
                 if screenshot else None)
        merge.Merger(self.env, self.hooks, pr, self.issue).merge_or_handoff(wt, shots)
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
