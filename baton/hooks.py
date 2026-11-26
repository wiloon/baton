"""The runner's telemetry hooks (phase 4 prerequisite, #276), ported from telemetry.sh.

Best-effort: a hook never fails its caller and prints nothing but what bash reads.
They record into the ledger through bin/telemetry.py, which is unchanged. The context a
hook needs is explicit: issue, PR and round come from the caller, and the two values
that outlive a call (the task's pipeline id, the last run's id) are kept in the item
file named by BATON_TELEMETRY_ITEM, created by begin_item and removed by finish_item.
"""

from . import agents, pr as pr_helpers, session
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

SHA = re.compile(r"[a-f0-9]{40,64}")


@dataclass
class Run:
    run_id: str
    trace_id: str
    span_id: str
    issue: int
    pr: int
    pipeline_id: str = ""


def number(value):
    return int(value) if str(value).isdecimal() else 0


class Telemetry:
    def __init__(self, self_dir, state, repo, item=None, issue=0, pr=0, round=0):
        self.dir, self.state, self.repo = Path(self_dir), str(state), repo
        self.item = Path(item) if item else None
        self.issue, self.pr, self.round = number(issue), number(pr), number(round)

    @property
    def enabled(self):
        return (self.dir / "telemetry-enabled").is_file()

    def _call(self, *args, input=None):
        """telemetry.py's stdout, or None when it failed."""
        try:
            result = session.run_process([sys.executable, str(self.dir / "telemetry.py"), "--runner-config", str(session.resolve().config.source), "--state", self.state, *map(str, args)],
                                    input=input, text=True, capture_output=True, check=False)
        except OSError:
            return None
        return result.stdout if result.returncode == 0 else None

    # --- what survives between calls of one item ---
    def _item(self):
        try:
            return json.loads(self.item.read_text())
        except (OSError, ValueError, AttributeError):
            return {}

    def _remember(self, **values):
        if self.item:
            try:
                self.item.write_text(json.dumps({**self._item(), **values}))
            except OSError:
                pass

    # --- hooks ---
    def begin(self, vendor, role, minutes, requested_model=""):
        """Open a run record owned by this process; None when disabled or refused."""
        if not self.enabled:
            return None
        out = self._call("begin", vendor, role, os.getpid(), minutes, self.issue, self.pr, self.round,
                         "--requested-model", requested_model,
                         "--repo", self.repo)
        fields = (out or "").split()
        if len(fields) < 3:
            return None
        pipeline = fields[3] if len(fields) > 3 else ""
        if pipeline:
            self._remember(pipeline_id=pipeline)
        return Run(*fields[:3], issue=self.issue, pr=self.pr, pipeline_id=pipeline)

    def start_attempt(self):
        self._remember(attempt_id=str(uuid.uuid4()), started_ms=int(time.time() * 1000))

    def finish_attempt(self, result):
        item = self._item()
        if self.enabled and all(k in item for k in ("pipeline_id", "attempt_id", "started_ms")):
            self._call("attempt", item['pipeline_id'], item['attempt_id'], item['started_ms'], result)

    def end(self, run, status, log):
        if run:
            self._call("end", run.run_id, status, log)
            self._remember(last_run_id=run.run_id)

    def tick(self, manual=False):
        if self.enabled and not manual:
            self._call("tick")

    def task(self, kind, key, *options):
        """One task event; options are telemetry.py's own (bounded codes only)."""
        if not self.enabled:
            return
        pipeline = self._item().get("pipeline_id", "")
        args = ["task", kind, "--repo", self.repo, "--issue", self.issue, "--pr", self.pr, "--key", key]
        if pipeline:
            args += ["--pipeline", pipeline]
        out = (self._call(*args, *options) or "").strip()
        if out:
            self._remember(pipeline_id=out)

    def owner_merged(self):
        """The owner merged this PR: its latest episode, found by the PR alone, ends merged.
        False when nothing was recorded and a later tick should try again."""
        if not (self.enabled and self.pr):
            return False
        return self._call("task", "merged", "--repo", self.repo, "--pr", self.pr, "--key", "merged",
                          "--merged-by", "owner") is not None

    def last_run_id(self):
        return self._item().get("last_run_id", "")

    def verdict(self, kind, vendor, head, round, outcome, reply, review_file="", *, run_id=None, author_agent=""):
        verdict = next((v for prefix, v in (("REVIEW: pass", "pass"), ("REVIEW: changes", "changes"),
                                            ("SPEC: accept", "accept"), ("SPEC: changes", "changes"),
                                            ("SPEC: reject", "reject")) if outcome.startswith(prefix)), "")
        if not verdict:
            return
        round = number(round)
        options = ["--verdict", verdict, "--agent", vendor, "--round", round, "--head-sha", head]
        if kind == "spec_review":
            options += ["--author-agent", author_agent or "unknown"]
        if last := (self.last_run_id() if run_id is None else run_id):
            options += ["--run-id", last]
        if kind in ("review", "screenshot_review"):   # a count only (#227): the review text never leaves this function
            text = Path(review_file).read_text() if review_file and Path(review_file).is_file() \
                and Path(review_file).stat().st_size else reply
            must = (self._call("must-fix", input=text) or "").strip()
            if must.isdecimal():
                options += ["--must-fix", must]
        self.task(kind, f"{kind}:{round}:{head}", *options)

    def fix(self, fixer, round, reply, reply_file=""):
        last = self._item().get("last_run_id")
        if not (self.enabled and last):
            return
        round = number(round)
        text = Path(reply_file).read_text() if reply_file and Path(reply_file).is_file() \
            and Path(reply_file).stat().st_size else reply
        answers = (self._call("fix-answers", input=text) or "").split()
        if len(answers) != 2 or not all(a.isdecimal() for a in answers):
            return
        self.task("fix", f"fix:{round}:{last}", "--agent", fixer, "--round", round,
                  "--adopted", answers[0], "--rejected", answers[1], "--run-id", last)

    def ci(self, pr, rc, before, no_checks=False, duration_ms=None):
        if not self.enabled:
            return
        repo = session.resolve().repo.gh_repo
        after, checks = "", ""
        try:
            after = pr_helpers.gh("pr", "view", str(pr), "-R", repo, "--json", "headRefOid", "--jq", ".headRefOid",
                                  limit=15).stdout.strip()
            checks = pr_helpers.gh("pr", "checks", str(pr), "-R", repo, "--json", "bucket", limit=15).stdout
        except OSError:
            pass
        result = (self._call("ci-result", rc, input=checks) or "unknown").strip() or "unknown"
        if no_checks:
            result = "no_checks"
        # A changed head cannot attribute the watched result to either revision.
        if before != after or not SHA.fullmatch(before):
            result = "unknown"
        options = ["--ci-result", result]
        if SHA.fullmatch(before):
            options += ["--head-sha", before]
        if duration_ms is not None:
            options += ["--duration-ms", duration_ms]
        self.task("ci", f"ci:{pr}:{before or 'unknown'}:{result}", *options)

    # --- the CLI child's exporters: set on that child only ---
    def child_env(self, vendor, role, run):
        if not run:
            return {}
        return agents.ADAPTERS[vendor].telemetry_env(role, run)


@session.scoped
def from_env(env=None, **context):
    env = session.resolve(env)
    return Telemetry(str(env.config.paths.self_dir), str(env.config.paths.state), env.repo.gh_repo,
                     env.runtime.telemetry_item, **context)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", default="0")
    parser.add_argument("--pr", default="0")
    parser.add_argument("--round", default="0")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tick")
    task = sub.add_parser("task")
    task.add_argument("kind")
    task.add_argument("key")
    task.add_argument("options", nargs=argparse.REMAINDER)
    for name, extra in (("verdict", ("kind", "vendor", "head", "round_")), ("fix", ("fixer", "round_")),
                        ("ci", ("ci_pr", "rc", "before"))):
        cmd = sub.add_parser(name)
        for arg in extra:
            cmd.add_argument(arg)
        cmd.add_argument("file", nargs="?", default="")
        cmd.add_argument("--outcome", default="")
        cmd.add_argument("--no-checks", action="store_true")
    a = parser.parse_args(argv)
    hooks = from_env(issue=a.issue, pr=a.pr, round=a.round)
    if a.command == "tick":
        hooks.tick(bool(session.resolve().runtime.manual))
    elif a.command == "task":
        hooks.task(a.kind, a.key, *a.options)
    elif a.command == "verdict":
        hooks.verdict(a.kind, a.vendor, a.head, a.round_, a.outcome, sys.stdin.read(), a.file)
    elif a.command == "fix":
        hooks.fix(a.fixer, a.round_, sys.stdin.read(), a.file)
    else:
        hooks.ci(a.ci_pr, a.rc, a.before, a.no_checks)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # best-effort: telemetry never fails the runner
