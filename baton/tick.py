"""The tick and the entry point (migration slice 4e, #284): the control flow of python -m baton.

The Python bootstrap loads typed TOML and prepares one Session for scheduled/manual
runs. A chained pass re-enters the same bootstrap with the selected configuration.

A tick takes the configured repositories in turn (#327): it starts with the one after the
repository whose item ran last (state `last-repo`), and still does one item in all. In each
repository it does one of, in this order (the order is a contract):
  - reconcile a PR whose creation was interrupted (pr-opening-N.json)
  - P0 issues (ADR 0010): their resumed reviews, fixes and specs, then a new one, whatever
    max_pending_prs says
  - a review the last run deferred (review-pending-N)
  - a PR the owner labelled agent-review
  - an agent PR labelled agent-fix (owner feedback)
  - a ready-for-agent issue: spec review, dev, review loop
Ticks for different repositories may overlap (ADR 0003, "Work locks"): a tick takes a
repository's lock before it looks for work there, holds one at a time and skips a locked one; the
housekeeping and the scan each have a lock of their own.

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
the item ended and the next pass may start (chain_next), 81 an assigned serve child (--issue /
--no-new-issue) that ran no item (ADR 0012).
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

from . import advisor, after_merge, agents, checkpoint, dev, eligibility, fix, handoff as owner_handoff, hooks as telemetry, maintenance, merge, names, notify, pick, pr as pr_helpers, pr_contract, review, scan, spec, statefile, tiers, tmp_sweep, vendors
from .common import Deferred, git
from .eligibility import open_blockers, priority_of

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
        # The repository's own files; runner_state (the work locks, cooldowns, last-repo) is shared.
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
        self.priorities = {}   # issue number -> priority_of, as last listed
        self.tried = set()     # (kind, number) of what this tick already tried
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
            statefile.replace(self.runner_state / "last-repo", f"{self.gh_repo}\n")
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
                producers = {agents.LEGACY_REPLY_VENDOR}   # legacy names, plus whoever the owner record names
                try:
                    recorded = json.loads((state / f"pr-{n}-fix-owner").read_text())["fixer"]
                    if recorded in agents.ADAPTERS:
                        producers.add(recorded)
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                for producer in producers:
                    for kind in ("base", "task"):
                        (state / f"{producer}-fix-{kind}-{n}").unlink(missing_ok=True)
                for path in (state / f"pr-body-base-{n}", state / f"owner-fix-feedback-{n}",
                             self.ctx / f"pr-{n}-fix-checks.json"):
                    path.unlink(missing_ok=True)
                pr_helpers.remove_body_request(self.ctx / f"pr-{n}-body.md")
                (state / f"pr-{n}-fix-owner").unlink(missing_ok=True)
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
        return eligibility.slot_markers(self.state, unreadable=lambda path: path.unlink(missing_ok=True))

    def draining(self):
        """Active maintenance reason, or None. An expired flag counts as none (as serve.py)."""
        return maintenance.admit(self.runner_state, None, acquire=False).reason

    def stop_if_draining(self):
        """0 when maintenance is active (logged once); None when the tick may take the next item."""
        if reason := self.draining():
            self.log(f"maintenance ({reason}): tick stops before the next item")
            return 0
        return None

    def after_item(self, rc):
        """What the tick does with an item's exit code: None to go on, else the exit code."""
        if rc == 75:
            if self.manual:
                return 75
            if (stopped := self.stop_if_draining()) is not None:
                return stopped
            return None
        if rc == 80:
            return 80 if self.chain_next() else 0
        return rc or None

    def chain_next(self):
        """A partial slice merged in this tick touches chain-next (finish_merged_task); the tick
        then starts over at once instead of leaving the issue idle until the next timer tick.
        CHAIN_MAX bounds the run. A `serve` child never re-execs: True tells it to exit 80, and
        the supervisor bounds the chain."""
        flag = self.state / "chain-next"
        if self.manual or not flag.exists():
            return False
        flag.unlink(missing_ok=True)
        if self.env.runtime.serve_child:
            self.log("chain: a slice merged, the supervisor starts the next pass")
            return True
        done, limit = int(self.env.runtime.chain or 0), int(self.env.config.scheduling.chain_max or 5)
        if done >= limit:
            self.log(f"chain: {limit} passes in a row, next slice waits for the timer")
            return False
        self.log("chain: a slice merged, starting the next pass now")
        self.env.runtime.chain = done + 1
        if self.lock_file:
            self.lock_file.close()
        sys.stdout.flush()
        self.exec(sys.executable, [sys.executable, "-m", "baton", "--config", str(self.env.config.source), "--chain", str(done + 1)])
        return False

    # --- agent steps that touch the item's markers ---------------------------------------
    def review_loop(self, pr, wt, note, fixer="agent", reviewer="pool", fixed_by="", issue=""):
        """The review rounds, then the merge or handoff (baton.review)."""
        self.active["review"] = self.active["cursor_fix"] = self.active["pr"] = str(pr)
        loop = review.Review(self.env.with_runtime(issue=issue), self.hooks(issue, pr), pr, wt, issue, fixed_by)
        return loop.loop(note, fixer, reviewer)

    def fix_pr(self, pr, wt, fixer, source, feedback, issue="", head="", *, owner_triggered=False, env=None):
        self.active["cursor_fix"] = str(pr)
        runner = fix.Runner((env or self.env).with_runtime(issue=issue, pr=str(pr)), self.hooks(issue, pr), pr, "")
        runner.required_retry_label = "agent-fix"
        self.fix_runner = runner
        return fix.fix_pr(runner, pr, wt, fixer, source, feedback, head, owner_triggered=owner_triggered)

    def spec_loop(self, issue):
        self.active["spec"] = self.active["issue"] = str(issue)
        payload = (getattr(self, "recovered_issues", {}) or {}).pop(str(issue), None)
        return spec.Spec(self.env.with_runtime(issue=str(issue)), self.hooks(issue), str(issue)).loop(
            issue_payload=payload)

    def pick_vendor(self, role, mode, candidates, pr="", issue="", exclude="", current="agent", env=None):
        return pick.choose(env or self.env, role, current, mode, candidates, pr=str(pr), issue=str(issue),
                           exclude=exclude)

    def all_vendors(self):
        return pick.all_vendors(self.env)

    # --- the tick -----------------------------------------------------------------------
    def run(self):
        """This repository's tick alone (by hand, or with one repository configured)."""
        return run([self])

    # --- work locks ----------------------------------------------------------------------
    def take(self, path):
        """PATH's work lock, or None when another process holds it. Maintenance found on here
        stops the tick."""
        logs = self.env.config.paths.logs
        admitted = maintenance.admit(self.runner_state, path,
                                     on_expired=lambda: notify.append(logs, "maintenance expired"))
        if admitted.reason is not None:
            notify.append(logs, f"maintenance ({admitted.reason})")
            raise Stop(0)
        return admitted.lock

    def log_busy(self):
        label = f"{notify.label(self.env)} " if len(self.env.config.repos) > 1 else ""
        notify.append(self.env.config.paths.logs, f"{label}busy (another tick holds the lock)")

    def take_repository(self):
        """This repository's lock (also kept for chain_next to release), or None when busy. Only its
        holder clears the chain flag."""
        self.lock_file = self.take(maintenance.repository_lock(self.runner_state, self.gh_repo))
        if self.lock_file is None:
            self.log_busy()
        else:
            (self.state / "chain-next").unlink(missing_ok=True)
        return self.lock_file

    def pass_(self):
        """The one repository's work and, still holding its lock, the scan."""
        assigned = bool(self.env.runtime.assign_issue or self.env.runtime.no_new_issue)
        rc = self.work()
        # An assigned serve child never scans: the supervisor already chose its work (#291).
        if rc is None and not self.fetch_failed and not assigned:
            if scan_lock := self.take(self.runner_state / maintenance.SCAN_LOCK):
                with scan_lock:
                    rc = self.scan_fallback()
        if rc is None:
            self.log("no work" if not self.fetch_failed else "git fetch failed; nothing done")
            if self.fetch_failed:
                return 1
            # Exit 81: an assigned child that took the lock and ran no item (ADR 0012).
            return 81 if assigned else 0
        return rc

    def work(self):
        """This repository's item, if it has one: its exit code, or None for nothing to do here."""
        if self.env.config.scheduling.cross_review is True:
            self.advance_cursor_verdicts()
        if session.run_process(["git", "-C", self.repo_dir, "fetch", "-q", "--prune", "origin"]).returncode != 0:
            self.log("git fetch failed")
            self.fetch_failed = True
            return None
        self.cleanup()
        owner_handoff.retry_ready_labels(self.env)
        if self.scan_cmd:
            return self.scan_command()
        self.file_merged_follow_ups()
        if (self.force_pr and not self.comment_post_pending(self.force_pr)
                and self.force_pr not in self.fix_push_episodes()
                and not self.is_agent_branch(self.force_pr)):
            print("python -m baton pr N takes an agent/issue-N PR; for the owner's own PR use python -m baton review N", file=sys.stderr)
            return 2
        self.tried = set()
        for step in (self.pending_openings, self.p0_pass, self.pending_reviews, self.pick_review, self.pick_fix,
                     self.pick_issue):
            done = step()
            if done is not None:
                return done
        return None

    # --- P0 goes first (ADR 0010) -----------------------------------------------------------
    def p0_pass(self):
        """The work of P0 issues ahead of everything else: what is in flight, then a new one. What
        cannot run now leaves the tick to the next P0 item and then the normal order."""
        if self.force_issue or self.force_pr or self.force_review:
            return None
        if (stopped := self.stop_if_draining()) is not None:
            return stopped
        p0 = self.p0_issues()
        if not p0:
            return None
        for step in (self.pending_reviews, self.pick_fix, self.resume_specs, self.start_p0):
            done = step(p0)
            if done is not None:
                return done
        return None

    def start_p0(self, p0):
        """A new P0 issue: every candidate filter applies, max_pending_prs does not."""
        if (stopped := self.stop_if_draining()) is not None:
            return stopped
        candidates = self.candidates(p0)
        prs = self.open_prs() if candidates else None
        if prs is None:
            return None
        for issue in self.runnable(candidates, {p["headRefName"] for p in prs}):
            if (stopped := self.stop_if_draining()) is not None:
                return stopped
            done = self.start(issue, ", ahead of lower-priority work; max_pending_prs not applied")
            if done is not None:
                return done
        return None

    def start(self, issue, note=""):
        rank = self.priorities.get(str(issue), 4)
        self.log(f"#{issue}: starting ({f'P{rank}' if rank < 4 else 'no priority'}){note}")
        return self.attempt(lambda: dev.run(self, issue))

    def attempt(self, body):
        """item(), then after_item. A review that a deferred item left behind has been tried: it is
        not resumed again in the same tick."""
        rc = self.item(body)
        if rc == 75 and self.active.get("pr"):
            self.tried.add(("review", self.active["pr"]))
        return self.after_item(rc)

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
                pr_helpers.gh("issue", "edit", n, "-R", self.gh_repo, "--remove-label", "needs-triage")
                self.log(f"#{n}: Cursor verdict {verdict}; waits for the owner's ready-for-agent")
                self.notify(f"#{n} waits for your ready-for-agent (Cursor: {verdict})", url)
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
                         if reason in (*owner_handoff.SPEC_WRITE_POSTS, *owner_handoff.ISSUE_PUSHES)}
        waiting_pushes = self.fix_push_episodes()
        waiting_issues = set().union(*waiting_pushes.values()) | waiting_posts
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
        self.retry_merged_settlements()

    def record_merged_settlement(self, pr, issue):
        """Record a merged PR for task settlement atomically; False when the write fails."""
        path = self.state / f"task-settle-pr-{pr}.json"
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            temp.write_text(json.dumps({"pr": str(pr), "issue": str(issue)}, sort_keys=True) + "\n")
            temp.replace(path)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            self.log(f"PR #{pr}: could not write settlement record: {exc}")
            return False
        return True

    def settle_recorded_merge(self, pr, issue, path=None):
        """Run finish_merged_task and after_merge for a recorded PR; delete the record only
        once task-merged-pr-<pr> exists."""
        pr, issue = str(pr), str(issue)
        path = path or (self.state / f"task-settle-pr-{pr}.json")
        recorded = self.state / f"merged-event-pr-{pr}"
        if not recorded.exists() and self.hooks(issue, pr).owner_merged():
            recorded.touch()
        merger = merge.Merger(self.env, self.hooks(issue, pr), pr, issue)
        if not merger.finish_merged_task():
            self.log(f"PR #{pr}: task recovery will retry")
        merger.after_merge()
        if (self.state / f"task-merged-pr-{pr}").exists():
            path.unlink(missing_ok=True)
            return True
        return False

    def record_and_settle_merge(self, pr, issue):
        """Record PR for settlement and run it now; True when task-merged-pr-<pr> exists."""
        if not self.record_merged_settlement(pr, issue):
            return False
        return self.settle_recorded_merge(pr, issue)

    def retry_merged_settlements(self):
        """Retry every pending settlement record, independent of open-issue state or labels."""
        for path in sorted(self.state.glob("task-settle-pr-*.json")):
            try:
                data = json.loads(path.read_text())
                pr = str(data["pr"])
                issue = str(data["issue"])
                if not pr.isdecimal() or not issue.isdecimal():
                    raise ValueError("invalid pr or issue")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.log(f"{path.name}: unreadable settlement record: {exc}")
                continue
            self.settle_recorded_merge(pr, issue, path)

    def matches_merged_issue(self, pr_data, issue):
        """True when a PR dict (REST or gh pr view) is authored by the owner on main in this
        repository and its task relation names ISSUE."""
        if "baseRefName" in pr_data:
            if pr_data.get("baseRefName") != "main" or pr_data.get("isCrossRepository") is not False:
                return False
            login = (pr_data.get("author") or {}).get("login") or ""
            branch = pr_data.get("headRefName") or ""
        else:
            login = (pr_data.get("user") or {}).get("login") or ""
            branch = (pr_data.get("head") or {}).get("ref") or ""
        if login != self.owner:
            return False
        try:
            related_issue, _ = pr_contract.relation({"body": pr_data.get("body") or "", "headRefName": branch})
        except ValueError:
            return False
        return related_issue == str(issue)

    def is_agent_branch(self, pr):
        out = self.gh("pr", "view", str(pr), "-R", self.gh_repo, "--json", "headRefName", "--jq", ".headRefName")
        return bool(names.issue_of((out or "").strip()))

    def pending_openings(self):
        """A previous tick may have created a PR before it recorded the review state."""
        for path in sorted(self.state.glob("pr-opening-*.json")):
            if (stopped := self.stop_if_draining()) is not None:
                return stopped
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
    def pending_reviews(self, only=None):
        """Even after its trigger label was removed, or after Cursor opened a PR: re-enter at the
        saved review/fix step. ONLY restricts this to the checkpoints of these issues."""
        posts = owner_handoff.comment_post_episodes(self.env)
        paths = set(self.state.glob("review-pending-*"))
        paths.update(self.state / f"review-pending-{pr}" for pr in posts)
        for path in sorted(paths):
            if (stopped := self.stop_if_draining()) is not None:
                return stopped
            pr = path.name.removeprefix("review-pending-")
            if ("review", pr) in self.tried or only is not None and self.checkpoint_issue(path) not in only:
                continue
            self.tried.add(("review", pr))
            done = self.after_item(self.item(lambda: self.pending_review(path, posts.get(pr, set()))))
            if done is not None:
                return done
        return None

    @staticmethod
    def checkpoint_issue(path):
        """The issue a saved checkpoint names, "" when it names none or cannot be read."""
        try:
            return str((checkpoint.read(path) or {}).get("ISSUE") or "")
        except (OSError, ValueError):
            return ""

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
                if state == "MERGED":
                    issue = str(saved.get("ISSUE") or "")
                    if issue.isdecimal() and not (self.state / f"task-merged-pr-{pr}").exists():
                        remote = self.gh_json("pr", "view", pr, "-R", self.gh_repo, "--json",
                                              "state,headRefName,baseRefName,isCrossRepository,author,body")
                        if not isinstance(remote, dict):
                            return 0
                        if remote.get("state") == "MERGED" and self.matches_merged_issue(remote, issue):
                            if not self.record_merged_settlement(pr, issue):
                                return 0
                            self.settle_recorded_merge(pr, issue)
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
        if (stopped := self.stop_if_draining()) is not None:
            return stopped
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
        found = owner_handoff.operation_episodes(
            self.env, (owner_handoff.FIX_PUSH, *owner_handoff.FIX_META_WRITES))
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

    def pick_fix(self, only=None):
        """The owner's review comments go to the vendor that changed the fewest lines of the PR and
        can run now (pick_by_contrib), then the review loop runs again. ONLY restricts this to the
        PRs of these issues, and then every one of them gets a turn."""
        if self.force_issue or self.force_review:
            return None
        if (stopped := self.stop_if_draining()) is not None:
            return stopped
        pushes = self.fix_push_episodes()
        for pr, episodes in sorted(pushes.items(), key=lambda pair: int(pair[0])):
            if (stopped := self.stop_if_draining()) is not None:
                return stopped
            if (self.force_pr and self.force_pr != pr) or (self.state / f"review-pending-{pr}").exists():
                continue
            if ("fix", pr) in self.tried or only is not None and not episodes & only:
                continue
            self.tried.add(("fix", pr))
            done = self.attempt(lambda pr=pr, episodes=episodes: self.resume_standalone_push(pr, episodes))
            if done is not None:
                return done
        if self.force_pr in pushes:
            return None
        if self.force_pr:
            head_ref = (self.gh("pr", "view", self.force_pr, "-R", self.gh_repo, "--json", "headRefName",
                                "--jq", ".headRefName") or "").strip()
            found = [(self.force_pr, head_ref)]
        else:
            listed = self.gh_json("pr", "list", "-R", self.gh_repo, "--state", "open", "--label", "agent-fix",
                                  "--limit", "20", "--json", "number,headRefName") or []
            listed = sorted((p for p in listed if (issue := names.issue_of(p["headRefName"]))
                             and (only is None or issue in only)
                             and ("fix", str(p["number"])) not in self.tried
                             and str(p["number"]) not in pushes
                             and not self.comment_post_pending(str(p["number"]))), key=lambda p: p["number"])
            found = [(str(p["number"]), p["headRefName"]) for p in (listed if only is not None else listed[:1])]
        for pr, head_ref in found:
            if (stopped := self.stop_if_draining()) is not None:
                return stopped
            self.tried.add(("fix", pr))
            done = self.attempt(lambda pr=pr, head_ref=head_ref: self.fix_item(pr, head_ref))
            if done is not None:
                return done
        return None

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
        step = tiers.carry(self.env.with_runtime(issue=issue), "fix", issue, pr)   # one read: pick and fix
        fixer = recovery.pending_fix(self.env, episode, pr) or self.pick_vendor(
            "fixer", "contrib", self.all_vendors(), pr=pr, issue=issue, env=step)
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
                         owner_triggered=True, env=step)
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
        waiting_meta = None
        if waiting is None:
            for meta_reason in owner_handoff.FIX_META_WRITES:
                waiting_meta = runner.waiting_meta(pr, meta_reason)
                if waiting_meta is not None:
                    break
        if (waiting is None and waiting_meta is None) or waiting == "unreadable" or waiting_meta == "unreadable":
            if waiting == "unreadable" or waiting_meta == "unreadable":
                pr_helpers.gh("pr", "edit", pr, "-R", self.gh_repo, "--remove-label", "agent-fix")
            return 80
        if waiting is not None:
            binding = waiting["continuation"]
            if len(episodes) != 1 or binding["caller"] != "standalone":
                for key in episodes - {episode}:
                    owner_handoff.clear_retry(self.env, key, owner_handoff.FIX_PUSH, pr)
                runner.handoff(pr, owner_handoff.FIX_PUSH, f"PR #{pr}: pushing fix failed",
                               "The saved push has no matching caller.")
                return self.finish_standalone_fix(pr, "", "", "", issue, 1)
            issue, wt, fixer = binding["issue"], binding["wt"], binding["fixer"]
        else:
            binding = waiting_meta["continuation"]
            issue = binding.get("issue") or issue
            fixer = binding.get("fixer") or "fixer"
            # A description deferral may still hold local commits; prefer its saved worktree.
            wt = binding.get("wt") or ""
            if not wt or not Path(wt).is_dir():
                head_ref = (self.gh("pr", "view", pr, "-R", self.gh_repo, "--json", "headRefName",
                                    "--jq", ".headRefName") or "").strip()
                wt = str(names.worktree_for(self.wt_root, head_ref)) if head_ref else ""
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
        """Resume durable spec reads, comment posts and issue-scoped pushes before new work can be blocked."""
        return eligibility.pending_specs(self.state)

    def resume_spec(self, issue):
        if self.spec_loop(issue):
            return dev.run(self, issue)   # an accept after a resumed post goes straight to dev
        return 80

    def pick_issue(self):
        """Start the next issue: a pending spec step first, then a candidates() one. A tick takes
        only ready-for-agent issues; python -m baton N takes one of any milestone, and also a
        claude-found one without that label. A serve assignment (--issue N / --no-new-issue) keeps
        scheduled semantics and may only start N (or nothing); p0_pass still may start a P0."""
        if self.force_pr or self.force_review:
            return None
        if (stopped := self.stop_if_draining()) is not None:
            return stopped
        pending = self.pending_specs()
        done = self.resume_specs()
        if done is not None:
            return done
        if self.force_issue and self.force_issue in pending:
            return None
        if self.force_issue:
            issue = self.forced_issue()
            return self.start(issue) if issue else None
        assign = self.env.runtime.assign_issue
        if self.env.runtime.no_new_issue:
            self.housekeep_admission()
            return None
        if assign:
            if (stopped := self.stop_if_draining()) is not None:
                return stopped
            self.housekeep_admission()
            return self.start(assign) if self.assigned_ready(assign) else None
        # A deferred issue waits alone (ADR 0002): the tick goes on to the next candidate, through
        # the whole admission check again, since the deferred one may have opened a PR (#323).
        while True:
            if (stopped := self.stop_if_draining()) is not None:
                return stopped
            issue = self.next_issue()
            if not issue:
                return None
            done = self.start(issue)
            if done is not None:
                return done

    def housekeep_admission(self):
        """The writes that come before a new issue: release stale agent-wip, hand off stalled PRs,
        drop stale slot markers. Returns (the cap count, the open PRs' branches), or None, logged,
        when GitHub cannot be listed."""
        wip = eligibility.list_wip_numbers(self.gh_json, self.gh_repo)
        if wip is None:
            self.log("could not list open agent-wip issues; not starting new work")
            return None
        prs = self.open_prs()
        if prs is None:
            return None
        branches = {p["headRefName"] for p in prs}
        # An agent-wip issue without an open PR holds no slot: release it, or leave it uncounted.
        for issue in wip:
            if not branches.intersection(names.branches(issue)):
                self.release_stale(issue)
        wip = [issue for issue in wip if branches.intersection(names.branches(issue))]
        # A PR waiting for the owner uses no agent capacity; it must not hold up the queue.
        with_owner = eligibility.with_owner_issues(prs)
        with_owner |= self.hand_off_stalled(set(wip), prs, with_owner)
        slots = eligibility.counted_wip(prs, wip, self.slot_markers(), with_owner)
        for pr in set(slots.markers) - {slots.held[n] for n in slots.deferred}:
            (self.state / f"slot-deferred-{pr}").unlink(missing_ok=True)
        return slots, branches

    def assigned_ready(self, issue):
        """Whether the serve-assigned ISSUE may start now (eligibility + vendor), else False."""
        cap = int(self.env.config.scheduling.max_pending_prs or 1)
        result = eligibility.eligible_candidates(
            self.gh_json, self.gh_repo, self.state,
            milestone=self.env.repo.agent_milestone, owner=self.owner, cap=cap, only={issue},
            log=self.log)
        if result is None:
            return False
        candidates, _ = result
        if not candidates or str(candidates[0].number) != str(issue):
            return False
        self.priorities[str(issue)] = candidates[0].priority
        prs = self.open_prs()
        if prs is None:
            return False
        return next(self.runnable([candidates[0].number], {p["headRefName"] for p in prs}), "") == str(issue)

    def resume_specs(self, only=None):
        """The pending_specs() entries that are due; ONLY restricts them to these issues."""
        pending = self.pending_specs()
        for issue in sorted(pending, key=int):
            if (stopped := self.stop_if_draining()) is not None:
                return stopped
            if self.force_issue and self.force_issue != issue:
                continue
            if ("issue", issue) in self.tried or only is not None and issue not in only:
                continue
            reason = pending[issue]
            accept_post = reason in owner_handoff.SPEC_ACCEPT_POSTS
            write_or_push = reason in (*owner_handoff.SPEC_WRITE_POSTS, *owner_handoff.ISSUE_PUSHES,
                                       owner_handoff.SPEC_ADR_MERGE)
            if write_or_push:
                try:
                    post = owner_handoff.load_retry(self.env, issue, reason)
                except owner_handoff.RetryUnreadable:
                    post = None  # Let the spec/dev path handle damaged state.
                adr_update = (post and reason == owner_handoff.SPEC_PENDING_ACCEPT_POST
                              and merge.Merger(self.env, None, post["continuation"]["pr"], issue).has_update())
                if (post and (post.get("reads") is not None
                              or (accept_post or not post.get("confirmed"))
                              and post["attempts"] < owner_handoff.MAX_ATTEMPTS)
                        and owner_handoff.clock() < post["due"]
                        and not post.get("phase") and not adr_update):
                    if self.force_issue:
                        return self.defer("handoff-retry", reason)
                    continue
            if not accept_post:
                data = self.gh_json("issue", "view", issue, "-R", self.gh_repo, "--json", "state,labels")
                if not isinstance(data, dict) or data.get("state") not in ("OPEN", "CLOSED"):
                    continue
                if data["state"] == "CLOSED":
                    if reason == owner_handoff.SPEC_ADR_MERGE and post:
                        merge.Merger(self.env, None, post["continuation"]["pr"], issue).clear_update()
                    owner_handoff.clear_episode(self.env, issue)
                    continue
                labels = data.get("labels")
                if not isinstance(labels, list) or any(not isinstance(label, dict)
                                                      or label.get("name") == "ready-for-human" for label in labels):
                    continue
            self.tried.add(("issue", issue))
            if reason in ("spec_pending_adr_read_failed", *owner_handoff.SPEC_WRITE_POSTS,
                          owner_handoff.SPEC_ADR_PUSH, owner_handoff.SPEC_ADR_MERGE):
                done = self.attempt(lambda: self.resume_spec(issue))
            else:
                done = self.attempt(lambda: dev.run(self, issue))
            if done is not None:
                return done
        return None

    def forced_issue(self):
        labels = self.gh_json("issue", "view", self.force_issue, "-R", self.gh_repo, "--json", "labels,author,blockedBy") or {"labels": []}
        label_names = [label["name"] for label in labels["labels"]]
        self.priorities[self.force_issue] = priority_of(label_names)
        if "ready-for-agent" not in label_names and "claude-found" not in label_names:
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
        return eligibility.owner_ready(self.gh_json, self.gh_repo, issue, self.owner)

    def pr_history(self, issue):
        """Every PR of the issue's agent branches (same repository, base main), any state, or None
        when any lookup fails: a partial history is never used."""
        return pr_helpers.issue_history(self.gh_repo, issue)

    def release_stale(self, issue):
        """An agent-wip issue with no open PR: remove the label unless its newest PR is a merge
        nobody settled yet (merged work must not be developed again). A failure leaves the label."""
        history = self.pr_history(issue)
        if history is None:
            self.log(f"#{issue}: PR history unreadable; agent-wip not released, retrying next tick")
            return
        newest = max(history, key=lambda pr: int(pr["number"]), default=None)
        if newest and newest.get("merged_at") and not (self.state / f"task-merged-pr-{newest['number']}").exists():
            pr = str(newest["number"])
            if not self.matches_merged_issue(newest, issue):
                self.log(f"#{issue}: merged PR #{pr} is not settled (identity mismatch); agent-wip kept")
                return
            if not self.record_and_settle_merge(pr, issue):
                self.log(f"#{issue}: merged PR #{pr} is not settled; agent-wip kept")
            return
        if self.gh("issue", "edit", issue, "-R", self.gh_repo, "--remove-label", "agent-wip") is None:
            self.log(f"#{issue}: agent-wip not released (label removal failed), retrying next tick")
            return
        self.log(f"#{issue}: released agent-wip (no open PR)")

    def next_issue(self):
        admission = self.housekeep_admission()
        if admission is None:
            return ""
        (pending, deferred, held, markers), branches = admission
        cap = int(self.env.config.scheduling.max_pending_prs or 1)
        # P0 skips the cap through p0_pass; here every new issue counts as unprioritised (4).
        if not eligibility.cap_allows(4, pending, deferred, cap):
            why = []
            for n in pending:
                if n in deferred:
                    why.append(f"#{n}: PR #{held[n]} deferred ({markers[held[n]]})")
                else:
                    why.append(f"#{n}: PR #{held[n]} in review")
            limit = f"total limit {cap + 1} reached" if len(pending) >= cap + 1 else f"{len(pending) - len(deferred)} not deferred"
            self.log(f"{len(pending)} issue(s) still waiting on review (agent-wip), cap {cap}, {len(deferred)} deferred, "
                     f"{limit}; not starting new work: " + "; ".join(why))
            return ""
        return next(self.runnable(self.candidates(), branches), "")

    def open_prs(self):
        """Every open PR (number, headRefName, labels), or None, logged, when they cannot be listed."""
        prs = eligibility.list_open_prs(self.gh_json, self.gh_repo)
        if prs is None:
            self.log("could not list open PRs; not starting new work")
        return prs

    def runnable(self, candidates, branches):
        """The CANDIDATES that can start now, in order: no open PR on BRANCHES, nothing pending,
        not tried already in this tick, and a vendor for their next step."""
        from . import recovery

        waiting = self.pending_specs()
        for n in candidates:
            if str(n) in waiting or any(names.issue_of(b) == str(n) for b in branches):
                continue   # already has a PR
            # These issues can be at different steps: one whose next step no vendor can run now must
            # not block another (revision needs a pool vendor).
            issue = str(n)
            if ("issue", issue) in self.tried:
                continue
            self.tried.add(("issue", issue))
            try:
                payload = spec.Spec(self.env, self.hooks(issue), issue).issue_payload()
            except Deferred as deferred:
                self.defer(deferred.vendor, deferred.reason)
                continue
            if payload is None:
                continue
            if not (spec.accept_pending(self.env, issue, listed=payload["comments"])
                    or recovery.path(self.env, issue, issue).exists()):
                role = spec.role_from(payload["comments"], self.env)
                vendor_candidates = list(self.env.config.pool()) if role == "reviser" else self.all_vendors()
                try:
                    self.pick_vendor(role, "contrib", vendor_candidates, issue=issue)
                except Deferred as deferred:
                    self.defer(deferred.vendor, deferred.reason)
                    continue
            self.recovered_issues[issue] = payload
            yield issue

    PIPELINE_LABELS = frozenset({"ready-for-human", "agent-review", "agent-fix"})

    def has_saved_state(self, pr, issue):
        """Whether any saved state says the pipeline still holds this PR."""
        from . import recovery
        state = self.state
        if any((state / f"{name}-{pr}").exists()
               for name in ("review-pending", "fix-push-waiting", "slot-deferred", "ready-label-pending")):
            return True
        if pr in owner_handoff.operation_episodes(self.env, owner_handoff.RETRY_REASONS):
            return True
        return recovery.path(self.env, issue, issue).exists() or any(state.glob(f"recovery-*-{pr}.json"))

    def hand_off_stalled(self, wip, prs, with_owner):
        """An agent-wip issue whose only open PR carries no pipeline label and no saved state is stuck
        (#265): hand the PR off so the owner sees it. Returns the issues handed off."""
        by_issue = {}
        for p in prs:
            issue = names.issue_of(p["headRefName"])
            if issue in wip:
                by_issue.setdefault(issue, []).append(p)
        stalled = set()
        for issue, found in sorted(by_issue.items()):
            if len(found) != 1 or issue in with_owner:
                continue
            p, pr = found[0], str(found[0]["number"])
            if self.PIPELINE_LABELS & {label.get("name") for label in p.get("labels") or []}:
                continue
            if self.has_saved_state(pr, issue):
                continue
            self.log(f"#{issue}: PR #{pr} has no pipeline label and no saved state; handing it off")
            owner_handoff.perform(
                self.env, "pr_stalled", kind="pr", number=pr, item=pr, episode=issue,
                title=f"PR #{pr} stalled",
                text=f"PR #{pr} for #{issue} has no pipeline label and nothing saved to resume it.",
                hooks=self.hooks(issue, pr))
            stalled.add(issue)
        return stalled

    def candidates(self, only=None):
        """The owner's ready-for-agent is what makes an issue a candidate, whoever opened it;
        AGENT_MILESTONE says "wanted now" ("" for any milestone), which the scan's claude-found
        issues do not need. Priority labels only order the candidates (ADR 0010). ONLY restricts
        them to these issue numbers. Shared with the serve supervisor (eligibility.ready_candidates)."""
        ready = eligibility.ready_candidates(
            self.gh_json, self.gh_repo, milestone=self.env.repo.agent_milestone, owner=self.owner,
            only=only, log=self.log)
        if ready is None:
            return []
        for candidate in ready:
            self.priorities[str(candidate.number)] = candidate.priority
        return [candidate.number for candidate in ready]

    def p0_issues(self):
        """The open issues labelled P0 (the cheap probe); none when the listing fails."""
        listed = self.gh_json("issue", "list", "-R", self.gh_repo, "--state", "open", "--label", "P0", "--limit", "1000",
                              "--json", "number,labels")
        if not isinstance(listed, list):
            self.log("could not list open P0 issues; no P0 preemption this tick")
            return set()
        return {str(i["number"]) for i in listed
                if priority_of(label["name"] for label in i.get("labels") or []) == 0}

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
        if not self.scanners():
            self.log("scan skipped: no vendor with scan role")
            return None
        self.item(self.scan_item)
        return 0

    def scanners(self):
        """Vendors holding the scan role whose adapter can scan."""
        return tuple(v for v in self.env.config.for_role("scan") if agents.ADAPTERS[v].SCAN)

    def scan_item(self):
        scanners = self.scanners()
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
    """One tick over TICKS (one per repository, in configured order): the runner-wide housekeeping
    once, then each repository's work in turn, then the scan, each under its own lock."""
    first = ticks[0]
    config, runner_state, manual = first.env.config, first.runner_state, bool(first.manual)
    if first.scan_cmd == "scan" and first.scan_arg == "list":
        for i, area in enumerate(first.scan_areas):
            print(i, area)
        return 0
    paused = config.scheduling.paused is True and not manual
    admitted = maintenance.admit(
        runner_state, None, acquire=False,
        on_expired=lambda: notify.append(config.paths.logs, "maintenance expired"),
    )
    if admitted.reason is not None:
        notify.append(config.paths.logs, f"maintenance ({admitted.reason})")
        return 0
    if paused:
        notify.append(config.paths.logs, "paused")
        return 0
    first.hooks().tick(manual)
    try:
        housekeeping(first)
        return run_one(first) if len(ticks) == 1 else walk(ticks)
    except Stop as stop:
        return stop.rc


def housekeeping(first):
    """The runner-wide chores, skipped when another tick is doing them."""
    config, runner_state = first.env.config, first.runner_state
    lock = first.take(runner_state / maintenance.HOUSEKEEPING_LOCK)
    if lock is None:
        return
    with lock:
        from .initialization import expire_context
        expire_context(config, first.clock())
        try:
            with session_bound(first.env):
                advisor.digest(first.env)   # the daily owner digest of advisor decisions (#17)
        except Exception as error:   # a digest never stops the tick
            notify.append(config.paths.logs, f"advisor digest failed: {type(error).__name__}")
        # Upgrade the old global cooldown as Claude-only; retain today's Claude counter.
        old, new = runner_state / "cooldown-until", runner_state / "claude-cooldown-until"
        with statefile.locked(runner_state):   # a cooldown written in between would be overwritten
            if old.is_file() and not new.exists():
                old.rename(new)


def run_one(t):
    """The only repository of this tick (the one configured, or a command's)."""
    lock = t.take_repository()
    if lock is None:
        return 0
    with lock:
        if t.scan_cmd != "scan":
            return t.pass_()
        scan_lock = t.take(t.runner_state / maintenance.SCAN_LOCK)
        if scan_lock is None:
            t.log_busy()
            return 0
        with scan_lock:
            return t.pass_()


def walk(ticks):
    """Several repositories in turn; a locked one is another tick's and is left to it."""
    first = ticks[0]
    try:
        last = (first.runner_state / "last-repo").read_text().strip()
    except OSError:
        last = ""
    by_name = {t.gh_repo: t for t in ticks}
    turn = [by_name[name] for name in rotation(by_name, last)]
    locked = set()

    def visit(t, step):
        lock = t.take_repository()
        if lock is None:
            locked.add(t.gh_repo)
            return None
        with lock, session_bound(t.env):
            return step()

    for t in turn:
        rc = visit(t, t.work)
        if rc is not None:
            return rc
    if scan_lock := first.take(first.runner_state / maintenance.SCAN_LOCK):
        with scan_lock:
            for t in turn:
                if t.gh_repo in locked:   # it has another tick's work
                    continue
                rc = visit(t, t.scan_fallback)
                if rc is not None:
                    return rc
    if not locked:
        notify.append(first.env.config.paths.logs, "no work in any repository")
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
