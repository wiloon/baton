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

from . import checkpoint, dev, fix, hooks as telemetry, merge, names, notify, pick, pr as pr_helpers, pr_contract, review, scan, spec, tmp_sweep, vendors
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
        self.active = {"review": "", "spec": "", "issue": "", "cursor_fix": ""}
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
            if a["review"]:
                (state / f"review-pending-{a['review']}").unlink(missing_ok=True)
            if a["spec"]:
                (state / f"spec-paused-{a['spec']}").unlink(missing_ok=True)
            if a["cursor_fix"]:
                n = a["cursor_fix"]
                for path in (state / f"cursor-fix-base-{n}", state / f"pr-body-base-{n}", self.ctx / f"pr-{n}-body.md",
                             self.ctx / f"pr-{n}-fix-checks.json"):
                    path.unlink(missing_ok=True)
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
        except Exception as exc:
            self.log(f"item failed: {type(exc).__name__}: {exc}")
            rc = 1
        finally:
            self.finish_item(rc)
        return rc

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
        self.active["review"] = self.active["cursor_fix"] = str(pr)
        loop = review.Review(self.env.with_runtime(issue=issue), self.hooks(issue, pr), pr, wt, issue, fixed_by)
        return loop.loop(note, fixer, reviewer)

    def fix_pr(self, pr, wt, fixer, source, feedback, issue="", head=""):
        if fixer == "cursor":
            self.active["cursor_fix"] = str(pr)
        runner = fix.Runner(self.env.with_runtime(issue=issue, pr=str(pr)), self.hooks(issue, pr), pr, "")
        return fix.fix_pr(runner, pr, wt, fixer, source, feedback, head)

    def spec_loop(self, issue):
        self.active["spec"] = self.active["issue"] = str(issue)
        return spec.Spec(self.env.with_runtime(issue=str(issue)), self.hooks(issue), str(issue)).loop()

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
        if self.force_pr and not self.is_agent_branch(self.force_pr):
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
        for n, path in names.worktrees(self.wt_root):
            wt = str(path)
            if self.state_of("issue", n) == "CLOSED":
                merge.remove_worktree(self.repo_dir, wt)
                Path(f"{wt}.pr.md").unlink(missing_ok=True)
                session.run_process(["rm", "-rf", str(self.ctx / f"issue-{n}-shots")])
                self.sweep(n)
                pr_helpers.gh("issue", "edit", n, "-R", self.gh_repo, "--remove-label", "agent-wip")
                self.log(f"cleaned up agent worktree for closed #{n}")
        for wt in sorted(glob.glob(str(self.wt_root / "spec-*/"))):
            wt = wt.rstrip("/")
            n = os.path.basename(wt).removeprefix("spec-")
            if self.state_of("issue", n) == "CLOSED":
                merge.remove_worktree(self.repo_dir, wt)
                self.sweep(n)
                self.log(f"cleaned up spec worktree for closed #{n}")
        for wt in sorted(glob.glob(str(self.wt_root / "pr-*/"))):
            wt = wt.rstrip("/")
            n = os.path.basename(wt).removeprefix("pr-")
            state = self.state_of("pr", n)
            if state in ("MERGED", "CLOSED"):
                merge.remove_worktree(self.repo_dir, wt)
                self.sweep(n)
                self.log(f"cleaned up worktree for {state} PR #{n}")
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
        them for such PRs merged in the last 7 days; both run once per PR (state markers)."""
        since = time.strftime("%F", time.localtime(self.clock() - 7 * 86400))
        merged = self.gh_json("pr", "list", "-R", self.gh_repo, "--state", "merged", "--search", f"merged:>={since}",
                              "--limit", "50", "--json", "number,headRefName,body,comments") or []
        for p in merged:
            if not any((c.get("author") or {}).get("login") == self.owner and (c.get("body") or "").startswith("<!-- baton-review ")
                       for c in p.get("comments") or []):
                continue
            issue, _ = pr_contract.relation(p)
            n = str(p["number"])
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
        for path in sorted(glob.glob(str(self.state / "review-pending-*"))):
            done = self.after_item(self.item(lambda path=path: self.pending_review(Path(path))))
            if done is not None:
                return done
        return None

    def pending_review(self, path):
        saved = checkpoint.read(path)
        if self.force_issue or self.scan_cmd or saved is None:
            return 0
        pr = saved.get("pr", "")
        if (self.force_pr or self.force_review) and pr != self.force_pr + self.force_review:
            return 0
        state = self.state_of("pr", pr)
        if state != "OPEN":
            if state in ("MERGED", "CLOSED"):
                path.unlink(missing_ok=True)
            return 0
        self.review_loop(pr, saved.get("wt", ""), saved.get("note", ""), saved.get("fixer", ""), saved.get("reviewer", ""),
                         issue=saved.get("ISSUE", ""))
        return 80

    # --- pick work: a PR the owner labelled agent-review ----------------------------------------
    def pick_review(self):
        """A PR the owner wrote with Claude Code, usually without an issue: its description is the
        spec. A vendor that did not write it reviews, and the fixer is picked by contribution."""
        if self.force_issue or self.force_pr:
            return None
        pr = self.force_review
        if not pr:
            listed = self.gh_json("pr", "list", "-R", self.gh_repo, "--state", "open", "--label", "agent-review",
                                  "--limit", "20", "--json", "number") or []
            pr = str(min((p["number"] for p in listed), default="")) if listed else ""
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
            self.review_loop(pr, wt, CURSOR_REVIEW_NOTE, "agent", "pool", issue=issue)
        else:
            self.notify(f"PR #{pr}: an independent vendor reviews it", url)
            self.review_loop(pr, wt, AGENT_REVIEW_NOTE.format(issue=f" and issue #{issue}" if issue else ""), "agent", "pool", issue=issue)
        return 80

    # --- pick work: an agent PR the owner labelled agent-fix --------------------------------------
    def pick_fix(self):
        """The owner's review comments go to the vendor that changed the fewest lines of the PR and
        can run now (pick_by_contrib), then the review loop runs again."""
        if self.force_issue:
            return None
        if self.force_pr:
            pr = self.force_pr
            head_ref = (self.gh("pr", "view", pr, "-R", self.gh_repo, "--json", "headRefName", "--jq", ".headRefName") or "").strip()
        else:
            listed = self.gh_json("pr", "list", "-R", self.gh_repo, "--state", "open", "--label", "agent-fix",
                                  "--limit", "20", "--json", "number,headRefName") or []
            listed = sorted((p for p in listed if names.issue_of(p["headRefName"])), key=lambda p: p["number"])
            pr, head_ref = (str(listed[0]["number"]), listed[0]["headRefName"]) if listed else ("", "")
        if not pr:
            return None
        return self.after_item(self.item(lambda: self.fix_item(pr, head_ref)))

    def fix_item(self, pr, head_ref):
        issue = names.issue_of(head_ref)
        wt = str(names.worktree_for(self.wt_root, head_ref))
        url = f"https://github.com/{self.gh_repo}/pull/{pr}"
        if not os.path.isdir(wt) and session.run_process(["git", "-C", self.repo_dir, "worktree", "add", "-q", "-B", head_ref, wt,
                                                      f"origin/{head_ref}"]).returncode != 0:
            self.log(f"worktree for PR #{pr} failed")
            return 1
        # Fast-forward only: local commits a failed push left behind are kept and pushed with the fix.
        if not (session.run_process(["git", "-C", wt, "checkout", "-q", head_ref]).returncode == 0
                and session.run_process(["git", "-C", wt, "merge", "-q", "--ff-only", f"origin/{head_ref}"]).returncode == 0):
            pr_helpers.gh("pr", "edit", pr, "-R", self.gh_repo, "--remove-label", "agent-fix", "--add-label", "ready-for-human")
            self.notify(f"PR #{pr} needs you", f"{wt} has diverged from origin/{head_ref}. {url}")
            return 80
        feedback = review.human_feedback(self.gh_repo, self.owner, pr)
        if not feedback.strip():
            pr_helpers.gh("pr", "edit", pr, "-R", self.gh_repo, "--remove-label", "agent-fix")
            self.notify(f"PR #{pr}: nothing to fix", f"No new review feedback from {self.owner}.")
            return 80
        if vendors.seed_authors(str(self.state), pr, self.gh_repo, self.env.config.scheduling.cursor_branch_re, self.cursor_login):
            self.log(f"PR #{pr}: recording authors failed")
            return 1
        for commit in vendors.seed_contrib(str(self.state), pr, wt):
            self.log(f"PR #{pr}: commit {commit} has no known vendor; not counted")
        fixer = self.pick_vendor("fixer", "contrib", self.all_vendors(), pr=pr, issue=issue)
        self.notify(f"PR #{pr}: {fixer} is addressing your review", url)
        # The label stays only on a deferral, so the fix resumes; any other outcome drops it.
        rc = self.fix_pr(pr, wt, fixer, f"{self.owner} (the repository owner)", feedback, issue)
        pr_helpers.gh("pr", "edit", pr, "-R", self.gh_repo, "--remove-label", "agent-fix")
        if rc != 0:
            return 80
        self.review_loop(pr, wt, f"""{fixer} just addressed this review feedback from {self.owner}. Check that each point is resolved or convincingly answered in its `<!-- baton-fix by={fixer}` comment, and review the new commits. The rest of the PR was reviewed before.

<feedback>
{feedback}
</feedback>""", "agent", "pool", fixer, issue)
        return 80

    # --- pick work: an issue for the pipeline ------------------------------------------------------
    def pick_issue(self):
        """Trusted authors' issues or external issues explicitly labelled ready-for-agent,
        with a plan comment: the latest
        accept/revise verdict from Claude's triage, from Cursor, or posted for Cursor (the spec
        review posts one when Cursor accepts). python -m baton N takes any ready-for-agent issue, or a
        claude-found one awaiting triage (any milestone)."""
        issue = ""
        if self.force_issue:
            issue = self.forced_issue()
        elif self.force_pr or self.force_review:
            return None
        else:
            issue = self.next_issue()
        if not issue:
            return None
        return self.after_item(self.item(lambda: dev.run(self, issue)))

    def forced_issue(self):
        labels = self.gh_json("issue", "view", self.force_issue, "-R", self.gh_repo, "--json", "labels,author") or {"labels": []}
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

    def next_issue(self):
        pending = self.gh_json("issue", "list", "-R", self.gh_repo, "--state", "open", "--label", "agent-wip", "--limit", "100",
                               "--json", "number") or []
        cap = int(self.env.config.scheduling.max_pending_prs or 1)
        prs = self.gh_json("pr", "list", "-R", self.gh_repo, "--state", "open", "--limit", "100",
                           "--json", "headRefName,labels") or []
        branches = {p["headRefName"] for p in prs}
        # A PR waiting for the owner uses no agent capacity; it must not hold up the queue.
        with_owner = {names.issue_of(p["headRefName"]) for p in prs
                      if any(label.get("name") == "ready-for-human" for label in p.get("labels") or [])}
        pending = [p for p in pending if str(p["number"]) not in with_owner]
        if len(pending) >= cap:
            self.log(f"{len(pending)} issue(s) still waiting on review (agent-wip), cap {cap}; not starting new work")
            return ""
        for n in self.candidates():
            if any(names.issue_of(b) == str(n) for b in branches):
                continue   # already has a PR
            # These issues can be at different steps: one whose next step no vendor can run now must
            # not block another (revision needs a pool vendor).
            next_vendors = self.all_vendors()
            if not spec.cursor_plan(self.env, str(n)) and spec.spec_state(self.env, str(n))[0] in ("review", "reply-human"):
                next_vendors = list(self.env.config.quota.vendor_pool)
            try:
                self.pick_vendor("next_vendor", "contrib", next_vendors)
            except Deferred as deferred:
                self.defer(deferred.vendor, deferred.reason)
                continue
            return str(n)
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
                                  "--json", "number,author,labels,milestone") or []:
                label_names = [label["name"] for label in i["labels"]]
                login = (i.get("author") or {}).get("login") or ""
                trusted = login == self.owner or bool(re.fullmatch(self.cursor_login, login))
                if not trusted and "ready-for-agent" not in label_names:
                    continue
                if milestone and not ((i.get("milestone") or {}).get("title") == milestone or "claude-found" in label_names):
                    continue
                if any(x in label_names for x in ("ready-for-human", "needs-info", "agent-wip")):
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
        self.item(self.scan_item)
        return 0

    def scan_item(self):
        why = vendors.vendor_blocked(str(self.runner_state), "claude")
        if why:
            return self.defer("claude", why)
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
    first.hooks().tick(manual)
    if config.scheduling.paused is True and not manual:
        notify.append(config.paths.logs, "paused")
        return 0
    lock = open(runner_state / "lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        notify.append(config.paths.logs, "busy (another tick holds the lock)")
        lock.close()
        return 0
    for t in ticks:
        t.lock_file = lock
    from .initialization import expire_context
    expire_context(config, first.clock())
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
