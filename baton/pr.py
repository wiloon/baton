"""GitHub PR helpers for the bash runner (migration slice 3a, #263).

Each command reads the PR through `gh` (its own auth, `-R GH_REPO`) with the same
calls the bash helpers made, and keeps their exit codes. Commands that change the PR
print JSON: `changed`, and `log` for a runner.log line that bash writes.
"""

from . import check_verdicts, checks, names, session
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


def call(*args, input=None, limit=None, kill=False):
    """One `gh ARGS` run. KILL uses SIGKILL at the limit for hard deadlines."""
    command = ["gh", *args] if limit is None else ["timeout", *(["-s", "KILL"] if kill else []), str(limit), "gh", *args]
    return session.run_process(command, input=input, text=True, capture_output=True, check=False)


def transient(result):
    stderr = result.stderr or ""
    if re.search(r"HTTP 4\d\d", stderr) and not re.search(r"secondary rate limit", stderr, re.IGNORECASE):
        return False
    return bool(TRANSIENT.search(stderr))


LABEL_MISSING = re.compile(r"could not add label: '([^']+)' not found")


def why(result):
    """gh's last stderr lines as ` (gh: ...)` for a runner.log line or a handoff; '' when it printed none.

    Redacts secrets before splitting and truncating: advisor patterns, secret_values of the
    session, and token-named values from the effective `gh` subprocess environment.
    """
    text = getattr(result, "stderr", "") or ""
    from . import advisor  # notify imports this module; keep redact off the import cycle
    values = set()
    withheld = False
    try:
        for name, value in (session.resolve().tools or {}).items():
            if value and len(value) >= 8 and advisor.SECRET_NAME.search(name):
                values.add(value)
    except Exception:
        withheld = True
    try:
        values |= advisor.secret_values(session.resolve())
    except Exception:
        withheld = True
    if withheld:
        return " (gh: [stderr withheld: redaction failed])" if text.strip() else ""
    text = advisor.redact(text, values)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return f" (gh: {' | '.join(lines[-STDERR_LINES:])})" if lines else ""


def missing_requested_labels(result, labels):
    """Requested label names stderr identifies as missing (GitHub CLI pre-write rejection)."""
    found = set(LABEL_MISSING.findall(getattr(result, "stderr", "") or ""))
    return [name for name in labels if name in found]


def create_issue(repo, *, title, labels, body=None, body_file=None, allow_unlabelled=False, context=""):
    """Create an issue with LABELS through gh. Returns (CompletedProcess, applied_labels).

    On a missing-label rejection for a requested label: log why(), run `gh label create` once
    per requested label without --force, then retry the labelled create once. If that still
    reports a requested label missing and allow_unlabelled, create without labels. Any other
    create failure returns at once with no label recovery. applied_labels is the label list on
    the successful issue (empty after an unlabelled fallback), else ().
    """
    from . import notify
    labels = list(labels)
    args = ["issue", "create", "-R", repo, "--title", title]
    if body is not None:
        args += ["--body", body]
    if body_file is not None:
        args += ["--body-file", body_file]

    def with_labels(requested):
        out = list(args)
        for name in requested:
            out += ["--label", name]
        return out

    result = gh(*with_labels(labels))
    if result.returncode == 0:
        return result, tuple(labels)
    if not missing_requested_labels(result, labels):
        return result, ()
    prefix = f"{context}: " if context else ""
    notify.log(f"{prefix}issue create missing label{why(result)}")
    for name in labels:
        created = gh("label", "create", name, "-R", repo)
        if created.returncode != 0:
            notify.log(f"{prefix}label create {name!r} failed{why(created)}")
    result = gh(*with_labels(labels))
    if result.returncode == 0:
        return result, tuple(labels)
    if missing_requested_labels(result, labels) and allow_unlabelled:
        result = gh(*args)
    return result, ()


def _option(args, name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


UNKNOWN = "unknown"


def _landed(args, input):
    """For a write that may have reached GitHub before its answer was lost: the result it
    would have given, None when it confirmably did not land, or UNKNOWN when the lookup was
    unreadable (the write is then not repeated, #151 section 3). Idempotent calls return None."""
    repo = _option(args, "-R")
    scope = ["-R", repo] if repo else []
    if args[1:2] == ("comment",) and args[0] in ("issue", "pr") and len(args) > 2:
        text = _option(args, "--body")
        if text is None and _option(args, "--body-file") is not None:
            source = _option(args, "--body-file")
            try:
                text = input if source == "-" else Path(source).read_text()
            except OSError:
                return UNKNOWN
        if text is None:
            return None
        seen = gh(args[0], "view", args[2], *scope, "--json", "comments")
        if seen.returncode != 0:
            return UNKNOWN
        try:
            bodies = [c["body"].strip() for c in json.loads(seen.stdout)["comments"][-20:]]
        except (ValueError, KeyError, TypeError, AttributeError):
            return UNKNOWN
        if text.strip() in bodies:
            return subprocess.CompletedProcess(["gh", *args], 0, "", "")
        return None
    if args[:2] == ("pr", "create") and (head := _option(args, "--head")):
        found = gh("pr", "list", *scope, "--state", "open", "--head", head, "--json", "url", "--jq", ".[0].url // empty")
        if found.returncode != 0:
            return UNKNOWN
        url = found.stdout.strip()
        return subprocess.CompletedProcess(["gh", *args], 0, url + "\n", "") if url else None
    if args[:2] == ("issue", "create") and (title := _option(args, "--title")):
        found = gh("issue", "list", *scope, "--state", "open", "--limit", "30", "--json", "title,url")
        if found.returncode != 0:
            return UNKNOWN
        try:
            issues = json.loads(found.stdout)
            if not isinstance(issues, list):
                return UNKNOWN
            url = next((i["url"] for i in issues if i["title"] == title), "")
        except (ValueError, KeyError, TypeError):
            return UNKNOWN
        if not isinstance(url, str):
            return UNKNOWN
        return subprocess.CompletedProcess(["gh", *args], 0, url + "\n", "") if url else None
    if args[:2] == ("pr", "merge") and len(args) > 2:
        state = gh("pr", "view", args[2], *scope, "--json", "state", "--jq", ".state")
        if state.returncode != 0:
            return UNKNOWN
        answer = state.stdout.strip()
        if answer == "MERGED":
            return subprocess.CompletedProcess(["gh", *args], 0, "", "")
        return None if answer == "OPEN" else UNKNOWN
    return None


def gh(*args, input=None, limit=None, reconcile=None, repeat=None, until=None):
    """`gh ARGS`, retried on transient failures (TLS or connection errors, 502/503/504, the
    secondary rate limit) with BACKOFF; a 4xx or any other failure is returned at once. Before
    retrying a write that is not idempotent (a comment, `pr create`, `issue create`,
    `pr merge`), it checks whether the first try landed. The one entry point for every
    baton `gh` call (#46); the final failure's stderr is in the result (why()). A saved
    write can supply RECONCILE, which returns landed, absent, or a state that stops repeats.
    It also checks the final transient failure, when no inner repeat remains (#151).

    REPEAT replaces `transient` as the test for an inner repeat (a call that must not be resent
    after an inconclusive failure). UNTIL is a wall-clock time (`time.time()`): every call gets only
    the time left, is killed then, and none starts after it (#151)."""
    retryable = repeat or transient

    def attempt():
        if until is None:
            return call(*args, input=input, limit=limit)
        budget = deadline_budget(until, limit)
        if budget:
            result = call(*args, input=input, limit=budget, kill=True)
            if time.time() < until:
                return result
        return subprocess.CompletedProcess(["gh", *args], 124, "", "deadline reached")

    result = attempt()
    for pause in BACKOFF:
        if result.returncode == 0 or not retryable(result) or (until is not None and time.time() + pause >= until):
            break
        sleep(pause)
        if reconcile is not None:
            state = reconcile()
            if state == "landed":
                return subprocess.CompletedProcess(result.args, 0, "", "")
            if state != "absent":
                return result
        elif (done := _landed(args, input)) is not None:
            return result if done == UNKNOWN else done
        result = attempt()
    if result.returncode != 0 and retryable(result):
        if reconcile is not None and reconcile() == "landed":
            return subprocess.CompletedProcess(result.args, 0, "", "")
        from . import notify   # notify imports this module
        notify.log(f"gh {' '.join(args[:2])} failed after {len(BACKOFF) + 1} tries{why(result)}")
    return result


def deadline_budget(until, limit=None):
    """Round down to milliseconds. A zero budget must never reach GNU timeout."""
    left = until - time.time()
    if limit is not None:
        left = min(left, limit)
    return int(max(left, 0) * 1000) / 1000


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


def branch_history(repo, branch):
    """Every same-repository PR on BRANCH with base main (any state), deduplicated by PR
    number, or None when any call, page or entry fails or is malformed."""
    owner = repo.split("/")[0]
    result = gh("api", f"repos/{repo}/pulls?state=all&head={owner}:{branch}&per_page=100", "--paginate", "--slurp")
    if result.returncode != 0:
        return None
    try:
        pages = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        return None
    by_number = {}
    for page in pages:
        for item in page:
            if not isinstance(item, dict):
                return None
            number = item.get("number")
            head = item.get("head")
            base = item.get("base")
            if (type(number) is not int or number <= 0
                    or not isinstance(head, dict) or not isinstance(base, dict)
                    or not isinstance(head.get("ref"), str) or not isinstance(base.get("ref"), str)):
                return None
            head_repo = head.get("repo")
            if head_repo is not None and not isinstance(head_repo, dict):
                return None
            if (head["ref"] == branch and base["ref"] == "main"
                    and isinstance(head_repo, dict) and head_repo.get("full_name") == repo):
                by_number[number] = item
    return list(by_number.values())


def issue_history(repo, issue):
    """Every same-repository PR of ISSUE's agent branches (base main, any state), deduplicated
    by PR number, or None when any lookup fails: a partial history is never used."""
    by_number = {}
    for branch in names.branches(issue):
        pulls = branch_history(repo, branch)
        if pulls is None:
            return None
        for item in pulls:
            by_number[item["number"]] = item
    return list(by_number.values())


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
    intended = prepared.rstrip("\n")
    if intended != before and not edit_body(pr, prepared, ctx, f"pr-{pr}-body-prepared."):
        # Look before deciding: a lost success response still counts (#151 section 5).
        current = view(pr, "headRefName,body")
        if current is None:
            return 1, False, "GitHub could not confirm whether the description edit landed", False
        after = (current.get("body") or "").rstrip("\n")
        if after == intended:
            before = after
        elif after == original:
            return 1, False, "GitHub could not update the PR description", False
        else:
            return 2, False, "the PR description changed during the edit", False
    task_changed = bool(completion and completion != original_completion)
    if task_changed:
        try:
            original = pr_contract.retarget(original, issue, completion).rstrip("\n")
        except ValueError:
            # The replacement also corrected text that prevented a relation-only edit.
            pass
    return 0, intended != original, "", task_changed


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


def apply_checks(pr, ctx, *, consume=True):
    """After a fix, the fixer's re-check of the whole PR replaces the body's
    checks section; none given reads "Not re-checked", which the merge gate hands
    off. A durable continuation can keep the input until its caller acknowledges the push.
    Returns (rc, changed, log): rc 1 while GitHub may still hold the old section."""
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
    if consume:
        file.unlink(missing_ok=True)
    return 0, changed, f"PR #{pr}: checks refreshed after the fix" if changed else ""


def checks_none(text):
    """True when a body has one checks section and it says exactly none (#265)."""
    return checks.is_none(text)


def gate(pr, cleared=None, carried=None):
    """After green CI: merge, hand off for a manual check, or queue a metadata review
    when the live description would close the wrong issue (preflight).
    CLEARED: the manual checks the screenshot review verified, all of them (#92); they
    clear the gate only while the live description still lists exactly those checks.
    CARRIED: open design findings (#96); one whose check this read of the body no longer lists
    never merges, so a body change after the caller's read cannot slip past.
    A checks handoff includes the body that caused it, when it could be read."""
    current = body(pr)
    if current is None:
        return {"action": "manual_checks"}
    listed = checks.items(current)   # none unless the body has exactly one checks section
    if any(check not in listed for item in carried or () for check in item["checks"]):
        return {"action": "manual_checks", "body": current}
    verified = cleared is not None and bool(listed) and listed == check_verdicts.items(cleared)
    if not (verified or checks_none(current)):
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
        print(json.dumps(gate(args.pr)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"pr: {exc}", file=sys.stderr)
        sys.exit(1)
