"""Antigravity CLI (`agy`), Gemini models only (#109).

agy also serves Claude and GPT-OSS models; a Claude model run through it would review
Claude's own work while counting as another vendor, so `valid_model` admits `gemini-*` only
and every run proves the model it used (ADR 0004). The CLI never fails a run that was denied
a tool: it exits 0 with an empty response, so denied actions, an empty response and bad model
evidence all make a failed run here, and a failed write run is reset to its start commit.
Like Cursor it gets no GitHub access: the runner pushes and posts for it.
"""
import json
import math
import os
import re
import shlex
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .common import Run, changed, child_env, events, git, outcome_line, read, restore, run_cli

ID = "agy"
DISPLAY_NAME = "Antigravity"
ANALYST_ALIAS = "agy_analyst"
# Not retrospective-capable (SUPPORTED_ROLES): the analyst constants only keep registry iteration total.
ANALYST_FAMILY = r"(?!)"
ANALYST_REQUEST_FAMILY = ANALYST_FAMILY
ANALYST_MIXED_MODELS = False
ANALYST_BUDGET = "deadline_and_call_caps"
ANALYST_ENV_KEEP = ()
ANALYST_ENV_STRIP = ()
ANALYST_ENV_SET = {}
SUPPORTED_ROLES = frozenset({"dev", "fixer", "reviewer", "spec_reviewer"})
QUOTA_SOURCE = "api"
QUOTA_WINDOWS = frozenset({"five_hour", "seven_day"})
# A run's output carries no quota data (refresh_quota asks `agy -p /quota`); a limit a run hits
# shows as a failed result (RESOURCE_EXHAUSTED / 429).
QUOTA_ERROR = r"RESOURCE_EXHAUSTED|\b429\b"
IMAGES = False
SCAN = False
SKIP_SCREENSHOT_AFTER_REVIEW = True
ROUTE_FIELDS = frozenset({"model"})
EMPTY_MODEL_OK = False
RECORDS_AUTHOR_MODEL = True
SAME_VENDOR_REVIEW = True
SAME_VENDOR_SAME_MODEL = False
# config.load: the review route's model must differ from the dev/fix routes' (effort alone is not distinct).
DISTINCT_REVIEW_MODEL = True
DEFAULT_MAX_MINUTES = 60
PREFLIGHT_CHECKS = (("agy", "--version"),)
VERSION_EXECUTABLE = "agy"
PROBE_SENDS_MODEL = False
RUNS_STEM = "agy-runs"
LOG_SUFFIX = "-agy"
CONFIG_FIELDS = frozenset({"fallback_models", "quota_probe_model"})
BUSY_RERUN = False
REQUIRES_CACHE_DIRS = False
COMMIT_MARKERS = ()
LEGACY_SEED_WITH_OWNER = False
UNMARKED_AUTHOR_INFERENCE = True
GITHUB_ACCESS = "runner"
FIX_RESUMABLE_VIA_REPLY = True
FIX_TEMPLATE = "agy-fix-prompt.md"
READONLY_RESET = True
REVIEW_TEMPLATE = "agy-review-prompt.md"
REVIEW_DELIVERY = "reply"
REVIEW_SKILL_POLICY = "none"
SCREENSHOT_DELIVERY = ""
SCREENSHOT_TOOL = ""
CONTINUITY_PIN = True
FIX_SCOPE_POLICY = "must_fix_nits_optional"
FIVE_MINUTE_CACHE_ROLES = ()
KINDS = ("RESULT", "SPEC", "REVIEW")
# The stream keys its events by "event"; usage sits inside the result (usage_of).
USAGE_EVENT = "result"
USAGE_EVENT_KEY = "event"
USAGE_FIELDS = {"input_tokens": "input_tokens", "cache_read_tokens": "cache_read_tokens",
                "cache_write_tokens": "cache_write_tokens", "output_tokens": "output_tokens"}
USAGE_CACHE_IN_INPUT = False
SPEC_STAGE = "spec-review"
SPEC_SAME_VENDOR = "known-distinct-model"
SPEC_TEMPLATE = "spec-prompt.md"
SPEC_UNMARKED_REVIEWER = False

CLI_LOG = ".gemini/antigravity-cli/cli.log"
# Read-only commands every role needs, and what dev/fix add on top. The repository's CI command
# and verify hook are appended per run (permissions_settings).
# Read-only inspection commands. A headless agy run stops with an empty reply at the first denied
# command, so a reviewer exploring with `find ... | sort` failed the whole review (PR #244).
READ_COMMANDS = ("git", "rg", "grep", "ls", "cat", "head", "tail", "wc", "find", "sort", "uniq", "diff")
WRITE_COMMANDS = READ_COMMANDS + ("python3", "mkdir")
EFFORT = re.compile(r"-(?:low|medium|high|xhigh)$")


def valid_model(model):
    """Gemini models that do the work themselves; orchestrators hand it to subagents (untested)."""
    return model.startswith("gemini-") and "orchestrator" not in model


def distinct_models(first, second):
    """Both known, and different once the effort suffix is dropped."""
    if not all(value and value != "unknown" for value in (first, second)):
        return False
    return EFFORT.sub("", first) != EFFORT.sub("", second)


def configure(role, route, settings, env):
    # The PR-description artifact lives in /tmp (dev); the ctx directory holds the prompts' context files.
    dirs = [str(env.dirs.ctx), "/tmp"] if role == "dev" else [str(env.dirs.ctx)]
    return route.model, settings.max_minutes, LOG_SUFFIX, {
        "fallback_models": " ".join(settings.fallback_models),
        "add_dirs": dirs, "shots_dir": env.runtime.shots_dir,
    }


def attach_dispatch_inputs(options, settings="", output=""):
    options["output"] = output
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
    """The model the runner.log end line records (the summary ends in model=...)."""
    return next((field.split("=", 1)[1] for field in fields if field.startswith("model=")), "")


def work_started(log_text):
    return False


def reported_model(run):
    return run.model


def probe_command(model, directory):
    return [], {}


def parse_quota_log(lines):
    return {}


# `agy -p /quota --output-format json` answers from the account's usage without a model turn
# (#252). baton runs agy on Gemini models only, so only that group's windows count.
QUOTA_COMMAND = ("agy", "-p", "/quota", "--output-format", "json")
QUOTA_TIMEOUT = 30
QUOTA_GROUP = "Gemini Models"
QUOTA_BUCKETS = {"5h": "five_hour", "weekly": "seven_day"}
# The reset time of a window nothing has used yet: no reset is pending.
NO_RESET = 0


class QuotaError(RuntimeError):
    """`agy -p /quota` gave no usable answer."""


def _run_quota(command, *, cwd, env, timeout):
    return subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=timeout, check=False)


def _reset_epoch(value):
    if not isinstance(value, str):
        raise QuotaError(f"agy quota: reset_time {value!r} is not a time")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise QuotaError(f"agy quota: reset_time {value!r} is not a time") from None
    if moment.tzinfo is None:
        raise QuotaError(f"agy quota: reset_time {value!r} has no time zone")
    return int(moment.timestamp())


def parse_quota(answer, now):
    """{five_hour, seven_day} of the Gemini group of a `/quota` JSON answer; raises QuotaError
    unless both windows parse. A never-used window resets one window length from NOW."""
    from . import quota
    try:
        groups = answer["command"]["data"]["groups"]
        group = next(g for g in groups if isinstance(g, dict) and g.get("name") == QUOTA_GROUP)
        buckets = {b.get("window"): b for b in group["buckets"] if isinstance(b, dict)}
    except (KeyError, TypeError, StopIteration):
        raise QuotaError(f"agy quota: no {QUOTA_GROUP} group in the answer") from None
    windows = {}
    for window, name in QUOTA_BUCKETS.items():
        if window not in buckets:
            raise QuotaError(f"agy quota: no {window} bucket in {QUOTA_GROUP}")
        bucket = buckets[window]
        remaining = bucket.get("remaining_fraction")
        if (isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining)
                or not 0 <= remaining <= 1):
            raise QuotaError(f"agy quota: {window} remaining_fraction {remaining!r} is not a fraction")
        reset = _reset_epoch(bucket.get("reset_time"))
        if reset == NO_RESET:
            reset = int(now) + quota.WINDOW_SECONDS[name]
        windows[name] = {"used": 1 - remaining, "resets_at": reset}
    return windows


def _auth_text(stderr, answer):
    """What may show a logout: stderr and the answer's status/error/message, never its response."""
    keep = {}
    if isinstance(answer, dict):
        keep = {key: answer[key] for key in ("status", "error", "message") if key in answer}
    return f"{stderr or ''}\n{json.dumps(keep, ensure_ascii=False)}"


def refresh_quota(state, now, run=_run_quota):
    """Record agy's Gemini five-hour and weekly windows from `agy -p /quota`. Raises when the CLI
    gives no usable answer, recording nothing (auth_expired when the failure reads as a logout).
    RUN takes (command, cwd=, env=, timeout=) and returns a CompletedProcess."""
    from . import agents, quota
    env = child_env(GH_TOKEN="no-github-for-agy", GITHUB_TOKEN="", GIT_SSH_COMMAND="/bin/false",
                    GIT_TERMINAL_PROMPT="0")
    with tempfile.TemporaryDirectory() as directory:
        done = run(list(QUOTA_COMMAND), cwd=directory, env=env, timeout=QUOTA_TIMEOUT)
    try:
        answer = json.loads(done.stdout or "")
    except ValueError:
        answer = None
    status = answer.get("status") if isinstance(answer, dict) else None
    if done.returncode != 0 or status != "SUCCESS":
        if re.search(agents.AUTH, _auth_text(done.stderr, answer), re.I):
            quota.credential(state, ID, "auth_expired", now)
            raise QuotaError("agy quota: not logged in")
        if done.returncode != 0:
            raise QuotaError(f"agy quota: rc={done.returncode}")
        if answer is None:
            raise QuotaError("agy quota: the answer is not JSON")
        raise QuotaError(f"agy quota: status {status or 'missing'}")
    windows = parse_quota(answer, now)
    quota.record(state, ID, windows, now)
    quota.credential(state, ID, "authenticated", now)
    return windows


# No preflight: agy keeps its login in the keyring when it runs outside an SSH session (the runner's
# systemd units: "ChainedAuth: authenticated via keyring"), and writes
# ~/.gemini/antigravity-cli/antigravity-oauth-token only for SSH sessions, a file that later
# disappears while the keyring login stays valid. Checking that file put a logged-in agy into a
# 4-hour auth cooldown (PR #247). A real logout shows as the run's result error
# ("authentication failed or timed out"), which classify() reads as auth through error_text().


def simple_commands(command):
    """The simple commands of a shell line, split on &&, ||, ; and |."""
    return [part.strip() for part in re.split(r"&&|\|\||;|\||\n", command) if part.strip()]


def command_rule(simple):
    """`command(<binary> [subcommand])`: whole-word prefix rules, no `*`. The binary, plus a second
    word when it is a plain word or path (`uv run`, `bash bin/verify.sh`); flags are not part of it."""
    try:
        words = shlex.split(simple)
    except ValueError:
        words = simple.split()
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words.pop(0)
    if not words:
        return ""
    rule = [words[0]]
    if len(words) > 1 and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./:-]*", words[1]):
        rule.append(words[1])
    return f"command({' '.join(rule)})"


def permissions_settings(role, ci_command, verify_hook=None):
    """The --settings JSON: an exact allowlist, never --dangerously-skip-permissions. Read roles get
    the read commands; dev and fix add what they need and the repository's own checks."""
    write = role in ("dev", "fix")
    rules = [f"command({name})" for name in (WRITE_COMMANDS if write else READ_COMMANDS)]
    if write:
        simples = simple_commands(ci_command or "")
        if verify_hook:
            simples.append(str(verify_hook))
        rules += [rule for rule in map(command_rule, simples) if rule]
    return json.dumps({"permissions": {"allow": list(dict.fromkeys(rules))}}, separators=(",", ":"))


def result_event(text):
    """The last `{"event":"result","result":{...}}` of a log: its inner object, or {}."""
    found = {}
    for _, obj in events(text):
        if isinstance(obj, dict) and obj.get("event") == "result" and isinstance(obj.get("result"), dict):
            found = obj["result"]
    return found


def error_text(text):
    """What agents.classify reads besides raw diagnostics: a result that did not succeed, only its
    status and error fields (never the response or denied actions, which carry untrusted text)."""
    result = result_event(text)
    if not result or result.get("status") == "SUCCESS":
        return ""
    keep = {key: result[key] for key in ("status", "error", "message") if key in result}
    return json.dumps(keep, separators=(",", ":"), ensure_ascii=False)


def init_event(text):
    for _, obj in events(text):
        if isinstance(obj, dict) and obj.get("event") == "init":
            return obj
    return {}


def init_model(row):
    """The model of an init event: the real CLI nests it (`{"event":"init","init":{"model":...}}`,
    #233); a top-level `model` is still accepted for older recorded logs."""
    nested = row.get("init")
    model = nested.get("model") if isinstance(nested, dict) else None
    if not (isinstance(model, str) and model):
        model = row.get("model")
    return model if isinstance(model, str) and model else ""


def usage_of(row):
    """The result's usage with snake_case token names, whatever case the CLI spells them in."""
    raw = (row.get("result") or {}).get("usage")
    if not isinstance(raw, dict):
        return None
    return {re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower(): value for key, value in raw.items()}


def usage(result):
    used = usage_of({"result": result}) or {}
    return (f"in={used.get('input_tokens', 'null')} out={used.get('output_tokens', 'null')}"
            f" cache_read={used.get('cache_read_tokens', 'null')}")


def label_to_model(label):
    """`Gemini 4 Neon (High)` -> `gemini-4-neon-high`: lowercase, the parenthesised effort moves to a
    suffix, spaces become `-`, a `.` in a version stays. "" when the label does not fit that rule."""
    m = re.fullmatch(r"([A-Za-z0-9. ]+?)\s*\((low|medium|high|xhigh)\)", label.strip(), re.I)
    if not m:
        return ""
    name = "-".join(m[1].lower().split())
    return f"{name}-{m[2].lower()}"


def cli_log_model(conversation, path=None):
    """The model cli.log recorded for CONVERSATION. The file is shared with interactive sessions, so
    only lines carrying this run's conversation id count; the last one wins."""
    if not conversation:
        return ""
    path = Path(path) if path else Path.home() / CLI_LOG
    found = ""
    for line in read(path).splitlines():
        if conversation in line and "model override" in line and (m := re.search(r'label="([^"]+)"', line)):
            found = label_to_model(m[1])
    return found


def reported(text, log_path=None):
    """(model, source): init.model when present, else the cli.log entry of this run's conversation."""
    init = init_event(text)
    if model := init_model(init):
        return model, "init"
    conversation = init.get("conversation_id") or result_event(text).get("conversation_id") or ""
    return cli_log_model(conversation, log_path), "cli.log"


def failure(req, result, model):
    """Why this run cannot count, or "". Denied tools and an empty reply still exit 0."""
    if result.get("status") != "SUCCESS":
        return f"Agy status {result.get('status') or 'missing'}"
    if result.get("denied_actions"):
        return f"Agy actions denied by the allowlist ({len(result['denied_actions'])})"
    if not (result.get("response") or "").strip():
        return "Agy returned an empty response"
    if not model:
        return "Agy reported no model"
    if not valid_model(model):
        return f"Agy reported a non-Gemini model {model}"
    if model != req.model:
        return f"Agy reported model {model}, requested {req.model}"
    return ""


def log_model(row, codex_home=None):
    """(done, model) for one log row; done stops the scan."""
    if row.get("event") == "init":
        return True, init_model(row) or None
    return False, None


def reconnect_row(row):
    return False


def run(req):
    from . import session
    head = git(req.cwd, "rev-parse", "HEAD")
    repo = session.resolve().repo
    settings = permissions_settings(req.role, repo.ci_command, repo.verify_hook)
    mode = "plan" if req.readonly else "accept-edits"
    seconds = max(req.minutes * 60 - 60, 60)
    dirs = [req.shots_dir, os.path.dirname(req.output) if req.output else ""]
    if not req.readonly:
        # A linked worktree keeps its objects and refs outside the checkout; commits need both.
        dirs = [git(req.cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                git(req.cwd, "rev-parse", "--absolute-git-dir"), *dirs]
    dirs = [d for d in dict.fromkeys([*req.add_dirs, *dirs]) if d]
    env = child_env(**req.env, GH_TOKEN="no-github-for-agy", GITHUB_TOKEN="", GIT_SSH_COMMAND="/bin/false",
                    GIT_TERMINAL_PROMPT="0")
    # -p takes the prompt and must be last.
    rc = run_cli(["agy", "--output-format", "stream-json", "--model", req.model, "--mode", mode, "--sandbox",
                  "--settings", settings, "--print-timeout", f"{seconds}s",
                  *[arg for d in dirs for arg in ("--add-dir", d)], "-p", req.prompt],
                 req.log, req.minutes, env, cwd=req.cwd)
    text = read(req.log)
    result = result_event(text)
    reply = result.get("response") if isinstance(result.get("response"), str) else ""
    model, _ = reported(text)
    used = usage(result) if result else ""
    outcome = outcome_line(reply, KINDS)
    why = failure(req, result, model) if rc == 0 else ""
    if why:
        outcome = f"RESULT: failed {why}"
        if head and not req.readonly:
            restore(req.cwd, head)
    else:
        outcome = outcome or f"RESULT: failed rc={rc} (log {req.log})"
    if req.readonly and head and changed(req.cwd, head):
        restore(req.cwd, head)
        outcome = f"RESULT: failed Agy changed files in a read-only {req.role}"
    verified = model if rc == 0 and not why and result else ""
    return Run(rc, outcome, reply, used, f"{outcome} {used} model={verified or req.model}", model=verified)
