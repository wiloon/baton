"""Claude Code (`claude -p`). Read-only and GitHub access come from the settings file
and guard.sh (a PreToolUse hook that reads BATON_AGENT_*), not from this adapter."""
import re

from .common import Run, child_env, events, last_result, outcome_line, read, run_cli

ID = "claude"
DISPLAY_NAME = "Claude"
ANALYST_ALIAS = "claude_strong"
SUPPORTED_ROLES = frozenset({
    "dev", "fixer", "reviewer", "screenshot_reviewer", "spec_reviewer", "reviser",
    "retrospective", "scan",
})
QUOTA_SOURCE = "probe-run"
QUOTA_WINDOWS = frozenset({"five_hour", "seven_day"})
IMAGES = True
SCAN = True
SKIP_SCREENSHOT_AFTER_REVIEW = False
ROUTE_FIELDS = frozenset({"model", "effort", "subagent_model"})
EMPTY_MODEL_OK = False
RECORDS_AUTHOR_MODEL = False
DEFAULT_MAX_MINUTES = 150
PREFLIGHT_CHECKS = (("claude", "--version"),)
PROMPT_PREFIX = ""
LOG_SUFFIX = ""
OTEL = True
FIVE_MINUTE_CACHE = True
FIVE_MINUTE_CACHE_ROLES = frozenset({"spec_review", "spec", "review", "screenshot_review", "fix"})
MODEL_LOG_LABEL = "claude_model"
GITHUB_VIA_RUNNER = False
DISPATCH_SETTINGS = True
DISPATCH_OUTPUT = False
VERSION_EXECUTABLE = "claude"
PROBE_SENDS_MODEL = True
RUNS_STEM = "runs"
SCREENSHOT_DELIVERY = "file"
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


def dispatch_options(env, role, route, settings):
    models = env.config.models
    return {
        "fallback_models": " ".join(settings.fallback_models),
        "budget": str(getattr(models, "budget_" + role)),
        "repo": str(env.dirs.clone),
        "subagent_model": route.subagent_model,
        "context_dir": str(env.dirs.ctx),
        "effort": route.effort,
    }


def telemetry_child_env(run, role, otel_stage):
    if not run:
        return {}
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_TRACES_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://127.0.0.1:4320/v1/metrics",
        "OTEL_METRICS_INCLUDE_SESSION_ID": "false",
        "OTEL_METRICS_INCLUDE_ACCOUNT_UUID": "false",
        "OTEL_METRICS_INCLUDE_VERSION": "false",
        "OTEL_METRIC_EXPORT_INTERVAL": "10000",
    }


def parse_quota_log(lines, _codex_home=None):
    from . import quota
    return quota.parse_claude(lines)


def analyst_effort(model, default):
    return default


def analyst_command(analyst, workspace, env, settings):
    return ['claude', '--safe-mode', '-p', '--model', analyst.model, '--effort', analyst.effort,
            '--output-format', 'json', '--tools', '', '--disallowedTools', '*',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--disable-slash-commands',
            '--setting-sources', '', '--settings', '{"disableAllHooks":true}',
            '--permission-mode', 'dontAsk', '--no-session-persistence', '--max-turns', '1',
            '--max-budget-usd', str(settings.budget_usd)]


def parse_analyst_result(raw, events_fn, code_re, final_message_fn):
    import json
    text = raw.decode(errors='replace')
    rows = [obj for _, obj in events_fn(text) if isinstance(obj, dict)]
    try:
        row = json.loads(text)
        rows = [row] if isinstance(row, dict) else rows
    except ValueError:
        pass
    result = next((r for r in reversed(rows) if r.get('type') == 'result'), {})
    models = result.get('modelUsage', {}) if isinstance(result.get('modelUsage'), dict) else {}
    actual = 'unknown'
    if models and all(code_re.fullmatch(k) for k in models):
        actual = next(iter(models)) if len(models) == 1 else 'mixed'
    elif code_re.fullmatch(str(result.get('actual_model', ''))):
        actual = result['actual_model']
    reply = result.get('result', '')
    valid = bool(result) and not result.get('is_error') and result.get('subtype') == 'success'
    tool_use = any(r.get('type') in ('tool_call', 'tool_use') for r in rows)
    return rows, reply, valid, actual, sorted(models) if isinstance(models, dict) else [], tool_use
