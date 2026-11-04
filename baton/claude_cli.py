"""Claude Code (`claude -p`). Read-only and GitHub access come from the settings file
and guard.sh (a PreToolUse hook that reads BATON_AGENT_*), not from this adapter."""
from .common import Run, child_env, last_result, outcome_line, read, run_cli

KINDS = ("RESULT", "REVIEW", "VERDICT", "SCAN", "SPEC")


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
    rc = run_cli(["claude", "-p", req.prompt,
                  "--model", req.model, *fallback,
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
               f"{outcome or 'no result line'} {used}")
