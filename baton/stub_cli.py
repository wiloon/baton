"""Test-double adapter (`stub-agent`): registered for capability-routing coverage (#112).

Declares runner-mediated GitHub access, reply-protocol fixes, after-run read-only
reset and no image support. Production hosts leave it undeclared; tests admit it with
`[vendors.stub]` and the four role grants.
"""
import json

from .common import Run, changed, child_env, events, git, last_result, outcome_line, read, restore, run_cli

ID = "stub"
DISPLAY_NAME = "Stub"
ANALYST_ALIAS = "stub_fast"
SUPPORTED_ROLES = frozenset({"dev", "fixer", "reviewer", "spec_reviewer"})
QUOTA_SOURCE = "probe-run"
QUOTA_WINDOWS = frozenset({"seven_day"})
QUOTA_ERROR = r"stub quota exhausted|stub_usage_limit"
IMAGES = False
SCAN = False
SKIP_SCREENSHOT_AFTER_REVIEW = False
ROUTE_FIELDS = frozenset({"model", "effort"})
EMPTY_MODEL_OK = False
RECORDS_AUTHOR_MODEL = False
SAME_VENDOR_REVIEW = False
SAME_VENDOR_SAME_MODEL = False
DEFAULT_MAX_MINUTES = 30
PREFLIGHT_CHECKS = (("stub-agent", "--version"),)
VERSION_EXECUTABLE = "stub-agent"
PROBE_SENDS_MODEL = True
RUNS_STEM = "stub-runs"
LOG_SUFFIX = "-stub"
CONFIG_FIELDS = frozenset({"fallback_models", "quota_probe_model"})
BUSY_RERUN = False
REQUIRES_CACHE_DIRS = False
COMMIT_MARKERS = ()
LEGACY_SEED_WITH_OWNER = False
UNMARKED_AUTHOR_INFERENCE = True
GITHUB_ACCESS = "runner"
FIX_RESUMABLE_VIA_REPLY = True
FIX_TEMPLATE = "stub-fix-prompt.md"
READONLY_RESET = True
REVIEW_TEMPLATE = "stub-review-prompt.md"
REVIEW_DELIVERY = "reply"
REVIEW_SKILL_POLICY = "none"
SCREENSHOT_DELIVERY = ""
CONTINUITY_PIN = True
FIX_SCOPE_POLICY = "must_fix_nits_optional"
FIVE_MINUTE_CACHE_ROLES = ()
KINDS = ("RESULT", "REVIEW", "SPEC")
USAGE_EVENT = "result"
USAGE_FIELDS = {"input_tokens": "input_tokens", "cache_read_tokens": "cache_read_input_tokens",
                "cache_write_tokens": "cache_creation_input_tokens", "output_tokens": "output_tokens"}
USAGE_CACHE_IN_INPUT = False
SPEC_STAGE = "spec-reply"
PROBE_PROMPT = "Reply with the word ok."


def configure(role, route, settings, env):
    return route.model, settings.max_minutes, LOG_SUFFIX, {
        "effort": route.effort,
        "fallback_models": " ".join(settings.fallback_models),
    }


def attach_dispatch_inputs(options, settings="", output=""):
    return options


def request_otel(run):
    return False


def run_env(role, run):
    return {"STUB_RUN_ROLE": role}


def telemetry_env(role, run):
    return {}


def end_line_identity(model):
    return ""


def work_started(log_text):
    return False


def reported_model(run):
    return run.model


def probe_command(model, directory):
    return ["stub-agent", "probe", "--model", model, "--output-format", "stream-json", PROBE_PROMPT], {}


def parse_quota_log(lines):
    """Weekly window from a stub probe/run line: {"type":"stub_quota","seven_day":{...}}."""
    windows = {}
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "stub_quota":
            continue
        week = event.get("seven_day") or {}
        if "used" in week and "resets_at" in week:
            windows["seven_day"] = {"used": float(week["used"]), "resets_at": int(week["resets_at"])}
    return windows


def final_message(text):
    last = None
    for _, obj in events(text):
        if isinstance(obj, dict) and obj.get("type") == "assistant":
            last = obj
    content = ((last or {}).get("message") or {}).get("content") or []
    return "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")


def main_model(text):
    for _, event in events(text):
        if isinstance(event, dict) and event.get("type") == "result" and isinstance(event.get("model"), str):
            return event["model"]
        if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
            model = event.get("model")
            if isinstance(model, str) and model:
                return model
    return ""


def run(req):
    head = git(req.cwd, "rev-parse", "HEAD") if req.readonly else None
    env = child_env(**req.env)
    effort = ["--effort", req.effort] if req.effort else []
    rc = run_cli(["stub-agent", "-p", "--model", req.model, *effort,
                  "--output-format", "stream-json", req.prompt],
                 req.log, req.minutes, env, cwd=req.cwd)
    text = read(req.log)
    result = last_result(text)
    reply = final_message(text) or (result.get("result") or "")
    if not isinstance(reply, str):
        reply = str(reply)
    outcome = outcome_line(reply, KINDS) or f"RESULT: failed rc={rc} (log {req.log})"
    model = main_model(text)
    if head and changed(req.cwd, head):
        restore(req.cwd, head)
        outcome = f"RESULT: failed Stub changed files in a read-only {req.role}"
    return Run(rc, outcome, reply, "", f"{outcome} model={model or req.model}", model=model or req.model)


def reconnect_row(row):
    return False


def log_model(row, codex_home=None):
    """(done, model) for one log row; done stops the scan."""
    if row.get("type") == "system" and row.get("subtype") == "init":
        return True, row.get("model")
    return False, None
