"""Claude Code (`claude -p`). Read-only and GitHub access come from the settings file
and guard.sh (a PreToolUse hook that reads BATON_AGENT_*), not from this adapter."""
import re

from .common import Run, child_env, events, last_result, outcome_line, read, run_cli

KINDS = ("RESULT", "REVIEW", "VERDICT", "SCAN", "SPEC")
MODEL = re.compile(
    r"(?P<prefix>claude-)?(?P<family>opus|sonnet|haiku)(?P<version>(?:-[0-9]+)*?)(?:-(?P<snapshot>[0-9]{8}))?"
)


def distinct_models(first, second):
    """Compare concrete IDs, conservatively treating shorthand as the whole family."""
    a, b = MODEL.fullmatch(first), MODEL.fullmatch(second)
    if not (a and b):
        return False
    if a["family"] != b["family"]:
        return True
    if not (a["prefix"] and b["prefix"] and a["version"] and b["version"]):
        return False
    # An unpinned version can resolve to its dated snapshot.
    return a["version"] != b["version"] or bool(
        a["snapshot"] and b["snapshot"] and a["snapshot"] != b["snapshot"])


def main_model(text):
    # Init describes the selection before fallback. Result usage can include subagents.
    models = set()
    for _, event in events(text):
        if (isinstance(event, dict) and event.get("type") == "assistant"
                and "parent_tool_use_id" in event and event["parent_tool_use_id"] is None):
            message = event.get("message")
            model = message.get("model") if isinstance(message, dict) else None
            if not isinstance(model, str) or not MODEL.fullmatch(model):
                return ""
            models.add(model)
    return models.pop() if len(models) == 1 else ""


def usage(result):
    u = result.get("usage") or {}
    def show(key):
        value = u.get(key)
        return "null" if value is None else value
    return (f"in={show('input_tokens')} out={show('output_tokens')} cache_read={show('cache_read_input_tokens')}"
            f" cache_write={show('cache_creation_input_tokens')} cost={result.get('total_cost_usd', 'null')}")


def run(req):
    env = child_env(BATON_AGENT="1", BATON_AGENT_ROLE=req.role, BATON_AGENT_WORKTREE=str(req.cwd),
                    BATON_AGENT_REPO=req.repo, BATON_AGENT_LOGS=str(req.log.parent), **req.env)
    if req.subagent_model:
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = req.subagent_model
    fallback = ["--fallback-model", req.fallback] if req.fallback else []
    # Without it dontAsk denies shell commands on the context files the prompt names, which sit
    # outside the worktree (`wc -l <ctx>/...md`, #53); Read was never affected. guard.sh still
    # limits writes to the worktree and /tmp.
    context = ["--add-dir", req.context_dir] if req.context_dir else []
    effort = ["--effort", req.effort] if req.effort else []
    rc = run_cli(["claude", "-p", req.prompt,
                  "--model", req.model, *fallback, *context, *effort,
                  "--max-budget-usd", req.budget,
                  "--setting-sources", "user",
                  "--settings", req.settings,
                  "--permission-mode", "dontAsk",
                  "--permission-prompts", "none",
                  "--output-format", "stream-json", "--verbose"],
                 req.log, req.minutes, env, cwd=req.cwd)
    text = read(req.log)
    result = last_result(text)
    final = result.get("result") or ""
    if not isinstance(final, str):
        final = str(final)
    outcome = outcome_line(final, KINDS)
    if not outcome and result.get("subtype") == "error_max_budget_usd":
        outcome = f"RESULT: failed budget ${req.budget} exceeded (log {req.log})"
    used = usage(result)
    return Run(rc, outcome or f"RESULT: failed rc={rc} (log {req.log})", final, used,
               f"{outcome or 'no result line'} {used} effort={req.effort or 'default'}", model=main_model(text))
