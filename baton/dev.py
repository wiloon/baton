"""The dev run for one issue (migration slice 4e, #284): the issue item of run.sh.

Spec review first when the issue has no agreed plan, then a vendor implements it in a worktree,
the runner verifies, pushes and opens the PR, and the review loop runs. `run(tick, issue)` is an
item body; it returns the exit code (80: the item ended, the tick may chain on).
"""

from . import session
import json
import os
from pathlib import Path
import re
import subprocess
import time

from . import after_merge, dispatch, fix, md, names, pr as pr_helpers, pr_contract, prompts, spec, vendors
from .common import Deferred, git, push, strip_markers

REPLY_CAP = 60000
RESUME_NOTE = ("This worktree already holds earlier work on this issue (an earlier run stopped early). "
               "Check git log and git status before you continue.")
REVIEW_NOTE = ("This PR was written by the {dev} agent. Besides the usual review, check that it implements the plan agreed on "
               "issue #{issue} (the latest `<!-- baton-verdict: accept|revise` comment; an accept means the issue as "
               "revised in its spec review); where the PR description says it deviates, judge whether the deviation is sound.")


def clean_file(path):
    """Drop every marker line of the file (an agent can forge them) and end it with one newline."""
    text = strip_markers(Path(path).read_text()).rstrip("\n")
    Path(path).write_text(text + "\n")


def copy_env_files(wt, env_files):
    """The agent's copies of the untracked env files a worktree needs (.worktreeinclude)."""
    include = wt / ".worktreeinclude"
    for line in include.read_text().splitlines() if include.is_file() else ():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        src = Path(env_files, name)
        if src.is_file():
            (wt / name).parent.mkdir(parents=True, exist_ok=True)
            (wt / name).write_bytes(src.read_bytes())


def setup_worktree(tick, wt, branch, issue):
    """The worktree an earlier run left (resume), or a new one off origin/main with the agent's
    untracked env files (.worktreeinclude). Returns (resume_note, ok)."""
    if wt.is_dir():
        return RESUME_NOTE, True
    added = session.run_process(["git", "-C", tick.repo_dir, "worktree", "add", "-q", "-B", branch, str(wt), "origin/main"])
    if added.returncode != 0:
        tick.log(f"worktree add failed for #{issue}")
        return "", False
    copy_env_files(wt, tick.env.repo.env_files)
    return "", True


def context_file(tick, issue, branch, plan):
    """The issue, the agreed plan and the slices already merged, for the prompt."""
    path = tick.ctx / f"issue-{issue}-dev-{time.strftime('%Y%m%d-%H%M%S')}.md"
    data = tick.gh_json("issue", "view", issue, "-R", tick.gh_repo, "--json", "title,body") or {}
    merged = tick.gh("pr", "list", "-R", tick.gh_repo, "--state", "merged", "--head", branch, "--json", "number,title,body")
    path.write_text(f"<issue>\n# {data.get('title', '')}\n\n{data.get('body', '')}\n</issue>\n\n<plan>\n{plan}\n</plan>\n"
                    f"<completed-slices>\n{merged or ''}</completed-slices>\n")
    return path


def check_result(tick, dev, outcome, wt, base, body):
    """Why the run is not a PR yet: an empty string when it is ready."""
    if outcome.startswith("RESULT: ready"):
        if Path(body).is_file() and Path(body).stat().st_size:
            clean_file(body)
        if not git(wt, "rev-list", f"{base}..HEAD"):
            return f"{dev} reported ready but made no commit"
        if not Path(body).is_file() or not Path(body).stat().st_size:
            return f"{dev} wrote no PR description ({body})"
        Path(body).write_text(md.strip_wrapping_fence(Path(body).read_text()))
        if not re.search(r"[^\s]", Path(body).read_text().split("\n", 1)[0]):
            return f"{dev}'s PR description has no title on its first line ({body})"
        if git(wt, "status", "--porcelain"):
            return f"{dev} left uncommitted or untracked files"
        return ""
    if outcome.startswith("RESULT: blocked"):
        return outcome.removeprefix("RESULT: ")
    return outcome


def pr_body(text, issue):
    """The PR description: everything after the title line, markers stripped, the task relation
    prepared for ISSUE (Refs for a partial slice, Closes for a complete one). ValueError when
    the relation is invalid."""
    body = "\n".join(text.split("\n")[1:]).lstrip("\n")
    body = strip_markers(body.rstrip("\n") + "\n").rstrip("\n")
    return pr_contract.prepare(body + "\n", issue).rstrip("\n")


def is_partial(text, issue):
    """Whether the PR description makes this a partial slice; an invalid one stops later, in pr_body."""
    try:
        return pr_contract.MARKER.search(pr_body(text, issue)).group(2) == "partial"
    except ValueError:
        return False


def stop(tick, issue, reason, title, text):
    """The handoff every gate shares: ready-for-human, a telemetry handoff, a Telegram message."""
    pr_helpers.gh("issue", "edit", issue, "-R", tick.gh_repo, "--remove-label", "agent-wip", "--add-label", "ready-for-human")
    tick.hooks(issue).task("handoff", reason, "--reason", reason)
    tick.notify(title, text)
    return 80


def opening_path(tick, issue):
    return tick.state / f"pr-opening-{issue}.json"


def save_opening(path, intent):
    """Keep the PR creation intent across a crash, without leaving partial JSON."""
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(intent, sort_keys=True) + "\n")
    temp.replace(path)


def finish_opening(tick, intent, path):
    """Finish the side effects after PR creation. Every step can be repeated after a crash."""
    issue, pr, vendor = intent["issue"], intent["pr"], intent["vendor"]
    shots = tick.ctx / f"issue-{issue}-shots"
    target = tick.ctx / f"pr-{pr}-shots"
    if not target.exists():
        if shots.is_dir():
            shots.replace(target)  # both live below CTX, even when /tmp is a separate mount
        else:
            target.mkdir()  # an older run may have no screenshots
    state = str(tick.state)
    vendors.record_author(state, pr, vendor)
    if vendor == "codex":
        vendors.record_author_model(state, pr, vendor, intent["model"])
    if not vendors.state_path(state, pr, "contrib").exists():
        vendors.record_contrib(state, pr, vendor, intent["wt"], intent["base"])
    Path(f"/tmp/baton-dev-{issue}.pr.md").unlink(missing_ok=True)
    tick.active["issue"] = ""
    if (tick.state / f"review-pending-{pr}").exists():
        path.unlink(missing_ok=True)
        return 0  # the normal pending-review step will resume it
    tick.notify(f"#{issue}: {vendor} opened PR #{pr}, reviewing", f"https://github.com/{tick.gh_repo}/pull/{pr}")
    try:
        after_merge.file_pr_bugs(pr, issue)
    except Exception as error:
        tick.log(f"PR #{pr}: filing side bugs failed: {error}")
    try:
        tick.review_loop(pr, intent["wt"], REVIEW_NOTE.format(dev=vendor, issue=issue), "agent", "pool", issue=issue)
    finally:
        # Once the review checkpoint exists it owns continuation, even if this call defers.
        if (tick.state / f"review-pending-{pr}").exists():
            path.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    return 80


def resume_opening(tick, path):
    """Reconcile a saved creation intent with GitHub before resuming its review."""
    try:
        intent = json.loads(path.read_text())
        issue = str(intent["issue"])
        branch = intent["branch"]
        wt = names.worktree_for(tick.wt_root, branch)
        if (not issue.isdecimal() or path != opening_path(tick, issue)
                or names.issue_of(branch) != issue or intent["wt"] != str(wt)
                or intent["vendor"] not in ("claude", "codex", "cursor")
                or not re.fullmatch(r"[a-f0-9]{40,64}", intent["head"])
                or not re.fullmatch(r"[a-f0-9]{40,64}", intent["base"])
                or not isinstance(intent["model"], str)):
            raise ValueError("saved branch identity is invalid")
        pr = str(intent.get("pr") or "")
        if pr and (tick.state / f"review-pending-{pr}").exists():
            path.unlink(missing_ok=True)
            return 0
        if not pr:
            listed = tick.gh_json("pr", "list", "-R", tick.gh_repo, "--state", "open", "--head", branch,
                                  "--json", "number")
            if listed is None or len(listed) != 1:
                raise ValueError("expected exactly one open PR for saved branch")
            pr = str(listed[0]["number"])
        if not pr.isdecimal():
            raise ValueError("invalid PR number")
        remote = tick.gh_json("pr", "view", pr, "-R", tick.gh_repo, "--json",
                              "state,headRefName,headRefOid,baseRefName,isCrossRepository,isDraft,author,body,labels")
        if not isinstance(remote, dict):
            raise ValueError("could not read PR from GitHub")
        if remote.get("state") in ("MERGED", "CLOSED"):
            path.unlink(missing_ok=True)
            return 0
        related_issue, _ = pr_contract.relation(remote)
        if (remote.get("state") != "OPEN" or remote.get("headRefName") != branch
                or remote.get("headRefOid") != intent["head"] or remote.get("baseRefName") != "main"
                or remote.get("isCrossRepository") is not False or remote.get("isDraft") is not False
                or (remote.get("author") or {}).get("login") != tick.owner or related_issue != issue):
            raise ValueError("PR identity, HEAD or task relation does not match saved intent")
        if any(label.get("name") == "ready-for-human" for label in remote.get("labels") or []):
            path.unlink(missing_ok=True)
            return 0
        if (not wt.is_dir() or git(wt, "branch", "--show-current") != branch
                or git(wt, "rev-parse", "HEAD") != intent["head"]):
            raise ValueError("saved worktree does not match")
        intent["pr"] = pr
        save_opening(path, intent)
        tick.log(f"#{issue}: recovering PR #{pr} after interrupted creation")
        return finish_opening(tick, intent, path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        tick.log(f"PR opening recovery stopped for {path.name}: {exc}")
        return 1


def run(tick, issue):
    env, repo = tick.env.with_runtime(issue=str(issue)), tick.gh_repo
    branch, wt = names.for_issue(tick.wt_root, issue)
    body = f"/tmp/baton-dev-{issue}.pr.md"
    shots = str(tick.ctx / f"issue-{issue}-shots")
    url = f"https://github.com/{repo}/issues/{issue}"
    tick.active["issue"] = issue
    hooks = tick.hooks(issue)
    plan = spec.cursor_plan(env, issue)
    if not plan:
        if not tick.spec_loop(issue):
            return 80
        session.run_process(["git", "-C", tick.repo_dir, "fetch", "-q", "--prune", "origin"])
        plan = spec.cursor_plan(env, issue)
        if not plan:
            tick.notify(f"#{issue}: spec accepted but no plan comment found", url)
            return 1
    resume_note, ok = setup_worktree(tick, wt, branch, issue)
    if not ok:
        return 1
    Path(body).unlink(missing_ok=True)
    dev_vendor = tick.pick_vendor("dev", "contrib", tick.all_vendors(), issue=issue)
    pr_helpers.gh("issue", "edit", issue, "-R", repo, "--add-label", "agent-wip")
    tick.notify(f"#{issue} started ({dev_vendor})", url)
    base = git(wt, "rev-parse", "origin/main")
    ctx = context_file(tick, issue, branch, plan)
    session.run_process(["rm", "-rf", shots])
    os.makedirs(shots)
    prompt = prompts.render(tick.self_dir / "dev-prompt.md", [
        ("VENDOR", dev_vendor), ("ISSUE", issue), ("GH_REPO", repo), ("WORKTREE", str(wt)), ("BRANCH", branch),
        ("PR_BODY_FILE", body), ("RESUME_NOTE", resume_note), ("CONTEXT_FILE", str(ctx)), ("SHOTS_DIR", shots)], env=env)
    out = dispatch.dispatch(dev_vendor, "dev", prompt, f"issue-{issue}-dev", str(wt), str(tick.self_dir / "settings.json"), body,
                            env.with_runtime(shots_dir=shots), hooks)
    if out["rc"] == 75:
        raise Deferred(dev_vendor, out["defer"])
    outcome, dev_model = out["outcome"], out.get("model") or "unknown"

    fail = check_result(tick, dev_vendor, outcome, str(wt), base, body)
    if fail:
        reply = "\n".join(line for line in strip_markers(out.get("reply") or "").split("\n") if not line.startswith("RESULT: "))
        pr_helpers.gh("issue", "comment", issue, "-R", repo, "--body",
                      f"<!-- baton-note by={dev_vendor} -->\n{dev_vendor} stopped: {fail}\n\n{reply[:REPLY_CAP]}")
        return stop(tick, issue, "implementation_failed", f"#{issue}: {dev_vendor} stopped", f"{fail} {url}")

    verifier = fix.Runner(env, hooks)
    verifier.gate_applies = not is_partial(Path(body).read_text(), issue)
    rc, failure, rounds = verifier.verify(str(wt), base, dev_vendor, "issue", issue)
    if rc != 0:
        pr_helpers.gh("issue", "comment", issue, "-R", repo, "--body",
                      f"<!-- baton-note by={dev_vendor} -->\nLocal CI verification failed before push after {rounds} of "
                      f"{env.config.scheduling.max_verify_fixes or 2} fix rounds. Nothing was pushed.\n\n{failure[:REPLY_CAP]}")
        return stop(tick, issue, "verify_failed", f"#{issue}: local CI verification failed", url)
    if not push(wt, branch, upstream=True, signoff=tick.env.repo.signoff):
        stop(tick, issue, "push_failed", f"#{issue}: pushing {dev_vendor}'s branch failed",
             f"{branch} is committed in {wt}. {url}")
        return 1
    lines = Path(body).read_text().split("\n")
    try:
        text = pr_body(Path(body).read_text(), issue)
    except ValueError:
        pr_helpers.gh("issue", "edit", issue, "-R", repo, "--remove-label", "agent-wip", "--add-label", "ready-for-human")
        tick.notify(f"#{issue}: invalid PR task relation", f"Review the completion field in {body}. {url}")
        return 1
    if verifier.gate_result:
        text += f"\n\n## E2E gate\n{verifier.gate_result}, on {git(wt, 'rev-parse', '--short', 'HEAD')}. The runner ran this."
    intent = {"issue": str(issue), "branch": branch, "wt": str(wt), "base": base,
              "head": git(wt, "rev-parse", "HEAD"), "vendor": dev_vendor,
              "model": dev_model if rounds == 0 else "unknown", "pr": ""}
    save_opening(opening_path(tick, issue), intent)
    created = pr_helpers.gh("pr", "create", "-R", repo, "--head", branch, "--base", "main", "--title", lines[0], "--body", text)
    if created.returncode != 0:
        # A lost GitHub response may still have created the PR; leave the intent to reconcile.
        tick.log(f"#{issue}: PR create returned an error; saved intent will be reconciled on the next tick")
        tick.notify(f"#{issue}: PR creation needs reconciliation", f"branch {branch} is pushed. {url}")
        return 1
    pr = created.stdout.strip().rsplit("/", 1)[-1]
    if not pr.isdecimal():
        tick.log(f"#{issue}: PR create returned an unexpected URL; saved intent will be reconciled")
        return 1
    intent["pr"] = pr
    save_opening(opening_path(tick, issue), intent)
    return finish_opening(tick, intent, opening_path(tick, issue))
