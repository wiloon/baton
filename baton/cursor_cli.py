"""Cursor (`cursor-agent -p`). Cursor has no hook like guard.sh, so it gets no GitHub
access at all: it edits and commits in its worktree; the runner pushes, opens the PR
and comments for it. A read-only run that changed anything is reset here, before a
quota deferral could leave the change behind."""
from .common import Run, changed, child_env, events, git, last_result, outcome_line, read, restore, run_cli

ID = "cursor"
DISPLAY_NAME = "Cursor"
ANALYST_ALIAS = "cursor_analyst"
SUPPORTED_ROLES = frozenset({
    "dev", "fixer", "reviewer", "spec_reviewer", "retrospective",
})
QUOTA_SOURCE = "api"
QUOTA_WINDOWS = frozenset({"seven_day"})
IMAGES = False
SCAN = False
SKIP_SCREENSHOT_AFTER_REVIEW = True
ROUTE_FIELDS = frozenset({"model"})
EMPTY_MODEL_OK = False
RECORDS_AUTHOR_MODEL = False
DEFAULT_MAX_MINUTES = 60
PREFLIGHT_CHECKS = (("cursor-agent", "--version"),)
PROMPT_PREFIX = "cursor-"
LOG_SUFFIX = ""
OTEL = False
FIVE_MINUTE_CACHE = False
MODEL_LOG_LABEL = ""
GITHUB_VIA_RUNNER = True
DISPATCH_SETTINGS = False
DISPATCH_OUTPUT = False
VERSION_EXECUTABLE = "cursor-agent"
PROBE_SENDS_MODEL = False
RUNS_STEM = "cursor-runs"
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


def dispatch_options(env, role, route, settings):
    return {}


def telemetry_child_env(run, role, otel_stage):
    return {}


def parse_quota_log(lines, _codex_home=None):
    return {}


def analyst_effort(model, default):
    return 'xhigh' if '-xhigh' in model else 'high'


def analyst_command(analyst, workspace, env, settings):
    import json
    import os
    from pathlib import Path
    directory = workspace / 'cursor-config'
    directory.mkdir(mode=0o700)
    denied = ['Shell(*)', 'Read(**)', 'Read(/**)', 'Write(**)', 'Write(/**)',
              'WebFetch(*)', 'Mcp(*:*)']
    (directory / 'cli-config.json').write_text(json.dumps(dict(
        version=1, editor={'vimMode': False}, permissions={'allow': [], 'deny': denied})))
    (directory / 'mcp.json').write_text('{"mcpServers":{}}')
    auth = Path(os.environ.get('CURSOR_CONFIG_DIR') or Path.home() / '.config/cursor') / 'auth.json'
    if auth.is_file():
        (directory / 'auth.json').symlink_to(auth)
    env['CURSOR_CONFIG_DIR'] = str(directory)
    project = workspace / '.cursor'
    project.mkdir(mode=0o700)
    (project / 'cli.json').write_text(json.dumps({'permissions': {'allow': [], 'deny': denied}}))
    return ['cursor-agent', '-p', '--mode', 'ask', '--trust', '--model', analyst.model,
            '--output-format', 'stream-json']


def parse_analyst_result(raw, events_fn, code_re, final_message_fn):
    text = raw.decode(errors='replace')
    rows = [obj for _, obj in events_fn(text) if isinstance(obj, dict)]
    result = next((r for r in reversed(rows) if r.get('type') == 'result'), {})
    actual = 'unknown'
    if code_re.fullmatch(str(result.get('actual_model', ''))):
        actual = result['actual_model']
    reply = final_message_fn(text) or result.get('result', '')
    valid = bool(result) and not result.get('is_error') and result.get('subtype') == 'success'
    tool_use = any(r.get('type') in ('tool_call', 'tool_use') or
                   (isinstance(r.get('item'), dict) and r['item'].get('type') in
                    ('command_execution', 'file_change', 'mcp_tool_call', 'web_search')) for r in rows)
    return rows, reply, valid, actual, [], tool_use
