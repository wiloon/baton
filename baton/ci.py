"""A PR's CI for the bash runner (migration slice 3a, #263): wait for it, and decide
whether a red head is a code-only failure an agent may repair once (#243).

`gh` and `timeout` are called as the bash helpers called them, so the exit codes bash
hands to telemetry (124 for a timeout) are unchanged.
"""

from . import session
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

FEEDBACK_MAX = 60000   # render's FEEDBACK is one argv element (Linux limit: 128 KiB)
LOG_LINES = 200


def run(*cmd):
    """(exit code, stdout and stderr together)."""
    result = session.run_process(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return result.returncode, result.stdout


def watch(pr, minutes):
    return run("timeout", f"{minutes}m", "gh", "pr", "checks", str(pr), "-R", session.resolve().repo.gh_repo,
               "--watch", "--fail-fast", "-i", "30")


def green(pr, minutes, sleep=time.sleep):
    """CI passed, or the PR triggers no checks (docs-only paths). Right after a push
    the checks may not be registered yet, so "none" counts only on a second look a
    minute later. rc is the last watch's exit code, for telemetry."""
    rc, out = watch(pr, minutes)
    if rc == 0:
        return {"green": True, "rc": 0, "no_checks": False}
    if "no checks reported" not in out:
        return {"green": False, "rc": rc, "no_checks": False}
    sleep(60)
    rc, out = watch(pr, minutes)
    if rc == 0:
        return {"green": True, "rc": 0, "no_checks": False}
    if "no checks reported" in out:
        return {"green": True, "rc": rc, "no_checks": True}
    return {"green": False, "rc": rc, "no_checks": False}


def fixable(checks, allowed):
    """Every failed check is a code check (ALLOWED names) and nothing is still running."""
    return (bool(checks) and any(c.get("bucket") == "fail" for c in checks)
            and all(c.get("bucket") in ("pass", "skipping")
                    or (c.get("bucket") == "fail" and c.get("name") in allowed) for c in checks))


def classify(pr, state, allowed):
    """The repair feedback for PR's red head, or None when the owner should get it:
    infrastructure and ambiguous failures, or a head already repaired once."""
    repo = session.resolve().repo.gh_repo
    result = session.run_process(["gh", "pr", "view", str(pr), "-R", repo, "--json", "headRefOid", "--jq", ".headRefOid"],
                            text=True, capture_output=True, check=False)
    head = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[a-f0-9]{40,64}", head):
        return None
    if Path(state, f"ci-fix-{pr}-{head}").exists():
        return None
    # green() stops at the first failure (--fail-fast); classify only once every check
    # has finished, or a slow image build still pending would hide behind a fast lint.
    run("timeout", "45m", "gh", "pr", "checks", str(pr), "-R", repo, "--watch", "-i", "30")
    # `gh pr checks` may exit 1 while still returning valid JSON: read its JSON, not its exit.
    result = session.run_process(["gh", "pr", "checks", str(pr), "-R", repo, "--json", "name,bucket,link"],
                            text=True, capture_output=True, check=False)
    try:
        checks = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(checks, list) or not fixable(checks, allowed):
        return None
    feedback = f"CI code checks failed on PR #{pr} at {head}. Fix only these errors:\n"
    for check in checks:
        if check.get("bucket") != "fail":
            continue
        found = re.search(r"/actions/runs/(\d+)(?:/job/(\d+))?", check.get("link") or "")
        if not found:
            return None
        run_id, job_id = found[1], found[2]
        # --job keeps two failed jobs of one run from each getting both logs.
        rc, logs = run("gh", "run", "view", run_id, *(["--job", job_id] if job_id else []), "-R", repo,
                       "--log-failed")
        logs = logs.rstrip("\n")
        if rc != 0 or not logs:
            return None
        tail = "\n".join(logs.split("\n")[-LOG_LINES:])
        feedback += f"\n{check['name']} (run {run_id}):\n{tail}\n"
    return {"head": head, "feedback": feedback[:FEEDBACK_MAX]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("green")
    cmd.add_argument("pr")
    cmd.add_argument("--minutes", type=int, required=True)
    sub.add_parser("classify").add_argument("pr")
    args = parser.parse_args(argv)
    if args.command == "green":
        result = green(args.pr, args.minutes)
        print(json.dumps(result))
        return 0 if result["green"] else 1
    allowed = list(session.resolve().repo.ci_fixable_checks)
    result = classify(args.pr, str(session.resolve().dirs.state), allowed)
    if result is None:
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ci: {exc}", file=sys.stderr)
        sys.exit(1)
