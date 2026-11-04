"""Cursor (`cursor-agent -p`). Cursor has no hook like guard.sh, so it gets no GitHub
access at all: it edits and commits in its worktree; the runner pushes, opens the PR
and comments for it. A read-only run that changed anything is reset here, before a
quota deferral could leave the change behind."""
from .common import Run, changed, child_env, events, git, last_result, outcome_line, read, restore, run_cli

KINDS = ("RESULT", "SPEC", "REVIEW")


def final_message(text):
    # .result joins every text Cursor wrote between tool calls; the final message is
    # the last assistant event.
    last = None
    for _, obj in events(text):
        if isinstance(obj, dict) and obj.get("type") == "assistant":
            last = obj
    content = ((last or {}).get("message") or {}).get("content") or []
    return "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")


def reconnected(text):
    """After a reconnect Cursor's result covers far less than the run did (#596, #470)."""
    return any(isinstance(obj, dict) and (obj.get("type"), obj.get("subtype")) == ("connection", "reconnecting")
               for _, obj in events(text))


def usage(result):
    try:
        u = result.get("usage") or {}
        def show(key):
            value = u.get(key)
            return "null" if value is None else value
        return (f"{int(result['duration_ms'] // 1000)}s in={show('inputTokens')} out={show('outputTokens')}"
                f" cache_read={show('cacheReadTokens')} cache_write={show('cacheWriteTokens')}")
    except (KeyError, TypeError):
        return ""


def run(req):
    head = git(req.cwd, "rev-parse", "HEAD") if req.readonly else None
    env = child_env(GH_TOKEN="no-github-for-cursor", GITHUB_TOKEN="", GIT_SSH_COMMAND="/bin/false",
                    GIT_TERMINAL_PROMPT="0")
    rc = run_cli(["cursor-agent", "-p", "--force", "--trust", "--model", req.model,
                  "--output-format", "stream-json", req.prompt],
                 req.log, req.minutes, env, cwd=req.cwd)
    text = read(req.log)
    result = last_result(text)
    reply = final_message(text)
    if not reply.strip():
        reply = result.get("result") or ""
    outcome = outcome_line(reply, KINDS)
    used = usage(result)
    if used and reconnected(text):
        used += " usage=partial"
    summary = f"{outcome or 'no result line'} {used}"
    outcome = outcome or f"RESULT: failed rc={rc} (log {req.log})"
    if head and changed(req.cwd, head):
        restore(req.cwd, head)
        outcome = f"RESULT: failed Cursor changed files in a read-only {req.role}"
    return Run(rc, outcome, reply, used, summary)
