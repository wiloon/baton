"""PR/issue relation protocol shared by creation, fix, review and merge recovery."""
import argparse
import json
import re
import sys

from . import names

MARKER = re.compile(r"<!-- baton-task issue=(\d+) completion=(partial|complete) -->")
LINK = re.compile(r"(?im)^\s*(refs?|closes|fixes|resolves)\s+#(\d+)\s*$")
CLOSING = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+"
    r"(?:(?:[\w.-]+/[\w.-]+)?#|https://github\.com/[\w.-]+/[\w.-]+/issues/)(\d+)\b"
)
FIELD = re.compile(r"(?im)^Task completion:\s*(partial|complete)\s*$")
CHECKS = re.compile(r"(?i)##\s+Manual checks:?\s*")
ADVANCED = re.compile(r"(?ims)^##\s+Acceptance advanced:?\s*$(.*?)(?=^##\s|\Z)")
NOT_RECHECKED = "Not re-checked after the fix run."


def relation(pr):
    body = pr.get("body") or ""
    markers = MARKER.findall(body)
    branch = names.BRANCH_RE.fullmatch(pr.get("headRefName", ""))
    if markers:
        if branch and any(n != branch[1] for n, _ in markers):
            raise ValueError("task marker disagrees with the Cursor branch")
        if len(set(markers)) != 1:
            raise ValueError("conflicting task relation markers")
        return markers[0]
    # Existing Cursor branches identify their issue even if the body cites others.
    issue = branch[1] if branch else ""
    links = LINK.findall(body)
    if not issue and links:
        issue = links[0][1]
    if not issue:
        return "", ""
    # Old runners prepended Closes even when Cursor explicitly wrote Refs.
    # Honor that conservative, markerless partial intent so its description is repairable.
    explicit_partial = any(word.lower() in {"ref", "refs"} and n == issue for word, n in links)
    completion = "complete" if not explicit_partial and issue in CLOSING.findall(body) else "partial"
    return issue, completion


def prepare(body, issue, required_completion=None):
    if not issue.isdigit() or int(issue) <= 0:
        raise ValueError("invalid issue number")
    markers = MARKER.findall(body)
    if any(n != issue for n, _ in markers):
        raise ValueError("a body edit cannot change the associated issue")
    fields = FIELD.findall(body)
    modes = set(fields + [mode for _, mode in markers])
    if len(modes) > 1:
        raise ValueError("conflicting task completion values")
    if len(re.findall(r"(?im)^Task completion:", body)) != len(fields):
        raise ValueError("Task completion must be partial or complete")
    completion = next(iter(modes), None)
    if completion is None:
        _, completion = relation({"body": body, "headRefName": names.BRANCH + issue})
    if required_completion and completion != required_completion:
        raise ValueError("a body edit cannot change task completion; request owner review")
    closing_issues = CLOSING.findall(body)
    if any(n != issue for n in closing_issues):
        raise ValueError("a task PR cannot close an unrelated issue")
    if completion == "partial" and closing_issues:
        raise ValueError("a partial PR cannot contain issue-closing keywords")
    # Agents may copy the current body, but only the runner emits protocol markers.
    body = re.sub(r"<!-- baton-.*?-->", "", body, flags=re.S)
    body = FIELD.sub("", body)
    body = LINK.sub(lambda match: "" if match[2] == issue else match[0], body).strip()
    keyword = "Closes" if completion == "complete" else "Refs"
    return f"<!-- baton-task issue={issue} completion={completion} -->\n\n{keyword} #{issue}\n\n{body}\n"


def retarget(body, issue, completion):
    """Switch an existing task PR between partial and complete. Only the runner calls
    this, after a fixer agreed with a reviewer; the next review checks the result."""
    body = MARKER.sub("", body)
    body = FIELD.sub("", body)
    # The runner's own Refs/Closes line is rewritten; closing words in prose still fail.
    body = LINK.sub(lambda match: "" if match[2] == issue else match[0], body)
    return prepare(f"Task completion: {completion}\n\n{body}", issue)


def split_checks(reply):
    """A fix reply's last `## Manual checks` block, and the reply without it (#155).
    The block ends at the next heading or the TASK:/RESULT: lines."""
    lines = reply.replace("\r\n", "\n").split("\n")
    starts = [i for i, line in enumerate(lines) if CHECKS.fullmatch(line)]
    if not starts:
        return None, reply
    start = end = starts[-1]
    end += 1
    while end < len(lines) and not re.match(r"## |(RESULT|TASK): ", lines[end]):
        end += 1
    checks = "\n".join(lines[start + 1:end]).strip() or None
    return checks, "\n".join(lines[:start] + lines[end:])


def set_checks(body, checks):
    """Replace the body's `## Manual checks` section with the fixer's re-check of the
    whole PR. No answer reads as not re-checked, which the merge gate hands off."""
    checks = re.sub(r"<!-- baton-.*?-->", "", checks or "", flags=re.S).strip() or NOT_RECHECKED
    lines = body.replace("\r\n", "\n").split("\n")
    out, done, skipping = [], False, False
    for line in lines:
        if line.startswith("## "):
            skipping = False
            if CHECKS.fullmatch(line):
                skipping = True  # a duplicate section would also reach the gate: drop it
                if not done:
                    out += ["## Manual checks", checks, ""]
                    done = True
                continue
        if not skipping:
            out.append(line)
    if not done:
        return body.rstrip("\n") + f"\n\n## Manual checks\n{checks}\n"
    return "\n".join(out)


def preflight(pr):
    """Raise ValueError when the live PR must not merge: a partial slice that advances
    no acceptance item, or that GitHub would still read as closing its issue."""
    _, completion = relation(pr)
    if completion != "partial":
        return
    # A partial slice must finish at least one acceptance item of its issue, or the
    # chain can merge dormant pieces forever (#484).
    advanced = ADVANCED.search(pr.get("body") or "")
    items = advanced[1].strip() if advanced else ""
    if not items or re.fullmatch(r"(?i)none\.?", items):
        raise ValueError("partial PR body has no `## Acceptance advanced` item; a slice must meet at least one acceptance item of its issue")
    if CLOSING.search(pr.get("body") or ""):
        raise ValueError("partial PR body contains closing keywords (negation does not disable them); use neutral wording")
    for commit in pr.get("commits", []):
        message = commit.get("messageHeadline", "") + "\n" + commit.get("messageBody", "")
        if CLOSING.search(message):
            raise ValueError("partial PR commit contains closing keywords; resolve before merging")
    if pr.get("closingIssuesReferences"):
        raise ValueError("partial PR still has GitHub closing references; unlink them before merging")


def route(pr, owner, cursor_login):
    """How a PR may be reviewed: "cursor" (a Cursor task branch), "owner" (the owner's
    own branch) or "reject". Only open, non-draft PRs from this repository qualify."""
    branch = pr.get("headRefName", "")
    login = (pr.get("author") or {}).get("login", "")
    if (pr.get("state") == "OPEN" and not pr.get("isDraft")
            and not pr.get("isCrossRepository") and branch != "main"
            and (login == owner or re.fullmatch(cursor_login, login))):
        if names.issue_of(branch):
            return "cursor"
        if not names.is_agent_prefix(branch) and login == owner:
            return "owner"
    return "reject"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "retarget", "relation", "relations", "describe", "preflight",
                                                       "split-checks", "set-checks"])
    parser.add_argument("issue_or_owner", nargs="?")
    parser.add_argument("--completion", choices=["partial", "complete"])
    parser.add_argument("--cursor-login", default=r"^(app/cursor|cursor(\[bot\])?)$")
    args = parser.parse_args()
    raw = sys.stdin.read()
    if args.command == "prepare":
        print(prepare(raw, args.issue_or_owner or "", args.completion), end="")
        return
    if args.command == "retarget":
        if not args.completion:
            raise ValueError("retarget needs --completion")
        print(retarget(raw, args.issue_or_owner or "", args.completion), end="")
        return
    if args.command == "split-checks":
        checks, reply = split_checks(raw)
        print(json.dumps({"checks": checks, "reply": reply}))
        return
    data = json.loads(raw)
    if args.command == "set-checks":
        print(set_checks(data["body"], data.get("checks")), end="")
        return
    if args.command == "preflight":
        preflight(data)
    elif args.command == "relations":
        for pr in data:
            issue, completion = relation(pr)
            print(pr["number"], issue or "-", completion or "-")
    elif args.command == "relation":
        issue, completion = relation(data)
        print(issue or "-", completion or "-")
    else:
        issue, completion = relation(data)
        print(data.get("headRefName", "") or "-", route(data, args.issue_or_owner, args.cursor_login),
              issue or "-", completion or "-")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
