"""After a merge (migration slice 3b, #263): side bugs and follow-up nits become GitHub
issues (principle P-2), "## Before release" items become Linear tasks (P-3).

Each runs once per PR (markers `$STATE/follow-ups-pr-N`, `owner-actions-pr-N`), and
Linear gets each owner action once per issue (`owner-actions-issue-N`, bug_key titles).
The Linear key is read here into a local variable; no agent inherits it.
"""

from . import session
import argparse
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

from . import credentials, md, notify as notifier
from .notify import log, notify
from .pr import body as pr_body, gh, view

REF = re.compile(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9]+):([0-9]+)(?:[-–]([0-9]+))?")
HUNK = re.compile(r"\+[0-9]+(,[0-9]+)?")
ISSUE_BODY_MAX = 60000
REVIEW_MARKER = "<!-- baton-review "
FIX_MARKER = "<!-- baton-fix "
ADOPTED = re.compile(r"`?:?\s+adopted\b", re.I)
FILED_BUGS_MARKER = "<!-- baton-filed-bugs -->"
LINEAR_ERROR_MAX = 500


def repo():
    return session.resolve().repo.gh_repo


def stdout(*args):
    """gh's output with trailing newlines removed, as `$(gh ...)` gave it; None on failure."""
    result = gh(*args)
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


# --- P-2: code the PR changed is fixed in the PR or dropped, never filed --------------

def own_code_ranges(pr):
    """(path, start, end) of each new-side hunk of the PR's diff."""
    ranges, path = [], ""
    for line in gh("pr", "diff", str(pr), "-R", repo()).stdout.split("\n"):
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
        elif line.startswith("@@ ") and path and (match := HUNK.search(line)):
            start, _, length = match.group()[1:].partition(",")
            length = int(length) if length else 1
            if length > 0:
                ranges.append((path, int(start), int(start) + length - 1))
    return ranges


def own_code(ranges, text):
    """Every path:line or path:start-end TEXT cites lies inside a hunk the PR changed."""
    refs = REF.findall(text)
    return bool(refs) and all(
        any((p == path or p.endswith("/" + path)) and start <= int(first) and int(last or first) <= end
            for p, start, end in ranges)
        for path, first, last in refs)


def adopted_nits(fixes):
    """The citations fix replies mark `path:line adopted`, exactly as the review wrote them."""
    found = set()
    for _, text in md.items("\n".join(review_sections(fixes, "Nits"))):
        ref = REF.match(text.lstrip("`"))
        if ref and ADOPTED.match(text.lstrip("`")[ref.end():]):
            found.add(ref.group())
    return found


def worth_filing(pr, ranges, adopted, text):
    """P-2: a nit is filed unless the PR's own code holds it or a fix round already did it."""
    line = text.split("\n", 1)[0]
    ref = REF.search(text)
    if ref and ref.group() in adopted:
        log(f"PR #{pr}: adopted nit not filed: {line}")
        return False
    if own_code(ranges, text):
        log(f"PR #{pr}: own-code nit not filed: {line}")
        return False
    return True


def is_follow_up(issue):
    if not re.fullmatch(r"[0-9]+", issue or ""):
        return False
    result = gh("issue", "view", issue, "-R", repo(), "--json", "labels")
    return result.returncode == 0 and any(
        label["name"] == "follow-up" for label in json.loads(result.stdout).get("labels", []))


def review_sections(reviews, heading):
    """The `## HEADING` sections of each review comment, each starting a new block."""
    out, on = [], False
    for review in reviews:
        on = False
        for line in review.split("\n"):
            if line.startswith("## "):
                on = line == f"## {heading}"
                if on:
                    out.append(line)
            elif on:
                out.append(line)
    return out


def filed_bugs_path(pr):
    return Path(str(session.resolve().dirs.state), f"filed-bugs-pr-{pr}")


def open_bug_keys():
    listed = gh("issue", "list", "-R", repo(), "--state", "open", "--limit", "200", "--json", "title")
    return {md.bug_key(t["title"]) for t in json.loads(listed.stdout or "[]")} if listed.returncode == 0 else set()


def file_bug(pr, src, text):
    """The new issue's number, or None when gh failed."""
    bug = text.split("\n", 1)[0].split(" — ", 1)[0]
    url = stdout("issue", "create", "-R", repo(), "--label", "needs-triage", "--title", bug[:120],
                 "--body", f"{text}\n\nFound while working on {src}, outside its scope.")
    if url is None:
        log(f"PR #{pr}: filing a side bug failed: {bug}")
        return None
    notify(f"PR #{pr}: filed a side bug", url)
    return url.rsplit("/", 1)[-1]


def file_pr_bugs(pr, issue):
    """When a PR opens, file the bugs its description lists and post their numbers: an agent
    has no GitHub access, so a reference the PR needs (a `test.fixme` that names its bug) can
    only be written once the runner has filed it. Runs once per PR."""
    done = filed_bugs_path(pr)
    if done.exists():
        return
    done.touch()   # before filing: a half-filed PR is logged, never filed twice
    src = f"PR #{pr}" + (f" (issue #{issue})" if issue else "")
    seen, filed = open_bug_keys(), []
    for kind, text in md.items(md.section("Follow-ups", pr_body(pr) or "")):
        if kind != "bug":
            continue
        bug = text.split("\n", 1)[0].split(" — ", 1)[0]
        key = md.bug_key(bug)
        if key in seen:
            log(f"PR #{pr}: side bug already filed: {bug}")
            continue
        seen.add(key)
        if number := file_bug(pr, src, text):
            with open(done, "a") as file:
                file.write(key + "\n")
            filed.append(f"- #{number} {bug}")
    if filed and gh("pr", "comment", str(pr), "-R", repo(), "--body",
                    f"{FILED_BUGS_MARKER}\nFiled from this PR's `## Follow-ups`; use these numbers where the "
                    "PR refers to the bug (for example in a `test.fixme`):\n" + "\n".join(filed)).returncode != 0:
        log(f"PR #{pr}: posting the filed bug numbers failed")


def follow_ups(pr, issue):
    """File side bugs once each (a bug the PR body and a review both list, or one with an
    open issue, is not filed again) and the remaining nits as one follow-up issue."""
    done = Path(str(session.resolve().dirs.state), f"follow-ups-pr-{pr}")
    if done.exists():
        return
    done.touch()   # before filing: a half-filed PR is logged, never filed twice
    src = f"PR #{pr}" + (f" (issue #{issue})" if issue else "")
    title = stdout("pr", "view", str(pr), "-R", repo(), "--json", "title", "--jq", ".title") or ""
    body = pr_body(pr) or ""
    comments = view(pr, "comments") or {}
    owner = session.resolve().owner
    own = [c.get("body", "") for c in comments.get("comments", []) if c.get("author", {}).get("login") == owner]
    reviews = [text for text in own if text.startswith(REVIEW_MARKER)]
    adopted = adopted_nits([text for text in own if text.startswith(FIX_MARKER)])
    drop_nits = is_follow_up(issue)   # its reviewers moved nits worth doing into Must fix
    ranges = own_code_ranges(pr)
    seen = open_bug_keys()
    try:
        seen.update(filed_bugs_path(pr).read_text().splitlines())   # filed when the PR opened
    except OSError:
        pass

    other = ""
    noted = md.section("Follow-ups", body) + "\n".join(review_sections(reviews, "Follow-ups"))
    for kind, text in md.items(noted):
        if kind == "bug":
            bug = text.split("\n", 1)[0].split(" — ", 1)[0]
            key = md.bug_key(bug)
            if key in seen:
                log(f"PR #{pr}: side bug already filed: {bug}")
                continue
            seen.add(key)
            file_bug(pr, src, text)
        elif not drop_nits and worth_filing(pr, ranges, adopted, text):
            other += f"- {text}\n"

    # Each review comment's Nits; a later round lists only what is new.
    lines = ["" if line == "## Nits" else line for line in review_sections(reviews, "Nits")]
    lines = [line for line in lines if not re.fullmatch(r"\s*none\.?\s*", line, re.I)]
    while lines and not lines[0]:
        lines.pop(0)
    nits = ""
    for _, text in md.items("\n".join(lines).rstrip("\n")):
        if worth_filing(pr, ranges, adopted, text):
            nits += f"- {text}\n"

    if drop_nits:
        if nits or other:
            log(f"PR #{pr}: follow-up issue #{issue}, nits dropped, not filed again")
        return
    if not nits and not other:
        return
    text = ("Non-blocking items " + src + " found in code it left untouched. "
            "None changes behaviour the PR shipped; pick any subset or close this.\n"
            + (f"\n## Review nits\n\n{nits}\n" if nits else "")
            + (f"\n## Noted while implementing or reviewing\n\n{other}" if other else "") + "\n")
    text = text.encode()[:ISSUE_BODY_MAX].decode(errors="ignore").rstrip("\n")
    url = stdout("issue", "create", "-R", repo(), "--label", "follow-up",
                 "--title", f"Follow-ups from #{pr}: {title[:110]}", "--body", text)
    if url is not None:
        notify(f"PR #{pr}: follow-ups filed", url)
    else:
        log(f"PR #{pr}: filing the follow-up issue failed")


# --- P-3: owner actions in Linear ------------------------------------------------------

def linear_create(title, description):
    """The new Linear task's URL; None when Linear is not configured, the key is missing or Linear
    fails, with the reason in runner.log (never the key)."""
    linear = session.resolve().config.linear
    if linear is None:
        return None
    try:
        key = credentials.load(linear.env_file, ("LINEAR_API_KEY",)).get("LINEAR_API_KEY")
    except credentials.CredentialError as error:
        log(f"Linear: cannot read the key: {error}")
        return None
    if not key:
        log(f"Linear: cannot read the key: no LINEAR_API_KEY in the environment or {linear.env_file}")
        return None
    payload = {"query": "mutation($i: IssueCreateInput!) { issueCreate(input: $i) { success issue { url } } }",
               "variables": {"i": {"title": title, "description": description, "teamId": linear.team_id,
                                   "projectId": linear.project_id, "labelIds": [linear.label_id],
                                   "assigneeId": linear.assignee_id}}}
    request = urllib.request.Request("https://api.linear.app/graphql", json.dumps(payload).encode(),
                                     {"Authorization": key, "Content-Type": "application/json"})
    def failed(why):
        log(f"Linear: creating {title!r} failed: {' '.join(str(why).split())[:LINEAR_ERROR_MAX]}")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.load(response)
        url = ((answer.get("data") or {}).get("issueCreate") or {}).get("issue") or {}
    except urllib.error.HTTPError as error:
        failed(f"HTTP {error.code} {error.read().decode(errors='replace')}")
        return None
    except (OSError, ValueError, AttributeError) as error:
        failed(f"{type(error).__name__}: {error}")
        return None
    url = url.get("url") if isinstance(url, dict) else None
    if isinstance(url, str) and url:
        return url
    failed(json.dumps(answer.get("errors") or answer))
    return None


def owner_actions(pr, issue):
    """Each "## Before release" item becomes a Linear task, once per issue: every partial
    slice of an issue repeats the issue's list. Without [linear], or if Linear fails, a
    ready-for-human GitHub issue carries the items, never just the log."""
    use_linear = session.resolve().config.linear is not None
    state = str(session.resolve().dirs.state)
    done = Path(state, f"owner-actions-pr-{pr}")
    if done.exists():
        return
    src = f"PR #{pr}" + (f" (issue #{issue})" if issue else "")
    prurl = f"https://github.com/{repo()}/pull/{pr}"
    filed = Path(state, f"owner-actions-issue-{issue or f'none-{pr}'}")
    actions = md.section("Before release", pr_body(pr) or "")
    if not actions:
        return
    keys = set(filed.read_text().splitlines()) if filed.exists() else set()
    links = failed = ""
    entries = md.items(actions)
    for _, text in entries:
        title = text.split("\n", 1)[0].split(" — ", 1)[0]
        key = md.bug_key(title)
        if key in keys:
            continue
        keys.add(key)
        with open(filed, "a") as file:
            file.write(key + "\n")
        url = use_linear and linear_create(title[:120], f"{text}\n\nFrom {src}: {prurl}\n"
                                           "Do this before the next `v*` tag. Filed by the baton runner.")
        if url:
            links += f"- [{title}]({url})\n"
        else:
            failed += f"- {text}\n"
    if not entries:
        failed = actions.rstrip("\n")   # prose rather than a list: hand it over whole
    if links:
        if gh("pr", "comment", str(pr), "-R", repo(), "--body",
              f"<!-- baton-owner-actions -->\nBefore the next release (Linear):\n{links}").returncode != 0:
            log(f"PR #{pr}: commenting the Linear links failed")
        notify(f"PR #{pr}: owner actions in Linear", links)
    if failed:
        url = stdout("issue", "create", "-R", repo(), "--label", "ready-for-human",
                     "--title", f"Before the next release: owner actions from #{pr}",
                     "--body", f"{'Filing these in Linear failed' if use_linear else 'Linear is not configured'}, "
                               f"so they are here instead (principle P-3). From {src}.\n\n{failed}")
        if url is not None:
            notify(f"PR #{pr}: owner actions" + (" (Linear failed)" if use_linear else ""), url)
        else:
            log(f"PR #{pr}: owner actions lost, Linear and GitHub both failed: {failed}")
    done.touch()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr")
    parser.add_argument("issue", nargs="?", default="")
    args = parser.parse_args(argv)
    follow_ups(args.pr, args.issue)
    owner_actions(args.pr, args.issue)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        notifier.log(f"PR #{sys.argv[1] if len(sys.argv) > 1 else '?'}: after-merge failed: {exc}")
        print(f"after_merge: {exc}", file=sys.stderr)
        sys.exit(1)
