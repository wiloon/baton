"""Cursor (`cursor-agent -p`). Cursor has no hook like guard.sh, so it gets no GitHub
access at all: it edits and commits in its worktree; the runner pushes, opens the PR
and comments for it. A read-only run that changed anything is reset here, before a
quota deferral could leave the change behind."""
import json
import os
import subprocess
import urllib.request
from pathlib import Path

from .common import (MODEL_CODE, Run, analyst_result, analyst_rows, analyst_tool_use, changed, child_env, events,
                     git, last_result, outcome_line, read, restore, run_cli)

ID = "cursor"
DISPLAY_NAME = "Cursor"
ANALYST_ALIAS = "cursor_analyst"
# Retrospective analyst: approved models, its environment and how its budget is enforced.
ANALYST_FAMILY = r"grok-4\.7(?:-(?:high|xhigh)(?:-fast)?)?"
# A request must never use Auto or an implicit level; only a returned identity may be the base name.
ANALYST_REQUEST_FAMILY = r"grok-4\.7-(?:high|xhigh)(?:-fast)?"
ANALYST_MIXED_MODELS = False
ANALYST_BUDGET = "deadline_and_call_caps"
ANALYST_ENV_KEEP = ()
ANALYST_ENV_STRIP = ()
ANALYST_ENV_SET = {}
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
RECORDS_AUTHOR_MODEL = True
SAME_VENDOR_REVIEW = True
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
LEGACY_REPLY_RESUME = True
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
SPEC_SAME_VENDOR = ""
SPEC_TEMPLATE = "spec-prompt.md"
SPEC_UNMARKED_REVIEWER = False


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


def end_line_model(fields):
    return ""


def work_started(log_text):
    return False


def reported_model(run):
    return run.model


def probe_command(model, directory):
    return [], {}


def parse_quota_log(lines):
    return {}


USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
# Where cursor-agent >= 2026.11.19 keeps its login on Linux (secret-tool attributes).
SECRET_ATTRIBUTES = ("service", "cursor-credentials", "account", "cursor-user")


def parse_usage(payload):
    """The weekly window of a GetCurrentPeriodUsage answer: {name: {used, resets_at}}."""
    try:
        used = float(payload["planUsage"]["totalPercentUsed"]) / 100
        return {"seven_day": {"used": used, "resets_at": int(payload["billingCycleEnd"]) // 1000}}
    except (KeyError, TypeError, ValueError):
        return {}


def _fetch_usage(token):
    request = urllib.request.Request(USAGE_URL, data=b"{}", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Connect-Protocol-Version": "1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


class LoginUnreadable(ValueError):
    """auth.json records a Cursor login, but its access token can't be read."""


def _secret_lookup():
    """The credential cursor-agent keeps in the Secret Service (Linux), or None."""
    try:
        found = subprocess.run(["secret-tool", "lookup", *SECRET_ATTRIBUTES], capture_output=True, text=True,
                               timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return found.stdout if found.returncode == 0 else None


def access_token(auth, lookup=_secret_lookup):
    """Cursor's access token; None when this machine has no Cursor login.

    cursor-agent 2026.11.19 moved the login into the OS secret store and left only
    {"storedIn": "os-secret-store"} in auth.json; older versions keep accessToken there.
    Raises LoginUnreadable when a login is recorded but no token comes back, so the
    snapshot can't silently freeze at its last value."""
    try:
        stored = json.loads(Path(auth).read_text())
    except OSError:
        return None
    except ValueError as error:
        raise LoginUnreadable(f"cursor auth.json is not JSON: {error}") from None
    if not isinstance(stored, dict) or stored.get("signedOut") is True:
        return None
    if token := stored.get("accessToken"):
        return token
    if stored.get("storedIn") == "os-secret-store":
        try:
            token = json.loads(lookup() or "")["accessToken"]
        except (ValueError, TypeError, KeyError):
            token = None
        if not token:
            raise LoginUnreadable("cursor login is in the OS secret store, but secret-tool returned no "
                                  f"access token ({' '.join(SECRET_ATTRIBUTES)})")
        return token
    raise LoginUnreadable("cursor auth.json has neither accessToken nor storedIn")


def refresh_quota(state, now, auth=None, fetch=_fetch_usage, lookup=_secret_lookup):
    """Record Cursor's usage; {} when it isn't logged in here. Raises when the login can't be
    read (recording probe_failed) or the API fails."""
    from . import quota
    try:
        token = access_token(auth or Path.home() / ".config/cursor/auth.json", lookup)
    except LoginUnreadable:
        quota.credential(state, ID, "probe_failed", now)
        raise
    if token is None:
        return {}
    windows = parse_usage(fetch(token))
    if windows:
        quota.record(state, ID, windows, now)
        quota.credential(state, ID, "authenticated", now)
    return windows


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
    # cursor-agent names no model, so the request does (ADR 0004 exception).
    model = req.model if req.model and req.model.lower() != "auto" else "unknown"
    return Run(rc, outcome, reply, used, summary, model=model)


def reconnect_row(row):
    return (row.get("type"), row.get("subtype")) == ("connection", "reconnecting")


def log_model(row, codex_home=None):
    """(done, model) for one log row; done stops the scan."""
    if row.get("type") == "system" and row.get("subtype") == "init":
        model = row.get("model")
        return True, None if str(model).lower() == "auto" else model
    return False, None


def analyst_effort(model, default):
    # Cursor advertises explicit reasoning-level model IDs.
    return "xhigh" if "-xhigh" in model else "high"


def analyst_command(settings, analyst, workspace, env):
    # Cursor's existing subscription supplies Grok. Never use Auto or --force.
    directory = workspace / "cursor-config"
    directory.mkdir(mode=0o700)
    denied = ["Shell(*)", "Read(**)", "Read(/**)", "Write(**)", "Write(/**)", "WebFetch(*)", "Mcp(*:*)"]
    (directory / "cli-config.json").write_text(json.dumps(dict(
        version=1, editor={"vimMode": False}, permissions={"allow": [], "deny": denied})))
    (directory / "mcp.json").write_text('{"mcpServers":{}}')
    auth = Path(os.environ.get("CURSOR_CONFIG_DIR") or Path.home() / ".config/cursor") / "auth.json"
    if auth.is_file():
        (directory / "auth.json").symlink_to(auth)
    env["CURSOR_CONFIG_DIR"] = str(directory)
    # Project permissions are an additional deny layer even on older config resolvers.
    project = workspace / ".cursor"
    project.mkdir(mode=0o700)
    (project / "cli.json").write_text(json.dumps({"permissions": {"allow": [], "deny": denied}}))
    return ["cursor-agent", "-p", "--mode", "ask", "--trust", "--model", analyst.model,
            "--output-format", "stream-json"]


def analyst_response(text):
    """(rows, reply, valid, actual model, models, tool_use) of the analyst's output."""
    rows = analyst_rows(text)
    result = analyst_result(rows)
    actual = result["actual_model"] if MODEL_CODE.fullmatch(str(result.get("actual_model", ""))) else "unknown"
    reply = final_message(text) or result.get("result", "")
    valid = bool(result) and not result.get("is_error") and result.get("subtype") == "success"
    return rows, reply, valid, actual, [], analyst_tool_use(rows)


def analyst_quota(lines, home):
    return {}
