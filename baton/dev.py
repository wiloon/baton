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

from . import advisor, after_merge, agents, checks as check_section, dispatch, fix, handoff as owner_handoff, md, names, pr as pr_helpers, pr_contract, prompts, recovery, spec, vendors
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


def context_file(tick, issue, branch, plan, data):
    """The issue, the agreed plan and the slices already merged, for the prompt."""
    path = tick.ctx / f"issue-{issue}-dev-{time.strftime('%Y%m%d-%H%M%S')}.md"
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


def stop(tick, issue, reason, title, text, **recovery_kw):
    """The handoff every gate shares: ready-for-human, a telemetry handoff, a Telegram message."""
    from . import handoff as owner_handoff
    result = owner_handoff.perform(
        tick.env, reason, kind="issue", number=issue, remove=("agent-wip",),
        title=title, text=text, hooks=tick.hooks(issue), item=str(issue), episode=str(issue), **recovery_kw)
    if result.handed_off:
        recovery.clear(tick.env, str(issue), str(issue))
        return 80
    return result


def opening_path(tick, issue):
    return tick.state / f"pr-opening-{issue}.json"


def attempts_path(tick, issue):
    return tick.state / f"dev-attempts-{issue}.json"


def load_attempts(tick, issue):
    """Author ranges of earlier runs: they survive a deferral or a stop, with the advisor off too."""
    try:
        rows = json.loads(attempts_path(tick, issue).read_text())
    except (OSError, ValueError):
        return []
    ok = isinstance(rows, list) and all(isinstance(row, list) and len(row) == 4 for row in rows)
    return rows if ok else []


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
    attempts = intent.get("attempts")
    if attempts is None:
        vendors.record_author(state, pr, vendor)
        if agents.ADAPTERS[vendor].RECORDS_AUTHOR_MODEL:
            vendors.record_author_model(state, pr, vendor, intent["model"])
        if not vendors.state_path(state, pr, "contrib").exists():
            vendors.record_contrib(state, pr, vendor, intent["wt"], intent["base"])
    else:
        for attempt in attempts:
            author, model, start, end = attempt
            if agents.ADAPTERS[author].RECORDS_AUTHOR_MODEL:
                vendors.record_author_model(state, pr, author, model)
            vendors.record_author(state, pr, author)
            vendors.record_contrib(state, pr, author, intent["wt"], start, end, once=True)
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
        rc = tick.review_loop(pr, intent["wt"], REVIEW_NOTE.format(dev=vendor, issue=issue), "agent", "pool", issue=issue)
    finally:
        # Once the review checkpoint exists it owns continuation, even if this call defers.
        if (tick.state / f"review-pending-{pr}").exists():
            path.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    return 75 if rc == 75 else 80


def _matches_merged_view(tick, remote, branch, issue):
    """True when a gh pr view payload matches the intent's branch, base, repo, owner and issue."""
    if remote.get("headRefName") != branch:
        return False
    return tick.matches_merged_issue(remote, issue)


def _matches_closed_intent(tick, item, intent, issue):
    """True when a REST history entry is closed/merged and matches the saved intent."""
    if (item.get("state") or "").lower() != "closed" and not item.get("merged_at"):
        return False
    if (item.get("head") or {}).get("sha") != intent["head"]:
        return False
    return tick.matches_merged_issue(item, issue)


def resume_opening(tick, path):
    """Reconcile a saved creation intent with GitHub before resuming its review."""
    try:
        intent = json.loads(path.read_text())
        issue = str(intent["issue"])
        branch = intent["branch"]
        wt = names.worktree_for(tick.wt_root, branch)
        if (not issue.isdecimal() or path != opening_path(tick, issue)
                or names.issue_of(branch) != issue or intent["wt"] != str(wt)
                or intent["vendor"] not in agents.ADAPTERS
                or not re.fullmatch(r"[a-f0-9]{40,64}", intent["head"])
                or not re.fullmatch(r"[a-f0-9]{40,64}", intent["base"])
                or not isinstance(intent["model"], str)):
            raise ValueError("saved branch identity is invalid")
        pr = str(intent.get("pr") or "")
        if pr and (tick.state / f"review-pending-{pr}").exists():
            if not pr.isdecimal():
                raise ValueError("invalid PR number")
            remote = tick.gh_json("pr", "view", pr, "-R", tick.gh_repo, "--json",
                                  "state,headRefName,baseRefName,isCrossRepository,author,body")
            if isinstance(remote, dict) and remote.get("state") == "MERGED" and _matches_merged_view(tick, remote, branch, issue):
                if not tick.record_merged_settlement(pr, issue):
                    raise OSError("could not write settlement record")
                tick.settle_recorded_merge(pr, issue)
            path.unlink(missing_ok=True)
            return 0
        if not pr:
            listed = tick.gh_json("pr", "list", "-R", tick.gh_repo, "--state", "open", "--head", branch,
                                  "--json", "number")
            if listed is None:
                raise ValueError("expected exactly one open PR for saved branch")
            if len(listed) == 0:
                history = pr_helpers.branch_history(tick.gh_repo, branch)
                if history is None:
                    raise ValueError("expected exactly one open PR for saved branch")
                matches = [item for item in history if _matches_closed_intent(tick, item, intent, issue)]
                if len(matches) != 1:
                    raise ValueError("expected exactly one open PR for saved branch")
                matched = matches[0]
                if matched.get("merged_at"):
                    matched_pr = str(matched["number"])
                    if not tick.record_merged_settlement(matched_pr, issue):
                        raise OSError("could not write settlement record")
                    tick.settle_recorded_merge(matched_pr, issue)
                path.unlink(missing_ok=True)
                return 0
            if len(listed) != 1:
                raise ValueError("expected exactly one open PR for saved branch")
            pr = str(listed[0]["number"])
        if not pr.isdecimal():
            raise ValueError("invalid PR number")
        remote = tick.gh_json("pr", "view", pr, "-R", tick.gh_repo, "--json",
                              "state,headRefName,headRefOid,baseRefName,isCrossRepository,isDraft,author,body,labels")
        if not isinstance(remote, dict):
            raise ValueError("could not read PR from GitHub")
        if remote.get("state") == "MERGED":
            if _matches_merged_view(tick, remote, branch, issue):
                if not tick.record_merged_settlement(pr, issue):
                    raise OSError("could not write settlement record")
                tick.settle_recorded_merge(pr, issue)
            path.unlink(missing_ok=True)
            return 0
        if remote.get("state") == "CLOSED":
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


def run_dev_worker(tick, issue, dev_vendor, wt, branch, body, base, plan, payload, resume_note, hooks, env):
    """Implementer dispatch and outcome check; returns fail text or '' when ready."""
    ctx = context_file(tick, issue, branch, plan, payload)
    shots = str(tick.ctx / f"issue-{issue}-shots")
    prompt = prompts.render(tick.self_dir / "dev-prompt.md", [
        ("VENDOR", dev_vendor), ("ISSUE", issue), ("GH_REPO", tick.gh_repo), ("WORKTREE", str(wt)), ("BRANCH", branch),
        ("PR_BODY_FILE", body), ("RESUME_NOTE", resume_note), ("CONTEXT_FILE", str(ctx)), ("SHOTS_DIR", shots)], env=env)
    out = dispatch.dispatch(dev_vendor, "dev", prompt, f"issue-{issue}-dev", str(wt), str(tick.self_dir / "settings.json"), body,
                            env.with_runtime(shots_dir=shots), hooks)
    if out["rc"] == 75:
        raise Deferred(dev_vendor, out["defer"])
    outcome = out["outcome"]
    fail = check_result(tick, dev_vendor, outcome, str(wt), base, body)
    if fail:
        reply = "\n".join(line for line in strip_markers(out.get("reply") or "").split("\n") if not line.startswith("RESULT: "))
        pr_helpers.gh("issue", "comment", issue, "-R", tick.gh_repo, "--body",
                      f"<!-- baton-note by={dev_vendor} -->\n{dev_vendor} stopped: {fail}\n\n{reply[:REPLY_CAP]}")
    return fail, outcome, out


def worker_failure(fail, outcome):
    if not fail:
        return ""
    if fail.startswith("blocked"):
        return "dev_blocked"
    return "dev_result_invalid" if outcome.startswith("RESULT: ready") else "dev_run_failed"


def issue_binding(wt, issue, branch, payload):
    # The worker does not rebase on resume, so main's tip is not an input of its retry.
    current = recovery.issue_fingerprint(wt, issue, base="origin/main")
    if current["branch"] != branch or not current["clean"] or not current["local_head"]:
        return None
    comments = [comment for comment in payload["comments"]
                if not comment["body"].startswith("<!-- baton-note by=")]
    current["issue_evidence"] = recovery.feedback_digest(json.dumps(
        [payload["title"], payload["body"], comments], sort_keys=True))
    return current


def live_issue_binding(tick, env, wt, issue, branch):
    fresh, _ = spec.read_issue(env, issue, recovery=True)
    if fresh is None:
        return None
    labels = {label.get("name") for label in fresh["labels"]}
    if "ready-for-agent" in labels:
        if not tick.owner_ready(issue):
            return None
    elif tick.force_issue != str(issue):
        return None
    return issue_binding(wt, issue, branch, fresh)


def push_flag(tick, issue):
    return tick.state / f"dev-push-waiting-{issue}"


def owner_feedback_digest(owner, comments):
    rows = [
        (row["id"], row["body"])
        for row in comments
        if (row["author"] or {}).get("login") == owner and not row["body"].startswith("<!-- baton")
    ]
    return owner_handoff.body_digest(json.dumps(sorted(rows)))


def check_dev_push(tick, env, binding):
    # Use the backed-off `gh` path (not once/call): tick tests patch `gh`, and a disposition
    # check after resume must not treat a missing `call` stub as a permanent failure.
    data, transient = spec.read_issue(env, binding["issue"])
    if data is None:
        return "retry" if transient else "failed"
    if data["state"] == "CLOSED":
        return "superseded"
    labels = {label["name"] for label in data["labels"]}
    if "ready-for-human" in labels:
        return "paused"
    # An open PR for this branch means another path already finished the push.
    listed = tick.gh_json("pr", "list", "-R", tick.gh_repo, "--state", "open", "--head", binding["branch"],
                          "--json", "number,isCrossRepository")
    if listed is None:
        return "retry"
    # A fork's same-named branch is absence, not supersession.
    if any(not row.get("isCrossRepository") for row in listed):
        return "superseded"
    if owner_feedback_digest(env.owner, data["comments"]) != binding["feedback"]:
        return "changed"
    return "ok"


def finish_dev_push(tick, binding):
    """Continuation after a landed dev push: open the PR from the saved description (#151)."""
    issue, wt, branch = binding["issue"], binding["wt"], binding["branch"]
    session.run_process(
        ["git", "-C", wt, "fetch", "-q", "origin",
         f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
        capture_output=True, text=True)
    intent = {"issue": str(issue), "branch": branch, "wt": str(wt), "base": binding["base"],
              "head": binding["commit"], "vendor": binding["vendor"],
              "model": binding["model"], "attempts": binding["attempts"], "pr": ""}
    Path(f"/tmp/baton-dev-{issue}.pr.md").write_text(binding["title"] + "\n\n" + binding["body_text"] + "\n")
    save_opening(opening_path(tick, issue), intent)
    recovery.clear(tick.env, issue, issue)
    attempts_path(tick, issue).unlink(missing_ok=True)
    owner_handoff.clear_retry(tick.env, issue, owner_handoff.DEV_PUSH)
    push_flag(tick, issue).unlink(missing_ok=True)
    created = pr_helpers.gh("pr", "create", "-R", tick.gh_repo, "--head", branch, "--base", "main",
                            "--title", binding["title"], "--body", binding["body_text"])
    if created.returncode != 0:
        tick.log(f"#{issue}: PR create returned an error; saved intent will be reconciled on the next tick"
                 f"{pr_helpers.why(created)}")
        tick.notify(f"#{issue}: PR creation needs reconciliation",
                    f"branch {branch} is pushed. https://github.com/{tick.gh_repo}/issues/{issue}")
        return 1
    pr = created.stdout.strip().rsplit("/", 1)[-1]
    if not pr.isdecimal():
        tick.log(f"#{issue}: PR create returned an unexpected URL; saved intent will be reconciled")
        return 1
    intent["pr"] = pr
    save_opening(opening_path(tick, issue), intent)
    return finish_opening(tick, intent, opening_path(tick, issue))


def push_with_retry(tick, env, issue, wt, branch, *, vendor, title, body_text, base, attempts, model,
                    gate_result, feedback, pending=None):
    """Save before pushing the implementation branch, or consume a due attempt (#151)."""
    reason = owner_handoff.DEV_PUSH
    url = f"https://github.com/{tick.gh_repo}/issues/{issue}"
    title_msg = f"#{issue}: pushing {vendor}'s branch failed"
    text = f"#{issue}: pushing {vendor}'s branch failed"
    initial = pending is None

    def handoff(message):
        push_flag(tick, issue).unlink(missing_ok=True)
        return stop(tick, issue, reason, title_msg, f"{message} {url}")

    try:
        found = owner_handoff.load_retry(env, issue, reason)
    except owner_handoff.RetryUnreadable as exc:
        return handoff(f"Could not read the saved push operation: {exc}")
    if initial:
        if found is not None:
            raise Deferred("handoff-retry", reason, lends_slot=False)
        if env.repo.signoff:
            from .common import sign_off
            if not sign_off(wt):
                return handoff(f"{branch} is committed in {wt}; sign-off failed.")
        dest = owner_handoff.push_destination(wt, branch) or branch
        commit = git(wt, "rev-parse", "HEAD")
        if not dest or not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
            return handoff(f"{branch} is committed in {wt}; local commit or branch missing.")
        remote, unreadable_transient = owner_handoff.observe_remote(wt, dest)
        if remote is None and not unreadable_transient:
            return handoff(f"{branch} is committed in {wt}; could not observe the remote ref.")
        if gate_result:
            # Named after sign-off so the SHA is the one that reaches origin.
            short = git(wt, "rev-parse", "--short", commit) or commit[:7]
            body_text += f"\n\n## E2E gate\n{gate_result}, on {short}. The runner ran this."
        binding = {
            "issue": str(issue), "commit": commit, "branch": dest, "ref": dest,
            "remote": remote, "upstream": True, "wt": str(wt), "feedback": feedback,
            "vendor": vendor, "title": title, "body_text": body_text, "base": base,
            "model": model, "gate_result": gate_result or "", "attempts": attempts,
        }
        push_flag(tick, issue).write_text(str(issue))
        pending = owner_handoff.prepare_retry(env, issue, reason, issue, binding)
        if pending is None:
            return handoff(f"{branch} is committed in {wt}; the push inputs could not be saved.")
    else:
        if found is None:
            if push_flag(tick, issue).exists():
                return handoff("The saved push operation is missing; not running the implementer again.")
            return handoff("The saved push operation is missing.")
        pending = found
        binding = pending["continuation"]
        if not pending.get("phase") and int(owner_handoff.clock()) < pending["due"]:
            raise Deferred("handoff-retry", reason, lends_slot=False)
        disposition = check_dev_push(tick, env, binding)
        if disposition == "superseded":
            advisor.finish_episode(env, "issue", issue, "superseded")
            owner_handoff.clear_retry(env, issue, reason)
            push_flag(tick, issue).unlink(missing_ok=True)
            return 80
        if disposition == "paused":
            raise Deferred("handoff-retry", reason, lends_slot=False)
        if disposition == "changed":
            return handoff(f"{binding['branch']} is committed in {binding['wt']}; owner feedback changed.")
        if pending.get("phase"):
            if disposition == "ok" and owner_handoff.check_push_landed(binding) == "landed":
                return finish_dev_push(tick, binding)
            return handoff(f"{binding['branch']} is committed in {binding['wt']}; could not confirm the landed push.")
        if pending["attempts"] < owner_handoff.MAX_ATTEMPTS:
            result = stop(tick, issue, reason, title_msg, f"{text}. {url}", resume=True)
            if isinstance(result, owner_handoff.Result):
                if result.action == owner_handoff.DEFER:
                    raise Deferred("handoff-retry", reason, lends_slot=False)
                if result.action != owner_handoff.RETRY:
                    push_flag(tick, issue).unlink(missing_ok=True)
                    return 80
                binding = result.continuation
            else:
                return result
        if disposition != "ok":
            if pending["attempts"] + 1 < owner_handoff.MAX_ATTEMPTS:
                raise Deferred("handoff-retry", reason, lends_slot=False)
            return handoff(f"{binding['branch']} is committed in {binding['wt']}; could not verify the owner inputs.")
        if binding["remote"] is None:
            if not owner_handoff.check_push_local(binding):
                return handoff(f"{binding['branch']} is committed in {binding['wt']}; local inputs changed.")
            remote, unreadable_transient = owner_handoff.observe_remote(binding["wt"], binding["branch"])
            if remote is not None:
                consumed = owner_handoff.load_retry(env, issue, reason)
                binding = owner_handoff.save_push_baseline(
                    env, issue, issue, consumed, remote, reason=reason)
                pending = {**pending, "continuation": binding}
            elif not unreadable_transient:
                return handoff(f"{binding['branch']} is committed in {binding['wt']}; could not observe the remote ref.")

    binding = pending["continuation"] if initial else binding
    if not initial:
        pr_helpers.gh("issue", "edit", issue, "-R", tick.gh_repo, "--add-label", "agent-wip")
    pushed = None
    if binding["remote"] is None:
        status = "retry"
    else:
        status = owner_handoff.check_push_landed(binding)
    if status == "absent" and (initial or pending["attempts"] < owner_handoff.MAX_ATTEMPTS):
        pushed = push(binding["wt"], f"{binding['commit']}:refs/heads/{binding['branch']}",
                      upstream=binding["upstream"], signoff=False)
        if pushed:
            status = "landed"
        else:
            status = owner_handoff.check_push_landed(binding)
            if status != "landed" and not pushed.transient:
                detail = pushed.detail or "permanent push failure"
                return handoff(f"{binding['branch']} is committed in {binding['wt']}; {detail}.")
            if status == "absent":
                status = "retry"
    if status == "landed":
        landed = {**owner_handoff.load_retry(env, issue, reason), "phase": "landed"}
        owner_handoff.restore_retry(env, issue, reason, issue, landed)
        return finish_dev_push(tick, binding)
    if status in ("changed", "failed"):
        return handoff(f"{binding['branch']} is committed in {binding['wt']}; inputs changed or the remote is unreadable.")
    if status == "retry":
        attempts_n = pending["attempts"] + (0 if initial else 1)
        if attempts_n < owner_handoff.MAX_ATTEMPTS:
            now = int(owner_handoff.clock())
            owner_handoff.restore_retry(
                env, issue, reason, issue,
                {**pending, "attempts": attempts_n, "scheduled": now,
                 "due": now + owner_handoff.DELAYS[attempts_n]})
            raise Deferred("handoff-retry", reason, lends_slot=False)
    detail = getattr(pushed, "detail", "") or status
    return handoff(f"{binding['branch']} is committed in {binding['wt']}; {detail}.")


def waiting_dev_push(tick, env, issue):
    try:
        found = owner_handoff.load_retry(env, issue, owner_handoff.DEV_PUSH)
    except owner_handoff.RetryUnreadable as exc:
        return stop(tick, issue, owner_handoff.DEV_PUSH,
                    f"#{issue}: pushing branch failed",
                    f"Could not read the saved push operation: {exc}")
    if found is None and push_flag(tick, issue).exists():
        return stop(tick, issue, owner_handoff.DEV_PUSH,
                    f"#{issue}: pushing branch failed",
                    "The saved push operation is missing; not running the implementer again.")
    return found


def run(tick, issue):
    env, repo = tick.env.with_runtime(issue=str(issue)), tick.gh_repo
    branch, wt = names.for_issue(tick.wt_root, issue)
    body = f"/tmp/baton-dev-{issue}.pr.md"
    shots = str(tick.ctx / f"issue-{issue}-shots")
    url = f"https://github.com/{repo}/issues/{issue}"
    tick.active["issue"] = issue
    hooks = tick.hooks(issue)
    waiting = waiting_dev_push(tick, env, issue)
    if isinstance(waiting, dict):
        return push_with_retry(tick, env, issue, wt, branch, vendor=waiting["continuation"]["vendor"],
                               title=waiting["continuation"]["title"],
                               body_text=waiting["continuation"]["body_text"],
                               base=waiting["continuation"]["base"],
                               attempts=waiting["continuation"]["attempts"],
                               model=waiting["continuation"]["model"],
                               gate_result=waiting["continuation"]["gate_result"],
                               feedback=waiting["continuation"]["feedback"], pending=waiting)
    if waiting is not None:
        return 80
    runner = spec.Spec(env, hooks, issue)
    payload = tick.recovered_issues.pop(str(issue), None)
    if payload is None:
        payload = runner.issue_payload()
    if payload is None:
        return 80
    plan = spec.verdict_plan(env, issue, listed=payload["comments"])
    if not plan:
        tick.recovered_issues[str(issue)] = payload
        if not tick.spec_loop(issue):
            return 80
        session.run_process(["git", "-C", tick.repo_dir, "fetch", "-q", "--prune", "origin"])
        payload = runner.issue_payload()
        if payload is None:
            return 80
        plan = spec.verdict_plan(env, issue, listed=payload["comments"])
        if not plan:
            tick.notify(f"#{issue}: spec accepted but no plan comment found", url)
            return 1
    if (verdict := spec.VERDICT.match(plan)) and verdict[1] == "accept":
        advisor.finish_episode(env, "issue", issue, "accept")
    resume_note, ok = setup_worktree(tick, wt, branch, issue)
    if not ok:
        return 1
    base = git(wt, "rev-parse", "origin/main")
    picker = dict(mode="contrib", candidates=tick.all_vendors(), issue=issue)
    binding = lambda: live_issue_binding(tick, env, wt, issue, branch)
    pending = recovery.resume(
        env, issue, issue, lambda reason: reason.startswith("dev_") and reason != "dev_verify_failed",
        binding, lambda text: stop(tick, issue, "dev_run_failed", f"#{issue} needs you", text), role="dev", **picker)
    if pending is None:
        pending = recovery.resume(
            env, issue, issue, lambda reason: reason == "dev_verify_failed", binding,
            lambda text: stop(tick, issue, "dev_verify_failed", f"#{issue} needs you", text), role="dev", local=True)
    if pending is False:
        return 80
    attempts = load_attempts(tick, issue)
    if pending and pending[0]["remaining_budget"].get("base_sha"):
        base = pending[0]["remaining_budget"]["base_sha"]   # a verification retry checks the same base
    dev_vendor = pending[1] if pending else tick.pick_vendor("dev", "contrib", tick.all_vendors(), issue=issue)

    def ask(reason, title, detail, *, budget=None, local=False, before_labels=None):
        return recovery.request(
            env, issue, issue, reason, "dev", dev_vendor, detail,
            issue_binding(wt, issue, branch, payload), binding,
            lambda text=detail, **kw: stop(tick, issue, reason, title, text, before_labels=before_labels, **kw),
            budget=budget or {}, local=local, **picker)

    def note_attempt(vendor, model, start):
        """Record the range of an attempt that committed, also when it was cut short."""
        end = git(wt, "rev-parse", "HEAD")
        if end == start:   # an attempt that wrote nothing is not an author
            return end
        # The first range also covers commits retained from before any was recorded.
        first = (git(wt, "merge-base", "HEAD", "origin/main") or start) if not attempts else start
        attempts.append([vendor, model, first, end])
        save_opening(attempts_path(tick, issue), attempts)
        return end

    def implement():
        start = git(wt, "rev-parse", "HEAD")
        Path(body).unlink(missing_ok=True)
        session.run_process(["rm", "-rf", shots])
        os.makedirs(shots)
        note = resume_note
        if pending and pending[0]["reason"] == "dev_task_invalid":
            note += "\nCorrect the task relation in the PR description. Retain the existing commits."
            note += f" Validation error: {pending[0]['remaining_budget'].get('validation_error', '')}"
        try:
            with recovery.dispatching(env, issue, issue, binding, role="dev"):
                fail, outcome, out = run_dev_worker(tick, issue, dev_vendor, wt, branch, body, base, plan, payload,
                                                    note, hooks, env)
        except Deferred:
            note_attempt(dev_vendor, "unknown", start)
            raise
        note_attempt(dev_vendor, out.get("model") or "unknown", start)
        reason = worker_failure(fail, outcome)
        if reason:
            return reason, fail
        try:
            pr_body(check_section.canonicalize(Path(body).read_text()), issue)
        except ValueError as error:
            return "dev_task_invalid", str(error)
        # The worker step is complete: a later deferral continues with verification, not the worker.
        recovery.update_budget(env, issue, issue, worker_done=True, body=Path(body).read_text())
        return "", ""

    pr_helpers.gh("issue", "edit", issue, "-R", repo, "--add-label", "agent-wip")
    tick.notify(f"#{issue} started ({dev_vendor})", url)
    verification = pending and pending[0]["reason"] == "dev_verify_failed"
    worker_done = bool(pending and pending[0]["remaining_budget"].get("worker_done"))
    if not verification and not worker_done:
        while True:
            reason, fail = implement()
            if not reason:
                break
            title = f"#{issue}: {dev_vendor} stopped"
            if reason == "dev_blocked" or pending:
                return stop(tick, issue, reason, title, f"{fail} {url}")
            budget = {"validation_error": fail} if reason == "dev_task_invalid" else {}
            pending = ask(reason, title, f"{fail} {url}", budget=budget)
            if not pending:
                return 80
            _, dev_vendor = pending
    else:
        budget = pending[0]["remaining_budget"]
        Path(body).write_text(budget["body"])

    verifier = fix.Runner(env, hooks)
    verifier.gate_applies = not is_partial(Path(body).read_text(), issue)
    start = git(wt, "rev-parse", "HEAD")
    try:
        if verification:
            rc, failure, rounds = verifier.verify_retry(
                str(wt), base, dev_vendor, "issue", issue, binding=binding,
                repairs=budget["verify_repairs"], repairs_before=budget["repairs_used"])
        else:
            with recovery.dispatching(env, issue, issue, binding, role="dev"):
                rc, failure, rounds = verifier.verify(str(wt), base, dev_vendor, "issue", issue)
    except Deferred:
        note_attempt(dev_vendor, "unknown", start)
        raise
    end = note_attempt(dev_vendor, "unknown", start)
    if rc != 0:
        retry = None
        reason = "dev_verify_failed"
        title = f"#{issue}: local CI verification failed"
        max_repairs = int(env.config.scheduling.max_verify_fixes or 2)
        def explain(used):
            pr_helpers.gh("issue", "comment", issue, "-R", repo, "--body",
                          f"<!-- baton-note by={dev_vendor} -->\nLocal CI verification failed before push after {used} of "
                          f"{max_repairs} fix rounds. Nothing was pushed.\n\n{failure[:REPLY_CAP]}")
        if not verification:
            remaining = max(0, max_repairs - rounds)
            retry = ask(reason, title, recovery.verify_status(repairs_used=rounds, max_repairs=max_repairs,
                                                              failure=failure),
                        budget={"verify_repairs": remaining, "repairs_used": rounds, "base_sha": base,
                                "body": Path(body).read_text()}, local=True,
                        before_labels=lambda: explain(rounds))   # the comment goes up before the label
            if not retry:
                return 80
            try:
                rc, failure, rounds = verifier.verify_retry(
                    str(wt), base, dev_vendor, "issue", issue, binding=binding,
                    repairs=remaining, repairs_before=rounds)
            finally:
                note_attempt(dev_vendor, "unknown", end)
        if rc:
            explain(rounds)
            return stop(tick, issue, reason, title, url)
    lines = Path(body).read_text().split("\n")
    try:
        text = pr_body(check_section.canonicalize(Path(body).read_text()), issue)
    except ValueError:
        return stop(tick, issue, "dev_task_invalid", f"#{issue}: invalid PR task relation",
                    f"Review the completion field in {body}. {url}")
    if rounds:
        for attempt in attempts:
            if attempt[0] == dev_vendor:
                attempt[1] = "unknown"
    feedback = owner_feedback_digest(env.owner, payload["comments"])
    model = attempts[-1][1] if attempts else "unknown"
    return push_with_retry(
        tick, env, issue, wt, branch, vendor=dev_vendor, title=lines[0], body_text=text,
        base=base, attempts=attempts, model=model, gate_result=verifier.gate_result or "",
        feedback=feedback)
