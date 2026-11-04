"""GitHub PR helpers for the bash runner (migration slice 3a, #263).

Each command reads the PR through `gh` (its own auth, `-R GH_REPO`) with the same
calls the bash helpers made, and keeps their exit codes. Commands that change the PR
print JSON: `changed`, and `log` for a runner.log line that bash writes.
"""

from . import session
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from . import pr_contract

TASK_LINE = re.compile(r"TASK: (partial|complete)")
# Footers a coding tool appends to whatever section ends the body.
ATTRIBUTION = re.compile(r"(🤖 Generated with |co-authored-by:)", re.IGNORECASE)


def gh(*args, input=None):
    return session.run_process(["gh", *args], input=input, text=True, capture_output=True, check=False)


def view(pr, fields):
    """`gh pr view PR --json FIELDS` as a dict; None when gh fails."""
    result = gh("pr", "view", str(pr), "-R", session.resolve().repo.gh_repo, "--json", fields)
    return json.loads(result.stdout) if result.returncode == 0 else None


def body(pr):
    """The PR description without trailing newlines, as `$(gh ... --jq .body)` gave it;
    None when gh fails."""
    result = gh("pr", "view", str(pr), "-R", session.resolve().repo.gh_repo, "--json", "body", "--jq", ".body")
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def edit_body(pr, text, directory, prefix):
    """Replace the PR description through a file, as the bash helpers did."""
    with tempfile.NamedTemporaryFile("w", dir=directory, prefix=prefix, delete=False) as file:
        file.write(text)
    try:
        return gh("pr", "edit", str(pr), "-R", session.resolve().repo.gh_repo, "--body-file", file.name).returncode == 0
    finally:
        os.unlink(file.name)


def details(pr, owner, cursor_login):
    """Branch, review route, issue and completion of PR (empty when absent)."""
    data = view(pr, "headRefName,author,isCrossRepository,state,isDraft,body")
    if data is None:
        raise ValueError(f"cannot read PR #{pr}")
    issue, completion = pr_contract.relation(data)
    return {"branch": data.get("headRefName", ""), "route": pr_contract.route(data, owner, cursor_login),
            "issue": issue, "completion": completion}


def relation(pr):
    data = view(pr, "headRefName,body")
    if data is None:
        raise ValueError(f"cannot read PR #{pr}")
    issue, completion = pr_contract.relation(data)
    return {"issue": issue, "completion": completion}


def preflight(pr):
    """Check the live PR again before merging: legacy or externally edited bodies may
    contain closing keywords even after a reviewer accepted the code. Raises ValueError."""
    data = view(pr, "headRefName,body,commits,closingIssuesReferences")
    if data is None:
        raise ValueError(f"cannot read PR #{pr}")
    pr_contract.preflight(data)


def apply_body(pr, request, state, ctx):
    """Apply an agent's PR description request (file REQUEST) with the runner-owned task
    relation. Returns (rc, changed): rc 0 done or no request, 1 GitHub failed (retry),
    2 invalid request. changed: the body differs from the one before the first attempt,
    so a retry after a write whose response was lost still counts the change."""
    path = Path(request)
    if not path.exists() and not path.is_symlink():
        return 0, False
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        return 2, False
    current = view(pr, "headRefName,body")
    if current is None:
        return 1, False
    try:
        issue, completion = pr_contract.relation(current)
        if not re.fullmatch(r"\d+", issue) or completion not in ("partial", "complete"):
            return 2, False
        prepared = pr_contract.prepare(path.read_text(), issue, completion)
    except ValueError:
        return 2, False
    before = (current.get("body") or "").rstrip("\n")
    baseline = Path(state, f"pr-body-base-{pr}")
    if not baseline.exists():
        baseline.write_text(before + "\n")
    if prepared.rstrip("\n") != before and not edit_body(pr, prepared, ctx, f"pr-{pr}-body-prepared."):
        return 1, False
    return 0, prepared.rstrip("\n") != baseline.read_text().rstrip("\n")


def apply_task(pr, reply, ctx):
    """A fixer that agrees with a reviewer about the task completion ends its reply with
    a `TASK: partial|complete` line of its own; rewrite the PR's task relation (Refs/
    Closes) to match. Returns (rc, changed, log): rc 1 GitHub failed, 2 invalid relation."""
    wanted = [m[1] for line in reply.split("\n") if (m := TASK_LINE.fullmatch(line))]
    if not wanted:
        return 0, False, ""
    want = wanted[-1]
    current = view(pr, "headRefName,body")
    if current is None:
        return 1, False, ""
    try:
        issue, completion = pr_contract.relation(current)
        if not re.fullmatch(r"\d+", issue):
            return 2, False, ""
        if completion == want:
            return 0, False, ""
        prepared = pr_contract.retarget((current.get("body") or "") + "\n", issue, want)
    except ValueError:
        return 2, False, ""
    if not edit_body(pr, prepared, ctx, f"pr-{pr}-task."):
        return 1, False, ""
    return 0, True, (f"PR #{pr}: task completion {completion} -> {want} at the fixer's request; "
                     "the next review checks it")


def checks_file(ctx, pr):
    return Path(ctx, f"pr-{pr}-fix-checks.json")


def split_checks(pr, reply, ctx):
    """REPLY without its `## Manual checks` block, which is kept for apply_checks (#155).
    The file outlives a deferred Cursor fix's resume."""
    checks, rest = pr_contract.split_checks(reply)
    checks_file(ctx, pr).write_text(json.dumps({"checks": checks}) + "\n")
    return rest


def fixer_gave_checks(ctx, pr):
    """True when the fixer's reply held a `## Manual checks` block, saved by split_checks."""
    try:
        return json.loads(checks_file(ctx, pr).read_text())["checks"] is not None
    except (OSError, ValueError, KeyError, TypeError):
        return False


def apply_checks(pr, ctx):
    """After a fix, the fixer's re-check of the whole PR replaces the body's
    `## Manual checks`; none given reads "Not re-checked", which the merge gate hands
    off. Returns (rc, changed, log): rc 1 while GitHub may still hold the old section."""
    file = checks_file(ctx, pr)
    try:
        checks = json.loads(file.read_text())["checks"]
    except (OSError, ValueError, KeyError, TypeError):
        checks = None
    current = body(pr)
    if current is None:
        return 1, False, ""
    updated = pr_contract.set_checks(current, checks).rstrip("\n")
    changed = updated != current
    if changed:
        result = gh("pr", "edit", str(pr), "-R", session.resolve().repo.gh_repo, "--body-file", "-", input=updated + "\n")
        if result.returncode != 0:
            return 1, False, ""
    file.unlink(missing_ok=True)
    return 0, changed, f"PR #{pr}: Manual checks refreshed after the fix" if changed else ""


def checks_none(text):
    """True when a body has one `## Manual checks` section and it says exactly none:
    its one non-empty line, tool attribution lines aside, is "none" or "none." (#265)."""
    on, sections, lines = False, 0, []
    for line in text.split("\n"):
        if line.startswith("## "):
            on = line.startswith("## Manual checks")
            sections += on
        elif on:
            lines.append(line)
    answer = [line for line in (re.sub(r"^[-*\s]+", "", line.replace("\r", "")) for line in lines)
              if line and not ATTRIBUTION.match(line)]
    return sections == 1 and len(answer) == 1 and bool(re.fullmatch(r"none\.?", answer[0].lower()))


def gate(pr):
    """After green CI: merge, hand off for a manual check, or queue a metadata review
    when the live description would close the wrong issue (preflight)."""
    current = body(pr)
    if current is None or not checks_none(current):
        return {"action": "manual_checks"}
    try:
        preflight(pr)
    except ValueError as error:
        return {"action": "queue_review", "reason": str(error)}
    return {"action": "merge"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command, names in (("details", ("pr",)), ("relation", ("pr",)), ("preflight", ("pr",)),
                           ("apply-body", ("pr", "request")), ("apply-task", ("pr",)),
                           ("split-checks", ("pr",)), ("apply-checks", ("pr",)),
                           ("checks-none", ("pr",)), ("gate", ("pr",))):
        cmd = sub.add_parser(command)
        for name in names:
            cmd.add_argument(name)
    args = parser.parse_args(argv)
    env = session.resolve()
    if args.command == "details":
        print(json.dumps(details(args.pr, env.owner,
                                 env.config.scheduling.cursor_login_re or r"^(app/cursor|cursor(\[bot\])?)$")))
    elif args.command == "relation":
        print(json.dumps(relation(args.pr)))
    elif args.command == "preflight":
        try:
            preflight(args.pr)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
    elif args.command == "apply-body":
        rc, changed = apply_body(args.pr, args.request, str(env.dirs.state), str(env.dirs.ctx))
        print(json.dumps({"changed": changed}))
        return rc
    elif args.command in ("apply-task", "apply-checks"):
        if args.command == "apply-task":
            rc, changed, log = apply_task(args.pr, sys.stdin.read(), str(env.dirs.ctx))
        else:
            rc, changed, log = apply_checks(args.pr, str(env.dirs.ctx))
        print(json.dumps({"changed": changed, "log": log}))
        return rc
    elif args.command == "split-checks":
        print(split_checks(args.pr, sys.stdin.read(), str(env.dirs.ctx)), end="")
    elif args.command == "checks-none":
        current = body(args.pr)
        return 0 if current is not None and checks_none(current) else 1
    elif args.command == "gate":
        print(json.dumps(gate(args.pr)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"pr: {exc}", file=sys.stderr)
        sys.exit(1)
