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
QUOTA_ERROR = (r"usage limit (reached|exceeded)|(monthly|weekly|daily) (usage |request )?limit "
               r"(reached|exceeded)|(insufficient|exhausted) quota")
IMAGES = False
SCAN = False
SKIP_SCREENSHOT_AFTER_REVIEW = True
ROUTE_FIELDS = frozenset({"model"})
EMPTY_MODEL_OK = False
RECORDS_AUTHOR_MODEL = False
SAME_VENDOR_REVIEW = False
DEFAULT_MAX_MINUTES = 60
PREFLIGHT_CHECKS = (("cursor-agent", "--version"),)
VERSION_EXECUTABLE = "cursor-agent"
PROBE_SENDS_MODEL = False
RUNS_STEM = "cursor-runs"
LOG_SUFFIX = ""
CONFIG_FIELDS = frozenset()
BUSY_RERUN = False
REQUIRES_CACHE_DIRS = False
COMMIT_MARKERS = ("cursoragent@cursor.com",)
LEGACY_SEED_WITH_OWNER = False
UNMARKED_AUTHOR_INFERENCE = False
GITHUB_ACCESS = "runner"
FIX_RESUMABLE_VIA_REPLY = True
FIX_TEMPLATE = "cursor-fix-prompt.md"
READONLY_RESET = True
REVIEW_TEMPLATE = "cursor-review-prompt.md"
REVIEW_DELIVERY = "reply"
REVIEW_SKILL_POLICY = "none"
SCREENSHOT_DELIVERY = ""
SCREENSHOT_TOOL = ""
CONTINUITY_PIN = True
FIX_SCOPE_POLICY = "must_fix_nits_optional"
FIVE_MINUTE_CACHE_ROLES = ()
KINDS = ("RESULT", "SPEC", "REVIEW")
USAGE_EVENT = "result"
USAGE_FIELDS = {"input_tokens": "inputTokens", "cache_read_tokens": "cacheReadTokens",
                "cache_write_tokens": "cacheWriteTokens", "output_tokens": "outputTokens"}
USAGE_CACHE_IN_INPUT = False
SPEC_STAGE = "spec-review"


def configure(role, route, settings, env):
    return route.model, settings.max_minutes, LOG_SUFFIX, {}


def attach_dispatch_inputs(options, settings="", output=""):
    return options


def request_otel(run):
    return False


def run_env(role, run):
    return {}


def telemetry_env(role, run):
    return {}


def end_line_identity(model):
    return ""


def work_started(log_text):
    return False


def reported_model(run):
    return run.model


def probe_command(model, directory):
    return [], {}


def parse_quota_log(lines):
    return {}


def refresh_quota(state, now, **kwargs):
    from . import quota
    return quota.refresh_cursor(state, now, **kwargs)


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


def reconnect_row(row):
    return (row.get("type"), row.get("subtype")) == ("connection", "reconnecting")


def log_model(row, codex_home=None):
    """(done, model) for one log row; done stops the scan."""
    if row.get("type") == "system" and row.get("subtype") == "init":
        model = row.get("model")
        return True, None if str(model).lower() == "auto" else model
    return False, None
