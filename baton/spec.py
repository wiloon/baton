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

from . import advisor, agents, checks as check_section, claude_cli, dispatch, e2e_gate, fix, handoff as owner_handoff, hooks as telemetry, merge, notify, pick, pr as pr_helpers, prompts, recovery, review, vendors
from .common import Deferred, changed, child_env, git, permission_denials, push, restore, strip_markers
from .quota import atomic_json

REPLY_CAP = 60000
DENIED_NOTE = ("\n\nA previous attempt at this round ended with `RESULT: failed` after this command was denied: "
               "`{command}`. Only that command was refused. Finish the round with the other allowed commands "
               "and the Read, Edit and Write tools; the worktree keeps whatever that attempt left.\n")
VERDICT = re.compile(r"<!-- baton-verdict: (accept|revise)( by=(cursor|codex|claude))? -->")
STATE_RE = re.compile(r"<!-- baton-spec-(?P<k>review|reply) round=(?P<r>[0-9]+)(?P<h> human)?")
KIND_RE = r"<!-- baton-spec-%s round=[0-9]+( human)? by=(?P<v>[a-z]+) -->"
VERDICT_LINE = re.compile(r"^(SPEC|RESULT): ")
ADVISOR_ANSWER = re.compile(
    r"^<!-- baton-advisor-answer round=(?P<r>\d+) questions=(?P<q>[0-9a-f]+) by=deepseek -->")
PENDING_ACCEPT = re.compile(
    r"^<!-- baton-spec-accept-pending pr=(?P<pr>\d+)(?: head=(?P<head>[0-9a-f]{40}))? -->\n(?P<verdict>.*)", re.DOTALL)


ISSUE_SNAPSHOT = """
query($owner: String!, $repo: String!, $issue: Int!, $endCursor: String) {
  repository(owner: $owner, name: $repo) {
    issue(number: $issue) {
      title body state
      labels(first: 100) { totalCount nodes { name } }
      comments(first: 100, after: $endCursor) {
        nodes { fullDatabaseId body author { login } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def read_issue(env, issue, *, once=False, recovery=False):
    """Read the issue and full comment history, including IDs shared with saved post records.
    RECOVERY also refuses an issue that is not open or is labelled ready-for-human (#125)."""
    owner, repo = env.repo.gh_repo.split("/", 1)
    request = pr_helpers.call if once else pr_helpers.gh
    result = request("api", "graphql", "--paginate", "--slurp", "-f", f"query={ISSUE_SNAPSHOT}",
                     "-f", f"owner={owner}", "-f", f"repo={repo}", "-F", f"issue={issue}")
    if result.returncode != 0:
        return None, pr_helpers.transient(result)
    try:
        pages = json.loads(result.stdout)
        if not isinstance(pages, list) or not pages:
            return None, False
        snapshot, comments, ids = None, [], set()
        for index, page in enumerate(pages):
            if page.get("errors"):
                return None, False
            row = page["data"]["repository"]["issue"]
            labels = row["labels"]["nodes"]
            if (row["state"] not in ("OPEN", "CLOSED") or not isinstance(row["title"], str)
                    or not isinstance(row["body"], str) or not isinstance(labels, list)
                    or row["labels"]["totalCount"] != len(labels)
                    or any(not isinstance(label["name"], str) for label in labels)):
                return None, False
            issue_fields = {"title": row["title"], "body": row["body"], "state": row["state"], "labels": labels}
            if snapshot is not None and snapshot != issue_fields:
                return None, False
            snapshot = issue_fields
            connection = row["comments"]
            if (connection["pageInfo"]["hasNextPage"] is not (index + 1 < len(pages))
                    or not isinstance(connection["nodes"], list)):
                return None, False
            for comment in connection["nodes"]:
                identity, body, author = comment["fullDatabaseId"], comment["body"], comment["author"]
                if isinstance(identity, str) and identity.isdecimal():
                    identity = int(identity)
                if (type(identity) is not int or identity <= 0 or identity in ids or not isinstance(body, str)
                        or author is not None and not isinstance(author["login"], str)):
                    return None, False
                ids.add(identity)
                comments.append({"id": identity, "body": body, "author": author})
    except (AttributeError, KeyError, TypeError, ValueError):
        return None, False
    if recovery and (snapshot["state"] != "OPEN"
                     or any(label["name"] == "ready-for-human" for label in snapshot["labels"])):
        return None, False
    return {**snapshot, "comments": comments}, False


def read_comments(env, issue):
    """Return the comments and whether the read failed transiently."""
    data, transient = read_issue(env, issue)
    if data is None:
        return None, transient
    return data.get("comments") or [], False


@session.scoped
def comments(env, issue):
    """The issue's comments, or None when gh fails or the payload is unreadable."""
    return read_comments(session.resolve(env), issue)[0]


def owner_bodies_from(listed, owner):
    return [c.get("body") or "" for c in listed or ()
            if (c.get("author") or {}).get("login") == owner]


@session.scoped
def owner_bodies(env, issue):
    env = session.resolve(env)
    return owner_bodies_from(comments(env, issue), env.owner)


def pending_accept_in(listed, owner, login_re=None):
    """The latest valid pending-acceptance comment by OWNER that is not yet superseded by a
    later owner/cursor verdict with the same saved body: {pr, verdict, index}, or None."""
    found = None
    for i, c in enumerate(listed or ()):
        if (c.get("author") or {}).get("login") != owner:
            continue
        match = PENDING_ACCEPT.match(c.get("body") or "")
        if match and VERDICT.match(match["verdict"]):
            found = {"pr": match["pr"], "verdict": match["verdict"], "head": match["head"] or "", "index": i}
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


def plan_from(listed, owner, login_re=None):
    """The body of the latest accept/revise verdict in LISTED, or empty while acceptance is pending."""
    login_re = login_re or r"^(app/cursor|cursor(\[bot\])?)$"
    if pending_accept_in(listed, owner, login_re):
        return ""
    found = []
    for c in listed or ():
        login = (c.get("author") or {}).get("login") or ""
        body = c.get("body") or ""
        if (login == owner or re.search(login_re, login)) and VERDICT.match(body):
            found.append(body)
    return found[-1] if found else ""


def state_from(listed, owner):
    """(kind, round) of the latest spec comment in LISTED: review, reply, reply-human or none."""
    found = [m for body in owner_bodies_from(listed, owner) if (m := STATE_RE.match(body))]
    if not found:
        return "none", 0
    last = found[-1]
    return last["k"] + ("-human" if last["h"] else ""), int(last["r"])


def role_from(listed, env):
    """Vendor-pool role for ISSUE given an already-fetched comment list."""
    env = session.resolve(env)
    login_re = env.config.scheduling.cursor_login_re or r"^(app/cursor|cursor(\[bot\])?)$"
    if pending_accept_in(listed, env.owner, login_re) or plan_from(listed, env.owner, login_re):
        return "dev"
    return pending_role(state_from(listed, env.owner)[0])


@session.scoped
def accept_pending(env, issue, listed=None):
    """Pending acceptance for ISSUE when comments are readable; None when absent, superseded,
    or unreadable. LISTED skips the GitHub read when the caller already fetched comments."""
    env = session.resolve(env)
    if listed is None:
        listed = comments(env, issue)
    if listed is None:
        return None
    login_re = env.config.scheduling.cursor_login_re or r"^(app/cursor|cursor(\[bot\])?)$"
    return pending_accept_in(listed, env.owner, login_re)


@session.scoped
def cursor_plan(env, issue, listed=None):
    """The body of the latest accept/revise verdict comment of the owner, Cursor or an agent
    posting for Cursor; empty when there is none, or while an acceptance is still pending (#122)."""
    env = session.resolve(env)
    login_re = env.config.scheduling.cursor_login_re or r"^(app/cursor|cursor(\[bot\])?)$"
    if listed is None:
        listed = comments(env, issue) or ()
    return plan_from(listed, env.owner, login_re)


@session.scoped
def spec_state(env, issue, listed=None):
    """(kind, round) of the latest spec comment: review, reply, reply-human or none."""
    env = session.resolve(env)
    if listed is None:
        return state_from(comments(env, issue), env.owner)
    return state_from(listed, env.owner)


def pending_role(kind):
    return "reviser" if kind in ("review", "reply-human") else "spec_reviewer"


def last_by(listed, owner, kind):
    """The vendor of the latest spec review (KIND review) or reply, or ''."""
    found = [m for body in owner_bodies_from(listed, owner) if (m := re.match(KIND_RE % kind, body))]
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


def trusted_comment_digest(listed, owner, login_re):
    """Bind the owner and Cursor comments used to choose the spec step."""
    login_re = login_re or r"^(app/cursor|cursor(\[bot\])?)$"
    bodies = []
    for comment in listed or []:
        login = (comment.get("author") or {}).get("login") or ""
        if login == owner or re.search(login_re, login):
            bodies.append(comment.get("body") or "")
    return recovery.feedback_digest(json.dumps(bodies))


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
        self.pr, self.base, self.adr_head = "", "origin/main", ""
        self.adr_read, self.adr_round = None, None
        self.extra = int(advisor.granted(self.env, "spec_limit", self.issue)
                         and not advisor.granted(self.env, "spec_questions", self.issue))
        self.fresh_payload = None
        self.answer_intent: dict | None = None

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

    def agent(self, vendor, role, prompt, tag, settings, output="", followup=False, round_s=""):
        env = self.env.with_runtime(spec_followup=followup)
        saved_role = {"spec_review": "spec_reviewer", "spec": "reviser"}.get(role)
        # A saved grant is consumed before the step runs; a short block settles it (recovery.dispatching).
        with recovery.dispatching(self.env, self.issue, self.issue, lambda: self.retry_binding(round_s),
                                  role=saved_role):
            out = dispatch.dispatch(vendor, role, prompt, tag, str(self.wt), str(self.self_dir / settings), output,
                                    env, self.hooks)
            if out["rc"] == 75:
                raise Deferred(vendor, out["defer"])
        return out

    def settle_answer_intent(self):
        """The revision worker ran to completion: recovery now owns the continuation, so drop the saved one."""
        if self.answer_intent_path().exists():
            self.clear_answer_intent()
            owner_handoff.clear_retry(self.env, self.issue, owner_handoff.SPEC_REPLY_POST)

    def keep_denied_spent(self, answer_intent, command):
        """Persist the selected rerun with the validated advisor continuation before dispatch."""
        if answer_intent is None:
            return True
        try:
            atomic_json(self.answer_intent_path(),
                        {**answer_intent, "denied_spent": True, "denied_command": command})
        except OSError:
            self.handoff("Could not save the advisor answer intent for the denied-command rerun.", "spec_questions")
            return False
        return True

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
    def handoff(self, why, reason="spec_review_run_failed", *, snapshot="", revalidate=None, observed_actions=None,
                transient=False, continuation=None, resume=False):
        """The owner takes over, or a category-1 read defers (#125, #151)."""
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
            hooks=self.hooks, item=self.issue, episode=self.issue,
            snapshot=snapshot, revalidate=revalidate, observed_actions=observed_actions,
            after_labels=after_labels, transient=transient, continuation=continuation, resume=resume)

    def spec_binding(self, round_s, issue_body, listed):
        login_re = self.env.config.scheduling.cursor_login_re
        fingerprint = recovery.spec_fingerprint(
            str(self.wt), self.issue, base=self.base,
            issue_body=issue_body, comment_digest=trusted_comment_digest(listed, self.env.owner, login_re),
            round_s=round_s, adr_head=getattr(self, "adr_head", "") or "")
        # The workers do not rebase on resume: main's tip is not an input of a spec retry.
        if (fingerprint["branch"] != self.branch or not fingerprint["local_head"]
                or not fingerprint["clean"]):
            return None
        return fingerprint

    def retry_binding(self, round_s):
        payload, _ = read_issue(self.env, self.issue, recovery=True)
        if payload is None:
            return None
        adr, _ = self.discover_adr()
        if adr is None or str(adr.get("headRefOid", "")) != getattr(self, "adr_head", ""):
            return None
        return self.spec_binding(round_s, payload["body"], payload["comments"])

    def recover_worker(self, why, reason, vendor, detail, *, role="spec_reviewer", round_s="",
                       issue_body="", listed=(), last_vendor="", other_model="", spec_followup=False,
                       budget=None):
        fingerprint = self.spec_binding(round_s, issue_body, listed)
        result = recovery.request(
            self.env, self.issue, self.issue, reason, role, vendor, detail, fingerprint,
            lambda: self.retry_binding(round_s),
            lambda text=why, **kw: self.handoff(text, reason, **kw),
            exclusions=[last_vendor] if last_vendor else [], budget=budget,
            **self.retry_picker(role, other_model, spec_followup))
        return result

    def retry_picker(self, role, other_model, spec_followup):
        candidates = list(self.env.config.quota.vendor_pool) if role == "reviser" else pick.all_vendors(self.env)
        return dict(mode="contrib", candidates=candidates, issue=self.issue, other_model=other_model,
                    spec_followup=spec_followup)

    def resume_worker(self, role, reason, round, other_model, spec_followup=False):
        return recovery.resume(
            self.env, self.issue, self.issue, lambda saved: saved.startswith("spec_"),
            lambda: self.retry_binding(round),
            lambda text: self.handoff(text, reason), role=role,
            **self.retry_picker(role, other_model, spec_followup))

    def retry_read(self, read, why, reason, *, continuation=None):
        """Consume a waiting attempt before reading; return the value or hand off."""
        try:
            pending = owner_handoff.load_retry(self.env, self.issue, reason)
        except owner_handoff.RetryUnreadable as exc:
            notify.log(f"#{self.issue}: {reason} retry unreadable: {exc}")
            self.handoff(why, reason)
            return None
        if pending is not None:
            result = self.handoff(why, reason, resume=True)
            if result.action == owner_handoff.DEFER:
                raise Deferred("handoff-retry", reason, lends_slot=False)
            if result.action != owner_handoff.RETRY:
                return None
        value, transient = read()
        if value is None:
            result = self.handoff(why, reason, transient=transient, continuation=continuation)
            if result.action == owner_handoff.DEFER:
                raise Deferred("handoff-retry", reason, lends_slot=False)
        else:
            owner_handoff.clear_retry(self.env, self.issue, reason)
        return value

    def issue_payload(self):
        return self.retry_read(lambda: read_issue(self.env, self.issue),
                               "Could not read the issue and its comments.", "spec_comments_read_failed")

    def owner_feedback_digest(self, comments):
        rows = [
            (row["id"], row["body"])
            for row in comments
            if (row["author"] or {}).get("login") == self.env.owner and not row["body"].startswith("<!-- baton")
        ]
        return owner_handoff.body_digest(json.dumps(sorted(rows)))

    def body_key(self, text):
        return owner_handoff.body_digest(text.strip())

    def comment_post_binding(self, body, round, reviewer, inputs, *, next_step, reply, max_rounds, limit_why,
                             issue_body=None):
        return {
            "issue": self.issue, "pr": self.pr or "", "round": round, "reviewer": reviewer,
            "author": self.env.owner, "body": body, "digest": owner_handoff.body_digest(body),
            "issue_body": self.body_key(inputs["body"] if issue_body is None else issue_body),
            "adr_head": self.adr_head or "",
            "feedback": self.owner_feedback_digest(inputs["comments"]),
            "comment_ids": [row["id"] for row in inputs["comments"]],
            "next": next_step, "reply": reply, "max_rounds": max_rounds, "limit_why": limit_why,
        }

    def check_comment_post(self, binding, *, sending):
        data, transient = read_issue(self.env, self.issue, once=True)
        if data is None:
            return "retry" if transient else "failed"
        if data["state"] == "CLOSED":
            return "superseded"
        labels = {label["name"] for label in data["labels"]}
        if "ready-for-human" in labels:
            return "paused"
        boundary = set(binding["comment_ids"])
        new_comments = [row for row in data["comments"] if row["id"] not in boundary]
        if "ready-for-agent" in labels and "needs-triage" not in labels and any(
            (row["author"] or {}).get("login") == binding["author"]
            and (verdict := VERDICT.match(row["body"])) and verdict[1] == "accept" for row in new_comments
        ):
            return "accepted"
        adr, transient = self.discover_adr(pr_helpers.call, ("-R", self.repo))
        if adr is None:
            return "retry" if transient else "failed"
        if (binding["issue"] != self.issue
                or self.body_key(data["body"]) != binding["issue_body"]
                or adr.get("headRefOid", "") != binding["adr_head"]
                or str(adr.get("number", "")) != binding["pr"]
                or self.owner_feedback_digest(data["comments"]) != binding["feedback"]):
            return "changed"
        matches = []
        for row in new_comments:
            trusted = (row["author"] or {}).get("login") == binding["author"]
            if row["body"] == binding["body"]:
                matches.append(trusted)
            elif trusted and row["body"].startswith(("<!-- baton-spec-review round=", "<!-- baton-spec-reply round=")):
                return "changed"
        if not matches:
            return "absent"
        if sending and matches == [True]:
            return "changed"
        return "landed" if matches == [True] else "ambiguous"

    def accept_post_binding(self, body, verdict, round, reviewer, inputs, *, next_step):
        return {
            "issue": self.issue, "pr": self.pr or "", "round": round, "reviewer": reviewer,
            "author": self.env.owner, "body": body, "verdict": verdict,
            "digest": owner_handoff.body_digest(body),
            "issue_body": self.body_key(inputs["body"]),
            "adr_head": self.adr_head or "",
            "feedback": self.owner_feedback_digest(inputs["comments"]),
            "comment_ids": [row["id"] for row in inputs["comments"]],
            "next": next_step,
        }

    def check_accept_post(self, binding, *, pending):
        if binding["author"] != self.env.owner:
            return "changed"
        data, transient = read_issue(self.env, self.issue, once=True)
        if data is None:
            return "retry" if transient else "failed"
        if data["state"] == "CLOSED":
            return "superseded"
        labels = {label["name"] for label in data["labels"]}
        if "ready-for-human" in labels:
            return "paused"

        boundary = set(binding["comment_ids"])
        login_re = self.env.config.scheduling.cursor_login_re or r"^(app/cursor|cursor(\[bot\])?)$"
        matches, verdict_matches = [], []
        conflict, latest_accept = False, ""
        for row in data["comments"]:
            if row["id"] in boundary:
                continue
            body = row["body"]
            login = (row["author"] or {}).get("login") or ""
            owner = login == binding["author"]
            if body == binding["body"]:
                matches.append(owner)
            if body == binding["verdict"]:
                verdict_matches.append(owner)
            if (owner or re.search(login_re, login)) and (verdict := VERDICT.match(body)):
                latest_accept = body if verdict[1] == "accept" else ""
                conflict |= not (owner and body == binding["verdict"])
            elif owner and STATE_RE.match(body):
                latest_accept = ""
                conflict = True
            elif owner and (marker := PENDING_ACCEPT.match(body)):
                if body == binding["body"] or (not pending and marker["pr"] == binding["pr"]
                                                and marker["verdict"] == binding["verdict"]):
                    continue
                latest_accept = ""
                conflict = True

        if latest_accept and "ready-for-agent" in labels and "needs-triage" not in labels:
            if latest_accept != binding["verdict"]:
                return "accepted"
            if verdict_matches == [True]:
                return "completed"
        if (conflict or self.body_key(data["body"]) != binding["issue_body"]
                or self.owner_feedback_digest(data["comments"]) != binding["feedback"]):
            return "changed"
        if pending and not matches:
            matches = verdict_matches  # The exact verdict also proves the pending write landed.
        if not matches:
            return "absent"
        return "landed" if matches == [True] else "ambiguous"

    def check_accept_revision(self, binding, *, pending):
        if binding["pr"]:
            revision, transient = self.read_pr_revision(binding["pr"])
            if revision is None:
                return "", "retry" if transient else "failed"
            state = revision["state"]
            if (revision["headRefOid"] != binding["adr_head"]
                    or state != "MERGED" and not (pending and state == "OPEN")):
                return "", "changed"
            return state, ""
        adr, transient = self.discover_adr(pr_helpers.call, ("-R", self.repo))
        if adr is None:
            return "", "retry" if transient else "failed"
        return "", "changed" if adr or binding["adr_head"] else ""

    def finish_accept(self, verdict):
        """Label transition done: tear down the worktree and announce acceptance."""
        n = self.issue
        merge.remove_worktree(self.repo_dir, self.wt)
        round_m = re.search(r"round (\d+): accept", verdict)
        notify.notify(f"#{n}: spec accepted (round {round_m.group(1) if round_m else '?'})", self.url)
        advisor.finish_episode(self.env, "issue", n, "accept")
        recovery.clear(self.env, n, n)
        self.finish()
        return "accept"

    def post_accept(self, reason, binding, *, initial):
        """Save before posting an accept or pending-accept comment; confirm the label transition (#151)."""
        n = self.issue
        pending_kind = reason == owner_handoff.SPEC_PENDING_ACCEPT_POST
        label = "pending acceptance" if pending_kind else "accept verdict"

        def text():
            return f"Posting the {label} of round {binding['round'] if binding else '?'} failed. {self.url}"

        def stop(message):
            self.handoff(f"{text()} {message}", reason)
            return "stop"

        try:
            pending = owner_handoff.load_retry(self.env, n, reason)
        except owner_handoff.RetryUnreadable as exc:
            return stop(str(exc))
        if initial:
            if pending is not None:
                raise Deferred("handoff-retry", reason, lends_slot=False)
            pending = owner_handoff.prepare_retry(self.env, n, reason, n, binding)
            if pending is None:
                return stop("The worker inputs could not be saved.")
        elif pending is not None:
            binding = pending["continuation"]
        confirmed = pending is not None and pending.get("confirmed", False)
        if not initial:
            result = self.handoff(text(), reason, resume=True)
            if result.action == owner_handoff.DEFER:
                raise Deferred("handoff-retry", reason, lends_slot=False)
            if result.action != owner_handoff.RETRY:
                return "stop"
            binding = result.continuation

        if not pending_kind:
            # The saved verdict operation now owns the continuation, even if a crash left both records.
            owner_handoff.clear_retry(self.env, n, owner_handoff.SPEC_PENDING_ACCEPT_POST)

        def confirm_verdict():
            nonlocal confirmed
            if not confirmed:
                saved = owner_handoff.load_retry(self.env, n, reason)
                owner_handoff.restore_retry(self.env, n, reason, n, {**saved, "confirmed": True})
                confirmed = True

        def continue_after_post():
            nonlocal edited
            if pending_kind:
                if adr_state == "MERGED":
                    return self.publish_accept(binding)
                return self.merge_adr_and_publish(binding)

            confirm_verdict()

            def reconcile_labels():
                state = reconcile()
                if state in ("accepted", "completed"):
                    return "landed"
                if state == "landed":
                    return "absent"
                return "changed" if state == "absent" else state

            if status == "landed":
                edited = pr_helpers.gh("issue", "edit", n, "-R", self.repo, "--remove-label", "needs-triage",
                                       "--add-label", "ready-for-agent", reconcile=reconcile_labels)
                if edited.returncode != 0 or status == "accepted":
                    return None
            elif status != "completed":
                return None
            outcome = self.finish_accept(binding["verdict"])
            owner_handoff.clear_retry(self.env, n, reason)
            return outcome

        def reconcile():
            nonlocal status, adr_state
            status = self.check_accept_post(binding, pending=pending_kind)
            if status == "landed" or (status == "absent" and not confirmed):
                adr_state, failure = self.check_accept_revision(binding, pending=pending_kind)
                status = failure or status
            return status

        status, adr_state = "", ""
        reconcile()
        posted = edited = None
        if not confirmed and status == "absent":
            posted = pr_helpers.gh("issue", "comment", n, "-R", self.repo, "--body", binding["body"],
                                   reconcile=reconcile)
            if posted.returncode == 0:
                if not pending_kind:
                    confirm_verdict()
                if status == "absent":
                    reconcile()
        if status == "landed" or (status == "completed" and not pending_kind):
            outcome = continue_after_post()
            if outcome is not None:
                return outcome
        if status in ("accepted", "completed"):
            # A completed acceptance (verdict + labels) supersedes any saved accept write.
            owner_handoff.clear_retry(self.env, n, reason)
            self.finish()
            return "accept"
        if status == "superseded":
            owner_handoff.clear_retry(self.env, n, reason)
            advisor.finish_episode(self.env, "issue", n, "superseded")
            self.gh("issue", "edit", n, "--remove-label", "agent-wip")
            self.finish()
            return "stop"
        if status == "paused":
            if confirmed:
                pending = {**pending, "confirmed": True}
            owner_handoff.restore_retry(self.env, n, reason, n, pending)
            raise Deferred("handoff-retry", reason, lends_slot=False)
        failure = edited if edited is not None else posted
        if status == "retry" or (failure is not None and pr_helpers.transient(failure)
                                  and status == ("landed" if edited is not None else "absent")):
            saved = owner_handoff.load_retry(self.env, n, reason)
            attempts = saved["attempts"]
            if attempts < owner_handoff.MAX_ATTEMPTS:
                now = int(owner_handoff.clock())
                owner_handoff.restore_retry(self.env, n, reason, n,
                                            {**saved, "scheduled": now, "due": now + owner_handoff.DELAYS[attempts]})
                raise Deferred("handoff-retry", reason, lends_slot=False)
        return stop(f"Reconciliation: {status}." + (pr_helpers.why(failure) if failure is not None else ""))

    def answer_intent_path(self):
        return Path(self.state, f"advisor-answer-{self.issue}.json")

    def clear_answer_intent(self):
        self.answer_intent_path().unlink(missing_ok=True)

    def load_answer_intent(self):
        path = self.answer_intent_path()
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        strings = ("body", "questions", "reviewer", "issue_body", "feedback", "pr", "adr_head", "document_head")
        if (not isinstance(data, dict) or any(not isinstance(data.get(key), str) for key in strings)
                or any(type(data.get(key)) is not int for key in ("round", "max_rounds", "question_id"))
                or not (0 < data["round"] < data["max_rounds"] and data["question_id"] > 0)):
            raise ValueError("Invalid saved advisor answer intent")
        return data

    def referenced_documents(self, texts, revision, limit, chars):
        """Read referenced ADR blobs from a commit, retaining whether the raw excerpt was cut."""
        if not revision:
            return []
        tree = git(self.wt, "ls-tree", "--format=%(objectmode) %(objectname) %(path)", revision, "--", "docs/adr/")
        documents = {}
        for row in (tree or "").splitlines():
            mode, oid, name = row.split(" ", 2)
            if mode in ("100644", "100755") and re.fullmatch(r"docs/adr/[A-Za-z0-9][A-Za-z0-9._-]*\.md", name):
                documents[name.removeprefix("docs/adr/")] = oid
        names = []
        for text in texts:
            for match in re.finditer(r"docs/adr/([A-Za-z0-9][A-Za-z0-9._-]*\.md)|\bADR[ -]?0*(\d{1,4})\b",
                                     text or "", re.IGNORECASE):
                if match.group(1):
                    names.append(match.group(1))
                else:
                    prefix = f"{int(match.group(2)):04d}-"
                    names.extend(name for name in documents if name.startswith(prefix))
        found = []
        for name in dict.fromkeys(names):
            if len(found) >= limit:
                break
            if name not in documents:
                continue
            try:
                blob = subprocess.run(["git", "-C", str(self.wt), "cat-file", "blob", documents[name]],
                                      capture_output=True, env=child_env(), check=True)
                text = blob.stdout.decode("utf-8")
            except (subprocess.CalledProcessError, UnicodeError, OSError):
                continue
            found.append((f"docs/adr/{name}", text[:chars], len(text) > chars))
        return found

    def questions_citation_entries(self, issue_body, comments, reply, document_head):
        """Baseline citation rows from the issue body and genuine owner comments."""
        entries, owner = [], self.env.owner

        def one_line(text, truncated=False):
            line = re.sub(r"\s+", " ", (text or "").strip())
            if line and (truncated or len(line) > 1500):
                return line[:1500] + advisor.TRUNCATED
            return line

        body = one_line(issue_body)
        if body:
            entries.append((f"c{len(entries) + 1}", "issue body", body))
        owner_texts = [issue_body, reply]
        for comment in comments or ():
            login = (comment.get("author") or {}).get("login") or ""
            text = one_line(comment.get("body") or "")
            if login != owner or not text or text.startswith("<!-- baton"):
                continue
            owner_texts.append(comment.get("body") or "")
            entries.append((f"c{len(entries) + 1}", "owner comment", text))
        for rel, text, truncated in self.referenced_documents(owner_texts, document_head, 5, 2000):
            text = one_line(text, truncated)
            if text:
                entries.append((f"c{len(entries) + 1}", f"document {rel}", text))
        return entries

    def document_revision(self):
        return git(self.wt, "rev-parse", "--verify", "origin/main^{commit}") or ""

    def latest_spec_comment(self, data):
        return next((row for row in reversed(data["comments"])
                     if (row["author"] or {}).get("login") == self.env.owner
                     and STATE_RE.match(row["body"])), None)

    def answer_inputs_current(self, data, saved):
        """True while the open issue, owner replies, latest trusted question and ADR match."""
        if data["state"] != "OPEN" or self.body_key(data["body"]) != saved["issue_body"]:
            return False
        if any(label["name"] == "ready-for-human" for label in data["labels"]):
            return False
        if self.owner_feedback_digest(data["comments"]) != saved["feedback"]:
            return False
        question = self.latest_spec_comment(data)
        if (question is None or question["id"] != saved["question_id"]
                or owner_handoff.body_digest(question["body"]) != saved["questions"]):
            return False
        if not saved["pr"] and saved["document_head"] and self.document_revision() != saved["document_head"]:
            return False
        adr, _ = self.discover_adr(pr_helpers.call, ("-R", self.repo))
        return (adr is not None and str(adr.get("number", "")) == saved["pr"]
                and adr.get("headRefOid", "") == saved["adr_head"])

    def questions_snapshot(self, binding, questions, payload):
        """(snapshot, revalidate) for the spec_questions decision point."""
        entries = self.questions_citation_entries(
            payload["body"], payload["comments"], binding.get("reply") or "", binding["document_head"])
        question_lines = "\n".join(f"- {q['id']}: {q['text']}" for q in questions)
        citation_block = advisor.format_citations(entries) if entries else "Citation evidence:\n"
        baseline = (
            f"Decision point: spec_questions. Issue #{self.issue} of {self.repo}, round {binding['round']}.\n"
            f"Reviser: {binding['reviewer']}. ADR PR: {binding.get('pr') or 'none'}; "
            f"head: {binding.get('adr_head') or 'none'}.\n"
            f"Questions:\n{question_lines}\n{citation_block}\n"
            f"Question comment digest: {binding['digest']}.\n"
            f"Latest reviser reply:\n{binding.get('reply') or ''}"
        )
        snapshot = advisor.augment_snapshot(self.env, baseline)
        saved = {
            "questions": binding["digest"], "question_id": binding["question_id"], "issue_body": binding["issue_body"],
            "feedback": binding["feedback"], "adr_head": binding.get("adr_head") or "",
            "pr": binding.get("pr") or "", "document_head": binding["document_head"],
        }

        def still_current():
            data, _ = read_issue(self.env, self.issue, once=True)
            return data is not None and self.answer_inputs_current(data, saved)

        return snapshot, still_current

    def format_answer_body(self, binding, answers, questions_digest):
        cleaned = []
        for row in answers:
            text = "\n".join(
                line for line in strip_markers(row["text"]).split("\n") if not VERDICT_LINE.match(line)
            ).rstrip("\n")
            cites = ", ".join(row["citations"])
            cleaned.append(f"### {row['id']}\n{text}\n\nCitations: {cites}")
        return (
            f"<!-- baton-advisor-answer round={binding['round']} questions={questions_digest} by=deepseek -->\n"
            f"## Advisor answers (DeepSeek), round {binding['round']}\n\n"
            f"Answering the reviser's questions (comment digest `{questions_digest[:12]}`).\n\n"
            + "\n\n".join(cleaned)
        )

    def publish_advisor_answers(self, binding, answers):
        """Persist intent, post one attributed answer comment, reconcile a lost response (#125)."""
        body = self.format_answer_body(binding, answers, binding["digest"])
        intent = {
            "body": body,
            "questions": binding["digest"], "question_id": binding["question_id"], "round": binding["round"],
            "max_rounds": binding["max_rounds"] + self.extra, "reviewer": binding["reviewer"],
            "issue_body": binding["issue_body"], "feedback": binding["feedback"],
            "adr_head": binding.get("adr_head") or "", "pr": binding.get("pr") or "",
            "document_head": binding["document_head"],
        }
        try:
            atomic_json(self.answer_intent_path(), intent)
        except OSError:
            self.handoff("Could not save the advisor answer before posting.", "spec_questions")
            return "stop"
        outcome = self.complete_answer_post(intent)
        if outcome == "answered":
            self.answer_intent = intent
        return outcome

    def stop_advisor_answer(self, intent, *, closed=False):
        self.clear_answer_intent()
        owner_handoff.clear_retry(self.env, self.issue, owner_handoff.SPEC_REPLY_POST)
        if closed:
            advisor.finish_episode(self.env, "issue", self.issue, "closed")
        else:
            self.handoff(f"{intent['reviewer']} has questions only you can answer. "
                         "Reply on the issue, then remove ready-for-human.", "spec_questions")

    def posted_answer_bodies(self, data, intent):
        """Trusted answers for this digest published after its bound question."""
        for row in reversed(data["comments"]):
            if row["id"] == intent["question_id"]:
                break
            if (row["author"] or {}).get("login") == self.env.owner:
                marker = ADVISOR_ANSWER.match(row["body"])
                if marker and marker["q"] == intent["questions"]:
                    yield row["body"]

    def answer_revision_payload(self, intent):
        """Re-read the publication and its inputs at the revision boundary."""
        data, transient = read_issue(self.env, self.issue, once=True)
        if data is None and transient:
            raise Deferred("advisor-answer", "spec_questions", lends_slot=False)
        if (data is None or not self.answer_inputs_current(data, intent)
                or intent["body"] not in self.posted_answer_bodies(data, intent)):
            self.stop_advisor_answer(intent, closed=bool(data and data["state"] == "CLOSED"))
            return None
        # The intent stays until the reviser is dispatched (agent): a short block resumes it, saved limit and all.
        return data

    def complete_answer_post(self, intent):
        """Post or reconcile the saved advisor answer; 'answered' resumes revision."""
        n = self.issue
        data, transient = read_issue(self.env, n, once=True)
        if data is None:
            if transient:
                raise Deferred("advisor-answer", "spec_questions", lends_slot=False)
            self.clear_answer_intent()
            self.handoff("Could not re-read the issue before posting advisor answers.", "spec_questions")
            return "stop"
        # A genuine owner reply, an edited issue or a moved ADR supersedes the saved answer, also on restart.
        if not self.answer_inputs_current(data, intent):
            self.stop_advisor_answer(intent, closed=data["state"] == "CLOSED")
            return "stop"

        posted_answers = list(self.posted_answer_bodies(data, intent))
        if intent["body"] in posted_answers:
            return "answered"
        if posted_answers:
            # A different body for the same question version: do not double-post.
            self.clear_answer_intent()
            self.handoff("An advisor answer for these questions is already present but does not match "
                         "the saved intent.", "spec_questions")
            return "stop"

        def reconcile():
            data, transient = read_issue(self.env, n, once=True)
            if data is None:
                return "retry" if transient else "failed"
            if data["state"] == "CLOSED":
                return "closed"
            if not self.answer_inputs_current(data, intent):
                return "superseded"
            posted_answers = list(self.posted_answer_bodies(data, intent))
            if intent["body"] in posted_answers:
                return "landed"
            return "superseded" if posted_answers else "absent"

        posted = pr_helpers.gh("issue", "comment", n, "-R", self.repo, "--body", intent["body"],
                               reconcile=reconcile)
        status = "landed" if posted.returncode == 0 else reconcile()
        if status == "landed":
            return "answered"
        if status in ("closed", "superseded"):
            self.stop_advisor_answer(intent, closed=status == "closed")
            return "stop"
        if pr_helpers.transient(posted):
            raise Deferred("advisor-answer", "spec_questions", lends_slot=False)
        self.clear_answer_intent()
        self.handoff(f"Posting the advisor answers failed.{pr_helpers.why(posted)}", "spec_questions")
        return "stop"

    def answer_or_handoff(self, binding):
        """Consult the advisor for grounded answers, or hand off (#125)."""
        why = (f"{binding['reviewer']} has questions only you can answer. "
               "Reply on the issue, then remove ready-for-human.")
        questions = advisor.extract_questions(binding.get("reply") or "")
        if not questions:
            self.handoff(why, "spec_questions")
            return "stop"
        # A revision plus an independent review must still fit in the remaining budget.
        if binding["round"] >= binding["max_rounds"] + self.extra:
            self.handoff(why + " No remaining round for an advisor answer and review.", "spec_questions")
            return "stop"
        payload = self.issue_payload()
        if payload is None:
            return "stop"
        question = self.latest_spec_comment(payload)
        if (question is None or question["body"] != binding["body"] or question["id"] in binding["comment_ids"]):
            self.handoff(why, "spec_questions")
            return "stop"
        document_head = ""
        if self.env.repo and self.env.repo.advisor_local_documents:
            document_head = binding["adr_head"] or self.document_revision()
        binding = {**binding, "question_id": question["id"], "document_head": document_head}
        snap, check = self.questions_snapshot(binding, questions, payload)
        result = self.handoff(why, "spec_questions", snapshot=snap, revalidate=check,
                              observed_actions=("answer_spec_questions",))
        if result.action != "answer_spec_questions":
            return "stop"
        answers = getattr(result.decision, "answers", ()) or ()
        if not answers:
            self.handoff(why, "spec_questions")
            return "stop"
        # Re-check citations against the current sources before posting.
        entries = {i: text for i, _, text in self.questions_citation_entries(
            payload["body"], payload["comments"], binding.get("reply") or "", document_head)}
        for row in answers:
            combined = "\n".join(entries[c] for c in row["citations"] if c in entries)
            if row["text"] not in combined:
                self.handoff(why + " Advisor citations no longer match the issue.", "spec_questions")
                return "stop"
        return self.publish_advisor_answers(binding, answers)

    def post_comment(self, reason, binding, *, initial, claim=None, context=None, after_save=None):
        """Save before sending a spec comment, or consume a due attempt before reconciling (#151).

        On a resume CLAIM (labels, worktree) runs only once the attempt is due and the issue is neither
        paused, closed, changed nor accepted."""
        n = self.issue
        label = "spec review" if reason == owner_handoff.SPEC_REVIEW_POST else "spec reply"

        def text():
            return f"Posting the {label} of round {binding['round'] if binding else '?'} failed. {self.url}"

        def stop(message):
            self.handoff(f"{text()} {message}", reason)
            return "stop"

        try:
            pending = owner_handoff.load_retry(self.env, n, reason)
        except owner_handoff.RetryUnreadable as exc:
            return stop(str(exc))
        if initial:
            if pending is not None:
                raise Deferred("handoff-retry", reason, lends_slot=False)
            pending = owner_handoff.prepare_retry(self.env, n, reason, n, binding)
            if pending is None:
                return stop("The worker inputs could not be saved.")
            if after_save is not None:
                after_save()
        elif pending is not None:
            binding = pending["continuation"]
        confirmed = pending is not None and pending.get("confirmed", False)
        consumed = False
        if confirmed and not initial and pending.get("reads") is not None:
            # Consume the due read first, so a restart never allows another (#151).
            reads = pending["reads"]
            if reads >= owner_handoff.MAX_ATTEMPTS:
                return stop("The comment landed, but its continuation could not be read.")
            reads += 1
            pending = {**pending, "reads": reads}
            if reads < owner_handoff.MAX_ATTEMPTS:
                now = int(owner_handoff.clock())
                pending.update(scheduled=now, due=now + owner_handoff.DELAYS[reads])
            owner_handoff.restore_retry(self.env, n, reason, n, pending)
            consumed = True
        if not initial and not confirmed:
            result = self.handoff(text(), reason, resume=True)
            if result.action == owner_handoff.DEFER:
                raise Deferred("handoff-retry", reason, lends_slot=False)
            if result.action != owner_handoff.RETRY:
                return "stop"
            binding = result.continuation
            # Keep the pre-resume attempts: resume already consumed one on disk (#151).

        def finish():
            if not confirmed:
                saved = owner_handoff.load_retry(self.env, n, reason)
                owner_handoff.restore_retry(self.env, n, reason, n, {**saved, "confirmed": True})
            if reason == owner_handoff.SPEC_REVIEW_POST:
                if binding["round"] >= binding["max_rounds"] + self.extra:
                    if context is not None:
                        path = Path(context)
                    else:
                        # A resume has no round context: rebuild it from a fresh read of the issue.
                        payload = self.issue_payload()
                        if payload is None:
                            return "stop"
                        self.fresh_payload = payload
                        path = Path(self.ctx, f"issue-{n}-spec-post.md")
                        path.write_text(review.issue_context(self.env, n, data=payload))
                    if not self.at_limit(binding["round"], binding["max_rounds"], binding["reviewer"],
                                         binding["reply"], path, binding["limit_why"]):
                        owner_handoff.clear_retry(self.env, n, reason)
                        return "stop"
                owner_handoff.clear_retry(self.env, n, reason)
                return "changes"
            if binding["next"] == "human":
                # Leave the confirmed reply discoverable by tick until the answer's revision starts.
                outcome = self.answer_or_handoff(binding)
                if outcome != "answered":
                    owner_handoff.clear_retry(self.env, n, reason)
                return outcome
            owner_handoff.clear_retry(self.env, n, reason)
            return "revised"

        def reconcile(*, sending=False):
            nonlocal status
            status = self.check_comment_post(binding, sending=sending)
            return status

        def claimed():
            return initial or claim is None or claim()

        status = reconcile(sending=initial)
        posted = None
        if status == "absent" and not confirmed:
            if not claimed():
                return "stop"
            posted = pr_helpers.gh("issue", "comment", n, "-R", self.repo, "--body", binding["body"],
                                   reconcile=reconcile)
            if posted.returncode == 0:
                return finish()
        if status == "landed":
            if not claimed():
                return "stop"
            return finish()
        if status == "accepted":
            owner_handoff.clear_retry(self.env, n, reason)
            return "stop"
        if status == "superseded":
            owner_handoff.clear_retry(self.env, n, reason)
            advisor.finish_episode(self.env, "issue", n, "superseded")
            self.gh("issue", "edit", n, "--remove-label", "agent-wip")
            self.finish()
            return "stop"
        if status == "paused":
            owner_handoff.restore_retry(self.env, n, reason, n, pending)
            raise Deferred("handoff-retry", reason, lends_slot=False)
        if not confirmed and (status == "retry" or status == "absent" and posted is not None
                              and pr_helpers.transient(posted)):
            attempts = pending["attempts"] + (0 if initial else 1)
            if attempts < owner_handoff.MAX_ATTEMPTS:
                now = int(owner_handoff.clock())
                owner_handoff.restore_retry(self.env, n, reason, n,
                                            {**pending, "attempts": attempts, "scheduled": now,
                                             "due": now + owner_handoff.DELAYS[attempts]})
                raise Deferred("handoff-retry", reason, lends_slot=False)
        if confirmed and status == "retry":
            # The post already landed: a transient read only delays the continuation (#151).
            if consumed:
                if pending["reads"] < owner_handoff.MAX_ATTEMPTS:
                    raise Deferred("handoff-retry", reason, lends_slot=False)
            else:
                now = int(owner_handoff.clock())
                owner_handoff.restore_retry(self.env, n, reason, n,
                                            {**pending, "reads": 0, "scheduled": now,
                                             "due": now + owner_handoff.DELAYS[0]})
                raise Deferred("handoff-retry", reason, lends_slot=False)
        return stop(f"Reconciliation: {status}." + (pr_helpers.why(posted) if posted is not None else ""))

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

    def keep_owner_body(self, body):
        """Before the first spec round: spec-owner-body-N keeps the body the owner wrote."""
        if self.path("spec-owner-body").is_file():
            return
        self.path("spec-owner-body").write_text(body)

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
        baseline = (f"Decision point: spec round limit. Issue #{n} of {self.repo} used {round} spec review rounds "
                    f"and the last reviewer ({reviewer}) still asks for changes.\n"
                    "Actions: extra_round = one more revision of the issue by an agent, then one more review; "
                    "handoff = the owner takes the spec.\n\n"
                    f'<last-review round="{round}" by="{reviewer}">\n{reply}\n</last-review>\n\n'
                    f"<adr-changes>\n{diff}\n</adr-changes>\n\n{issue_text}")
        diff_excerpt = ""
        if env.repo.advisor_diff_excerpts:
            diff_excerpt = (git(self.wt, "diff", "origin/main...HEAD") or "")[:4000]
        docs = []
        if env.repo.advisor_local_documents:
            revision = self.adr_head or self.document_revision()
            docs = [(path, text + advisor.TRUNCATED if truncated else text)
                    for path, text, truncated in self.referenced_documents([issue_text, reply], revision, 3, 1500)]
        snapshot = advisor.augment_snapshot(env, baseline, diff_excerpt=diff_excerpt,
                                            document_excerpts=tuple(docs))
        return snapshot, (lambda: spec_state(env, n) == state)

    def at_limit(self, round, max_rounds, reviewer, reply, context, why):
        """True when the spec goes on for one advisor-granted round; otherwise hands off."""
        if round < max_rounds + self.extra:
            return True
        if advisor.granted(self.env, "spec_questions", self.issue):
            self.handoff(why, "spec_limit", observed_actions=())
            return False
        snap, check = self.limit_snapshot(round, reviewer, reply, context)
        result = self.handoff(why, "spec_limit", snapshot=snap, revalidate=check)
        if result.action == "extra_round":
            self.extra = 1
            notify.log(f"#{self.issue}: advisor granted one more spec round: {result.decision.reason}")
            return True
        return False

    def review_round(self, round, max_rounds, context, inputs):
        """"accept", "changes" or "stop" (the owner or a failure took over)."""
        n, wt, env = self.issue, self.wt, self.env
        listed, issue_body = inputs["comments"], inputs["body"]
        adr_note = "No ADR has been revised yet."
        if git(wt, "rev-list", "origin/main..HEAD"):
            adr_note = (f"Earlier rounds revised ADRs on this branch (docs PR #{self.pr or '?'}): "
                        "see `git diff origin/main...HEAD`.")
        head = git(wt, "rev-parse", "HEAD") or ""
        last_vendor = last_by(listed, env.owner, "reply")
        other_model = last_model(env, n, "reply", round - 1, last_vendor)
        pending = self.resume_worker("spec_reviewer", "spec_review_run_failed", round, other_model)
        if pending is False:
            return "stop"
        reviewer = pending[1] if pending else self.pick("spec_reviewer", pick.all_vendors(env),
                                                        last_vendor, other_model)
        attempt = int(bool(pending))
        while True:
            tag = f"issue-{n}-spec-{round}" + (f"-retry{attempt}" if attempt else "")
            out = self.agent(reviewer, "spec_review", self.render(
                self.self_dir / "spec-review-prompt.md", VENDOR=reviewer, ISSUE=n, GH_REPO=self.repo, WORKTREE=str(wt),
                BRANCH=self.branch, ROUND=str(round), MAX_ROUNDS=str(max_rounds), CONTEXT_FILE=str(context),
                ADR_NOTE=adr_note, ROUND_NOTE=self.round_note(round)), tag, "review-settings.json",
                followup=self.followup(round, reviewer, last_vendor), round_s=str(round))
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
                # The worker step is done; the verdict and comment posts keep their own durable retry (#151).
                recovery.clear(self.env, self.issue, self.issue)
                problems = self.accept_problems()
                if not problems:
                    return self.accept(reviewer, round, mode_note, reply, inputs)
                points = "\n".join(f"{i}. [decidable] {p}" for i, p in enumerate(problems, 1))
                body = (f"<!-- baton-spec-review round={round} by={reviewer} -->\n"
                        f"## Spec review ({reviewer}), round {round}: accept refused by the runner\n\n"
                        f"{reviewer} accepted, but the issue body breaks the runner's rules:\n\n{points}\n\n"
                        f"{mode_note}{reply[:REPLY_CAP]}")
                binding = self.comment_post_binding(
                    body, round, reviewer, inputs, next_step="revise", reply=points, max_rounds=max_rounds,
                    limit_why=(f"{reviewer} accepted, but the issue body breaks the runner's rules "
                               f"after {round} spec rounds."))
                return self.post_comment(owner_handoff.SPEC_REVIEW_POST, binding, initial=True, context=context)
            if outcome.startswith("SPEC: changes"):
                recovery.clear(self.env, self.issue, self.issue)
                body = (f"<!-- baton-spec-review round={round} by={reviewer} -->\n"
                        f"## Spec review ({reviewer}), round {round}\n\n{mode_note}{reply[:REPLY_CAP]}")
                binding = self.comment_post_binding(
                    body, round, reviewer, inputs, next_step="revise", reply=reply[:REPLY_CAP],
                    max_rounds=max_rounds,
                    limit_why=f"{reviewer} still has points after {round} spec rounds.")
                return self.post_comment(owner_handoff.SPEC_REVIEW_POST, binding, initial=True, context=context)
            if outcome.startswith("SPEC: reject"):
                self.gh("issue", "comment", n, "--body", f"<!-- baton-note by={reviewer} -->\n"
                        f"## Spec review ({reviewer}), round {round}: reject\n\n{reply[:REPLY_CAP]}")
                self.handoff(f"{reviewer} rejects the issue.", "spec_rejected")
                return "stop"
            retry = self.recover_worker(
                f"{reviewer}'s spec review failed: {outcome}", "spec_review_run_failed", reviewer, outcome,
                round_s=str(round), issue_body=issue_body, listed=listed, last_vendor=last_vendor,
                other_model=other_model, spec_followup=self.followup(round, reviewer, last_vendor))
            if not retry:
                return "stop"
            _, reviewer = retry
            attempt += 1

    def accept_verdict(self, reviewer, round, mode_note, reply):
        return (f"<!-- baton-verdict: accept by={reviewer} -->\n"
                f"## Spec review ({reviewer}), round {round}: accept\n\n{mode_note}{reply[:REPLY_CAP]}")

    def read_pr_revision(self, pr):
        result = self.gh("pr", "view", str(pr), "--json", "state,headRefOid")
        if result.returncode != 0:
            return None, pr_helpers.transient(result)
        try:
            data = json.loads(result.stdout)
            # An unrecognized state is a readable answer: the caller hands off as spec_adr_state_invalid.
            if (isinstance(data["state"], str)
                    and re.fullmatch(r"[0-9a-f]{40}", data["headRefOid"])):
                return data, False
        except (KeyError, TypeError, ValueError):
            pass
        return None, True

    def discover_adr(self, request=None, extra=()):
        fields = "number,headRefName,headRefOid,baseRefName,isCrossRepository,headRepository,headRepositoryOwner"
        result = (request or self.gh)("pr", "list", "--state", "open", "--head", self.branch, "--limit", "100",
                                      "--json", fields, *extra)
        if result.returncode != 0:
            return None, pr_helpers.transient(result)
        try:
            rows = json.loads(result.stdout)
            if not isinstance(rows, list) or len(rows) >= 100:
                return None, False
            matching = []
            for row in rows:
                if row["isCrossRepository"] is True:
                    continue
                owner, repo = row["headRepositoryOwner"]["login"], row["headRepository"]["name"]
                if (row["isCrossRepository"] is not False or f"{owner}/{repo}".casefold() != self.repo.casefold()
                        or row["headRefName"] != self.branch or row["baseRefName"] != "main"):
                    return None, False
                if (type(row["number"]) is not int or row["number"] < 1
                        or not re.fullmatch(r"[0-9a-f]{40}", row["headRefOid"])):
                    return None, False
                matching.append(row)
            if len(matching) <= 1:
                return (matching[0] if matching else {}), False
        except (KeyError, TypeError, ValueError):
            pass
        return None, False

    def adr_discovery(self):
        return self.retry_read(self.discover_adr, "Could not identify the ADR pull request.",
                               "spec_pending_adr_read_failed", continuation={"step": "discovery"})

    def publish_accept(self, binding):
        """Carry the reviewed inputs into the durable verdict and label operation."""
        verdict = binding["verdict"]
        publication = {**binding, "body": verdict, "digest": owner_handoff.body_digest(verdict),
                       "next": "transition"}
        return self.post_accept(owner_handoff.SPEC_ACCEPT_POST, publication, initial=True)

    def merge_adr_and_publish(self, binding):
        """CI + shared merge helper for an open ADR PR, then publish the verdict once MERGED."""
        pr, verdict = binding["pr"], binding["verdict"]
        n, who = self.issue, binding["reviewer"]
        merger = merge.Merger(self.env, self.hooks, pr, n)
        ci_message = f"{who} accepted, but CI on the ADR PR #{pr} is not green."

        def stop(why, reason, **status):
            self.handoff(why, reason, **merger.observation(reason, acceptance=verdict, **status))
            return "stop"

        def merge_failed(result):
            return stop(f"{who} accepted, but merging the ADR PR #{pr} failed.{result.get('detail') or ''}",
                        "spec_adr_merge_failed", failure=result.get("reason") or "")

        # An update saved by an earlier run settles first, and its deadline before any PR read.
        pending = merger.settle_pending_update()
        if isinstance(pending, dict):
            return merge_failed(pending)
        if pending:
            merger.validated_head = pending
            merger.update_count += 1
        if not merger.ci_green(30):
            return stop(ci_message, "spec_adr_ci_before_merge_not_green", ci_failed=True)
        result = merger.merge_with_updates(30, pinned=binding.get("adr_head") or "")
        if not result.get("ok"):
            if result.get("reason") == "ci_after_update_not_green":
                return stop(ci_message, "spec_adr_ci_after_update_not_green", ci_failed=True)
            return merge_failed(result)
        revision, _ = self.read_pr_revision(pr)
        if not revision or revision["state"] != "MERGED":
            return stop(f"{who} accepted, but ADR PR #{pr} is still open after the merge attempt.",
                        "spec_adr_merge_unconfirmed")
        notify.notify(f"#{n}: ADR PR #{pr} merged", f"https://github.com/{self.repo}/pull/{pr}")
        return self.publish_accept({**binding, "adr_head": merger.merge_head})

    def resume_pending_accept(self, binding, payload=None, *, recovering=False):
        """Resume acceptance against the original inputs and one authoritative PR revision."""
        pr, verdict = binding["pr"], binding["verdict"]
        self.pr = pr
        reason = "spec_pending_adr_read_failed"
        why = f"Could not read inputs to resume acceptance of ADR PR #{pr}."

        inputs = None

        def read():
            nonlocal inputs
            data = payload
            if data is None:
                data, transient = read_issue(self.env, self.issue)
                if data is None:
                    return None, transient
            current = accept_pending(self.env, self.issue, listed=data["comments"])
            if not current:
                if plan_from(data["comments"], self.env.owner, self.env.config.scheduling.cursor_login_re) == verdict:
                    return "PUBLISHED", False
                return "CHANGED", False
            if (current["pr"] != pr or current["verdict"] != verdict or current["head"] != binding["adr_head"]
                    or data["body"] != binding["issue_body"]):
                return "CHANGED", False
            revision, transient = self.read_pr_revision(pr)
            if revision is None:
                return None, transient
            state, head = revision["state"], revision["headRefOid"]
            updating = Path(self.state, f"adr-update-pr-{pr}.json").exists()   # its head moved by our own update
            if (binding["adr_head"] and head != binding["adr_head"] and not updating
                    or recovering and not binding["adr_head"]):
                return "CHANGED", False
            self.adr_head = head
            inputs = data
            return state, False

        state = self.retry_read(read, why, reason, continuation=binding)
        if state is None:
            return False
        if state == "PUBLISHED":
            return True
        if state == "CHANGED":
            self.handoff(f"Issue body, verdict, or ADR head changed or could not be verified for PR #{pr}.", reason)
            return False
        if state == "CLOSED":
            self.handoff(f"ADR PR #{pr} was closed without merging; acceptance stays pending.", "spec_adr_closed")
            return False
        if state not in ("OPEN", "MERGED"):
            reason = "spec_adr_state_invalid"
            merger = merge.Merger(self.env, self.hooks, pr, self.issue)
            self.handoff(f"ADR PR #{pr} state is unreadable ({state}); acceptance stays pending.",
                         reason, **merger.observation(reason, acceptance=verdict))
            return False
        match = VERDICT.match(verdict)
        reviewer = (match[3] if match else None) or "claude"
        round_m = re.search(r"round (\d+): accept", verdict)
        if not round_m:
            self.handoff("Could not bind the accept verdict for a retry.", owner_handoff.SPEC_ACCEPT_POST)
            return False
        publication = self.accept_post_binding(verdict, verdict, int(round_m.group(1)), reviewer,
                                               inputs, next_step="transition")
        if state == "MERGED":
            return self.publish_accept(publication) == "accept"
        return self.merge_adr_and_publish(publication) == "accept"

    def accept(self, reviewer, round, mode_note, reply, inputs):
        """Merge the ADR PR when there is one, post the verdict, and make the issue runnable."""
        pr = self.pr
        verdict = self.accept_verdict(reviewer, round, mode_note, reply[:REPLY_CAP])
        if pr:
            pending = f"<!-- baton-spec-accept-pending pr={pr} head={self.adr_head} -->\n{verdict}"
            binding = self.accept_post_binding(
                pending, verdict, round, reviewer, inputs, next_step="merge")
            return self.post_accept(owner_handoff.SPEC_PENDING_ACCEPT_POST, binding, initial=True)
        binding = self.accept_post_binding(verdict, verdict, round, reviewer, inputs, next_step="transition")
        return self.publish_accept(binding)

    def push_flag(self):
        """Says an ADR push is waiting, so a lost record fails closed instead of rerunning the reviser."""
        return Path(self.state, f"spec-adr-push-waiting-{self.issue}")

    def adr_push_binding(self, reviser, model, round, human, reply, mode_note, max_rounds, inputs, applied):
        """Inputs known before the ADR push; commit and remote are filled at save time (#151)."""
        return {
            "issue": self.issue, "reviser": reviser, "model": model or "", "round": round,
            "human": human, "reply": reply, "mode_note": mode_note,
            "max_rounds": max_rounds, "limit_why": "n/a", "pr": self.pr or "",
            "issue_body": self.body_key(applied if applied is not None else inputs["body"]),
            "feedback": self.owner_feedback_digest(inputs["comments"]),
            "comment_ids": [row["id"] for row in inputs["comments"]],
            "author": self.env.owner, "next": "human" if human else "review",
        }

    def check_adr_push(self, binding):
        """Revalidate the saved ADR push before touching the remote or running its continuation."""
        data, transient = read_issue(self.env, self.issue, once=True)
        if data is None:
            return "retry" if transient else "failed"
        self.adr_read = data   # the continuation reuses this read instead of making a second one
        if data["state"] == "CLOSED":
            return "superseded"
        labels = {label["name"] for label in data["labels"]}
        if "ready-for-human" in labels:
            return "paused"
        if "ready-for-agent" in labels and "needs-triage" not in labels and any(
            (row["author"] or {}).get("login") == binding["author"]
            and (verdict := VERDICT.match(row["body"])) and verdict[1] == "accept"
            for row in data["comments"] if row["id"] not in set(binding["comment_ids"])
        ):
            return "superseded"
        if (self.body_key(data["body"]) != binding["issue_body"]
                or self.owner_feedback_digest(data["comments"]) != binding["feedback"]):
            return "changed"
        return "ok"

    def finish_revision_reply(self, reviser, model, round, human, reply, mode_note, max_rounds, inputs, applied,
                              after_save=None):
        """Post the reply after ADR work (or when there was none) and clear the worker grant."""
        n = self.issue
        adr = f"\nADR revisions: #{self.pr}\n" if self.pr else ""
        if self.pr:
            self.adr_head = git(self.wt, "rev-parse", "HEAD") or self.adr_head
        body = (f"<!-- baton-spec-reply round={round}{human} by={reviser} -->\n"
                f"## Spec reply ({reviser}), round {round}\n{adr}\n{mode_note}{reply}")
        binding = self.comment_post_binding(
            body, round, reviser, inputs, next_step="human" if human else "review",
            reply=reply, max_rounds=max_rounds, limit_why="n/a", issue_body=applied)
        recovery.clear(self.env, self.issue, self.issue)
        try:
            return self.post_comment(owner_handoff.SPEC_REPLY_POST, binding, initial=True, after_save=after_save)
        finally:
            for leftover in Path("/tmp").glob(f"baton-spec-{n}-{round}.*"):
                leftover.unlink()

    def body_edit_binding(self, intended, original, round, reviser, model, human, reply, mode_note,
                          max_rounds, inputs):
        """Inputs known before applying a revised issue body (#151)."""
        return {
            "issue": self.issue, "reviser": reviser, "model": model or "", "round": round,
            "human": human, "reply": reply, "mode_note": mode_note,
            "max_rounds": max_rounds, "limit_why": "n/a",
            "intended": intended, "original": self.body_key(original),
            "feedback": self.owner_feedback_digest(inputs["comments"]),
            "comment_ids": [row["id"] for row in inputs["comments"]],
            "author": self.env.owner, "next": "human" if human else "review",
            "has_commits": bool(git(self.wt, "rev-list", f"{self.base}..HEAD")),
            "wt": str(self.wt),
        }

    def check_body_edit(self, binding):
        """Revalidate a saved body edit before rewriting or running its continuation."""
        data, transient = read_issue(self.env, self.issue, once=True)
        if data is None:
            return "retry" if transient else "failed"
        self.adr_read = data
        if data["state"] == "CLOSED":
            return "superseded"
        labels = {label["name"] for label in data["labels"]}
        if "ready-for-human" in labels:
            return "paused"
        if "ready-for-agent" in labels and "needs-triage" not in labels and any(
            (row["author"] or {}).get("login") == binding["author"]
            and (verdict := VERDICT.match(row["body"])) and verdict[1] == "accept"
            for row in data["comments"] if row["id"] not in set(binding["comment_ids"])
        ):
            return "superseded"
        if self.owner_feedback_digest(data["comments"]) != binding["feedback"]:
            return "changed"
        kind, round = state_from(data["comments"], self.env.owner)
        if kind in ("review", "reply", "reply-human") and round > binding["round"]:
            return "changed"
        if self.body_key(data["body"]) == self.body_key(binding["intended"]):
            return "landed"
        if self.body_key(data["body"]) == binding["original"]:
            return "absent"
        return "changed"

    def finish_body_edit(self, binding):
        """Continuation after a landed body edit: push ADR commits or post the reply (#151)."""
        n = self.issue

        def clear():
            # Only once the next operation is saved: a crash before this resumes from the landed edit.
            owner_handoff.clear_retry(self.env, n, owner_handoff.SPEC_BODY_EDIT)

        data = self.adr_read
        if data is None:
            data, _ = read_issue(self.env, n, once=True)
            if data is None:
                clear()
                self.handoff("Could not read the issue after the body edit landed.",
                             "spec_comments_read_failed")
                return "stop"
        known = set(binding["comment_ids"])
        inputs = {"body": binding["intended"],
                  "comments": [row for row in data["comments"] if row["id"] in known]}
        applied = binding["intended"]
        if binding["has_commits"]:
            try:
                self.record(binding["reviser"], binding["model"])
            except OSError:
                return "stop"
            push_binding = self.adr_push_binding(
                binding["reviser"], binding["model"], binding["round"], binding["human"],
                binding["reply"], binding["mode_note"], binding["max_rounds"], inputs, applied)
            self.adr_round = (inputs, applied)
            return self.push_adr(push_binding, initial=True, after_save=clear)
        try:
            self.record(binding["reviser"], binding["model"])
        except OSError:
            return "stop"
        return self.finish_revision_reply(
            binding["reviser"], binding["model"], binding["round"], binding["human"],
            binding["reply"], binding["mode_note"], binding["max_rounds"], inputs, applied,
            after_save=clear)

    def edit_body(self, binding, *, initial, pending=None):
        """Save before applying a revised issue body, or consume a due attempt (#151)."""
        n, reason = self.issue, owner_handoff.SPEC_BODY_EDIT
        text = f"Applying the revised issue body failed. {self.url}"

        def stop(message):
            self.handoff(f"{text} {message}", reason)
            return "stop"

        resumed_landed = False
        try:
            found = owner_handoff.load_retry(self.env, n, reason)
        except owner_handoff.RetryUnreadable as exc:
            return stop(str(exc))
        if initial:
            if found is not None:
                raise Deferred("handoff-retry", reason, lends_slot=False)
            pending = owner_handoff.prepare_retry(self.env, n, reason, n, binding)
            if pending is None:
                return stop("The worker inputs could not be saved.")
        else:
            pending = found if pending is None else pending
            if pending is None:
                return stop("The saved body edit is missing.")
            binding = pending["continuation"]
            if pending.get("phase") == "landed":
                resumed_landed = True
            else:
                result = self.handoff(text, reason, resume=True)
                if result.action == owner_handoff.DEFER:
                    raise Deferred("handoff-retry", reason, lends_slot=False)
                if result.action != owner_handoff.RETRY:
                    return "stop"
                binding = result.continuation
                # Keep the pre-resume attempts: resume already consumed one on disk (#151).

        def reconcile(*, sending=False):
            return self.check_body_edit(binding)

        def claim():
            # A resumed attempt claims the issue only once it is known not to be paused or superseded.
            if initial:
                return True
            if not self.worktree():
                stop("spec worktree missing.")
                return False
            self.gh("issue", "edit", n, "--add-label", "agent-wip")
            return True

        status = reconcile(sending=initial)
        edited = None
        if resumed_landed and status not in ("landed", "superseded", "paused"):
            return stop(f"Reconciliation: {status}.")
        if status in ("absent", "landed") and not claim():
            return "stop"
        if status == "absent":
            body_file = Path(self.ctx, f"issue-{n}-body-edit.md")
            body_file.write_text(binding["intended"])
            edited = pr_helpers.gh("issue", "edit", n, "-R", self.repo, "--body-file", str(body_file),
                                   reconcile=reconcile)
            if edited.returncode == 0:
                status = "landed"
            elif pr_helpers.transient(edited):
                status = "retry"
            else:
                return stop(f"permanent failure.{pr_helpers.why(edited)}")
        if status == "landed":
            if not resumed_landed:
                landed = {**owner_handoff.load_retry(self.env, n, reason), "phase": "landed"}
                owner_handoff.restore_retry(self.env, n, reason, n, landed)
            return self.finish_body_edit(binding)
        if status == "superseded":
            owner_handoff.clear_retry(self.env, n, reason)
            advisor.finish_episode(self.env, "issue", n, "superseded")
            self.gh("issue", "edit", n, "--remove-label", "agent-wip")
            self.finish()
            return "stop"
        if status == "paused":
            owner_handoff.restore_retry(self.env, n, reason, n, pending)
            raise Deferred("handoff-retry", reason, lends_slot=False)
        if status == "retry":
            attempts = pending["attempts"] + (0 if initial else 1)
            if attempts < owner_handoff.MAX_ATTEMPTS:
                now = int(owner_handoff.clock())
                owner_handoff.restore_retry(
                    self.env, n, reason, n,
                    {**pending, "attempts": attempts, "scheduled": now,
                     "due": now + owner_handoff.DELAYS[attempts]})
                raise Deferred("handoff-retry", reason, lends_slot=False)
        return stop(f"Reconciliation: {status}." + (pr_helpers.why(edited) if edited is not None else ""))

    def adr_open_binding(self, push_binding, applied):
        """Inputs known before opening the ADR docs PR (#151)."""
        return {
            "issue": push_binding["issue"], "reviser": push_binding["reviser"],
            "model": push_binding["model"], "round": push_binding["round"],
            "human": push_binding["human"], "reply": push_binding["reply"],
            "mode_note": push_binding["mode_note"], "max_rounds": push_binding["max_rounds"],
            "limit_why": push_binding["limit_why"], "author": push_binding["author"],
            "next": push_binding["next"], "feedback": push_binding["feedback"],
            "comment_ids": list(push_binding["comment_ids"]),
            "issue_body": push_binding["issue_body"],
            "commit": push_binding["commit"], "branch": push_binding["branch"],
            "base": "main", "wt": push_binding["wt"],
            "applied": applied if applied is not None else "",
        }

    def check_adr_open(self, binding):
        """Revalidate a saved ADR open before creating the PR or running its continuation."""
        data, transient = read_issue(self.env, self.issue, once=True)
        if data is None:
            return "retry" if transient else "failed"
        self.adr_read = data
        if data["state"] == "CLOSED":
            return "superseded"
        labels = {label["name"] for label in data["labels"]}
        if "ready-for-human" in labels:
            return "paused"
        if "ready-for-agent" in labels and "needs-triage" not in labels and any(
            (row["author"] or {}).get("login") == binding["author"]
            and (verdict := VERDICT.match(row["body"])) and verdict[1] == "accept"
            for row in data["comments"] if row["id"] not in set(binding["comment_ids"])
        ):
            return "superseded"
        if (self.body_key(data["body"]) != binding["issue_body"]
                or self.owner_feedback_digest(data["comments"]) != binding["feedback"]):
            return "changed"
        kind, round = state_from(data["comments"], self.env.owner)
        if kind in ("review", "reply", "reply-human") and round > binding["round"]:
            return "changed"
        found, read_transient = self.discover_adr(request=pr_helpers.call, extra=("-R", self.repo))
        if found is None:
            return "retry" if read_transient else "invalid"
        if not found:
            return "absent"
        if found.get("headRefOid") != binding["commit"]:
            return "changed"
        self.pr = str(found["number"])
        return "landed"

    def finish_adr_open(self, binding):
        """Continuation after a landed ADR PR create: post the saved reply (#151)."""
        n = self.issue
        try:
            self.record(binding["reviser"], binding["model"])
        except OSError:
            return "stop"
        data = self.adr_read
        if data is None:
            data, _ = read_issue(self.env, n, once=True)
            if data is None:
                self.handoff("Could not read the issue after the ADR PR opened.",
                             "spec_comments_read_failed")
                return "stop"
        known = set(binding["comment_ids"])
        applied = binding["applied"] or None
        inputs = {"body": applied if applied is not None else data["body"],
                  "comments": [row for row in data["comments"] if row["id"] in known]}

        def after_save():
            # Only once the reply operation is saved: a crash before this still resumes from the landed open.
            owner_handoff.clear_retry(self.env, n, owner_handoff.SPEC_ADR_OPEN)

        return self.finish_revision_reply(
            binding["reviser"], binding["model"], binding["round"], binding["human"],
            binding["reply"], binding["mode_note"], binding["max_rounds"], inputs, applied,
            after_save=after_save)

    def open_adr(self, binding, *, initial, pending=None):
        """Save before opening the ADR docs PR, or consume a due attempt (#151)."""
        n, reason = self.issue, owner_handoff.SPEC_ADR_OPEN
        text = f"Opening the ADR PR for {binding['branch'] if binding else '?'} failed. {self.url}"

        def stop(message):
            self.handoff(f"{text} {message}", reason)
            return "stop"

        resumed_landed, saved_pr = False, ""
        try:
            found = owner_handoff.load_retry(self.env, n, reason)
        except owner_handoff.RetryUnreadable as exc:
            return stop(str(exc))
        if initial:
            if found is not None:
                raise Deferred("handoff-retry", reason, lends_slot=False)
            pending = owner_handoff.prepare_retry(self.env, n, reason, n, binding)
            if pending is None:
                return stop("The worker inputs could not be saved.")
            # The open operation owns the continuation; a crash before create still resumes here.
            owner_handoff.clear_retry(self.env, n, owner_handoff.SPEC_ADR_PUSH)
            self.push_flag().unlink(missing_ok=True)
        else:
            pending = found if pending is None else pending
            if pending is None:
                return stop("The saved ADR open is missing.")
            binding = pending["continuation"]
            if pending.get("phase") == "landed":
                resumed_landed = True
                saved_pr = pending.get("pr", "")
            else:
                result = self.handoff(text, reason, resume=True)
                if result.action == owner_handoff.DEFER:
                    raise Deferred("handoff-retry", reason, lends_slot=False)
                if result.action != owner_handoff.RETRY:
                    return "stop"
                binding = result.continuation
                # Keep the pre-resume attempts: resume already consumed one on disk (#151).

        def reconcile(*, sending=False):
            return self.check_adr_open(binding)

        def claim():
            # A resumed attempt claims the issue only once it is known not to be paused or superseded.
            if initial:
                return True
            if not self.worktree():
                stop("spec worktree missing.")
                return False
            self.gh("issue", "edit", n, "--add-label", "agent-wip")
            self.base = f"origin/{binding['branch']}"
            return True

        status = reconcile(sending=initial)
        if status == "invalid":
            self.handoff(f"Could not identify the ADR pull request for {binding['branch']}.",
                         "spec_adr_state_invalid")
            owner_handoff.clear_retry(self.env, n, reason)
            return "stop"
        # Discovery supplies the canonical positive PR number. An exact match also rejects missing or malformed state.
        if resumed_landed and status == "landed" and saved_pr != self.pr:
            return stop("The saved ADR PR number does not match the open ADR PR.")
        created = None
        if resumed_landed and status not in ("landed", "superseded", "paused"):
            return stop(f"Reconciliation: {status}.")
        if status in ("absent", "landed") and not claim():
            return "stop"
        if status == "absent":
            created = pr_helpers.gh(
                "pr", "create", "--head", binding["branch"], "--base", binding["base"],
                "-R", self.repo, "--title", f"docs(adr): spec revisions for #{n}", "--body",
                f"ADR revisions from the spec review of #{n} ({binding['reviser']} revises). The runner merges this "
                "once a spec reviewer accepts the spec; the issue stays open for implementation.\n\n"
                f"## {check_section.HEADING}\nnone",
                reconcile=reconcile)
            if created.returncode == 0:
                self.pr = created.stdout.strip().rsplit("/", 1)[-1] or self.pr
                status = "landed"
            elif pr_helpers.transient(created):
                status = "retry"
            else:
                return stop(f"permanent failure.{pr_helpers.why(created)}")
        if status == "landed":
            if not resumed_landed:
                landed = {**owner_handoff.load_retry(self.env, n, reason), "phase": "landed", "pr": self.pr}
                owner_handoff.restore_retry(self.env, n, reason, n, landed)
            return self.finish_adr_open(binding)
        if status == "superseded":
            owner_handoff.clear_retry(self.env, n, reason)
            advisor.finish_episode(self.env, "issue", n, "superseded")
            self.gh("issue", "edit", n, "--remove-label", "agent-wip")
            self.finish()
            return "stop"
        if status == "paused":
            owner_handoff.restore_retry(self.env, n, reason, n, pending)
            raise Deferred("handoff-retry", reason, lends_slot=False)
        if status == "retry":
            attempts = pending["attempts"] + (0 if initial else 1)
            if attempts < owner_handoff.MAX_ATTEMPTS:
                now = int(owner_handoff.clock())
                owner_handoff.restore_retry(
                    self.env, n, reason, n,
                    {**pending, "attempts": attempts, "scheduled": now,
                     "due": now + owner_handoff.DELAYS[attempts]})
                raise Deferred("handoff-retry", reason, lends_slot=False)
        return stop(f"Reconciliation: {status}." + (pr_helpers.why(created) if created is not None else ""))

    def finish_adr_push(self, binding):
        """Continuation after a landed ADR push: open the docs PR when needed, then the reply (#151)."""
        n, wt = self.issue, binding["wt"]
        session.run_process(
            ["git", "-C", wt, "fetch", "-q", "origin",
             f"+refs/heads/{binding['branch']}:refs/remotes/origin/{binding['branch']}"],
            capture_output=True, text=True)
        self.base = f"origin/{binding['branch']}"
        self.pr = binding["pr"] or self.pr
        if self.adr_round is not None:
            inputs, applied = self.adr_round   # first path: the round's own inputs, no new read
        else:
            data = getattr(self, "adr_read", None)
            if data is None:
                data, _ = read_issue(self.env, n, once=True)
            if data is None:
                self.handoff("Could not read the issue after the ADR push landed.",
                             "spec_comments_read_failed")
                return "stop"
            self.adr_read = data
            known = set(binding["comment_ids"])
            inputs = {"body": data["body"], "comments": [r for r in data["comments"] if r["id"] in known]}
            applied = None
        if not self.pr:
            open_binding = self.adr_open_binding(binding, applied)
            return self.open_adr(open_binding, initial=True)
        try:
            self.record(binding["reviser"], binding["model"])
        except OSError:
            owner_handoff.clear_retry(self.env, n, owner_handoff.SPEC_ADR_PUSH)
            self.push_flag().unlink(missing_ok=True)
            return "stop"

        def after_save():
            # Only once the reply operation is saved: a crash before this still resumes from the landed record.
            owner_handoff.clear_retry(self.env, n, owner_handoff.SPEC_ADR_PUSH)
            self.push_flag().unlink(missing_ok=True)

        return self.finish_revision_reply(
            binding["reviser"], binding["model"], binding["round"], binding["human"],
            binding["reply"], binding["mode_note"], binding["max_rounds"], inputs, applied,
            after_save=after_save)

    def push_adr(self, binding, *, initial, pending=None, after_save=None):
        """Save before pushing ADR commits, or consume a due attempt before reconciling (#151)."""
        n, reason = self.issue, owner_handoff.SPEC_ADR_PUSH
        title = f"#{n}: pushing ADR branch failed"
        text = f"Pushing {self.branch} failed."

        def stop(message):
            self.push_flag().unlink(missing_ok=True)
            self.handoff(f"{text} {message}", reason)
            return "stop"

        try:
            found = owner_handoff.load_retry(self.env, n, reason)
        except owner_handoff.RetryUnreadable as exc:
            return stop(str(exc))
        if initial:
            if found is not None:
                raise Deferred("handoff-retry", reason, lends_slot=False)
            if self.env.repo.signoff:
                from .common import sign_off
                if not sign_off(self.wt):
                    return stop("sign-off failed.")
            branch = owner_handoff.push_destination(self.wt, self.branch) or self.branch
            commit = git(self.wt, "rev-parse", "HEAD")
            if not branch or not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
                return stop("local commit or branch missing.")
            remote, unreadable_transient = owner_handoff.observe_remote(self.wt, branch)
            if remote is None and not unreadable_transient:
                return stop("could not observe the remote ref.")
            binding = {
                **binding, "commit": commit, "branch": branch, "ref": branch,
                "remote": remote, "upstream": True, "wt": str(self.wt),
            }
            self.push_flag().write_text(n)
            pending = owner_handoff.prepare_retry(self.env, n, reason, n, binding)
            if pending is None:
                return stop("the push inputs could not be saved.")
            if after_save:
                after_save()
        else:
            if found is None:
                if self.push_flag().exists():
                    return stop("The saved push operation is missing; not running the reviser again.")
                return stop("The saved push operation is missing.")
            pending = found
            binding = pending["continuation"]
            if not pending.get("phase") and int(owner_handoff.clock()) < pending["due"]:
                raise Deferred("handoff-retry", reason, lends_slot=False)
            disposition = self.check_adr_push(binding)
            if disposition == "superseded":
                advisor.finish_episode(self.env, "issue", n, "superseded")
                owner_handoff.clear_retry(self.env, n, reason)
                self.push_flag().unlink(missing_ok=True)
                self.gh("issue", "edit", n, "--remove-label", "agent-wip")
                self.finish()
                return "stop"
            if disposition == "paused":
                raise Deferred("handoff-retry", reason, lends_slot=False)
            if disposition == "changed":
                return stop("owner feedback or the issue body changed.")
            if pending.get("phase"):
                if disposition == "ok" and owner_handoff.check_push_landed(binding) == "landed":
                    return self.finish_adr_push(binding)
                return stop("could not confirm the landed push.")
            if pending["attempts"] < owner_handoff.MAX_ATTEMPTS:
                result = self.handoff(text, reason, resume=True)
                if result.action == owner_handoff.DEFER:
                    raise Deferred("handoff-retry", reason, lends_slot=False)
                if result.action != owner_handoff.RETRY:
                    self.push_flag().unlink(missing_ok=True)
                    return "stop"
                binding = result.continuation
            if disposition != "ok":
                if pending["attempts"] + 1 < owner_handoff.MAX_ATTEMPTS:
                    raise Deferred("handoff-retry", reason, lends_slot=False)
                return stop("could not verify the owner inputs.")
            if binding["remote"] is None:
                if not owner_handoff.check_push_local(binding):
                    return stop("local inputs changed.")
                remote, unreadable_transient = owner_handoff.observe_remote(binding["wt"], binding["branch"])
                if remote is not None:
                    consumed = owner_handoff.load_retry(self.env, n, reason)
                    binding = owner_handoff.save_push_baseline(
                        self.env, n, n, consumed, remote, reason=reason)
                    pending = {**pending, "continuation": binding}
                elif not unreadable_transient:
                    return stop("could not observe the remote ref.")

        binding = pending["continuation"] if initial else binding
        if not initial:
            if git(self.repo_dir, "rev-parse", "-q", "--verify", f"origin/{binding['branch']}") is not None:
                self.base = f"origin/{binding['branch']}"
            if not self.worktree():
                return stop("spec worktree missing.")
            self.gh("issue", "edit", n, "--add-label", "agent-wip")
        pushed = None
        if binding["remote"] is None:
            status = "retry"
        else:
            status = owner_handoff.check_push_landed(binding)
        if status == "absent" and (initial or pending["attempts"] < owner_handoff.MAX_ATTEMPTS):
            pushed = push(binding["wt"], f"{binding['commit']}:refs/heads/{binding['branch']}",
                          upstream=binding["upstream"], signoff=False)
            if pushed:
                status = "landed"
            else:
                status = owner_handoff.check_push_landed(binding)
                if status != "landed" and not pushed.transient:
                    detail = pushed.detail or "permanent push failure"
                    return stop(f"{detail}.")
                if status == "absent":
                    status = "retry"
        if status == "landed":
            landed = {**owner_handoff.load_retry(self.env, n, reason), "phase": "landed"}
            owner_handoff.restore_retry(self.env, n, reason, n, landed)
            return self.finish_adr_push(binding)
        if status in ("changed", "failed"):
            return stop("inputs changed or the remote is unreadable.")
        if status == "retry":
            attempts = pending["attempts"] + (0 if initial else 1)
            if attempts < owner_handoff.MAX_ATTEMPTS:
                now = int(owner_handoff.clock())
                owner_handoff.restore_retry(
                    self.env, n, reason, n,
                    {**pending, "attempts": attempts, "scheduled": now,
                     "due": now + owner_handoff.DELAYS[attempts]})
                raise Deferred("handoff-retry", reason, lends_slot=False)
        detail = getattr(pushed, "detail", "") or status
        return stop(f"{detail}.")

    # --- the reviser's round ---
    def revise_round(self, round, context, inputs, max_rounds=None, *, answer_intent=None):
        """"revised", "answered" (advisor posted grounded answers) or "stop"."""
        n, wt, env = self.issue, self.wt, self.env
        listed, issue_body = inputs["comments"], inputs["body"]
        if max_rounds is None:
            max_rounds = max(int(env.config.scheduling.max_spec_rounds or 3), round + 1)
        out_base = f"/tmp/baton-spec-{n}-{round}"
        for stale in Path("/tmp").glob(f"baton-spec-{n}-{round}.*"):
            stale.unlink()
        last_vendor = last_by(listed, env.owner, "review")
        other_model = last_model(env, n, "review", round, last_vendor)
        claude_followup = self.reviser_followup(round, "claude", last_vendor)
        pending = self.resume_worker("reviser", "spec_revision_run_failed", round, other_model, claude_followup)
        if pending is False:
            return "stop"
        reviser = pending[1] if pending else self.pick(
            "reviser", list(env.config.quota.vendor_pool), last_vendor, other_model, spec_followup=claude_followup)
        self.mark_base(round)
        budget = pending[0]["remaining_budget"] if pending else {}
        saved_answer = answer_intent or {}
        denied_spent = bool(budget.get("denied_spent")) or saved_answer.get("denied_spent") is True
        denied_command = budget.get("denied_command") or str(saved_answer.get("denied_command") or "")
        if denied_spent and not self.keep_denied_spent(answer_intent, denied_command):
            return "stop"
        attempt = int(bool(pending))
        restored = denied_spent  # a saved, already-spent denied retry: the first dispatch repeats its note
        human = ""
        while True:
            template = self.self_dir / ("codex-spec-prompt.md" if reviser == "codex" else "spec-prompt.md")
            prompt = self.render(
                template, ISSUE=n, GH_REPO=self.repo, WORKTREE=str(wt), BRANCH=self.branch, ROUND=str(round),
                CONTEXT_FILE=str(context), OUT=out_base)
            followup = reviser == "claude" and claude_followup
            for suffix in (".body", ".comment"):
                Path(f"{out_base}{suffix}").unlink(missing_ok=True)
            tag = f"issue-{n}-spec-reply-{round}" + (f"-retry{attempt}" if attempt else "")
            if restored and denied_command:
                # A resumed worker keeps the denied-command instruction of the rerun it replaces.
                prompt += DENIED_NOTE.format(command=denied_command)
                tag += "-rerun"
                restored = False
            out = self.agent(reviser, "spec", prompt, tag, "spec-settings.json", out_base, followup=followup,
                             round_s=str(round))
            denied = permission_denials(out.get("log", "")) if out["outcome"].startswith("RESULT: failed") else []
            if denied and not denied_spent:
                if not self.keep_denied_spent(answer_intent, denied[0]):
                    return "stop"
                denied_spent, denied_command = True, denied[0]
                notify.log(f"#{n}: spec revision round {round} failed after a denied command ({denied[0]}); rerunning once")
                for suffix in (".body", ".comment"):
                    Path(f"{out_base}{suffix}").unlink(missing_ok=True)
                out = self.agent(reviser, "spec", prompt + DENIED_NOTE.format(command=denied[0]), f"{tag}-rerun",
                                 "spec-settings.json", out_base, followup=followup, round_s=str(round))
                denied = permission_denials(out.get("log", "")) if out["outcome"].startswith("RESULT: failed") else []
            self.settle_answer_intent()
            outcome, model = out["outcome"], out["model"] or ""
            if (self.unverified(reviser, last_vendor, model, other_model)
                    or (reviser == last_vendor == "claude" and not claude_cli.distinct_models(model, other_model))):
                self.handoff(f"{reviser.capitalize()} spec reviser model was not verified as different from the reviewer model.",
                             "spec_reviser_model_unverified")
                return "stop"
            mode_note = self.mode_note("Revision", reviser, last_vendor, model, other_model)
            comment, body = Path(f"{out_base}.comment"), Path(f"{out_base}.body")
            if git(wt, "status", "--porcelain"):
                self.handoff(f"Uncommitted changes left in {wt}; the runner will not discard them.",
                             "spec_revision_run_failed")
                return "stop"
            if re.match(r"SPEC: (revised|human)", outcome) and not (comment.is_file() and comment.stat().st_size):
                outcome = f"RESULT: failed no reply in {comment}"
            if outcome.startswith("SPEC: revised"):
                human = ""
                break
            if outcome.startswith("SPEC: human"):
                human = " human"
                break
            why = f" A command was denied ({denied[0]}): check the spec role's permissions." if denied else ""
            retry = self.recover_worker(
                f"{reviser} spec run failed: {outcome}{why}", "spec_revision_run_failed", reviser, outcome,
                role="reviser", round_s=str(round), issue_body=issue_body, listed=listed,
                last_vendor=last_vendor, other_model=other_model, spec_followup=claude_followup,
                budget={"denied_spent": denied_spent, "denied_command": denied_command})
            if not retry:
                return "stop"
            _, reviser = retry
            attempt += 1
        text = strip_markers(comment.read_text()).encode()[:REPLY_CAP].decode(errors="ignore").rstrip("\n")
        applied = None
        if body.is_file() and body.stat().st_size:
            body.write_text(check_section.canonicalize(strip_markers(body.read_text())).rstrip("\n") + "\n")
            applied = body.read_text()
            binding = self.body_edit_binding(
                applied, inputs["body"], round, reviser, model, human, text, mode_note, max_rounds, inputs)
            try:
                return self.edit_body(binding, initial=True)
            finally:
                for leftover in Path("/tmp").glob(f"baton-spec-{n}-{round}.*"):
                    leftover.unlink()
        if git(wt, "rev-list", f"{self.base}..HEAD"):
            try:
                self.record(reviser, model)
            except OSError:
                return "stop"
            binding = self.adr_push_binding(
                reviser, model, round, human, text, mode_note, max_rounds, inputs, applied)
            self.adr_round = (inputs, applied)
            try:
                return self.push_adr(binding, initial=True)
            finally:
                for leftover in Path("/tmp").glob(f"baton-spec-{n}-{round}.*"):
                    leftover.unlink()
        try:
            self.record(reviser, model)
        except OSError:
            return "stop"
        return self.finish_revision_reply(reviser, model, round, human, text, mode_note, max_rounds, inputs, applied)

    # --- the loop ---
    def waiting_adr_push(self):
        """A saved ADR push for this issue, or None; hands off when the record is damaged or missing."""
        reason = owner_handoff.SPEC_ADR_PUSH
        try:
            found = owner_handoff.load_retry(self.env, self.issue, reason)
        except owner_handoff.RetryUnreadable as exc:
            self.handoff(f"Could not read the saved {reason} operation: {exc}", reason)
            return "unreadable"
        if found is None and self.push_flag().exists():
            self.handoff("The saved push operation is missing; not running the reviser again.", reason)
            return "unreadable"
        return found

    def waiting_comment_post(self):
        """The first saved spec review/reply or accept post for this issue, or None."""
        for reason in (*owner_handoff.SPEC_POSTS, owner_handoff.SPEC_ACCEPT_POST,
                       owner_handoff.SPEC_PENDING_ACCEPT_POST):
            try:
                pending = owner_handoff.load_retry(self.env, self.issue, reason)
            except owner_handoff.RetryUnreadable as exc:
                self.handoff(f"Could not read the saved {reason} operation: {exc}", reason)
                return "unreadable"
            if pending is not None:
                return reason, pending
        return None

    def waiting_body_write(self):
        """The first saved body edit or ADR open for this issue, or None."""
        for reason in owner_handoff.SPEC_BODY_WRITES:
            try:
                pending = owner_handoff.load_retry(self.env, self.issue, reason)
            except owner_handoff.RetryUnreadable as exc:
                self.handoff(f"Could not read the saved {reason} operation: {exc}", reason)
                return "unreadable"
            if pending is not None:
                return reason, pending
        return None

    def loop(self, issue_payload=None):
        """True once the spec reviewer accepted (verdict comment posted, any ADR PR merged),
        False when the owner or a failure took over. Deferred when a vendor cannot run now.

        ISSUE_PAYLOAD is an already-fetched {title, body, comments} from a recovered read (#151).
        """
        n, env = self.issue, self.env
        try:
            answer_intent = self.load_answer_intent()
        except (OSError, ValueError):
            self.handoff("Could not read the saved advisor answer intent.", "spec_questions")
            self.clear_answer_intent()
            owner_handoff.clear_retry(self.env, n, owner_handoff.SPEC_REPLY_POST)
            return False
        if answer_intent is not None:
            outcome = self.complete_answer_post(answer_intent)
            if outcome != "answered":
                return False
        resume_post = self.waiting_comment_post() if answer_intent is None else None
        if resume_post == "unreadable":
            return False
        resume_write = None if resume_post else self.waiting_body_write()
        if resume_write == "unreadable":
            return False
        resume_push = None
        if resume_write and resume_write[0] == owner_handoff.SPEC_BODY_EDIT:
            # A push or open saved after the body edit landed owns the continuation.
            try:
                later_open = owner_handoff.load_retry(env, n, owner_handoff.SPEC_ADR_OPEN)
                later_push = owner_handoff.load_retry(env, n, owner_handoff.SPEC_ADR_PUSH)
            except owner_handoff.RetryUnreadable as exc:
                self.handoff(f"Could not read the saved ADR operation: {exc}", owner_handoff.SPEC_ADR_PUSH)
                return False
            if later_open is not None:
                resume_write = (owner_handoff.SPEC_ADR_OPEN, later_open)
            elif later_push is not None:
                resume_write, resume_push = None, later_push
            if resume_write is None or resume_write[0] != owner_handoff.SPEC_BODY_EDIT:
                owner_handoff.clear_retry(env, n, owner_handoff.SPEC_BODY_EDIT)
        if resume_post and resume_post[0] == owner_handoff.SPEC_REPLY_POST:
            # The saved reply owns the continuation even if the process stopped during push cleanup.
            owner_handoff.clear_retry(env, n, owner_handoff.SPEC_ADR_PUSH)
            owner_handoff.clear_retry(env, n, owner_handoff.SPEC_ADR_OPEN)
            owner_handoff.clear_retry(env, n, owner_handoff.SPEC_BODY_EDIT)
            self.push_flag().unlink(missing_ok=True)
        elif resume_write and resume_write[0] == owner_handoff.SPEC_ADR_OPEN:
            owner_handoff.clear_retry(env, n, owner_handoff.SPEC_ADR_PUSH)
            self.push_flag().unlink(missing_ok=True)
        elif not resume_post and not resume_write and resume_push is None:
            resume_push = self.waiting_adr_push()
            if resume_push == "unreadable":
                return False
        reason = "spec_pending_adr_read_failed"
        try:
            waiting = None if (resume_post or resume_write or answer_intent or resume_push is not None) else owner_handoff.load_retry(env, n, reason)
        except owner_handoff.RetryUnreadable:
            self.handoff("Could not read the saved ADR operation.", reason)
            return False
        adr = None
        if waiting:
            binding = waiting["continuation"]
            if binding["step"] == "acceptance":
                return self.resume_pending_accept(binding, issue_payload, recovering=True)
            adr = self.adr_discovery()
            if adr is None:
                return False
        if (issue_payload is None and not resume_post and not resume_write and answer_intent is None
                and resume_push is None):
            issue_payload = self.issue_payload()
            if issue_payload is None:
                return False
        if resume_write:
            write_reason, pending = resume_write
            binding = pending["continuation"]
            if write_reason == owner_handoff.SPEC_BODY_EDIT:
                outcome = self.edit_body(None, initial=False, pending=pending)
            else:
                outcome = self.open_adr(None, initial=False, pending=pending)
            if outcome == "answered":
                answer_intent = self.answer_intent
                step, round = "revise", binding["round"]
            elif outcome == "revised":
                step, round = "review", binding["round"] + 1
            else:
                return outcome == "accept"
            max_rounds = binding["max_rounds"]
            if answer_intent is None:
                issue_payload, self.fresh_payload = self.fresh_payload or self.issue_payload(), None
                if issue_payload is None:
                    return False
            if git(self.repo_dir, "rev-parse", "-q", "--verify", f"origin/{self.branch}") is not None:
                self.base = f"origin/{self.branch}"
            if not self.worktree():
                notify.log(f"spec worktree for #{n} failed")
                return False
        elif resume_push is not None:
            binding = resume_push["continuation"]
            outcome = self.push_adr(None, initial=False, pending=resume_push)
            if outcome == "answered":
                answer_intent = self.answer_intent
                step, round = "revise", binding["round"]
            elif outcome == "revised":
                step, round = "review", binding["round"] + 1
            else:
                return outcome == "accept"
            max_rounds = binding["max_rounds"]
            if answer_intent is None:
                issue_payload, self.fresh_payload = self.fresh_payload or self.issue_payload(), None
                if issue_payload is None:
                    return False
            if git(self.repo_dir, "rev-parse", "-q", "--verify", f"origin/{self.branch}") is not None:
                self.base = f"origin/{self.branch}"
            if not self.worktree():
                notify.log(f"spec worktree for #{n} failed")
                return False
        elif resume_post:
            post_reason, pending = resume_post
            binding = pending["continuation"]

            def claim():
                if git(self.repo_dir, "rev-parse", "-q", "--verify", f"origin/{self.branch}") is not None:
                    self.base = f"origin/{self.branch}"
                if not self.worktree():
                    notify.log(f"spec worktree for #{n} failed")
                    return False
                self.gh("issue", "edit", n, "--add-label", "agent-wip")
                return True

            if post_reason in owner_handoff.SPEC_ACCEPT_POSTS:
                return self.post_accept(post_reason, None, initial=False) == "accept"
            outcome = self.post_comment(post_reason, None, initial=False, claim=claim)
            if outcome == "answered":
                answer_intent = self.answer_intent
            if outcome in ("changes", "answered"):
                step, round = "revise", binding["round"]
            elif outcome == "revised":
                step, round = "review", binding["round"] + 1
            else:
                return outcome == "accept"
            # The loop's own limit, restored unchanged: a deferral must not raise it (#151).
            max_rounds = binding["max_rounds"]
            if answer_intent is None:
                issue_payload, self.fresh_payload = self.fresh_payload or self.issue_payload(), None
                if issue_payload is None:
                    return False
        else:
            if answer_intent is not None:
                step, round, max_rounds = "revise", answer_intent["round"], answer_intent["max_rounds"]
            else:
                if issue_payload is None or issue_payload["state"] == "CLOSED":
                    return False
                listed = issue_payload["comments"]
                pending = accept_pending(env, n, listed=listed)
                if pending:
                    self.gh("issue", "edit", n, "--remove-label", "ready-for-human", "--add-label", "agent-wip")
                    binding = {"step": "acceptance", "pr": pending["pr"], "verdict": pending["verdict"],
                               "issue_body": issue_payload["body"], "adr_head": pending["head"]}
                    return self.resume_pending_accept(binding, issue_payload)
                kind, round = state_from(listed, env.owner)
                if pending_role(kind) == "reviser":
                    step = "revise"   # the reviewer asked for changes, or the owner answered
                elif kind == "reply":
                    step, round = "review", round + 1   # a reviser revised; the spec is reviewed again
                else:
                    step, round = "review", 1
                    self.keep_owner_body(issue_payload["body"])
                # An owner answer earns another round.
                max_rounds = max(int(env.config.scheduling.max_spec_rounds or 3), round + 1)
            if git(self.repo_dir, "rev-parse", "-q", "--verify", f"origin/{self.branch}") is not None:
                self.base = f"origin/{self.branch}"
            recovering_worker = recovery.path(env, n, n).exists()
            if recovering_worker and not (self.wt.is_dir() and self.path("spec-paused").is_file()):
                self.handoff("The saved recovery worktree is missing.", "spec_review_run_failed")
                return False
            if not self.worktree():
                notify.log(f"spec worktree for #{n} failed")
                return False
            if not recovering_worker and answer_intent is None:
                self.gh("issue", "edit", n, "--remove-label", "ready-for-human", "--add-label", "agent-wip")
        # Sent by started() once a vendor is picked: a deferred tick (no vendor can run now) repeats every
        # timer interval, and announcing each one floods the owner with notices of work that never ran.
        self.announce = f"#{n}: spec review (round {round}, {'revision' if step == 'revise' else step})"
        while answer_intent is not None or (issue_payload is not None and issue_payload["state"] == "OPEN"):
            self.hooks.round = round
            self.path("spec-paused").write_text(f"{n}\n")
            if answer_intent is not None:
                issue_payload = self.answer_revision_payload(answer_intent)
                if issue_payload is None:
                    return False
                self.pr, self.adr_head = answer_intent["pr"], answer_intent["adr_head"]
                max_rounds, self.extra = answer_intent["max_rounds"], 0
            else:
                if issue_payload is None:
                    return False
                if adr is None:
                    adr = self.adr_discovery()
                    if adr is None:
                        return False
                self.pr = str(adr["number"]) if adr else ""
                self.adr_head = adr["headRefOid"] if adr else ""
            adr = None
            context = Path(self.ctx, f"issue-{n}-spec-{round}-{step}-{time.strftime('%Y%m%d-%H%M%S')}.md")
            context.write_text(review.issue_context(env, n, data=issue_payload))
            if step == "review":
                result = self.review_round(round, max_rounds, context, issue_payload)
                if result == "changes":
                    step = "revise"
                else:
                    return result == "accept"
            else:
                revised = self.revise_round(round, context, issue_payload, max_rounds, answer_intent=answer_intent)
                if revised == "answered":
                    answer_intent = self.answer_intent
                    continue
                if revised != "revised":
                    return False
                answer_intent = None
                step, round = "review", round + 1
            issue_payload = self.issue_payload()
            if issue_payload is None:
                return False
        return False


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
