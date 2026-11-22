"""GitHub PR helpers for the bash runner (migration slice 3a, #263).

Each command reads the PR through `gh` (its own auth, `-R GH_REPO`) with the same
calls the bash helpers made, and keeps their exit codes. Commands that change the PR
print JSON: `changed`, and `log` for a runner.log line that bash writes.
"""

from . import check_verdicts, checks, session
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import pr_contract

TASK_LINE = re.compile(r"TASK: (partial|complete)")


def task_wanted(reply):
    """The fixer's last full-line `TASK: partial|complete`, or '' when absent."""
    wanted = [m[1] for line in reply.split("\n") if (m := TASK_LINE.fullmatch(line))]
    return wanted[-1] if wanted else ""


# A gh failure worth another try: the network or GitHub's edge, not the request (#46).
TRANSIENT = re.compile(r"TLS handshake timeout|connection reset|connection refused|i/o timeout|unexpected EOF"
                       r"|timeout awaiting response headers|Client\.Timeout exceeded|error connecting to"
                       r"|HTTP 50[234]|secondary rate limit", re.IGNORECASE)
# Pauses before each retry, in seconds: three retries over about 20 s.
BACKOFF = (2, 5, 15)
sleep = time.sleep
STDERR_LINES = 3


def call(*args, input=None, limit=None):
    """One `gh ARGS` run; LIMIT (seconds) runs it under `timeout`."""
    command = ["gh", *args] if limit is None else ["timeout", str(limit), "gh", *args]
    return session.run_process(command, input=input, text=True, capture_output=True, check=False)


def transient(result):
    stderr = result.stderr or ""
    if re.search(r"HTTP 4\d\d", stderr) and not re.search(r"secondary rate limit", stderr, re.IGNORECASE):
        return False
    return bool(TRANSIENT.search(stderr))


def why(result):
    """gh's last stderr lines as ` (gh: ...)` for a runner.log line or a handoff; '' when it printed none."""
    lines = [line.strip() for line in (getattr(result, "stderr", "") or "").splitlines() if line.strip()]
    return f" (gh: {' | '.join(lines[-STDERR_LINES:])})" if lines else ""


def _option(args, name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


# Comment / create / merge reconciliation during gh backoff (#46, #151).
LANDED = "landed"
ABSENT = "absent"
UNREADABLE = "unreadable"   # also untrusted identical: do not rewrite


def _landed(args, input):
    """Whether a non-idempotent write reached GitHub: (LANDED, result), (ABSENT, None),
    (UNREADABLE, None), or None when ARGS is not such a write."""
    repo = _option(args, "-R")
    scope = ["-R", repo] if repo else []
    if args[1:2] == ("comment",) and args[0] in ("issue", "pr") and len(args) > 2:
        text = _option(args, "--body")
        if text is None and _option(args, "--body-file") is not None:
            source = _option(args, "--body-file")
            try:
                text = input if source == "-" else Path(source).read_text()
            except OSError:
                return UNREADABLE, None
        if text is None:
            return UNREADABLE, None
        seen = call(args[0], "view", args[2], *scope, "--json", "comments")
        if seen.returncode != 0:
            return UNREADABLE, None
        try:
            comments = json.loads(seen.stdout)["comments"]
            if not isinstance(comments, list):
                return UNREADABLE, None
        except (ValueError, KeyError, TypeError):
            return UNREADABLE, None
        owner = session.resolve().owner
        want = text.strip()
        matched = [c for c in comments if isinstance(c, dict) and (c.get("body") or "").strip() == want]
        if any((c.get("author") or {}).get("login") == owner for c in matched):
            return LANDED, subprocess.CompletedProcess(["gh", *args], 0, "", "")
        if matched:
            return UNREADABLE, None
        return ABSENT, None
    if args[:2] == ("pr", "create") and (head := _option(args, "--head")):
        found = call("pr", "list", *scope, "--state", "open", "--head", head, "--json", "url", "--jq", ".[0].url // empty")
        if found.returncode != 0:
            return UNREADABLE, None
        url = found.stdout.strip()
        return (LANDED, subprocess.CompletedProcess(["gh", *args], 0, url + "\n", "")) if url else (ABSENT, None)
    if args[:2] == ("issue", "create") and (title := _option(args, "--title")):
        found = call("issue", "list", *scope, "--state", "open", "--limit", "30", "--json", "title,url")
        if found.returncode != 0:
            return UNREADABLE, None
        try:
            url = next((i["url"] for i in json.loads(found.stdout) if i.get("title") == title), "")
        except (ValueError, KeyError, TypeError):
            return UNREADABLE, None
        return (LANDED, subprocess.CompletedProcess(["gh", *args], 0, url + "\n", "")) if url else (ABSENT, None)
    if args[:2] == ("pr", "merge") and len(args) > 2:
        state = call("pr", "view", args[2], *scope, "--json", "state", "--jq", ".state")
        if state.returncode != 0:
            return UNREADABLE, None
        if state.stdout.strip() == "MERGED":
            return LANDED, subprocess.CompletedProcess(["gh", *args], 0, "", "")
        return ABSENT, None
    return None


def gh(*args, input=None, limit=None):
    """`gh ARGS`, retried on transient failures (TLS or connection errors, 502/503/504, the
    secondary rate limit) with BACKOFF; a 4xx or any other failure is returned at once. Before
    retrying a write that is not idempotent (a comment, `pr create`, `issue create`,
    `pr merge`), it checks whether the first try landed. An unreadable or ambiguous lookup
    stops the inner repeats and leaves the transient failure for the caller's budget (#151).
    The one entry point for every baton `gh` call (#46); the final failure's stderr is in the
    result (why())."""
    result = call(*args, input=input, limit=limit)
    for pause in BACKOFF:
        if result.returncode == 0 or not transient(result):
            break
        sleep(pause)
        status = _landed(args, input)
        if status is not None:
            kind, done = status
            if kind == LANDED:
                return done
            if kind == UNREADABLE:
                break
        result = call(*args, input=input, limit=limit)
    if result.returncode != 0 and transient(result):
        from . import notify   # notify imports this module
        notify.log(f"gh {' '.join(args[:2])} failed after {len(BACKOFF) + 1} tries{why(result)}")
    return result


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
    """Branch, review route, issue and completion of PR (empty when absent). A PR whose
    branch and body name no issue takes the first local issue GitHub says it closes."""
    data = view(pr, "headRefName,author,isCrossRepository,state,isDraft,body,closingIssuesReferences")
    if data is None:
        raise ValueError(f"cannot read PR #{pr}")
    issue, completion = linked_task(data, session.resolve().repo.gh_repo)
    return {"branch": data.get("headRefName", ""), "route": pr_contract.route(data, owner, cursor_login),
            "issue": issue, "completion": completion}


def linked_task(data, repo):
    issue, completion = pr_contract.relation(data)
    if not issue:
        for ref in data.get("closingIssuesReferences") or ():
            repository = ref.get("repository") or {}
            ref_owner = repository.get("owner") or {}
            name = f'{ref_owner.get("login", "")}/{repository.get("name", "")}'
            if ref.get("number") and name.casefold() == repo.casefold():
                return str(ref["number"]), completion
    return issue, completion


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


def _prepare_request(text, issue, live, wanted):
    """Canonical body for a description request. WANTED from `TASK:` may authorize a
    change: the text may carry the live completion or the requested one; the result
    uses WANTED. With no WANTED (or when it matches LIVE), the text must match LIVE."""
    if not wanted or wanted == live:
        return pr_contract.prepare(text, issue, live)
    try:
        prepared = pr_contract.prepare(text, issue, live)
    except ValueError as error:
        if str(error) != "a body edit cannot change task completion; a fix needs a matching TASK: line":
            raise
        return pr_contract.prepare(text, issue, wanted)
    return pr_contract.retarget(prepared, issue, wanted)


def remove_body_request(path):
    """Discard even a rejected directory at the agent's request path."""
    try:
        path.unlink(missing_ok=True)
    except IsADirectoryError:
        shutil.rmtree(path)


def _original_body(baseline, current):
    if baseline is None:
        return current
    if baseline.is_file():
        return baseline.read_text().rstrip("\n")
    temp = baseline.with_suffix(".tmp")
    temp.write_text(current + "\n")
    temp.replace(baseline)
    return current


def apply_body(pr, request, state, ctx, completion=None):
    """Apply an agent's PR description request (file REQUEST) with the runner-owned task
    relation. COMPLETION is the fixer's `TASK:` value when the fix path parsed one; the
    standalone apply-body CLI leaves it None so the body must match the live completion.
    Returns (rc, text_changed, reason, task_changed): rc 0 done or no request,
    1 GitHub failed (retry), 2 invalid request. Changes are measured from before
    the first attempt so a write whose response was lost still counts."""
    path = Path(request)
    if not path.exists() and not path.is_symlink():
        return 0, False, "", False
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        return 2, False, "description request must be a non-empty regular file and cannot be a symlink", False
    current = view(pr, "headRefName,body")
    if current is None:
        return 1, False, "GitHub could not read the current PR description", False
    before = (current.get("body") or "").rstrip("\n")
    baseline = Path(state, f"pr-body-base-{pr}")
    original = _original_body(baseline, before)
    try:
        relation_source = {**current, "body": original} if completion else current
        issue, original_completion = pr_contract.relation(relation_source)
        if not re.fullmatch(r"\d+", issue) or original_completion not in ("partial", "complete"):
            return 2, False, "cannot determine the PR's issue and task completion", False
        if completion and pr_contract.relation(current)[0] != issue:
            return 2, False, "the PR's associated issue changed since the fix began", False
        prepared = _prepare_request(path.read_text(), issue, original_completion, completion)
    except ValueError as error:
        return 2, False, str(error), False
    if prepared.rstrip("\n") != before and not edit_body(pr, prepared, ctx, f"pr-{pr}-body-prepared."):
        return 1, False, "GitHub could not update the PR description", False
    task_changed = bool(completion and completion != original_completion)
    if task_changed:
        try:
            original = pr_contract.retarget(original, issue, completion).rstrip("\n")
        except ValueError:
            # The replacement also corrected text that prevented a relation-only edit.
            pass
    return 0, prepared.rstrip("\n") != original, "", task_changed


def apply_task(pr, want, ctx, baseline=None):
    """Apply a parsed task completion. A saved baseline distinguishes an earlier
    landed write from a no-op. Returns (rc, changed, log): 1 GitHub failed, 2 unusable relation,
    3 unsafe retarget (e.g. a changed issue or a closing keyword in a partial PR)."""
    if not want:
        return 0, False, ""
    current = view(pr, "headRefName,body")
    if current is None:
        return 1, False, ""
    try:
        issue, completion = pr_contract.relation(current)
        before = (current.get("body") or "").rstrip("\n")
        original = _original_body(baseline, before)
        original_issue, original_completion = pr_contract.relation({**current, "body": original})
        if original_issue != issue:
            return 3, False, f"PR #{pr}: the associated issue changed since the fix began"
        if not re.fullmatch(r"\d+", issue):
            return 2, False, ""
        if completion == want:
            return 0, original_completion != want, ""
        try:
            prepared = pr_contract.retarget(before + "\n", issue, want)
        except ValueError:
            return 3, False, ""
    except ValueError:
        return 2, False, ""
    if not edit_body(pr, prepared, ctx, f"pr-{pr}-task."):
        return 1, False, ""
    return 0, True, (f"PR #{pr}: task completion {completion} -> {want} at the fixer's request; "
                     "the next review checks it")


def checks_file(ctx, pr):
    return Path(ctx, f"pr-{pr}-fix-checks.json")


def split_checks(pr, reply, ctx):
    """REPLY without its checks block, which is kept for apply_checks (#155).
    The file outlives a deferred Cursor fix's resume."""
    checks, rest = pr_contract.split_checks(reply)
    checks_file(ctx, pr).write_text(json.dumps({"checks": checks}) + "\n")
    return rest


def fixer_gave_checks(ctx, pr):
    """True when the fixer's reply held a checks block, saved by split_checks."""
    try:
        return json.loads(checks_file(ctx, pr).read_text())["checks"] is not None
    except (OSError, ValueError, KeyError, TypeError):
        return False


def apply_checks(pr, ctx):
    """After a fix, the fixer's re-check of the whole PR replaces the body's
    checks section; none given reads "Not re-checked", which the merge gate hands
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
    return 0, changed, f"PR #{pr}: checks refreshed after the fix" if changed else ""


def checks_none(text):
    """True when a body has one checks section and it says exactly none (#265)."""
    return checks.is_none(text)


def gate(pr, defer=False, cleared=None):
    """After green CI: merge, hand off for a manual check, or queue a metadata review
    when the live description would close the wrong issue (preflight). DEFER: the
    repository's manual checks wait for a later trial instead of the merge (#48).
    CLEARED: the manual checks the screenshot review verified, all of them (#92); they
    clear the gate only while the live description still lists exactly those checks.
    A checks handoff includes the body that caused it, when it could be read."""
    current = body(pr)
    if current is None:
        return {"action": "manual_checks"}
    listed = checks.items(current)   # none unless the body has exactly one checks section
    verified = cleared is not None and bool(listed) and listed == check_verdicts.items(cleared)
    if not (defer or verified or checks_none(current)):
        return {"action": "manual_checks", "body": current}
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
        rc, changed, _reason, _task_changed = apply_body(args.pr, args.request, str(env.dirs.state), str(env.dirs.ctx))
        print(json.dumps({"changed": changed}))
        return rc
    elif args.command in ("apply-task", "apply-checks"):
        if args.command == "apply-task":
            rc, changed, log = apply_task(args.pr, task_wanted(sys.stdin.read()), str(env.dirs.ctx))
            if rc == 3:
                rc = 2
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
        print(json.dumps(gate(args.pr, defer=env.repo.manual_checks == "defer")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"pr: {exc}", file=sys.stderr)
        sys.exit(1)
