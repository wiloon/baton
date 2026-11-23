"""The tick and the entry point (migration slice 4e, #284): the control flow of python -m baton.

The Python bootstrap loads typed TOML and prepares one Session for scheduled/manual
runs. A chained pass re-enters the same bootstrap with the selected configuration.

A tick takes the configured repositories in turn (#327): it starts with the one after the
repository whose item ran last (state `last-repo`), and still does one item in all. In each
repository it does one of, in this order (the order is a contract):
  - reconcile a PR whose creation was interrupted (pr-opening-N.json)
  - a review the last run deferred (review-pending-N)
  - a PR the owner labelled agent-review
  - an agent PR labelled agent-fix (owner feedback)
  - a ready-for-agent (or claude-found + needs-triage) issue: spec review, dev, review loop
Before any of that: Cursor's verdicts on claude-found issues, the cleanup of finished worktrees,
and the follow-ups of PRs the owner merged. When no repository has any of that, at most every
SCAN_INTERVAL_HOURS Claude scans one area of the first one in turn that has scan areas. By hand
(--repo OWNER/NAME before the command when several repositories are configured):
    python -m baton [N]            an issue (the same pipeline the timer runs)
    python -m baton pr N           an agent PR with owner feedback, as with agent-fix
    python -m baton review N       a PR to review, as with agent-review
    python -m baton scan [N|list]  scan area N (default: the next one) and keep the draft
    python -m baton file-scan DRAFT  file a kept draft as claude-found + needs-triage

Exit codes: 0 ok, 1 error, 2 usage, 75 deferred (a vendor must rest), 80 inside the tick only:
the item ended and the next pass may start (chain_next).
"""

from . import session
import fcntl
import glob
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from . import advisor, after_merge, checkpoint, dev, fix, handoff as owner_handoff, hooks as telemetry, maintenance, merge, names, notify, pick, pr as pr_helpers, pr_contract, review, scan, spec, tmp_sweep, vendors
from .common import Deferred, git

AGENT_REVIEW_NOTE = ("The PR description{issue} give the problem, its cause and the fix. Check the diagnosis first: does the "
                     "stated cause hold, and does the change fix it rather than a symptom? Then review the code.")
CURSOR_REVIEW_NOTE = ("Review this Cursor-authored PR, including its current description. Previous metadata fixes may have "
                      "changed the description without a new commit.")
VERDICT_CURSOR = re.compile(r"<!-- baton-verdict: (accept|revise|reject|human) -->")
VERDICT_BY_CURSOR = re.compile(r"<!-- baton-verdict: (accept|revise|reject|human) by=cursor -->")
VERDICT_ANY = re.compile(r"<!-- baton-verdict: (?P<v>[a-z]+)( by=(cursor|codex|claude))? -->")


class Stop(Exception):
    """Leave the current item (or the tick) with this exit code."""
    def __init__(self, rc):
        super().__init__(rc)
        self.rc = rc



def parse_args(argv):
    """(force_issue, force_pr, force_review, scan_cmd, scan_arg) or raise Stop(1) / Stop(2)."""
    first = argv[0] if argv else ""
    second = argv[1] if len(argv) > 1 else ""
    if first == "pr" or first == "review":
        if not second:
            print(f"python -m baton {first} <number>", file=sys.stderr)
            raise Stop(1)
        return ("", second, "", "", "") if first == "pr" else ("", "", second, "", "")
    if first in ("scan", "file-scan"):
        return "", "", "", first, second
    return first, "", "", "", ""


def open_blockers(item):
    """The open issues that block ITEM, from GitHub's issue dependencies (`blockedBy`, #70).
    A "Blocked by #N" line in a body is not a dependency. When GitHub lists fewer blockers than
    it counts, the unlisted ones are unknown and count as open ("?")."""
    blocked = item.get("blockedBy") or {}
    nodes = blocked.get("nodes") or []
    found = [n.get("number") for n in nodes if (n.get("state") or "").upper() != "CLOSED"]
    if (blocked.get("totalCount") or 0) > len(nodes):
        found.append("?")
    return found


class Tick:
    @session.scoped
    def __init__(self, env=None, argv=(), run_sh="", clock=time.time):
        env = session.resolve(env)
        self.env, self.run_sh, self.clock = env, run_sh, clock
        self.force_issue, self.force_pr, self.force_review, self.scan_cmd, self.scan_arg = parse_args(list(argv))
        self.manual = self.force_issue + self.force_pr + self.force_review + self.scan_cmd
        # The repository's own files; runner_state (the lock, cooldowns, chain-next, last-repo) is shared.
        self.state, self.logs = Path(str(env.dirs.state)), Path(str(env.dirs.logs))
        self.ctx, self.wt_root = Path(str(env.dirs.ctx)), Path(str(env.dirs.worktrees))
        self.runner_state = Path(str(env.config.paths.state))
        self.repo_dir, self.gh_repo = str(env.dirs.clone), env.repo.gh_repo
        self.owner = env.owner
        self.self_dir = Path(str(env.config.paths.self_dir))
        self.cursor_login = env.config.scheduling.cursor_login_re or r"^(app/cursor|cursor(\[bot\])?)$"
        self.scan_areas = list(env.repo.scan_areas)
        self.now = int(clock())
        self.lock_file = None
        self.exec = os.execv
        self.active = {}
        self.recovered_issues = {}
        self.fetch_failed = False

    # --- logging, GitHub and git --------------------------------------------------------
    def log(self, message):
        notify.log(message)

    def notify(self, title, text=""):
        notify.notify(title, text)

    def gh(self, *args):
        """stdout of `gh ARGS -R repo`'s sibling call, or None when gh fails."""
        result = pr_helpers.gh(*args)
        return result.stdout if result.returncode == 0 else None

    def gh_json(self, *args):
        out = self.gh(*args)
        try:
            return json.loads(out) if out is not None else None
        except ValueError:
            return None

    def hooks(self, issue="", pr="", round=""):
        return telemetry.from_env(self.env, issue=issue or self.force_issue or 0, pr=pr or 0, round=round)

    # --- one item: begin_item / finish_item of roles.sh -----------------------------------
    def begin_item(self):
        self.active = {"review": "", "spec": "", "issue": "", "cursor_fix": "", "pr": ""}
        if not self.manual:   # the next tick starts with the repository after this one
            (self.runner_state / "last-repo").write_text(f"{self.gh_repo}\n")
        fd, path = tempfile.mkstemp(prefix="baton-telemetry-item.", dir=self.env.tools.get("TMPDIR", "/tmp") or "/tmp")
        os.close(fd)
        self.env.runtime.telemetry_item = path
        self.hooks().start_attempt()

    def finish_item(self, rc):
        self.hooks().finish_attempt(rc)
        item = self.env.runtime.telemetry_item
        self.env.runtime.telemetry_item = ""
        if item:
            Path(item).unlink(missing_ok=True)
        a, state = self.active, self.state
        if rc != 75:
            pr = a.get("pr")
            if pr:   # a non-deferred attempt ends the PR's slot loan
                (state / f"slot-deferred-{pr}").unlink(missing_ok=True)
            push_pending = pr and pr in self.fix_push_episodes()
            if a["review"] and not push_pending:
                (state / f"review-pending-{a['review']}").unlink(missing_ok=True)
            if a["spec"]:
                (state / f"spec-paused-{a['spec']}").unlink(missing_ok=True)
            if a["cursor_fix"] and not push_pending:
                n = a["cursor_fix"]
                for path in (state / f"cursor-fix-base-{n}", state / f"pr-body-base-{n}",
                             state / f"cursor-fix-task-{n}", state / f"owner-fix-feedback-{n}",
                             self.ctx / f"pr-{n}-fix-checks.json"):
                    path.unlink(missing_ok=True)
                pr_helpers.remove_body_request(self.ctx / f"pr-{n}-body.md")
        elif a["issue"]:
            pr_helpers.gh("issue", "edit", a["issue"], "-R", self.gh_repo, "--remove-label", "agent-wip")

    def defer(self, vendor, reason):
        """defer_vendor: 75 means deferred, never an agent failure."""
        outcome = f"DEFERRED: {vendor} {reason}"
        self.log(outcome)
        if self.manual:
            print(outcome, file=sys.stderr)
        return 75

    def item(self, body):
        """Run BODY as one item and return its exit code. A deferral yields the item (75)."""
        self.begin_item()
        rc = 0
        try:
            rc = body() or 0
        except Stop as stop:
            rc = stop.rc
        except Deferred as deferred:
            rc = self.defer(deferred.vendor, deferred.reason)
            self.lend_slot(deferred)
        except Exception as exc:
            self.log(f"item failed: {type(exc).__name__}: {exc}")
            rc = 1
        finally:
            self.finish_item(rc)
        return rc

    def lend_slot(self, deferred):
        """An open PR of a counted agent-wip issue whose next step yields on vendor quota or
        cooldown lends its max_pending_prs slot (state file slot-deferred-<pr>). A lease-only
        deferral (the vendor's capacity is reserved for a moment) does not."""
        pr = self.active.get("pr")
        if pr and deferred.vendor == "handoff-retry" and deferred.reason == owner_handoff.FIX_PUSH:
            (self.state / f"slot-deferred-{pr}").unlink(missing_ok=True)
        if not pr or not deferred.lends_slot:
            return
        info = self.gh_json("pr", "view", str(pr), "-R", self.gh_repo, "--json", "state,headRefName,labels")
        if not isinstance(info, dict) or info.get("state") != "OPEN" \
                or any(label.get("name") == "ready-for-human" for label in info.get("labels") or []):
            return
        issue = names.issue_of(info.get("headRefName") or "")
        task = self.gh_json("issue", "view", issue, "-R", self.gh_repo, "--json", "state,labels") if issue else None
        if not isinstance(task, dict) or task.get("state") != "OPEN" \
                or not any(label.get("name") == "agent-wip" for label in task.get("labels") or []):
            return
        marker = self.state / f"slot-deferred-{pr}"
        new = not marker.exists()
        marker.write_text(json.dumps({"vendor": deferred.vendor, "reason": deferred.reason}))
        if new:
            self.log(f"PR #{pr} (issue #{issue}) lends its agent-wip slot while deferred: {deferred.vendor} {deferred.reason}")

    def slot_markers(self):
        """{PR number: "vendor reason"} of the PRs that lend their slots."""
        found = {}
        for path in self.state.glob("slot-deferred-*"):
            try:
                data = json.loads(path.read_text())
                found[path.name.removeprefix("slot-deferred-")] = f"{data['vendor']} {data['reason']}"
            except (OSError, ValueError, KeyError, TypeError):
                path.unlink(missing_ok=True)
        return found

    def after_item(self, rc):
        """What the tick does with an item's exit code: None to go on, else the exit code."""
        if rc == 75:
            return 75 if self.manual else None
        if rc == 80:
            self.chain_next()
            return 0
        return rc or None

    def chain_next(self):
        """A partial slice merged in this tick touches chain-next (finish_merged_task); the tick
        then starts over at once instead of leaving the issue idle until the next timer tick.
        CHAIN_MAX bounds the run."""
        flag = self.runner_state / "chain-next"
        if self.manual or not flag.exists():
            return
        flag.unlink(missing_ok=True)
        done, limit = int(self.env.runtime.chain or 0), int(self.env.config.scheduling.chain_max or 5)
        if done >= limit:
            self.log(f"chain: {limit} passes in a row, next slice waits for the timer")
            return
        self.log("chain: a slice merged, starting the next pass now")
        self.env.runtime.chain = done + 1
        if self.lock_file:
            self.lock_file.close()
        sys.stdout.flush()
        self.exec(sys.executable, [sys.executable, "-m", "baton", "--config", str(self.env.config.source), "--chain", str(done + 1)])

    # --- agent steps that touch the item's markers ---------------------------------------
    def review_loop(self, pr, wt, note, fixer="agent", reviewer="pool", fixed_by="", issue=""):
        """The review rounds, then the merge or handoff (baton.review)."""
        self.active["review"] = self.active["cursor_fix"] = self.active["pr"] = str(pr)
        loop = review.Review(self.env.with_runtime(issue=issue), self.hooks(issue, pr), pr, wt, issue, fixed_by)
        return loop.loop(note, fixer, reviewer)

    def fix_pr(self, pr, wt, fixer, source, feedback, issue="", head="", *, owner_triggered=False):
        self.active["cursor_fix"] = str(pr)
        runner = fix.Runner(self.env.with_runtime(issue=issue, pr=str(pr)), self.hooks(issue, pr), pr, "")
        runner.required_retry_label = "agent-fix"
        self.fix_runner = runner
        return fix.fix_pr(runner, pr, wt, fixer, source, feedback, head, owner_triggered=owner_triggered)

    def spec_loop(self, issue):
        self.active["spec"] = self.active["issue"] = str(issue)
        payload = (getattr(self, "recovered_issues", {}) or {}).pop(str(issue), None)
        return spec.Spec(self.env.with_runtime(issue=str(issue)), self.hooks(issue), str(issue)).loop(
            issue_payload=payload)

    def pick_vendor(self, role, mode, candidates, pr="", issue="", exclude="", current="agent"):
        return pick.choose(self.env, role, current, mode, candidates, pr=str(pr), issue=str(issue), exclude=exclude)

    def all_vendors(self):
        return pick.all_vendors(self.env)

    # --- the tick -----------------------------------------------------------------------
    def run(self):
        """This repository's tick alone (by hand, or with one repository configured)."""
        return run([self])

    def pass_(self):
        rc = self.work()
        if rc is None and not self.fetch_failed:
            rc = self.scan_fallback()
        if rc is None:
            self.log("no work" if not self.fetch_failed else "git fetch failed; nothing done")
        return 1 if rc is None and self.fetch_failed else rc or 0

    def work(self):
        """This repository's item, if it has one: its exit code, or None for nothing to do here."""
        if self.env.config.scheduling.cross_review is True:
            self.advance_cursor_verdicts()
        if session.run_process(["git", "-C", self.repo_dir, "fetch", "-q", "--prune", "origin"]).returncode != 0:
            self.log("git fetch failed")
            self.fetch_failed = True
            return None
        self.cleanup()
        if self.scan_cmd:
            return self.scan_command()
        self.file_merged_follow_ups()
        if (self.force_pr and not self.comment_post_pending(self.force_pr)
                and self.force_pr not in self.fix_push_episodes()
                and not self.is_agent_branch(self.force_pr)):
            print("python -m baton pr N takes an agent/issue-N PR; for the owner's own PR use python -m baton review N", file=sys.stderr)
            return 2
        for step in (self.pending_openings, self.pending_reviews, self.pick_review, self.pick_fix, self.pick_issue):
            done = step()
            if done is not None:
                return done
        return None

    # --- Cursor's verdicts on claude-found issues (no agent run) ---------------------------
    def advance_cursor_verdicts(self):
        """A verdict on a plan the scan filed comes from Cursor's GitHub identity as
        <!-- baton-verdict: X -->, or from the owner's account as
        <!-- baton-verdict: X by=cursor -->. The runner moves the labels."""
        issues = self.gh_json("issue", "list", "-R", self.gh_repo, "--state", "open", "--label", "claude-found",
                              "--label", "needs-triage", "--limit", "50", "--json", "number,author,comments") or []
        for issue in issues:
            if (issue.get("author") or {}).get("login") != self.owner:
                continue
            found = None
            for c in issue.get("comments") or []:
                login, text = (c.get("author") or {}).get("login") or "", c.get("body") or ""
                if text.startswith("<!-- baton-advisor-answer"):
                    continue   # advisor provenance must not drive Cursor triage (#125)
                if (re.search(self.cursor_login, login) and VERDICT_CURSOR.match(text)) \
                        or (login == self.owner and VERDICT_BY_CURSOR.match(text)):
                    found = text
            match = VERDICT_ANY.match(found) if found else None
            if not match:
                continue
            n, verdict, url = str(issue["number"]), match["v"], f"https://github.com/{self.gh_repo}/issues/{issue['number']}"
            if verdict in ("accept", "revise"):
                pr_helpers.gh("issue", "edit", n, "-R", self.gh_repo, "--remove-label", "needs-triage", "--add-label", "ready-for-agent")
                self.log(f"#{n}: Cursor verdict {verdict}; handed to Cursor")
                self.notify(f"#{n} handed to Cursor (Cursor: {verdict})", url)
            elif verdict in ("reject", "human"):
                pr_helpers.gh("issue", "edit", n, "-R", self.gh_repo, "--remove-label", "needs-triage", "--add-label", "ready-for-human")
                self.log(f"#{n}: Cursor verdict {verdict}")
                self.notify(f"#{n} needs you (Cursor: {verdict})", url)

    # --- clean up worktrees whose work is finished -----------------------------------------
    def sweep(self, number):
        env = {**self.env.tools, "BATON_TMP_PREFIXES": tmp_sweep.prefixes([self.env.repo])}
        session.run_process([str(self.self_dir / "tmp-sweep.sh"), str(number)], env=env)

    def state_of(self, kind, number):
        out = self.gh(kind, "view", str(number), "-R", self.gh_repo, "--json", "state", "--jq", ".state")
        return (out or "").strip()

    def cleanup(self):
        waiting_posts = {issue for issue, reason in self.pending_specs().items()
                         if reason in owner_handoff.SPEC_WRITE_POSTS}
        waiting_pushes = self.fix_push_episodes()
        waiting_issues = set().union(*waiting_pushes.values())
        for n, path in names.worktrees(self.wt_root):
            if n in waiting_posts or n in waiting_issues:
                continue
            wt = str(path)
            if self.state_of("issue", n) == "CLOSED":
                advisor.finish_episode(self.env, "issue", n, "closed")
                merge.remove_worktree(self.repo_dir, wt)
                Path(f"{wt}.pr.md").unlink(missing_ok=True)
                session.run_process(["rm", "-rf", str(self.ctx / f"issue-{n}-shots")])
                self.sweep(n)
                pr_helpers.gh("issue", "edit", n, "-R", self.gh_repo, "--remove-label", "agent-wip")
                self.log(f"cleaned up agent worktree for closed #{n}")
        for wt in sorted(glob.glob(str(self.wt_root / "spec-*/"))):
            wt = wt.rstrip("/")
            n = os.path.basename(wt).removeprefix("spec-")
            if n in waiting_posts:
                continue
            if self.state_of("issue", n) == "CLOSED":
                advisor.finish_episode(self.env, "issue", n, "closed")
                merge.remove_worktree(self.repo_dir, wt)
                self.sweep(n)
                self.log(f"cleaned up spec worktree for closed #{n}")
        for wt in sorted(glob.glob(str(self.wt_root / "pr-*/"))):
            wt = wt.rstrip("/")
            n = os.path.basename(wt).removeprefix("pr-")
            if n in waiting_pushes:
                continue
            state = self.state_of("pr", n)
            if state in ("MERGED", "CLOSED"):
                advisor.finish_episode(self.env, "pr", n, state.lower())
                merge.remove_worktree(self.repo_dir, wt)
                self.sweep(n)
                self.log(f"cleaned up worktree for {state} PR #{n}")
        # A PR reviewed in its issue's worktree has no pr-N worktree; finish its episode by number.
        for n in advisor.pending_items(self.env, "pr"):
            state = self.state_of("pr", n)
            if state in ("MERGED", "CLOSED"):
                advisor.finish_episode(self.env, "pr", n, state.lower())
        # Screenshot state can outlive a worktree (including Cursor issue worktrees).
        for shots in sorted(glob.glob(str(self.ctx / "pr-*-shots"))):
            if os.path.isdir(shots):
                n = os.path.basename(shots).removeprefix("pr-").removesuffix("-shots")
                if self.state_of("pr", n) in ("MERGED", "CLOSED"):
                    session.run_process(["rm", "-rf", shots])

    # --- by hand: python -m baton scan [N] | python -m baton file-scan DRAFT ---------------------------------
    def scan_command(self):
        if self.scan_cmd == "scan":
            read = (self.state / "scan-area").read_text().strip() if (self.state / "scan-area").is_file() else "0"
            arg = self.scan_arg or read or "0"
            if not arg.isdecimal() or not self.scan_areas:
                print("python -m baton scan [N|list]", file=sys.stderr)
                return 2
            index = int(arg) % len(self.scan_areas)
            result = {}

            def body():
                result["outcome"], result["draft"] = scan.scan_area(self.env, str(index), self.scan_areas[index])
            rc = self.item(body)
            if rc or result["outcome"] is None:
                return rc or 1
            print(result["outcome"])
            if result["draft"]:
                print(f"draft: {result['draft']}.title {result['draft']}.md\nfile it: python -m baton file-scan {result['draft']}")
            return 0
        draft = self.scan_arg
        if not all(Path(f"{draft}.{s}").is_file() and Path(f"{draft}.{s}").stat().st_size for s in ("title", "md", "area")):
            print(f"no scan draft at '{draft}'", file=sys.stderr)
            return 2
        return 0 if scan.file_scan(self.env, draft) else 1

    # --- follow-ups of reviewed PRs the owner merged ----------------------------------------
    def file_merged_follow_ups(self):
        """A PR handed to the owner (manual checks, CI, review rounds) is merged by them, not by
        merge_or_handoff, so its follow-ups and owner actions would be lost. Every tick files
        them for such PRs merged in the last 7 days, and records the merge in telemetry; each runs
        once per PR (state markers)."""
        after_merge.defer_since(self.env)   # before this tick merges anything (#52)
        since = time.strftime("%F", time.localtime(self.clock() - 7 * 86400))
        merged = self.gh_json("pr", "list", "-R", self.gh_repo, "--state", "merged", "--search", f"merged:>={since}",
                              "--limit", "50", "--json", "number,headRefName,body,comments") or []
        for p in merged:
            if not any((c.get("author") or {}).get("login") == self.owner and (c.get("body") or "").startswith("<!-- baton-review ")
                       for c in p.get("comments") or []):
                continue
            issue, _ = pr_contract.relation(p)
            n = str(p["number"])
            recorded = self.state / f"merged-event-pr-{n}"
            if not recorded.exists() and self.hooks(issue, n).owner_merged():
                recorded.touch()
            merger = merge.Merger(self.env, self.hooks(issue, n), n, issue)
            if not merger.finish_merged_task():
                self.log(f"PR #{n}: task recovery will retry")
            merger.after_merge()

    def is_agent_branch(self, pr):
        out = self.gh("pr", "view", str(pr), "-R", self.gh_repo, "--json", "headRefName", "--jq", ".headRefName")
        return bool(names.issue_of((out or "").strip()))

    def pending_openings(self):
        """A previous tick may have created a PR before it recorded the review state."""
        for path in sorted(self.state.glob("pr-opening-*.json")):
            issue = path.name.removeprefix("pr-opening-").removesuffix(".json")
            if self.force_issue and self.force_issue != issue:
                continue
            if self.force_pr or self.force_review or self.scan_cmd:
                continue
            done = self.after_item(self.item(lambda path=path: dev.resume_opening(self, path)))
            if done is not None:
                return done
        return None

    # --- a paused local review stays discoverable --------------------------------------------
    def pending_reviews(self):
        """Even after its trigger label was removed, or after Cursor opened a PR: re-enter at the
        saved review/fix step."""
        posts = owner_handoff.comment_post_episodes(self.env)
        paths = set(self.state.glob("review-pending-*"))
        paths.update(self.state / f"review-pending-{pr}" for pr in posts)
        for path in sorted(paths):
            pr = path.name.removeprefix("review-pending-")
            done = self.after_item(self.item(lambda: self.pending_review(path, posts.get(pr, set()))))
            if done is not None:
                return done
        return None

    def comment_post_pending(self, pr):
        if pr in owner_handoff.comment_post_episodes(self.env):
            return True
        saved = checkpoint.read(self.state / f"review-pending-{pr}") or {}
        return bool(saved.get("post_waiting") or saved.get("post_outcome"))

    def pending_review(self, path, post_episodes=None):
        pr = path.name.removeprefix("review-pending-")
        if self.force_issue or self.scan_cmd:
            return 0
        if (self.force_pr or self.force_review) and pr != self.force_pr + self.force_review:
            return 0
        if post_episodes is None:
            post_episodes = owner_handoff.comment_post_episodes(self.env).get(pr, set())
        try:
            saved = checkpoint.read(path) or {}
        except (OSError, ValueError):
            if not post_episodes:
                raise
            saved = {}
        episode = saved.get("ISSUE") or pr
        if post_episodes and (saved.get("pr") != pr or not saved.get("wt") or post_episodes != {episode}):
            self.active["pr"] = pr
            reason = next((name for name in owner_handoff.COMMENT_POSTS
                           if pr in owner_handoff.comment_post_episodes(self.env, name)),
                          owner_handoff.REVIEW_POST)
            for key in post_episodes:
                for name in owner_handoff.COMMENT_POSTS:
                    owner_handoff.clear_retry(self.env, key, name, pr)
            owner_handoff.perform(self.env, reason, kind="pr", number=pr, item=pr,
                                  episode=episode, title=f"PR #{pr} comment post failed",
                                  text="The saved comment post has no matching review checkpoint.",
                                  hooks=self.hooks(saved.get("ISSUE", ""), pr))
            return 80
        if not saved:
            return 0
        post = post_episodes or saved.get("post_waiting") or saved.get("post_outcome")
        push = pr in self.fix_push_episodes()
        if push:
            (self.state / f"slot-deferred-{pr}").unlink(missing_ok=True)
        if not post and not push and (saved.get("step") != "fix" or not (saved.get("reviewed_head") or saved.get("feedback_waiting"))):
            state = self.state_of("pr", pr)
            if state not in ("OPEN", "MERGED", "CLOSED"):
                return 0
            if state != "OPEN":
                self.active["pr"] = str(pr)
                advisor.finish_episode(self.env, "pr", pr, state.lower())
                path.unlink(missing_ok=True)
                return 0
        self.active["pr"] = str(pr)
        rc = self.review_loop(pr, saved.get("wt", ""), saved.get("note", ""), saved.get("fixer", ""),
                              saved.get("reviewer", ""), issue=saved.get("ISSUE", ""))
        return 75 if rc == 75 else 80

    # --- pick work: a PR the owner labelled agent-review ----------------------------------------
    def pick_review(self):
        """A PR the owner wrote with Claude Code, usually without an issue: its description is the
        spec. A vendor that did not write it reviews, and the fixer is picked by contribution."""
        if self.force_issue or self.force_pr:
            return None
        pr = self.force_review
        pushes = self.fix_push_episodes()
        if pr in pushes:
            return None
        if not pr:
            listed = self.gh_json("pr", "list", "-R", self.gh_repo, "--state", "open", "--label", "agent-review",
                                  "--limit", "20", "--json", "number") or []
            pr = str(min((p["number"] for p in listed if str(p["number"]) not in pushes
                          and not self.comment_post_pending(str(p["number"]))),
                         default=""))
        if not pr:
            return None
        return self.after_item(self.item(lambda: self.review_item(pr)))

    def review_item(self, pr):
        url = f"https://github.com/{self.gh_repo}/pull/{pr}"
        try:
            d = pr_helpers.details(pr, self.owner, self.cursor_login)
        except ValueError:
            d = {"branch": "", "route": "", "issue": "", "completion": ""}
        head_ref, ok, issue = d["branch"], d["route"], d["issue"]
        pr_helpers.gh("pr", "edit", pr, "-R", self.gh_repo, "--remove-label", "agent-review")
        if ok not in ("owner", "cursor"):
            self.notify(f"PR #{pr}: not taken for review",
                        f"Only trusted open, non-draft PRs from this repo, on a supported branch. {url}")
            return 80
        wt = ""
        for candidate in sorted(glob.glob(str(self.wt_root / "*/"))):
            if git(candidate.rstrip("/"), "branch", "--show-current") == head_ref:
                wt = candidate.rstrip("/")
                break
        if not wt:
            wt = str(self.wt_root / f"pr-{pr}")
            added = session.run_process(["git", "-C", self.repo_dir, "worktree", "add", "-q", wt, head_ref], capture_output=True).returncode == 0 \
                or session.run_process(["git", "-C", self.repo_dir, "worktree", "add", "-q", "-b", head_ref, wt, f"origin/{head_ref}"]).returncode == 0
            if not added:
                self.log(f"worktree for PR #{pr} ({head_ref}) failed")
                return 1
        if session.run_process(["git", "-C", wt, "pull", "-q", "--ff-only", "origin", head_ref]).returncode != 0:
            self.log(f"pull of {head_ref} failed")
            return 1
        if ok == "cursor":
            self.notify(f"PR #{pr}: independent review resumes", url)
            rc = self.review_loop(pr, wt, CURSOR_REVIEW_NOTE, "agent", "pool", issue=issue)
        else:
            self.notify(f"PR #{pr}: an independent vendor reviews it", url)
            rc = self.review_loop(pr, wt, AGENT_REVIEW_NOTE.format(issue=f" and issue #{issue}" if issue else ""), "agent", "pool", issue=issue)
        return 75 if rc == 75 else 80

    # --- pick work: an agent PR the owner labelled agent-fix --------------------------------------
    def fix_push_episodes(self):
        found = owner_handoff.operation_episodes(self.env, (owner_handoff.FIX_PUSH,))
        for flag in self.state.glob("fix-push-waiting-*"):
            pr = flag.name.removeprefix("fix-push-waiting-")
            if not pr.isdecimal() or pr in found:
                continue
            try:
                issue = flag.read_text().strip()
            except (OSError, ValueError):
                issue = ""
            found[pr] = {issue if issue.isdecimal() else pr}
        return found

    def pick_fix(self):
        """The owner's review comments go to the vendor that changed the fewest lines of the PR and
        can run now (pick_by_contrib), then the review loop runs again."""
        if self.force_issue or self.force_review:
            return None
        pushes = self.fix_push_episodes()
        for pr, episodes in sorted(pushes.items(), key=lambda pair: int(pair[0])):
            if (self.force_pr and self.force_pr != pr) or (self.state / f"review-pending-{pr}").exists():
                continue
            done = self.after_item(self.item(lambda pr=pr, episodes=episodes: self.resume_standalone_push(pr, episodes)))
            if done is not None:
                return done
        if self.force_pr in pushes:
            return None
        if self.force_pr:
            pr = self.force_pr
            head_ref = (self.gh("pr", "view", pr, "-R", self.gh_repo, "--json", "headRefName", "--jq", ".headRefName") or "").strip()
        else:
            listed = self.gh_json("pr", "list", "-R", self.gh_repo, "--state", "open", "--label", "agent-fix",
                                  "--limit", "20", "--json", "number,headRefName") or []
            listed = sorted((p for p in listed if names.issue_of(p["headRefName"])
                             and str(p["number"]) not in pushes
                             and not self.comment_post_pending(str(p["number"]))), key=lambda p: p["number"])
            pr, head_ref = (str(listed[0]["number"]), listed[0]["headRefName"]) if listed else ("", "")
        if not pr:
            return None
        return self.after_item(self.item(lambda: self.fix_item(pr, head_ref)))

    def fix_item(self, pr, head_ref):
        self.active["cursor_fix"] = self.active["pr"] = str(pr)
        issue = names.issue_of(head_ref)
        if episodes := self.fix_push_episodes().get(pr):
            return self.resume_standalone_push(pr, episodes)
        wt = str(names.worktree_for(self.wt_root, head_ref))
        url = f"https://github.com/{self.gh_repo}/pull/{pr}"
        if not os.path.isdir(wt) and session.run_process(["git", "-C", self.repo_dir, "worktree", "add", "-q", "-B", head_ref, wt,
                                                      f"origin/{head_ref}"]).returncode != 0:
            self.log(f"worktree for PR #{pr} failed")
            return 1
        # Fast-forward only: local commits a failed push left behind are kept and pushed with the fix.
        from . import handoff as owner_handoff, recovery
        reason = "fix_worktree_diverged"
        episode = issue or pr
        detail = f"{wt} could not fast-forward to origin/{head_ref}. {url}"

        def handoff(text=detail, **kwargs):
            return owner_handoff.perform(
                self.env, reason, kind="pr", number=pr, remove=("agent-fix",),
                item=pr, episode=episode, title=f"PR #{pr} needs you", text=text,
                hooks=self.hooks(issue or "0", pr=pr), **kwargs)

        def binding():
            if not recovery.worktree_ready(wt, head_ref)[0]:
                return None
            remote = git(wt, "rev-parse", f"origin/{head_ref}")
            current = recovery.worker_fingerprint(wt, pr, extra={"origin_branch": remote},
                                                  head_ref=head_ref, required_label="agent-fix")
            return current if current and current["remote_head"] == remote else None

        def fast_forward():
            return (session.run_process(["git", "-C", wt, "checkout", "-q", head_ref]).returncode == 0
                    and session.run_process(["git", "-C", wt, "merge", "-q", "--ff-only",
                                             f"origin/{head_ref}"]).returncode == 0)

        pending = recovery.resume(self.env, episode, pr, lambda saved: saved == reason, binding, handoff, local=True)
        if pending is False:
            return 80
        if not fast_forward():
            ready, why = recovery.worktree_ready(wt, head_ref)
            if pending or not ready:
                handoff(text=f"{wt}: {why}. {url}" if why else detail)
                return 80
            retry = recovery.request(self.env, episode, pr, reason, "worktree", "", detail,
                                     binding(), binding, handoff, local=True)
            if not retry:
                return 80
            if not fast_forward():
                handoff()
                return 80
            pending = retry
        if pending:
            recovery.clear(self.env, episode, pr)
        feedback_file = self.state / f"owner-fix-feedback-{pr}"
        if feedback_file.is_file():
            owner_text, findings = self.cached_owner_feedback(feedback_file)
        else:
            fb = review.human_feedback(self.gh_repo, self.owner, pr)
            if fb is None:
                # Keep agent-fix: dropping it or reporting "nothing to fix" would lose the feedback.
                self.log(f"PR #{pr}: could not read the review comments completely; retrying later")
                return 75
            owner_text, findings = fb.owner, fb.findings
        if not owner_text.strip():
            pr_helpers.gh("pr", "edit", pr, "-R", self.gh_repo, "--remove-label", "agent-fix")
            self.notify(f"PR #{pr}: nothing to fix", f"No new review feedback from {self.owner}.")
            return 80
        if vendors.seed_authors(str(self.state), pr, self.gh_repo, self.env.config.scheduling.cursor_branch_re, self.cursor_login):
            self.log(f"PR #{pr}: recording authors failed")
            return 1
        for commit in vendors.seed_contrib(str(self.state), pr, wt):
            self.log(f"PR #{pr}: commit {commit} has no known vendor; not counted")
        fixer = recovery.pending_fix(self.env, episode, pr) or self.pick_vendor(
            "fixer", "contrib", self.all_vendors(), pr=pr, issue=issue)
        self.notify(f"PR #{pr}: {fixer} is addressing your review", url)
        feedback = review.format_fix_feedback(owner_text, findings)
        # Posting a fix reply advances human_feedback's cutoff even when an edit defers.
        if not feedback_file.is_file():
            temp = feedback_file.with_suffix(".tmp")
            temp.write_text(json.dumps({"owner": owner_text, "findings": findings}))
            temp.replace(feedback_file)
        # The label stays only on a deferral, so the fix resumes; any other outcome drops it.
        self.fix_runner = None
        rc = self.fix_pr(pr, wt, fixer, f"{self.owner} (the repository owner)", feedback, issue,
                         owner_triggered=True)
        return self.finish_standalone_fix(pr, wt, fixer, feedback, issue, rc)

    @staticmethod
    def cached_owner_feedback(feedback_file):
        """The (owner words, findings) saved for a deferred owner fix."""
        raw = feedback_file.read_text()
        try:
            cached = json.loads(raw)
        except ValueError:
            cached = None
        if (isinstance(cached, dict) and isinstance(cached.get("owner"), str)
                and isinstance(cached.get("findings"), str)):
            return cached["owner"], cached["findings"]
        # Legacy plain-text cache from an exit-75 deferral before #178.
        return raw, ""

    def resume_standalone_push(self, pr, episodes):
        self.active["cursor_fix"] = self.active["pr"] = pr
        episode = min(episodes, key=int)
        issue = episode if episode != pr else ""
        runner = fix.Runner(self.env.with_runtime(issue=issue, pr=pr), self.hooks(issue, pr), pr)
        waiting = runner.waiting_push(pr)
        if waiting is None or waiting == "unreadable":
            if waiting == "unreadable":
                pr_helpers.gh("pr", "edit", pr, "-R", self.gh_repo, "--remove-label", "agent-fix")
            return 80
        binding = waiting["continuation"]
        if len(episodes) != 1 or binding["caller"] != "standalone":
            for key in episodes - {episode}:
                owner_handoff.clear_retry(self.env, key, owner_handoff.FIX_PUSH, pr)
            runner.handoff(pr, owner_handoff.FIX_PUSH, f"PR #{pr}: pushing fix failed",
                           "The saved push has no matching caller.")
            return self.finish_standalone_fix(pr, "", "", "", issue, 1)
        issue, wt, fixer = binding["issue"], binding["wt"], binding["fixer"]
        feedback_file = self.state / f"owner-fix-feedback-{pr}"
        feedback = (review.format_fix_feedback(*self.cached_owner_feedback(feedback_file))
                    if feedback_file.is_file() else "")
        # The label stays only on a deferral, so the fix resumes; any other outcome drops it.
        self.fix_runner = None
        rc = self.fix_pr(pr, wt, fixer, f"{self.owner} (the repository owner)", feedback, issue,
                         owner_triggered=True)
        return self.finish_standalone_fix(pr, wt, fixer, feedback, issue, rc)

    def finish_standalone_fix(self, pr, wt, fixer, feedback, issue, rc):
        if rc == 75:
            return 75
        if rc == fix.PUSH_SUPERSEDED:
            return 80
        pr_helpers.gh("pr", "edit", pr, "-R", self.gh_repo, "--remove-label", "agent-fix")
        if rc != 0:
            return 80
        if self.fix_runner:
            fixer = self.fix_runner.last_fixer or fixer
        fixed_by = " ".join(dict.fromkeys([*(self.fix_runner.attempt_authors if self.fix_runner else []), fixer]))
        note = f"""{fixer} just addressed this review feedback from {self.owner}. Check that each of the owner's points is resolved or convincingly answered in its `<!-- baton-fix by={fixer}` comment, and review the new commits. The findings block is context: an item the owner did not ask for or expressly declined is not to be reopened. The rest of the PR was reviewed before.

<feedback>
{feedback}
</feedback>"""
        runner = fix.Runner(self.env.with_runtime(issue=issue, pr=pr), self.hooks(issue, pr), pr)
        if runner.push_flag(pr).exists():
            loop = review.Review(runner.env, runner.hooks, pr, wt, issue, fixed_by)
            loop.values = {"note": note, "fixer": "agent", "reviewer": "pool", "step": "review",
                           "round": 1, "push_completed": "1"}
            loop.save()
            runner.acknowledge_push(pr)
        (self.state / f"owner-fix-feedback-{pr}").unlink(missing_ok=True)
        rc = self.review_loop(pr, wt, note, "agent", "pool", fixed_by, issue)
        return 75 if rc == 75 else 80

    # --- pick work: an issue for the pipeline ------------------------------------------------------
    def pending_specs(self):
        """Resume durable spec reads and comment posts before new work can be blocked by admission or labels."""
        found = {}
        for reason in ("spec_comments_read_failed", "spec_pending_adr_read_failed",
                       *owner_handoff.SPEC_WRITE_POSTS):
            for prefix in ("waiting", "retry"):
                for path in self.state.glob(f"handoff-{prefix}-*-{reason}.json"):
                    issue = path.name.removeprefix(f"handoff-{prefix}-").removesuffix(f"-{reason}.json")
                    if issue.isdecimal():
                        found[issue] = reason
        return found

    def resume_spec(self, issue):
        if self.spec_loop(issue):
            return dev.run(self, issue)   # an accept after a resumed post goes straight to dev
        return 80

    def pick_issue(self):
        """Trusted authors' issues or external issues explicitly labelled ready-for-agent,
        with a plan comment: the latest
        accept/revise verdict from Claude's triage, from Cursor, or posted for Cursor (the spec
        review posts one when Cursor accepts). python -m baton N takes any ready-for-agent issue, or a
        claude-found one awaiting triage (any milestone)."""
        if self.force_pr or self.force_review:
            return None
        pending = self.pending_specs()
        for issue in sorted(pending, key=int):
            if self.force_issue and self.force_issue != issue:
                continue
            reason = pending[issue]
            accept_post = reason in owner_handoff.SPEC_ACCEPT_POSTS
            if reason in owner_handoff.SPEC_WRITE_POSTS:
                try:
                    post = owner_handoff.load_retry(self.env, issue, reason)
                except owner_handoff.RetryUnreadable:
                    post = None  # Let the spec loop handle damaged state.
                if (post and (post.get("reads") is not None
                              or (accept_post or not post.get("confirmed"))
                              and post["attempts"] < owner_handoff.MAX_ATTEMPTS)
                        and owner_handoff.clock() < post["due"]):
                    if self.force_issue:
                        return self.defer("handoff-retry", reason)
                    continue
            if not accept_post:
                data = self.gh_json("issue", "view", issue, "-R", self.gh_repo, "--json", "state,labels")
                if not isinstance(data, dict) or data.get("state") not in ("OPEN", "CLOSED"):
                    continue
                if data["state"] == "CLOSED":
                    owner_handoff.clear_episode(self.env, issue)
                    continue
                labels = data.get("labels")
                if not isinstance(labels, list) or any(not isinstance(label, dict)
                                                      or label.get("name") == "ready-for-human" for label in labels):
                    continue
            if reason in ("spec_pending_adr_read_failed", *owner_handoff.SPEC_WRITE_POSTS):
                done = self.after_item(self.item(lambda: self.resume_spec(issue)))
            else:
                done = self.after_item(self.item(lambda: dev.run(self, issue)))
            if done is not None:
                return done
        if self.force_issue and self.force_issue in pending:
            return None
        issue = self.forced_issue() if self.force_issue else self.next_issue()
        if not issue:
            return None
        return self.after_item(self.item(lambda: dev.run(self, issue)))

    def forced_issue(self):
        labels = self.gh_json("issue", "view", self.force_issue, "-R", self.gh_repo, "--json", "labels,author,blockedBy") or {"labels": []}
        label_names = [label["name"] for label in labels["labels"]]
        if "ready-for-agent" not in label_names and not ("claude-found" in label_names and "needs-triage" in label_names):
            print(f"#{self.force_issue} needs the ready-for-agent label", file=sys.stderr)
            raise Stop(2)
        if "ready-for-agent" in label_names:
            if not self.owner_ready(self.force_issue):
                print(f"#{self.force_issue}: ready-for-agent must be applied by {self.owner}; cannot verify owner authorization",
                      file=sys.stderr)
                raise Stop(2)
        else:
            login = (labels.get("author") or {}).get("login") or ""
            if login != self.owner and not re.fullmatch(self.cursor_login, login):
                print(f"#{self.force_issue}: external issues need the owner's ready-for-agent label", file=sys.stderr)
                raise Stop(2)
        if blockers := open_blockers(labels):
            # A manual run is the owner's explicit request: say so, and run anyway (#70).
            warning = f"#{self.force_issue}: still blocked by {' '.join(f'#{n}' for n in blockers)}; running it as asked"
            print(warning, file=sys.stderr)
            self.log(warning)
        for branch in names.branches(self.force_issue):
            open_prs = self.gh("pr", "list", "-R", self.gh_repo, "--state", "open", "--head", branch,
                               "--json", "number", "--jq", ".[].number")
            if (open_prs or "").strip():
                print(f"#{self.force_issue} already has an open PR on {branch}; use python -m baton pr <number>",
                      file=sys.stderr)
                raise Stop(2)
        return self.force_issue

    def owner_ready(self, issue):
        """Only the owner's latest ready-for-agent label event authorizes work. Missing or
        unreadable history is not approval; all pages count, including later removals/relabels."""
        pages = self.gh_json("api", f"repos/{self.gh_repo}/issues/{issue}/events?per_page=100", "--paginate", "--slurp")
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            return False
        latest = None
        for page in pages:
            for event in page:
                if not isinstance(event, dict):
                    return False
                if event.get("event") not in ("labeled", "unlabeled") or (event.get("label") or {}).get("name") != "ready-for-agent":
                    continue
                if type(event.get("id")) is not int:
                    return False
                if latest is None or event["id"] > latest["id"]:
                    latest = event
        return bool(latest and latest["event"] == "labeled"
                    and ((latest.get("actor") or {}).get("login") or "").casefold() == self.owner.casefold())

    def admission_items(self, query):
        """Fetch every page when an admission list reaches its limit."""
        pages = self.gh_json("api", f"repos/{self.gh_repo}/{query}&per_page=100", "--paginate", "--slurp")
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            return None
        return [item for page in pages for item in page]

    def next_issue(self):
        listed = self.gh_json("issue", "list", "-R", self.gh_repo, "--state", "open", "--label", "agent-wip", "--limit", "100",
                              "--json", "number")
        if isinstance(listed, list) and len(listed) == 100:
            issues = self.admission_items("issues?state=open&labels=agent-wip")
            listed = None if issues is None else [item for item in issues if "pull_request" not in item]
        if not isinstance(listed, list):
            self.log("could not list open agent-wip issues; not starting new work")
            return ""
        cap = int(self.env.config.scheduling.max_pending_prs or 1)
        prs = self.gh_json("pr", "list", "-R", self.gh_repo, "--state", "open", "--limit", "100",
                           "--json", "number,headRefName,labels")
        if isinstance(prs, list) and len(prs) == 100:
            pulls = self.admission_items("pulls?state=open")
            prs = None if pulls is None else [
                {"number": pr["number"], "headRefName": pr["head"]["ref"], "labels": pr["labels"]} for pr in pulls]
        if not isinstance(prs, list):
            self.log("could not list open PRs; not starting new work")
            return ""
        branches = {p["headRefName"] for p in prs}
        # A PR waiting for the owner uses no agent capacity; it must not hold up the queue.
        with_owner = {names.issue_of(p["headRefName"]) for p in prs
                      if any(label.get("name") == "ready-for-human" for label in p.get("labels") or [])}
        pending = [p for p in listed if str(p["number"]) not in with_owner]
        pr_of = {names.issue_of(p["headRefName"]): str(p.get("number", "")) for p in prs
                 if names.issue_of(p["headRefName"]) not in with_owner}
        markers = self.slot_markers()
        held = {str(p["number"]): pr_of.get(str(p["number"])) for p in pending}
        deferred = {n for n, pr in held.items() if pr and pr in markers}
        for pr in set(markers) - {held[n] for n in deferred}:
            (self.state / f"slot-deferred-{pr}").unlink(missing_ok=True)
        if len(pending) - len(deferred) >= cap or len(pending) >= cap + 1:
            why = []
            for p in pending:
                n = str(p["number"])
                if not held[n]:
                    why.append(f"#{n}: no open PR")
                elif n in deferred:
                    why.append(f"#{n}: PR #{held[n]} deferred ({markers[held[n]]})")
                else:
                    why.append(f"#{n}: PR #{held[n]} in review")
            limit = f"total limit {cap + 1} reached" if len(pending) >= cap + 1 else f"{len(pending) - len(deferred)} not deferred"
            self.log(f"{len(pending)} issue(s) still waiting on review (agent-wip), cap {cap}, {len(deferred)} deferred, "
                     f"{limit}; not starting new work: " + "; ".join(why))
            return ""
        from . import recovery

        waiting = self.pending_specs()
        for n in self.candidates():
            if str(n) in waiting or any(names.issue_of(b) == str(n) for b in branches):
                continue   # already has a PR
            # These issues can be at different steps: one whose next step no vendor can run now must
            # not block another (revision needs a pool vendor).
            issue = str(n)
            try:
                payload = spec.Spec(self.env, self.hooks(issue), issue).issue_payload()
            except Deferred as deferred:
                self.defer(deferred.vendor, deferred.reason)
                continue
            if payload is None:
                continue
            if spec.accept_pending(self.env, issue, listed=payload["comments"]):
                self.recovered_issues[issue] = payload
                return issue
            if recovery.path(self.env, issue, issue).exists():
                self.recovered_issues[issue] = payload
                return issue
            role = spec.role_from(payload["comments"], self.env)
            candidates = list(self.env.config.pool()) if role == "reviser" else self.all_vendors()
            try:
                self.pick_vendor(role, "contrib", candidates, issue=issue)
            except Deferred as deferred:
                self.defer(deferred.vendor, deferred.reason)
                continue
            self.recovered_issues[issue] = payload
            return issue
        return ""

    def candidates(self):
        """ready-for-agent says "specified", AGENT_MILESTONE says "wanted now" ("" for any
        milestone); the scan's claude-found issues need no milestone. External authors need
        ready-for-agent: scanner labels alone never authorize their issues."""
        milestone = self.env.repo.agent_milestone
        found = {}
        for labels in (("ready-for-agent",), ("claude-found", "needs-triage")):
            args = [x for label in labels for x in ("--label", label)]
            for i in self.gh_json("issue", "list", "-R", self.gh_repo, "--state", "open", *args, "--limit", "50",
                                  "--json", "number,author,labels,milestone,blockedBy") or []:
                label_names = [label["name"] for label in i["labels"]]
                login = (i.get("author") or {}).get("login") or ""
                trusted = login == self.owner or bool(re.fullmatch(self.cursor_login, login))
                if not trusted and "ready-for-agent" not in label_names:
                    continue
                if milestone and not ((i.get("milestone") or {}).get("title") == milestone or "claude-found" in label_names):
                    continue
                if any(x in label_names for x in ("ready-for-human", "needs-info", "agent-wip")):
                    continue
                if blockers := open_blockers(i):
                    self.log(f"#{i['number']}: skipped; blocked by {' '.join(f'#{n}' for n in blockers)}")
                    continue
                if (labels == ("ready-for-agent",) or "ready-for-agent" in label_names) and not self.owner_ready(i["number"]):
                    self.log(f"#{i['number']}: skipped; cannot verify ready-for-agent was applied by {self.owner}")
                    continue
                found[i["number"]] = None
        return sorted(found)

    # --- nothing else to do: scan one area for a defect ----------------------------------------------
    def scan_fallback(self):
        """0 when an area was scanned (or tried), None when this repository scans nothing now."""
        if self.env.config.scheduling.cross_review is not True or self.env.config.scheduling.scan is not True \
                or not self.scan_areas or self.fetch_failed:
            return None
        try:
            last = int((self.state / "scan-last").read_text().strip())
        except (OSError, ValueError):
            last = 0
        listed = self.gh_json("issue", "list", "-R", self.gh_repo, "--state", "open", "--label", "claude-found",
                              "--label", "needs-triage", "--limit", "50", "--json", "number")
        max_waiting = int(self.env.config.scheduling.scan_max_waiting or 2)
        waiting = len(listed) if listed is not None else max_waiting   # gh failed: don't scan blind
        interval = int(self.env.config.scheduling.scan_interval_hours or 6) * 3600
        if self.now - last < interval:
            self.log(f"no scan; next after {time.strftime('%F %T', time.localtime(last + interval))}")
            return None
        if waiting >= max_waiting:
            self.log(f"no scan; {waiting} claude-found issue(s) still wait for Cursor")
            return None
        if not self.env.config.for_role("scan"):
            self.log("scan skipped: no vendor with scan role")
            return None
        self.item(self.scan_item)
        return 0

    def scan_item(self):
        scanners = self.env.config.for_role("scan")
        if not scanners:
            self.log("scan skipped: no vendor with scan role")
            return None
        vendor = ""
        blocked = ""
        for candidate in scanners:
            status = vendors.vendor_blocked(str(self.runner_state), candidate)
            if not status:
                vendor = candidate
                break
            blocked = status
        if not vendor:
            return self.defer(scanners[0], blocked or f"{scanners[0]} blocked")
        path = self.state / "scan-area"
        try:
            index = int(path.read_text().strip()) % len(self.scan_areas)
        except (OSError, ValueError):
            index = 0
        outcome, draft = scan.scan_area(self.env, str(index), self.scan_areas[index])
        if outcome is None:
            return 1
        path.write_text(f"{(index + 1) % len(self.scan_areas)}\n")
        (self.state / "scan-last").write_text(f"{self.now}\n")
        if draft:
            scan.file_scan(self.env, draft)
        return 0


def rotation(names, last):
    """NAMES in turn, starting after LAST (as configured when LAST is not among them)."""
    names = list(names)
    if last not in names:
        return names
    i = names.index(last) + 1
    return names[i:] + names[:i]


def run(ticks):
    """One tick over TICKS (one per repository, in configured order): the lock and the
    runner-wide housekeeping once, then each repository's work in turn, then the scan."""
    first = ticks[0]
    config, runner_state, manual = first.env.config, first.runner_state, bool(first.manual)
    if first.scan_cmd == "scan" and first.scan_arg == "list":
        for i, area in enumerate(first.scan_areas):
            print(i, area)
        return 0
    paused = config.scheduling.paused is True and not manual
    admitted = maintenance.admit(
        runner_state, runner_state / "lock", acquire=not paused,
        on_expired=lambda: notify.append(config.paths.logs, "maintenance expired"),
    )
    if admitted.reason is not None:
        notify.append(config.paths.logs, f"maintenance ({admitted.reason})")
        return 0
    if paused:
        notify.append(config.paths.logs, "paused")
        return 0
    lock = admitted.lock
    if lock is None:
        notify.append(config.paths.logs, "busy (another tick holds the lock)")
        return 0
    for t in ticks:
        t.lock_file = lock
    first.hooks().tick(manual)
    from .initialization import expire_context
    expire_context(config, first.clock())
    try:
        with session_bound(first.env):
            advisor.digest(first.env)   # the daily owner digest of advisor decisions (#17)
    except Exception as error:   # a digest never stops the tick
        notify.append(config.paths.logs, f"advisor digest failed: {type(error).__name__}")
    (runner_state / "chain-next").unlink(missing_ok=True)
    # Upgrade the old global cooldown as Claude-only; retain today's Claude counter.
    old, new = runner_state / "cooldown-until", runner_state / "claude-cooldown-until"
    if old.is_file() and not new.exists():
        old.rename(new)
    if len(ticks) == 1:
        try:
            return first.pass_()
        except Stop as stop:
            return stop.rc
    try:
        last = (runner_state / "last-repo").read_text().strip()
    except OSError:
        last = ""
    by_name = {t.gh_repo: t for t in ticks}
    turn = [by_name[name] for name in rotation(by_name, last)]
    try:
        for t in turn:
            with session_bound(t.env):
                rc = t.work()
            if rc is not None:
                return rc
        for t in turn:
            with session_bound(t.env):
                rc = t.scan_fallback()
            if rc is not None:
                return rc
    except Stop as stop:
        return stop.rc
    notify.append(config.paths.logs, "no work in any repository")
    return 1 if any(t.fetch_failed for t in turn) else 0


class session_bound:
    """Helpers that resolve the session (notify.log, gh -R) see this repository's."""
    def __init__(self, env):
        self.env = env

    def __enter__(self):
        self.token = session.bind(self.env)

    def __exit__(self, *exc):
        session._active.reset(self.token)


@session.scoped
def setup(env):
    from .initialization import prepare
    prepare(session.resolve(env).config)


def main(argv=None):
    from .bootstrap import main as bootstrap_main
    return bootstrap_main(argv)


if __name__ == '__main__':
    sys.exit(main())
