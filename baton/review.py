"""The review loop for the bash runner (migration slice 4c, #280): `run_review`,
`review_loop` and the review context of run.sh.

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
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from typing import NamedTuple

from . import advisor, agents, check_verdicts, checkpoint, checks as check_section, design_findings, dispatch, fix, handoff as owner_handoff, hooks as telemetry, merge, notify, pick, pr as pr_helpers, prompts, vendors
from .common import Deferred, Exhausted, changed, git, restore, strip_markers
from . import recovery as task_recovery

REPLY_CAP = 60000
ALREADY = "handed_off"   # fail_reason: review_allowed already handed the PR over
REVIEWED = ("REVIEW: pass", "REVIEW: changes")
DEFERRED_POST = "RESULT: deferred category-1"
MARKER_LINE = re.compile(r"^(REVIEW|RESULT): ")
FIX_NEEDS_INFO = re.compile(r"^<!-- baton-fix\b[^>]*\boutcome=needs-info\b")
REVIEW_ROUND = re.compile(r"^<!-- baton-review\b[^>]*\bround=(\d+)\b")
OWNER_WORDS_NOTE = "Later items override earlier ones where they conflict."


def continuity_pin(reviewer):
    """Whether this selection retains the reviewer across short blocks (CONTINUITY_PIN)."""
    adapter = agents.ADAPTERS.get(reviewer)
    return bool(adapter and adapter.CONTINUITY_PIN)


class HumanFeedback(NamedTuple):
    """Owner instructions and the latest review findings for an agent-fix run (#178)."""
    owner: str = ""
    findings: str = ""


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


def issue_context_from(issue, data, owner, login_re, limit):
    """Format an already-fetched issue payload; empty when DATA is missing."""
    if not data:
        return ""
    lines = [f'<issue number="{issue}">\n# {jq_str(data.get("title"))}\n\n{cut(data.get("body"), limit)}\n</issue>']
    for comment in data.get("comments") or ():
        login = (comment.get("author") or {}).get("login") or ""
        body = comment.get("body") or ""
        # Advisor answers are runner-wrapped owner posts: keep them out of owner context (#125).
        if login == owner and re.match(
                r"<!-- baton-advisor-answer round=\d+ questions=[0-9a-f]+ by=deepseek -->", body):
            safe = cut(body, limit).replace("</advisor-answer>", "<\\/advisor-answer>")
            lines.append(f'<advisor-answer author="DeepSeek">\n{safe}\n</advisor-answer>')
            continue
        if login == owner or re.search(login_re, login):
            lines.append(f'<issue-comment author="{login}">\n{cut(body, limit)}\n</issue-comment>')
    return "".join(f"{line}\n" for line in lines)


@session.scoped
def issue_context(env, issue, data=None):
    """The issue and its comments: only the owner's, Cursor's and the agents' are kept.

    DATA skips the GitHub read when the caller already fetched title, body and comments (#151).
    """
    env = session.resolve(env)
    owner, limit = env.owner, int(env.config.scheduling.review_context_max_chars or 20000)
    login_re = env.config.scheduling.cursor_login_re or r"^(app/cursor|cursor(\[bot\])?)$"
    if data is None:
        result = pr_helpers.gh("issue", "view", str(issue), "-R", env.repo.gh_repo, "--json", "title,body,comments")
        if result.returncode != 0:
            return ""
        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError):
            return ""
    return issue_context_from(issue, data, owner, login_re, limit)


@session.scoped
def review_context(env, pr, issue, data=None):
    """The issue, then the PR with its comments, reviews and inline comments. Context files
    keep only the comments that count (public repo: others dropped); each body is cut."""
    env = session.resolve(env)
    owner, repo, limit = env.owner, env.repo.gh_repo, int(env.config.scheduling.review_context_max_chars or 20000)
    login_re = env.config.scheduling.cursor_login_re or r"^(app/cursor|cursor(\[bot\])?)$"

    def kept(login):
        return login == owner or bool(re.search(login_re, login))
    text = issue_context(env, issue) if re.fullmatch(r"\d+", str(issue or "")) else ""
    if data is None:
        result = pr_helpers.gh("pr", "view", str(pr), "-R", repo, "--json", "title,body,comments,reviews")
        if result.returncode == 0:
            data = json.loads(result.stdout)
    lines = []
    if data is not None:
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
    inline = data.get("inline") if data is not None else None
    for comment in inline if inline is not None else paginated(["api", f"repos/{repo}/pulls/{pr}/comments", "--paginate"]):
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
    return read_agent_comment(repo, owner, pr, marker, round)[0]


def read_agent_comment(repo, owner, pr, marker, round):
    """Return the comment and whether an unavailable comment is worth another read (#151)."""
    result = pr_helpers.gh("pr", "view", str(pr), "-R", repo, "--json", "comments")
    if result.returncode != 0:
        return "", pr_helpers.transient(result)
    try:
        comments = json.loads(result.stdout).get("comments") or ()
    except (AttributeError, ValueError, TypeError):
        return "", False
    body = marked_comment(comments, owner, marker, round)
    return body, not body


def marked_comment(comments, owner, marker, round):
    prefix = f"<!-- {marker} round={round} "
    bodies = [c.get("body") or "" for c in comments
              if (c.get("author") or {}).get("login") == owner and (c.get("body") or "").startswith(prefix)]
    return bodies[-1].rstrip("\n") if bodies else ""


FIX_MARKER = re.compile(r"^<!-- baton-fix\b[^>]*\bloop=(\S+)\s+round=(\d+)\b[^>]*-->[ \t]*\n?")


def fix_replies(comments, owner, loop_id, low, high):
    """The bodies (marker stripped) of this loop's fix comments with LOW <= round < HIGH: the
    fixer's answers since the previous screenshot review. A comment of another loop, or with no
    loop id, is never an answer."""
    found = []
    for comment in comments or ():
        body = comment.get("body") or ""
        match = FIX_MARKER.match(body)
        if (match and loop_id and match.group(1) == loop_id and (comment.get("author") or {}).get("login") == owner
                and low <= int(match.group(2)) < high):
            found.append(body[match.end():])
    return found


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


class DesignOverflow(Exception):
    """The design records alone do not fit review_context_max_chars (#96)."""


def combined_feedback(code, screenshot, limit, open_block=""):
    """The code review comment, a blank line, then the screenshot review comment, within LIMIT
    characters. When it overflows each comment gets half of the room, an unused share goes to the
    longer one, and a cut comment keeps its Must fix section first.

    The screenshot comment's `## Design findings` section and OPEN_BLOCK (the open design findings
    block, with its heading) are complete records (#96): never cut, kept whole ahead of the rest,
    which takes the remaining room. DesignOverflow when they alone exceed LIMIT."""
    joined = f"{code}\n\n{screenshot}" if screenshot or not open_block else code
    if open_block:
        joined += f"\n\n{open_block}"
    if len(joined) <= limit:
        return joined
    shot_rest, design = design_findings.split_section(screenshot)
    if re.sub(r"[-*.\s]", "", design.partition("\n")[2]).lower() in ("", "none"):   # nothing to keep whole
        shot_rest, design = screenshot, ""
    tail = "\n\n".join(part for part in (design, open_block) if part)
    if not tail:
        return plain_feedback(code, screenshot, limit)
    if len(tail) > limit:
        raise DesignOverflow
    room = limit - len(tail) - 2
    if room <= 0:
        return tail
    return f"{plain_feedback(code, shot_rest, room).rstrip()}\n\n{tail}"


def plain_feedback(code, screenshot, limit):
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


def read_feedback_history(repo, pr, request):
    data = {}
    for field, route, resource in (("comments", "issues", "comments"), ("reviews", "pulls", "reviews"),
                                   ("inline", "pulls", "comments")):
        result = request("api", f"repos/{repo}/{route}/{pr}/{resource}", "--paginate", "--slurp")
        if result.returncode != 0:
            return None
        try:
            pages = json.loads(result.stdout)
            if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
                return None
            rows = [row for page in pages for row in page]
            if any(type(row["id"]) is not int or row["id"] <= 0
                   or (row["user"] is not None and not isinstance(row["user"]["login"], str))
                   or (not isinstance(row["body"], str) and not (field == "reviews" and row["body"] is None))
                   or (field == "reviews" and not isinstance(row["state"], str)) for row in rows):
                return None
            if len({row["id"] for row in rows}) != len(rows):
                return None
            data[field] = [{**row, "author": row["user"], "body": row["body"] or ""} for row in rows]
        except (ValueError, KeyError, TypeError):
            return None
    return data


def owner_feedback_digest(owner, data):
    rows = [
        (kind, row["id"], row["state"] if kind == "reviews" else "", row["body"])
        for kind in ("comments", "reviews", "inline")
        for row in data[kind]
        if (row["author"] or {}).get("login") == owner and not row["body"].startswith("<!-- baton")
    ]
    return owner_handoff.body_digest(json.dumps(sorted(rows)))


def ordinary_fix_cutoff(comments, owner):
    """The createdAt of the latest owner-authored baton-fix without outcome=needs-info."""
    fixes = []
    for c in comments or ():
        if (c.get("author") or {}).get("login") != owner:
            continue
        body = c.get("body") or ""
        if body.startswith("<!-- baton-fix") and not FIX_NEEDS_INFO.match(body):
            fixes.append(c.get("createdAt") or "")
    return max(fixes, default="")


def must_fix_section(body):
    """The ## Must fix section of a review comment, or empty when it is none."""
    findings = must_fix_first(body or "")[2]
    return f"## Must fix\n{findings}" if findings else ""


def review_findings(comments, owner):
    """Must fix of the latest owner baton-review, plus the same-round screenshot review after it."""
    latest_i, latest_body, round_no = -1, "", ""
    for i, c in enumerate(comments or ()):
        if (c.get("author") or {}).get("login") != owner:
            continue
        body = c.get("body") or ""
        match = REVIEW_ROUND.match(body)
        if match:
            latest_i, latest_body, round_no = i, body, match.group(1)
    if latest_i < 0:
        return ""
    parts = [must_fix_section(latest_body)]
    prefix = f"<!-- baton-screenshot-review round={round_no} "
    shot = ""
    for c in (comments or ())[latest_i + 1:]:
        if (c.get("author") or {}).get("login") != owner:
            continue
        body = c.get("body") or ""
        if body.startswith(prefix):
            shot = body
    if shot:
        parts.append(must_fix_section(shot))
    return "\n\n".join(p for p in parts if p)


def owner_words(reviews, comments, inline, owner, since):
    """Owner reviews, PR comments and inline comments after SINCE, oldest first."""
    items = []
    for r in reviews or ():
        if ((r.get("author") or {}).get("login") == owner and r.get("body")
                and not r["body"].startswith("<!-- baton") and (r.get("submittedAt") or "") > since):
            items.append((r.get("submittedAt") or "", f"Review ({r.get('submittedAt')}):\n{r['body']}"))
    for c in comments or ():
        body = c.get("body") or ""
        if ((c.get("author") or {}).get("login") == owner and not body.startswith("<!-- baton")
                and (c.get("createdAt") or "") > since):
            items.append((c.get("createdAt") or "", f"Comment ({c.get('createdAt')}):\n{body}"))
    for comment in inline or ():
        if ((comment.get("user") or {}).get("login") == owner and (comment.get("created_at") or "") > since
                and not (comment.get("body") or "").startswith("<!-- baton")):
            line = comment.get("line") if comment.get("line") is not None else comment.get("original_line")
            text = (f"Inline comment on {jq_str(comment.get('path'))}:{jq_str(line)}:\n"
                    f"{comment.get('body')}")
            items.append((comment.get("created_at") or "", text))
    items.sort(key=lambda item: item[0])
    bodies = [text for _, text in items]
    if not bodies:
        return ""
    joined = "\n\n---\n\n".join(bodies)
    if len(bodies) > 1:
        return f"{OWNER_WORDS_NOTE}\n\n{joined}"
    return joined


def format_fix_feedback(owner, findings):
    """Owner instructions plus an optional findings block for the fixer and next reviewer."""
    owner = (owner or "").rstrip("\n")
    findings = (findings or "").strip()
    if not findings:
        return owner
    return (f"{owner}\n\n<findings>\n"
            "The reviewer's findings below are for reference only; they are in scope only as far "
            "as the owner's words above ask for them.\n\n"
            f"{findings}\n</findings>")


def read_rows(repo, route, pr, resource):
    """Every row of a PR's paginated REST list, or None when a page is missing or malformed."""
    result = pr_helpers.gh("api", f"repos/{repo}/{route}/{pr}/{resource}", "--paginate", "--slurp")
    if result.returncode != 0:
        return None
    try:
        pages = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        return None
    rows = [row for page in pages for row in page]
    return rows if all(isinstance(row, dict) for row in rows) else None


def human_feedback(repo, owner, pr):
    """Owner words since the last ordinary fix, and Must fix findings from the latest review.
    None when any of the three lists could not be read completely: a partial read would move
    the cutoff or drop a correction, so the caller retries."""
    comments = read_rows(repo, "issues", pr, "comments")
    reviews = read_rows(repo, "pulls", pr, "reviews")
    inline = read_rows(repo, "pulls", pr, "comments")
    if comments is None or reviews is None or inline is None:
        return None
    comments = [{"author": c.get("user"), "body": c.get("body") or "", "createdAt": c.get("created_at")}
                for c in comments]
    reviews = [{"author": r.get("user"), "body": r.get("body") or "", "submittedAt": r.get("submitted_at")}
               for r in reviews]
    since = ordinary_fix_cutoff(comments, owner)
    findings = review_findings(comments, owner)
    return HumanFeedback(owner=owner_words(reviews, comments, inline, owner, since), findings=findings)


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
        self.binding_fields = {}
        self.fail_reason = ""   # the handoff reason of the last failed run; ALREADY when it handed off itself

    def failed(self, reason, outcome):
        """Classify a runner-generated failure where it is produced (#125)."""
        self.fail_reason = reason
        return outcome

    # --- small steps every gate shares ---
    def label(self, *options):
        pr_helpers.gh("pr", "edit", str(self.pr), "-R", self.repo, *options)

    def handoff(self, reason, title, text, *, snapshot="", revalidate=None, transient=False,
                continuation=None, resume=False, observed_actions=None, fallback=False):
        """Owner handoff or category-1 retry through the shared entry (#125, #151)."""
        return owner_handoff.perform(
            self.env, reason, kind="pr", number=self.pr,
            title=title, text=text, hooks=self.hooks, item=self.pr, episode=self.issue or self.pr,
            snapshot=snapshot, revalidate=revalidate, observed_actions=observed_actions,
            transient=transient, continuation=continuation, resume=resume, fallback=fallback)

    def retry_binding(self, role):
        evidence = None
        if role == "screenshot_reviewer":
            try:
                evidence = {"screenshots": {str(path): hashlib.sha256(Path(path).read_bytes()).hexdigest()
                                            for path in notify.shots(self.pr, self.ctx)}}
            except OSError:
                return None
        return task_recovery.worker_fingerprint(self.wt, self.pr, round_s=self.values.get("round", ""),
                                                extra=evidence)

    def retry_picker(self, pinned):
        return dict(mode="review", candidates=self.vendors(), pr=str(self.pr), issue=self.issue,
                    fixed_by=self.fixed_by, pinned=pinned)

    def recover_worker(self, reason, title, detail, vendor, *, role="reviewer", pinned=False,
                       fallback=False):
        binding = lambda: self.retry_binding(role)
        fingerprint = getattr(self, "worker_start", {}).get(role)
        if role not in getattr(self, "worker_start", {}):
            fingerprint = binding()
        result = task_recovery.request(
            self.env, self.issue or self.pr, self.pr, reason, role, vendor, detail, fingerprint, binding,
            lambda text=detail, **kw: self.handoff(reason, title, f"{text} {self.url}",
                                                   fallback=fallback, **kw),
            local=reason == "review_author_state_failed", **self.retry_picker(pinned))
        return (result[0]["action"], result[1]) if result else (owner_handoff.HANDOFF, vendor)

    def resume_worker(self, role, *, pinned=False):
        prefix = "screenshot_" if role == "screenshot_reviewer" else "review_"
        return task_recovery.resume(
            self.env, self.issue or self.pr, self.pr,
            lambda reason: reason.startswith(prefix) and reason != "review_author_state_failed",
            lambda: self.retry_binding(role),
            lambda text: self.handoff("review_run_failed" if role == "reviewer" else "screenshot_run_failed",
                                      f"PR #{self.pr} needs you", text),
            **self.retry_picker(pinned))

    BINDING_FIELDS = ("reviewed_head", "base_branch", "reviewed_issue", "verdict_digest",
                      "verdict_round", "screenshot_digest", "feedback_waiting", "post_waiting", "post_outcome")
    REVISION_FIELDS = "headRefOid,baseRefName,headRefName,state,body,closingIssuesReferences"

    def read_post_inputs(self, *, once=False):
        """Read the PR and complete comment history used to bind or reconcile a review comment."""
        data, _ = self.read_pr("title,labels", once=once)
        if data is None:
            return None, "retry"
        if data["state"] != "OPEN":
            return None, "superseded"
        labels = data.get("labels")
        if not isinstance(labels, list) or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                                               for label in labels):
            return None, "retry"
        if any(label.get("name") == "ready-for-human" for label in labels):
            return None, "paused"
        history = read_feedback_history(self.repo, self.pr, pr_helpers.call if once else pr_helpers.gh)
        return ({**data, **history}, "ok") if history is not None else (None, "retry")

    def comment_post_binding(self, body, round, outcome, reviewer, head, data):
        return {
            "pr": str(self.pr), "round": round, "reviewer": reviewer, "author": self.owner,
            "head": head, "base": data["baseRefName"], "issue": data["linked_issue"],
            "body": body, "digest": owner_handoff.body_digest(body),
            "description": owner_handoff.body_digest(data["body"]),
            "feedback": owner_feedback_digest(self.owner, data),
            "comment_ids": [row["id"] for row in data["comments"]],
            "outcome": outcome, "run_id": self.hooks.last_run_id(),
        }

    def check_comment_post(self, binding, round, reviewer, *, sending, marker="baton-review",
                           step="review"):
        data, status = self.read_post_inputs(once=True)
        if status != "ok":
            return status
        if (self.values.get("step") != step or self.binding_fields.get("verdict_round") != str(round)
                or (round, reviewer, self.owner) != (binding["round"], binding["reviewer"], binding["author"])
                or git(self.wt, "rev-parse", "HEAD") != binding["head"]
                or (data["headRefOid"], data["baseRefName"], data["linked_issue"]) !=
                    (binding["head"], binding["base"], binding["issue"])
                or owner_handoff.body_digest(data["body"]) != binding["description"]
                or owner_feedback_digest(self.owner, data) != binding["feedback"]):
            return "changed"
        boundary = set(binding["comment_ids"])
        matches = []
        for row in data["comments"]:
            if row["id"] in boundary:
                continue
            trusted = (row["author"] or {}).get("login") == binding["author"]
            if row["body"] == binding["body"]:
                matches.append(trusted)
            elif trusted and row["body"].startswith(f"<!-- {marker} round="):
                return "changed"
        if not matches:
            return "absent"
        if sending and matches == [True]:
            return "changed"
        return "landed" if matches == [True] else "ambiguous"

    def post_pending(self):
        return (self.binding_fields.get("post_waiting") or self.binding_fields.get("post_outcome")
                or str(self.pr) in owner_handoff.comment_post_episodes(self.env))

    def screenshot_post_pending(self):
        waiting = self.binding_fields.get("post_waiting")
        return (waiting == owner_handoff.SCREENSHOT_POST
                or (self.values.get("step") == "screenshot" and bool(self.binding_fields.get("post_outcome")))
                or str(self.pr) in owner_handoff.comment_post_episodes(self.env, owner_handoff.SCREENSHOT_POST))

    def post_comment(self, round, reviewer, binding, *, reason, kind, marker, step, fail_label,
                     digest_field):
        """Save before sending, or consume a due attempt before reconciling the saved write (#151)."""
        pr, episode = str(self.pr), self.issue or self.pr
        title = f"PR #{pr} comment post failed"
        text = f"Posting the {fail_label} of round {round} failed. {self.url}"

        def stop(message):
            self.handoff(reason, title, f"{text} {message}")
            return self.failed(ALREADY, f"RESULT: failed posting {fail_label}")

        initial = binding is not None
        completed = self.binding_fields.get("post_outcome")
        digest = self.binding_fields.get(digest_field, "")
        if completed and (not completed.startswith(REVIEWED) or self.values.get("step") != step
                          or self.binding_fields.get("verdict_round") != str(round)
                          or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            return stop("The saved verdict is invalid.")
        try:
            pending = owner_handoff.load_retry(self.env, episode, reason, pr)
        except owner_handoff.RetryUnreadable as exc:
            if not completed:
                return stop(str(exc))
            # The post was confirmed before cleanup began. Its two files can be partly gone.
            owner_handoff.clear_retry(self.env, episode, reason, pr)
            pending = None
        if initial:
            self.save_binding(post_waiting=reason)
            if pending is not None:
                return DEFERRED_POST
            pending = owner_handoff.prepare_retry(self.env, episode, reason, pr, binding)
            if pending is None:
                return stop("The worker inputs could not be saved.")
        elif not completed:
            result = self.handoff(reason, title, text, resume=True)
            if result.action == owner_handoff.DEFER:
                return DEFERRED_POST
            if result.action != owner_handoff.RETRY:
                return self.failed(ALREADY, f"RESULT: failed posting {fail_label}")
            binding = result.continuation
            if reviewer is None:
                reviewer = binding["reviewer"]

        def finish():
            fields = {"post_outcome": binding["outcome"], digest_field: binding["digest"]}
            self.save_binding(**fields)
            self.hooks.verdict(kind, binding["reviewer"], binding["head"], binding["round"],
                               binding["outcome"], binding["body"], run_id=binding["run_id"])
            owner_handoff.clear_retry(self.env, episode, reason, pr)
            self.save_binding(post_waiting="")
            return binding["outcome"]

        if completed:
            if pending is not None:
                binding = pending["continuation"]
                if (completed, digest) != (binding["outcome"], binding["digest"]):
                    return stop("The saved verdict differs from its post.")
                owner_handoff.clear_retry(self.env, episode, reason, pr)
            self.save_binding(post_waiting="")
            return completed

        def reconcile(*, sending=False):
            nonlocal status
            status = self.check_comment_post(binding, round, reviewer, sending=sending,
                                             marker=marker, step=step)
            return status

        status = reconcile(sending=initial)
        posted = None
        if status == "absent":
            posted = pr_helpers.gh("pr", "comment", pr, "-R", self.repo, "--body", binding["body"],
                                   reconcile=reconcile)
            if posted.returncode == 0:
                return finish()
        if status == "landed":
            return finish()
        if status == "superseded":
            owner_handoff.clear_retry(self.env, episode, reason, pr)
            advisor.finish_episode(self.env, "pr", pr, "superseded")
            return self.failed(ALREADY, "RESULT: failed PR closed during comment post")
        if status == "paused":
            owner_handoff.restore_retry(self.env, episode, reason, pr, pending)
            return DEFERRED_POST
        if status == "retry" or status == "absent" and posted is not None and pr_helpers.transient(posted):
            attempts = pending["attempts"] + (0 if initial else 1)
            if attempts < owner_handoff.MAX_ATTEMPTS:
                now = int(owner_handoff.clock())
                owner_handoff.restore_retry(self.env, episode, reason, pr,
                                            {**pending, "attempts": attempts, "scheduled": now,
                                             "due": now + owner_handoff.DELAYS[attempts]})
                return DEFERRED_POST
        return stop(f"Reconciliation: {status}." + (pr_helpers.why(posted) if posted is not None else ""))

    def post_review_comment(self, round, reviewer, binding=None):
        """Save before sending, or consume a due attempt before reconciling the saved write."""
        return self.post_comment(
            round, reviewer, binding, reason=owner_handoff.REVIEW_POST, kind="review",
            marker="baton-review", step="review", fail_label="review comment",
            digest_field="verdict_digest")

    def post_screenshot_comment(self, round, reviewer=None, binding=None):
        """Save before sending a screenshot review, or resume a due attempt (#151)."""
        return self.post_comment(
            round, reviewer, binding, reason=owner_handoff.SCREENSHOT_POST, kind="screenshot_review",
            marker="baton-screenshot-review", step="screenshot", fail_label="screenshot review",
            digest_field="screenshot_digest")

    def read_pr(self, more, *, once=False):
        fields = f"{self.REVISION_FIELDS},{more}"
        request = pr_helpers.call if once else pr_helpers.gh
        result = request("pr", "view", str(self.pr), "-R", self.repo, "--json", fields)
        if result.returncode != 0:
            return None, pr_helpers.transient(result)
        try:
            data = json.loads(result.stdout)
            if not isinstance(data, dict) or data.get("state") not in ("OPEN", "MERGED", "CLOSED"):
                return None, False
            if data["state"] == "OPEN":
                if (not re.fullmatch(r"[0-9a-f]{40}", data["headRefOid"])
                        or not isinstance(data["baseRefName"], str) or not data["baseRefName"]
                        or not isinstance(data["headRefName"], str) or not isinstance(data["body"], str)):
                    return None, False
                if any(not isinstance(comment, dict) or not isinstance(comment.get("body"), str)
                       or not isinstance(comment.get("author") or {}, dict) for comment in data.get("comments", [])):
                    return None, False
                if "comments" in more.split(",") and not isinstance(data.get("comments"), list):
                    return None, False
                data["linked_issue"] = pr_helpers.linked_task(data, self.repo)[0]
            return data, False
        except (AttributeError, KeyError, TypeError, ValueError):
            return None, False

    def save_binding(self, **fields):
        self.binding_fields.update(fields)
        self.values.update(fields)
        self.save()

    def capture_feedback(self, body, field):
        self.save_binding(**{field: hashlib.sha256(body.encode()).hexdigest()})

    def binding_from_checkpoint(self, round, screenshot):
        fields = self.binding_fields
        issue = fields.get("reviewed_issue")
        digest = fields.get("verdict_digest", "")
        shots = fields.get("screenshot_digest", "") if screenshot else ""
        if (fields.get("verdict_round") != str(round) or not fields.get("reviewed_head")
                or not fields.get("base_branch") or not issue or not digest or screenshot and not shots):
            return None
        return {"pr": str(self.pr), "round": round, "head": fields["reviewed_head"],
                "base": fields["base_branch"], "digest": digest, "issue": "" if issue == "none" else issue,
                "screenshot_digest": shots}

    def fixer_feedback(self, round, screenshot, fix_head):
        """Gate feedback reads and verify the revision and both runner-posted bodies."""
        pr = str(self.pr)
        episode = self.issue or pr
        reason = "review_comment_missing"
        title = f"PR #{pr} review comment missing"
        text = f"A review comment of round {round} is unavailable. {self.url}"

        def stop(message=text):
            self.handoff(reason, title, message)
            return "", 0

        try:
            pending = owner_handoff.load_retry(self.env, episode, reason, pr)
        except owner_handoff.RetryUnreadable as exc:
            notify.log(f"PR #{pr}: review feedback retry unreadable: {exc}")
            return stop()
        binding = self.binding_from_checkpoint(round, screenshot)
        marker = self.binding_fields.get("feedback_waiting")
        if ((marker and not pending) or (pending and pending["continuation"] != binding)
                or (marker and marker != f"{episode}/{pr}/{reason}")):
            return stop()
        if pending:
            result = self.handoff(reason, title, text, resume=True)
            if result.action != owner_handoff.RETRY:
                return "", 75 if result.action == owner_handoff.DEFER else 0

        data, retryable = self.read_pr("comments,labels")
        if data:
            if data["state"] != "OPEN":
                owner_handoff.clear_retry(self.env, episode, reason, pr)
                return "", 0
            if any(label.get("name") == "ready-for-human" for label in data.get("labels") or ()):
                if pending:
                    owner_handoff.restore_retry(self.env, episode, reason, pr, pending)
                return "", 75
            if not binding or (data["headRefOid"], data["baseRefName"], data["linked_issue"], fix_head) != (
                    binding["head"], binding["base"], binding["issue"], binding["head"]):
                return stop(f"PR #{pr} revision or linked issue changed; the saved feedback cannot be applied. {self.url}")
            code = marked_comment(data["comments"], self.owner, "baton-review", round)
            shots = marked_comment(data["comments"], self.owner, "baton-screenshot-review", round) if screenshot else ""
            if (code and hashlib.sha256(code.encode()).hexdigest() != binding["digest"]
                    or shots and hashlib.sha256(shots.encode()).hexdigest() != binding["screenshot_digest"]):
                return stop(f"PR #{pr} feedback differs from the runner-posted verdict. {self.url}")
            if code and (not screenshot or shots):
                owner_handoff.clear_retry(self.env, episode, reason, pr)
                self.save_binding(feedback_waiting="")
                cap = int(self.env.config.scheduling.review_context_max_chars or 20000)
                open_block = f"## Open design findings\n{design_findings.block(self.design)}" if self.design else ""
                try:
                    if screenshot or open_block:
                        return combined_feedback(code, shots, cap, open_block), None
                except DesignOverflow:
                    self.handoff("review_run_failed", f"PR #{pr} review failed",
                                 f"design findings exceed review_context_max_chars. {self.url}")
                    return "", 0
                return code, None
            retryable = True

        if binding is None or not retryable:
            return stop()
        self.save_binding(feedback_waiting=f"{episode}/{pr}/{reason}")
        result = self.handoff(reason, title, text, transient=True, continuation=binding)
        return "", 75 if result.action == owner_handoff.DEFER else 0

    def design_fields(self):
        return {"design_open": design_findings.dump(self.design),
                "design_unusable": "1" if self.design_unusable else "",
                "design_shot_round": str(self.shot_round), "loop_id": self.loop_id,
                "design_only": "1" if self.design_only else ""}

    @staticmethod
    def new_loop_id():
        """A loop id unique per PR: the start time in seconds and a short random suffix."""
        return f"{int(time.time())}-{secrets.token_hex(3)}"

    def design_answers(self, round):
        """The fixer's current answers to the open findings for the review of ROUND: this loop's fix
        comments since the previous screenshot review."""
        result = pr_helpers.gh("pr", "view", str(self.pr), "-R", self.repo, "--json", "comments")
        try:
            comments = json.loads(result.stdout).get("comments") if result.returncode == 0 else []
        except (AttributeError, ValueError, TypeError):
            comments = []
        return design_findings.current_answers(
            fix_replies(comments, self.owner, self.loop_id, self.shot_round, round))

    def current_screenshot(self, round):
        """The screenshot review comment this run posted for ROUND, bound by its digest; "" when
        it can't be read or is not the one posted (an earlier loop's comment of the same round)."""
        body = agent_comment(self.repo, self.owner, self.pr, "baton-screenshot-review", round)
        digest = self.binding_fields.get("screenshot_digest", "")
        if body and digest and hashlib.sha256(body.encode()).hexdigest() == digest:
            return body
        return ""

    def design_round(self, round, checks):
        """Fold this round's screenshot review into the open design findings: resolved ones close,
        new ones open. A comment that can't be read or has no usable `## Design findings` leaves
        the carried ones open and marks the verdicts unusable (every check goes to the owner)."""
        body, listed = self.current_screenshot(round), check_verdicts.items(checks)
        found = design_findings.parse(body, round, len(listed)) if body else None
        self.design_unusable = found is None
        if found is None:
            return
        carried = design_findings.apply_answers(self.design, self.design_answers(round))
        self.design = design_findings.judge(carried, body) + design_findings.with_check_text(found, listed)
        self.shot_round = round

    def render(self, template, **pairs):
        return prompts.render(self.self_dir / template, list(pairs.items()), env=self.env)

    def review_followup(self, round, reviewer):
        """A later round mostly re-checks the fix commits, so it runs on the cheaper model. Codex keeps
        its usual one on code Codex wrote: the two must be different models."""
        return round > 1 and not (vendors.same_vendor_review(reviewer)
                                  and reviewer in vendors.review_excluded(self.state, self.pr, self.fixed_by))

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
                           issue=self.issue, fixed_by=self.fixed_by, pinned=continuity_pin(current))

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
    def skill_step(self, reviewer, lines):
        """REVIEW_SKILL_POLICY: Claude's diff-size rule, Codex's direct review, or none for reply."""
        policy = agents.ADAPTERS[reviewer].REVIEW_SKILL_POLICY
        if policy == "direct":
            return ("Review the diff yourself, following AGENTS.md; no Claude plugins or hooks are available, "
                    "so don't read the `code-review` skill.")
        if policy == "none":
            return ""
        if lines > int(self.env.config.scheduling.review_skill_min_lines or 800) and self.env.skills:
            return (f"The diff is {lines} lines: run the `mattpocock-skills:code-review` skill on it and use "
                    "its findings.")
        return (f"The diff is {lines} lines: review it yourself; don't run the `mattpocock-skills:code-review` "
                "skill or start sub-agents.")

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
        self.values.update(round=round, step="review", reviewer=reviewer)
        followup = self.review_followup(round, reviewer)
        expected = self.env.config.models.route("review", reviewer, followup=followup).model
        if not self.review_allowed(reviewer, expected):
            return self.failed(ALREADY, "RESULT: failed reviewer model may be a PR author")
        numstat = git(wt, "diff", "--numstat", "origin/main...HEAD") or ""
        lines = sum(int(f) for row in numstat.splitlines() for f in row.split()[:2] if f.isdecimal())
        skill = self.skill_step(reviewer, lines)
        context = Path(self.ctx, f"pr-{pr}-review-{round}-{time.strftime('%Y%m%d-%H%M%S')}.md")
        data, status = self.read_post_inputs()
        if status == "paused":
            return DEFERRED_POST
        if not data or data["headRefOid"] != head:
            return self.failed("review_comment_missing", "RESULT: failed reading the PR revision for review")
        self.save_binding(reviewed_head=head, base_branch=data["baseRefName"], reviewed_issue=data["linked_issue"] or "none",
                          verdict_round=str(round), verdict_digest="", screenshot_digest="", feedback_waiting="",
                          post_waiting="", post_outcome="")
        text = review_context(self.env, pr, data["linked_issue"], data=data)
        try:
            pr_helpers.preflight(pr)
        except ValueError as error:
            text += (f"\n<runner-preflight>\n{error}\nReport this as Must fix; description-only changes use the "
                     "runner artifact.\n</runner-preflight>\n")
        context.write_text(text)
        adapter = agents.ADAPTERS[reviewer]
        review_file = None if adapter.REVIEW_DELIVERY == "reply" else Path(f"/tmp/baton-review-{pr}-{round}.md")
        fields = {"PR": str(pr), "ISSUE": self.issue or "unknown", "GH_REPO": self.repo, "WORKTREE": str(wt),
                  "ROUND": str(round), "MAX_ROUNDS": str(self.max_rounds), "ROUND_NOTE": note,
                  "CONTEXT_FILE": str(context), "NIT_RULE": nit_rule(self.repo, self.issue)}
        runtime = self.env.with_runtime(review_followup=followup)
        if review_file is None:
            out = self.agent(reviewer, "review", self.render(adapter.REVIEW_TEMPLATE, **fields),
                             f"pr-{pr}-{reviewer}-review-{round}", env=runtime)
            if changed(wt, head):
                restore(wt, head)
                return f"RESULT: failed {adapter.DISPLAY_NAME} changed files in a read-only review"
        else:
            review_file.unlink(missing_ok=True)
            out = self.agent(reviewer, "review", self.render(adapter.REVIEW_TEMPLATE, SKILL_STEP=skill, **fields),
                             f"pr-{pr}-review-{round}", str(self.self_dir / "review-settings.json"),
                             str(review_file), runtime)
        outcome, model = out["outcome"], out["model"] or ""
        if not self.review_allowed(reviewer, model):
            return self.failed(ALREADY, "RESULT: failed actual reviewer model may be a PR author")
        if not outcome.startswith(REVIEWED):
            return outcome
        review_note = ""
        if vendors.same_vendor_review(reviewer) and reviewer in vendors.review_excluded(self.state, pr, self.fixed_by):
            mode = vendors.same_vendor_review_mode(self.state, pr, reviewer, model)
            review_note = f"Review mode: {mode} ({reviewer}/{model}).\n\n"
        if review_file is None:
            reply = strip_markers(out["reply"] or "")
            body = "\n".join(line for line in reply.split("\n") if not MARKER_LINE.match(line)).rstrip("\n")[:REPLY_CAP]
        elif review_file.is_file() and review_file.stat().st_size:
            body = strip_markers(review_file.read_text()).rstrip("\n")
        else:
            return self.failed("review_run_failed", "RESULT: failed no review artifact")
        posted_body = f"<!-- baton-review round={round} by={reviewer} -->\n{review_note}{body}"
        binding = self.comment_post_binding(posted_body, round, outcome, reviewer, head, data)
        return self.post_review_comment(round, reviewer, binding)

    # --- the screenshot review (#67): its own run after a successful code review ---
    def screenshot_enabled(self, reviewer):
        """Whether to check the PR body for screenshot review after this code review."""
        return (self.env.repo.screenshot_review
                and not agents.ADAPTERS[reviewer].SKIP_SCREENSHOT_AFTER_REVIEW
                and "owner" not in vendors.review_excluded(self.state, self.pr))

    def screenshot_vendor(self):
        """An image-capable adapter (IMAGES), by the code review's exclusions."""
        return pick.choose(self.env, "screenshot_reviewer", "", "review", self.vendors(), pr=str(self.pr),
                           issue=self.issue, fixed_by=self.fixed_by)

    def shots_step(self, shots, vendor):
        """The screenshot reviewer's look at the manual-check screenshots (#50): every valid PNG,
        or one Must fix when there are none."""
        if not shots:
            return (f"This PR has `## {check_section.HEADING}` but there are no screenshots: report one Must fix asking the "
                    "author to shoot every manual-check item the way the repository's agent docs describe.")
        listed = "".join(f"\n   - `{path}`" for path in shots)
        tool = agents.ADAPTERS[vendor].SCREENSHOT_TOOL
        return ("Look at every one of the screenshots of this PR's manual checks "
                f"({tool}):{listed}\n"
                "   Compare each with the manual checks above and the issue.")

    def run_screenshot_review(self, round, data, checks, vendor=""):
        """One screenshot reviewer run and its comment; returns OUTCOME. Deferred when the reviewer
        cannot run now. It never hands the PR over: the loop does, once, on a failed outcome."""
        pr, wt = self.pr, self.wt
        self.hooks.round = round
        head = git(wt, "rev-parse", "HEAD") or ""
        if data["headRefOid"] != head:
            return self.failed("screenshot_run_failed", "RESULT: failed reading the PR revision for screenshot review")
        vendor = vendor or self.screenshot_vendor()
        self.screenshot_reviewer = vendor
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
        if agents.ADAPTERS[vendor].SCREENSHOT_DELIVERY == "final_message":
            delivery = "Include the full review in your final message, above the verdict line."
        else:
            delivery = f"Write the comment to `{review_file}` with the Write tool."
        open_design = (design_findings.block(design_findings.apply_answers(self.design, self.design_answers(round)))
                       if round > 1 else "none")
        if open_design != "none" and len(open_design) > limit:   # a complete record, never cut (#96)
            raise DesignOverflow
        out = self.agent(vendor, "screenshot_review", self.render(
            "screenshot-review-prompt.md", PR=str(pr), ISSUE=self.issue or "unknown", GH_REPO=self.repo,
            WORKTREE=str(wt), ROUND=str(round), MAX_ROUNDS=str(self.max_rounds), CONTEXT_FILE=str(context),
            OPEN_DESIGN=open_design,
            MANUAL_CHECKS=cut(check_verdicts.numbered(checks), limit), SHOTS_STEP=self.shots_step(notify.shots(pr, self.ctx), vendor), DELIVERY=delivery),
            f"pr-{pr}-screenshot-review-{round}", str(self.self_dir / "review-settings.json"), str(review_file), self.env)
        outcome, model = out["outcome"], out["model"] or ""
        if not vendors.screenshot_allowed(self.state, pr, vendor, model, self.fixed_by):
            return self.failed("screenshot_independence_denied",
                               "RESULT: failed actual screenshot reviewer model may be a PR author")
        if outcome.startswith(REVIEWED):
            if review_file.is_file() and review_file.stat().st_size:
                body = strip_markers(review_file.read_text()).rstrip("\n")
                mode = (vendors.same_vendor_review_mode(self.state, pr, vendor, model)
                        if vendor in vendors.review_excluded(self.state, pr, self.fixed_by) else "")
                review_note = f"Review mode: {mode} ({vendor}/{model}).\n\n" if mode else ""
                posted_body = f"<!-- baton-screenshot-review round={round} by={vendor} -->\n{review_note}{body}"
                binding = self.comment_post_binding(posted_body, round, outcome, vendor, head, data)
                return self.post_screenshot_comment(round, vendor, binding)
            outcome = self.failed("screenshot_run_failed", "RESULT: failed no screenshot review artifact")
        self.hooks.verdict("screenshot_review", vendor, head, round, outcome, out["reply"] or "", str(review_file))
        return outcome

    # --- the checkpoint ---
    def save(self):
        checkpoint.write(self.checkpoint, {"pr": self.pr, "wt": self.wt, "ISSUE": self.issue, **self.values,
                                           "fixed_by": self.fixed_by})

    def drop(self):
        self.checkpoint.unlink(missing_ok=True)
        task_recovery.clear(self.env, self.issue or self.pr, self.pr)

    def finish_review(self, saved):
        pr, wt = self.pr, self.wt
        approved = saved.get("merge_head", "")
        runner = fix.Runner(self.env, self.hooks, pr, "ci")
        waiting = runner.waiting_push(pr)
        if waiting == "unreadable":
            return 0
        relevant_repair = (waiting and waiting["continuation"]["caller"] == "ci"
                           and (approved == waiting["continuation"]["ci_head"]
                                or (waiting.get("phase") == "finished"
                                    and approved == waiting["continuation"]["commit"])))
        if relevant_repair and not waiting.get("phase") and int(owner_handoff.clock()) < waiting["due"]:
            return 75
        if waiting and not relevant_repair:
            runner.handoff(pr, owner_handoff.FIX_PUSH, f"PR #{pr} needs you",
                           f"The saved CI repair does not match the approved head. {self.url}")
            return 0
        if not waiting:
            try:
                data = pr_helpers.view(pr, "state,headRefOid")
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("state") in ("MERGED", "CLOSED"):
                self.drop()
                return 0
            if (not approved or not isinstance(data, dict) or data.get("headRefOid") != approved
                    or git(wt, "rev-parse", "HEAD") != approved):
                self.handoff("merge_failed", f"PR #{pr} needs you", f"The approved head could not be confirmed. {self.url}")
                return 0
        design = design_findings.load(saved.get("design_open", ""))
        shots = ({"checks": saved.get("screenshot_checks", ""), "comment": saved.get("screenshot_comment", ""),
                  "design": design, "design_unusable": saved.get("design_unusable") == "1"}
                 if saved.get("screenshot") or design else None)
        merger = merge.Merger(self.env, self.hooks, pr, self.issue)
        merger.approved_head = approved
        try:
            merger.merge_or_handoff(wt, shots)
        except Deferred:
            return 75
        self.drop()
        return 0

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
        baseline = (f"Decision point: review round limit. PR #{pr} of {self.repo} used all {round} review rounds "
                    f"(max_review_rounds = {self.max_rounds}) and the last review still has must-fix items.\n"
                    f"Last reviewer: {reviewer}; last fixer: {self.fixed_by or fixer or 'none'}. "
                    f"Diff: {files} files, {lines} lines changed.\n"
                    "Actions: extra_round = one more fix by a fixer the vendor rules pick, then one more review; "
                    "handoff = the owner takes the PR.\n\n"
                    f"{reviews}\n\n{review_context(self.env, pr, self.issue)}")
        diff_excerpt = ""
        if self.env.repo.advisor_diff_excerpts:
            diff_excerpt = (git(wt, "diff", "origin/main...HEAD") or "")[:4000]
        snapshot = advisor.augment_snapshot(self.env, baseline, diff_excerpt=diff_excerpt)

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
            self.design = design_findings.load(saved.get("design_open", ""))
            self.design_unusable = saved.get("design_unusable") == "1"
            self.shot_round = int(saved["design_shot_round"]) if saved.get("design_shot_round", "").isdecimal() else 0
            self.loop_id = saved.get("loop_id") or self.new_loop_id()
            self.design_only = saved.get("design_only") == "1"
            self.fixed_by, self.issue = saved.get("fixed_by", ""), saved.get("ISSUE", "")
            self.env = self.env.with_runtime(issue=self.issue)
            self.binding_fields = {k: saved.get(k, "") for k in
                                   self.BINDING_FIELDS}
            if step == "review" and saved.get("push_completed"):
                fix.Runner(self.env, self.hooks, pr, str(round)).acknowledge_push(pr)
        else:
            self.binding_fields = {}
            self.design, self.design_unusable = [], False
            self.shot_round, self.design_only, self.loop_id = 0, False, self.new_loop_id()
        pending_post = self.post_pending()
        screenshot_post = self.screenshot_post_pending()
        cfg = self.env.config
        if (not pending_post and reviewer != "pool"
                and (reviewer not in cfg.declared() or not cfg.allows(reviewer, "reviewer"))):
            reviewer = "pool"
        if screenshot_post and (step != "screenshot" or code_verdict not in ("pass", "changes")):
            # A damaged screenshot checkpoint fails closed, before the loop rewrites any of it.
            self.handoff(owner_handoff.SCREENSHOT_POST, f"PR #{pr} comment post failed",
                         f"The saved screenshot review post has an invalid checkpoint. {self.url}")
            for key in {self.issue or pr, pr}:
                owner_handoff.clear_retry(self.env, key, owner_handoff.SCREENSHOT_POST, str(pr))
            self.drop()
            return 0
        resuming_fix = step == "fix"
        if step == "merge":
            return self.finish_review(saved)
        self.values = {"round": round}
        self.worker_start = {}
        if not (resuming_fix or pending_post):
            seed_reason = "review_author_state_failed"
            pending_seed = task_recovery.resume(
                self.env, self.issue or pr, pr, lambda reason: reason == seed_reason,
                lambda: self.retry_binding("reviewer"),
                lambda text: self.handoff(seed_reason, f"PR #{pr} needs you", text), local=True)
            if pending_seed is False:
                self.drop()
                return 0
            seeded = self.seed()
            if pending_seed:
                if not seeded:
                    self.handoff(seed_reason, f"PR #{pr} needs you",
                                 f"RESULT: failed recording PR authors after retry {self.url}")
                    self.drop()
                    return 0
                task_recovery.clear(self.env, self.issue or pr, pr)
            elif not seeded:
                action, _ = self.recover_worker(
                    seed_reason, f"PR #{pr} needs you",
                    "RESULT: failed recording PR authors", "", role="reviewer", fallback=True)
                if (action == owner_handoff.HANDOFF and advisor.enabled(self.env)
                        and advisor.settings(self.env).mode == "live"):
                    # Live: a handoff advice or a grant that failed revalidation ends it, with one
                    # handoff. Only off and observe keep the old continuation below.
                    self.handoff(seed_reason, f"PR #{pr} needs you",
                                 f"RESULT: failed recording PR authors {self.url}")
                    self.drop()
                    return 0
                if action != owner_handoff.HANDOFF and not self.seed():
                    self.handoff(seed_reason, f"PR #{pr} needs you",
                                 f"RESULT: failed recording PR authors after retry {self.url}")
                    self.drop()
                    return 0
                if action != owner_handoff.HANDOFF:
                    task_recovery.clear(self.env, self.issue or self.pr, pr)
            self.seed_contrib()
        starting_review = not saved or (step == "review" and round == 1 and saved.get("push_completed"))
        if starting_review and not pending_post:
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
                               extra="1" if extra else "", **self.design_fields(), **self.binding_fields)
            self.save()
            if step in ("review", "screenshot") or pending_post:
                verdict = code_verdict
                if screenshot_post:
                    pending_post = False
                elif step == "review" or pending_post:
                    self.fail_reason = ""
                    if pending_post:
                        outcome = self.post_review_comment(round, reviewer)
                        pending_post = False
                    else:
                        try:
                            pending = self.resume_worker("reviewer", pinned=continuity_pin(reviewer))
                            if pending is False:
                                self.drop()
                                return 0
                            reviewer = pending[1] if pending else self.review_vendor(reviewer)
                        except Exhausted as exhausted:
                            self.handoff("reviewer_unavailable", f"PR #{pr} needs you",
                                         exhausted.reason or "No eligible reviewer for this PR.")
                            self.drop()
                            return 0
                        self.values["reviewer"] = reviewer
                        self.save()
                        if advisor.enabled(self.env):
                            self.worker_start["reviewer"] = self.retry_binding("reviewer")
                        outcome = self.run_review(round, rnote, reviewer)
                        while not (outcome.startswith(REVIEWED) or outcome == DEFERRED_POST):
                            if self.fail_reason == ALREADY:   # handed off where it was decided
                                self.drop()
                                return 0
                            reason = self.fail_reason or "review_run_failed"
                            action, reviewer = self.recover_worker(
                                reason, f"PR #{pr} review failed ({reviewer})", outcome, reviewer,
                                pinned=continuity_pin(reviewer))
                            if action == owner_handoff.HANDOFF:
                                self.drop()
                                return 0
                            self.values["reviewer"] = reviewer
                            self.save()
                            self.fail_reason = ""
                            outcome = self.run_review(round, rnote, reviewer)
                    if outcome == DEFERRED_POST:
                        return 75
                    if not outcome.startswith(REVIEWED):
                        if self.fail_reason != ALREADY:   # a refusal handed off where it was decided
                            self.handoff(self.fail_reason or "review_run_failed", f"PR #{pr} review failed ({reviewer})",
                                         f"{outcome} {self.url}")
                        self.drop()
                        return 0
                    task_recovery.clear(self.env, self.issue or str(pr), str(pr))
                    verdict = "pass" if outcome.startswith("REVIEW: pass") else "changes"
                    self.binding_fields["post_outcome"] = self.values["post_outcome"] = ""
                data = None
                if not screenshot_post and (step == "screenshot" or self.screenshot_enabled(reviewer)):
                    data, status = self.read_post_inputs()
                    if status == "paused":
                        self.values.update(step="screenshot", code_verdict=verdict)
                        self.save()
                        return 75
                    if status == "superseded":
                        advisor.finish_episode(self.env, "pr", pr, "superseded")
                        self.drop()
                        return 0
                    if status != "ok":
                        self.handoff("screenshot_run_failed", f"PR #{pr} screenshot review failed",
                                     f"Reading the PR revision for screenshot review failed. {self.url}")
                        self.drop()
                        return 0
                    checks = (check_section.read(data["body"]) or "") if check_section.items(data["body"]) else ""
                if screenshot_post or (data is not None and checks):
                    step = "screenshot"
                    code_verdict = verdict
                    self.values.update(step=step, code_verdict=code_verdict, screenshot_checks=checks)
                    self.save()
                    if screenshot_post:
                        self.fail_reason = ""
                        outcome = self.post_screenshot_comment(round)
                        screenshot_post = False
                        if outcome == DEFERRED_POST:
                            return 75
                        if not outcome.startswith(REVIEWED):
                            if self.fail_reason != ALREADY:
                                self.handoff(self.fail_reason or "screenshot_run_failed",
                                             f"PR #{pr} screenshot review failed", f"{outcome} {self.url}")
                            self.drop()
                            return 0
                    else:
                        pending = self.resume_worker("screenshot_reviewer")
                        if pending is False:
                            self.drop()
                            return 0
                        selected = pending[1] if pending else ""
                        if advisor.enabled(self.env):
                            self.worker_start["screenshot_reviewer"] = self.retry_binding("screenshot_reviewer")
                        while True:
                            self.fail_reason = ""
                            try:
                                outcome = self.run_screenshot_review(round, data, checks, selected)
                            except DesignOverflow:
                                self.handoff("review_run_failed", f"PR #{pr} review failed",
                                             f"design findings exceed review_context_max_chars. {self.url}")
                                self.drop()
                                return 0
                            except Exhausted as exhausted:
                                outcome = self.failed("screenshot_reviewer_unavailable",
                                                      exhausted.reason or "No screenshot reviewer available.")
                            if outcome.startswith(REVIEWED) or outcome == DEFERRED_POST:
                                break
                            if self.fail_reason == ALREADY:   # the comment post handed off where it failed
                                self.drop()
                                return 0
                            reason = self.fail_reason or "screenshot_run_failed"
                            if reason in ("screenshot_reviewer_unavailable", "screenshot_reviewer_role_denied",
                                          "screenshot_independence_denied"):
                                self.handoff(reason, f"PR #{pr} screenshot review failed", f"{outcome} {self.url}")
                                self.drop()
                                return 0
                            action, selected = self.recover_worker(
                                reason, f"PR #{pr} screenshot review failed", outcome, self.screenshot_reviewer,
                                role="screenshot_reviewer")
                            if action == owner_handoff.HANDOFF:
                                self.drop()
                                return 0
                        if outcome == DEFERRED_POST:
                            return 75
                    task_recovery.clear(self.env, self.issue or str(pr), str(pr))
                    screenshot = "1"
                    self.binding_fields["post_outcome"] = self.values["post_outcome"] = ""
                    if outcome.startswith("REVIEW: changes"):
                        verdict = "changes"
                    self.design_round(round, checks)
                    self.values.update(self.design_fields())
                    self.save()
                self.design_only = False
                self.values["design_only"] = ""
                # A disagreement the pipeline can't settle waits for the owner at the merge; only
                # the other open findings keep the loop going.
                if verdict == "pass" and not [item for item in self.design if not item.get("disagree")]:
                    break
                if verdict == "pass":
                    # Only design findings are open: they go to the fixer, but never as review_limit.
                    verdict = "changes"
                    self.design_only = True
                    self.values["design_only"] = "1"
                    self.save()
                    if round >= self.max_rounds + extra:
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
            runner = fix.Runner(self.env.with_runtime(fix_followup=round > 1), self.hooks, pr, str(round))
            runner.loop_id = self.loop_id
            runner.design_ids = [item["id"] for item in self.design if not item.get("disagree")]
            runner.design_only = self.design_only and bool(self.design)
            # The next round's reviewer must stay free: a continuity-pinned reviewer, or a sole pool vendor.
            pool = self.env.config.quota.vendor_pool
            exclude = reviewer if continuity_pin(reviewer) else ("" if len(pool) > 1 else pool[0])
            runner.retry_exclusions = exclude.split()
            waiting = runner.waiting_push(pr)
            waiting_meta = None
            if waiting is None:
                for meta_reason in owner_handoff.FIX_META_WRITES:
                    waiting_meta = runner.waiting_meta(pr, meta_reason)
                    if waiting_meta is not None:
                        break
            if waiting is not None or waiting_meta is not None:
                # A saved push or metadata write resumes first: no reads or vendor choice (#151).
                if waiting == "unreadable" or waiting_meta == "unreadable":
                    self.drop()
                    return 0
                saved = (waiting or waiting_meta)["continuation"]
                fixer = saved.get("fixer") or fixer
                self.values["fixer"] = fixer
                rc = fix.fix_pr(runner, pr, wt, fixer, f"the reviewer agent (round {round})", "", fix_head)
            else:
                feedback, deferred = self.fixer_feedback(round, bool(screenshot), fix_head)
                if deferred is not None:
                    if deferred == 0:
                        self.drop()
                    return deferred
                if resuming_fix and self.seed():
                    self.seed_contrib()
                if not task_recovery.pending_fix(self.env, self.issue or pr, pr):
                    fixer = pick.choose(self.env, "fixer", fixer, "contrib", self.vendors(), pr=str(pr),
                                        exclude=exclude, issue=self.issue)
                self.values["fixer"] = fixer
                self.save()
                rc = fix.fix_pr(runner, pr, wt, fixer, f"the reviewer agent (round {round})", feedback, fix_head)
            resuming_fix = False
            if rc == 75:
                return 75
            if rc == fix.PUSH_SUPERSEDED:
                self.drop()
                return 0
            if rc != 0:
                if not runner.handed_off:   # e.g. authorship recording failed: the owner still needs one handoff
                    runner.handoff(pr, "fix_run_unfinished", f"PR #{pr} needs you",
                                   f"The fix run of round {round} failed before a handoff. {self.url}")
                self.drop()
                return 0
            fixer = runner.last_fixer or fixer
            self.fixed_by = " ".join(dict.fromkeys([*runner.attempt_authors, fixer]))
            round, step = round + 1, "review"
            code_verdict = screenshot = checks = ""
            if runner.push_flag(pr).exists():
                self.values.update(round=round, step=step, code_verdict="", screenshot="",
                                   screenshot_checks="", push_completed="1")
                self.save()
                runner.acknowledge_push(pr)
        # Keep the checkpoint through merge so a category-1 CI-repair push can resume (#151).
        step = "merge"
        # The last round's screenshot verdicts decide which manual checks still need the owner (#92).
        comment = self.current_screenshot(round) if screenshot else ""
        self.values.update(self.design_fields(), step=step, code_verdict=code_verdict, screenshot=screenshot,
                           merge_head=self.binding_fields.get("reviewed_head", ""),
                           screenshot_checks=checks, screenshot_comment=comment)
        self.save()
        return self.finish_review(self.values)


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
        fb = human_feedback(env.repo.gh_repo, env.owner, args.pr)
        if fb is None:
            print("could not read the PR's comments completely", file=sys.stderr)
            return 1
        print(fb.owner)
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
