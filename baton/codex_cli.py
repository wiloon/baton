"""Codex (`codex exec`). Its sandbox enforces read-only and network access; GitHub
credentials are invalid. Codex writes artifacts into a scratch directory inside the
worktree; only the runner copies them out (markers stripped) to the path the caller
named, so only the runner publishes artifacts and markers."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .common import Run, changed, child_env, events, git, read, restore, run_cli, strip_markers

ID = "codex"
DISPLAY_NAME = "Codex"
ANALYST_ALIAS = "codex_fast"
SUPPORTED_ROLES = frozenset({
    "dev", "fixer", "reviewer", "screenshot_reviewer", "spec_reviewer", "reviser",
    "retrospective",
})
QUOTA_SOURCE = "probe-run"
QUOTA_WINDOWS = frozenset({"five_hour", "seven_day"})
QUOTA_ERROR = r"usage_limit_reached|hit your usage limit|usage limit (reached|exceeded)|insufficient_quota"
IMAGES = True
SCAN = False
SKIP_SCREENSHOT_AFTER_REVIEW = False
ROUTE_FIELDS = frozenset({"model", "effort"})
EMPTY_MODEL_OK = True
RECORDS_AUTHOR_MODEL = True
SAME_VENDOR_REVIEW = True
DEFAULT_MAX_MINUTES = 60
PREFLIGHT_CHECKS = (("codex", "--version"), ("codex", "login", "status"))
VERSION_EXECUTABLE = "codex"
PROBE_SENDS_MODEL = False
RUNS_STEM = "codex-runs"
LOG_SUFFIX = "-codex"
CONFIG_FIELDS = frozenset({"fallback_models", "cache_dirs"})
BUSY_RERUN = True
REQUIRES_CACHE_DIRS = True
COMMIT_MARKERS = ()
LEGACY_SEED_WITH_OWNER = False
UNMARKED_AUTHOR_INFERENCE = True
GITHUB_ACCESS = "denied"
FIX_RESUMABLE_VIA_REPLY = False
FIX_TEMPLATE = "codex-fix-prompt.md"
READONLY_RESET = True
REVIEW_TEMPLATE = "codex-review-prompt.md"
REVIEW_DELIVERY = "artifact"
REVIEW_SKILL_POLICY = "direct"
SCREENSHOT_DELIVERY = "final_message"
CONTINUITY_PIN = False
FIX_SCOPE_POLICY = "every_point"
FIVE_MINUTE_CACHE_ROLES = ()
USAGE_EVENT = "turn.completed"
USAGE_FIELDS = {"input_tokens": "input_tokens", "cache_read_tokens": "cached_input_tokens",
                "cache_write_tokens": "cache_write_input_tokens", "output_tokens": "output_tokens",
                "reasoning_tokens": "reasoning_output_tokens"}
# Codex counts cached tokens inside input_tokens; telemetry stores them separately.
USAGE_CACHE_IN_INPUT = True
SPEC_STAGE = "spec-reply"
OTEL = "http://127.0.0.1:4318/v1/"
OTEL_STAGE = {"spec": "spec-reply", "spec_review": "spec-review", "review": "review",
              "screenshot_review": "review", "fix": "fix"}
PROBE_PROMPT = "Reply with the word ok."


def configure(role, route, settings, env):
    return route.model, settings.max_minutes, LOG_SUFFIX, {
        "effort": route.effort, "fallback_models": " ".join(settings.fallback_models),
        "add_dirs": list(map(str, env.caches)), "shots_dir": env.runtime.shots_dir,
        "skills_dir": env.skills,
    }


def attach_dispatch_inputs(options, settings="", output=""):
    options["output"] = output
    return options


def request_otel(run):
    return bool(run)


def run_env(role, run):
    return {}


def telemetry_env(role, run):
    attrs = f"baton.run_id={run.run_id},baton.role={role}"
    if run.issue:
        attrs += f",baton.issue={run.issue}"
    if run.pr:
        attrs += f",baton.pr={run.pr}"
    if role in OTEL_STAGE:
        attrs += f",baton.stage={OTEL_STAGE[role]}"
    if run.pipeline_id:
        attrs += f",baton.pipeline_id={run.pipeline_id}"
    return {"TRACEPARENT": f"00-{run.trace_id}-{run.span_id}-01", "OTEL_RESOURCE_ATTRIBUTES": attrs,
            "OTEL_METRIC_EXPORT_INTERVAL": "1000"}


def end_line_identity(model):
    return ""


def work_started(log_text):
    return any(isinstance(obj, dict) and obj.get("type") in ("item.started", "item.completed")
               for _, obj in events(log_text))


def reported_model(run):
    actual = re.search(r"\bmodel=([^\s]+)", run.summary)
    return actual.group(1) if actual else run.model


def probe_command(model, directory):
    return ["codex", "exec", "--ignore-user-config", "--ignore-rules", "-s", "read-only", "--skip-git-repo-check",
            "-C", str(directory), "--json", PROBE_PROMPT], {
        "GH_TOKEN": "invalid", "GITHUB_TOKEN": "invalid", "GIT_SSH_COMMAND": "/bin/false"}


def parse_quota_log(lines):
    from . import quota
    return quota.parse_codex(lines, os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))


# The terminal verdict each role may end with (its final message's last line).
VERDICTS = {
    "review": ("REVIEW: pass", "REVIEW: changes"),
    "screenshot_review": ("REVIEW: pass", "REVIEW: changes"),
    "spec": ("SPEC: revised", "SPEC: human"),
    "fix": ("RESULT: fixed", "RESULT: needs-info"),
    "spec_review": ("SPEC: accept", "SPEC: changes", "SPEC: reject"),
    "dev": ("RESULT: ready",),
}
# Fixed context the runner's agents never use (measured with codex-cli 0.163.0: a "reply
# ok" run in a mid-sized repository goes from 17.2K to 14.8K input tokens). The Matt skills reach Codex
# as plain paths in the prompt, so the skills listing only names Codex's own.
UNUSED = ("skills.include_instructions=false", "features.plugins=false", "features.apps=false",
          "features.image_generation=false", "features.browser_use=false", "features.browser_use_external=false",
          "features.computer_use=false", "features.in_app_browser=false")
AGENTS_NOTE = "\nAGENTS.md is already in your instructions above; don't read it again."


def preflight():
    """None when Codex can run, else the failed outcome (status auth)."""
    try:
        ok = subprocess.run(["codex", "login", "status"], capture_output=True).returncode == 0
    except OSError:
        ok = False
    return None if ok else "RESULT: failed Codex login required; run codex login --device-auth"


def valid(role, outcome):
    return (outcome in VERDICTS.get(role, ())
            or (role == "dev" and outcome.startswith("RESULT: blocked"))
            # The fix prompt's own failure line: the reason reaches the owner's handoff.
            or (role == "fix" and outcome.startswith("RESULT: failed ")))


def usage(text):
    last = None
    for _, obj in events(text):
        if isinstance(obj, dict) and obj.get("type") == "turn.completed":
            last = obj
    u = ((last or {}).get("usage")) or {}
    def show(key):
        value = u.get(key)
        return "null" if value is None else value
    return f"in={show('input_tokens')} out={show('output_tokens')} cache_read={show('cached_input_tokens')}"


def model(text):
    """"model=M effort=E" as the session rollout recorded them (without -m, Codex picks
    its default model, which a CLI upgrade can change)."""
    thread = next((obj.get("thread_id") for _, obj in events(text)
                   if isinstance(obj, dict) and obj.get("type") == "thread.started"), None)
    if thread:
        sessions = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
        for rollout in sessions.rglob(f"rollout-*-{thread}.jsonl"):
            for _, obj in events(read(rollout)):
                if isinstance(obj, dict) and obj.get("type") == "turn_context":
                    p = obj.get("payload") or {}
                    return f"model={p.get('model') or 'unknown'} effort={p.get('effort') or p.get('reasoning_effort') or 'default'}"
            break
    return "model=unknown"


def copy_stripped(src, dest):
    Path(dest).write_text("".join(line for line in read(src).splitlines(keepends=True)
                                  if "<!-- baton-" not in line))


def skill_paths(skills_dir):
    """(name, path) for every Matt skill (skills/GROUP/NAME/SKILL.md). Given a glob,
    Codex guessed skills/NAME/ and read nothing; exact paths leave nothing to guess."""
    return sorted((p.parent.name, str(p)) for p in Path(skills_dir).glob("*/*/SKILL.md")
                  if p.parent.parent.name != "deprecated")


def run(req):
    cwd, output, prompt = req.cwd, req.output, req.prompt
    base = Path(output).name if output else ""
    args, artifacts = [], None
    if req.model:
        args += ["-m", req.model]
    if req.effort:
        args += ["-c", f'model_reasoning_effort="{req.effort}"']
    if req.readonly:
        sandbox, head = "read-only", git(cwd, "rev-parse", "HEAD")
    else:
        sandbox, head = "workspace-write", None
        # Explicit metadata roots allow local commits in a linked worktree. Adding the
        # clone directory alone leaves its protected .git subtree read-only.
        common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        gitdir = git(cwd, "rev-parse", "--absolute-git-dir")
        if not common or not gitdir:
            return Run(1, "RESULT: failed Codex did not complete", "", summary="no git metadata")
        args += ["--add-dir", common, "--add-dir", gitdir]
        if req.role in ("dev", "fix") and req.shots_dir:
            args += ["--add-dir", req.shots_dir]
        if req.online:
            for d in req.add_dirs:
                Path(d).mkdir(parents=True, exist_ok=True)
                args += ["--add-dir", d]
        artifacts = Path(tempfile.mkdtemp(prefix=".baton-codex-", dir=cwd))
        # The caller supplies the exact output path separately from untrusted context.
        if output:
            prompt = prompt.replace(output, str(artifacts / base))
        prompt += "\nDo not add or commit the .baton-codex-* artifact directory. Commit only the requested repository changes."
    if req.skills_dir and (paths := skill_paths(req.skills_dir)):
        prompt += ("\nSkills named in these instructions or AGENTS.md (e.g. `mattpocock-skills:tdd`) are plain files;"
                   " read the one named, at the path given here, and don't search for them:\n"
                   + "\n".join(f"- {name}: {path}" for name, path in paths))
    prompt += AGENTS_NOTE
    for setting in UNUSED:
        args += ["-c", setting]
    last = Path(f"{req.log}.last")
    last.unlink(missing_ok=True)
    otel = []
    if req.otel:
        otel += ["-c", "otel.log_user_prompt=false"]
        for key, signal in (("exporter", "logs"), ("metrics_exporter", "metrics"), ("trace_exporter", "traces")):
            otel += ["-c", f'otel.{key}={{otlp-http={{endpoint="{OTEL}{signal}",protocol="json"}}}}']
    env = child_env(GH_TOKEN="invalid", GITHUB_TOKEN="invalid", GIT_SSH_COMMAND="/bin/false", GIT_TERMINAL_PROMPT="0", **req.env)
    rc = run_cli(["codex", "exec", "-c", 'approval_policy="never"',
                  "-c", f"sandbox_workspace_write.network_access={'true' if req.online else 'false'}",
                  "-s", sandbox, "-C", str(cwd), *args, *otel,
                  "--json", "--output-last-message", str(last), prompt],
                 req.log, req.minutes, env)
    final = strip_markers(read(last)).rstrip("\n") if last.is_file() and last.stat().st_size else ""
    if rc == 0 and final.strip():
        outcome = final.split("\n")[-1]
        if not valid(req.role, outcome):
            outcome = "RESULT: failed invalid Codex terminal verdict"
    else:
        outcome = f"RESULT: failed Codex rc={rc} or missing final message (log {req.log})"
    failed = lambda: outcome.startswith("RESULT: failed")  # noqa: E731
    if req.readonly:
        if head is None or changed(cwd, head):
            restore(cwd, head or "HEAD")
            outcome = f"RESULT: failed Codex changed files in a read-only {req.role}"
        elif outcome.startswith("REVIEW:") and output:
            Path(output).write_text("".join(f"{line}\n" for line in final.split("\n")
                                            if not line.startswith("REVIEW: ")))
    elif not failed():
        need = {"fix": "", "spec": ".comment", "dev": "" if outcome == "RESULT: ready" else None}.get(req.role)
        what = {"fix": "no Codex fix artifact", "spec": "no Codex spec reply artifact", "dev": "no Codex PR description"}
        if need is not None and not _nonempty(artifacts / f"{base}{need}"):
            outcome = f"RESULT: failed {what[req.role]}"
        if not failed():
            suffixes = {"spec": (".body", ".comment"), "fix": ("", ".body"), "dev": ("",)}.get(req.role, ())
            for suffix in suffixes:
                src = artifacts / f"{base}{suffix}"
                if src.is_file():
                    try:
                        copy_stripped(src, f"{output}{suffix}")
                    except OSError:
                        outcome = "RESULT: failed copying Codex artifact"
    if artifacts:
        shutil.rmtree(artifacts, ignore_errors=True)
    if not req.readonly and git(cwd, "status", "--porcelain"):
        outcome = f"RESULT: failed Codex left uncommitted changes in {cwd}"
    if not failed():
        need = {"fix": "", "spec": ".comment", "review": "", "screenshot_review": ""}.get(req.role)
        what = {"fix": "no Codex fix artifact", "spec": "no Codex spec reply artifact", "review": "no Codex review artifact",
                "screenshot_review": "no Codex review artifact"}
        if need is not None and not _nonempty(f"{output}{need}"):
            outcome = f"RESULT: failed {what[req.role]}"
    text = read(req.log)
    used = usage(text)
    return Run(rc, outcome, final, used, f"{outcome} {used} {model(text)}")


def _nonempty(path):
    try:
        return Path(path).stat().st_size > 0
    except OSError:
        return False


def reconnect_row(row):
    return False


def codex_model(thread, codex_home=None):
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", thread):
        return None
    sessions = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    for rollout in sessions.rglob(f"rollout-*-{thread}.jsonl"):
        with rollout.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("type") == "turn_context":
                    return (row.get("payload") or {}).get("model")
    return None


def log_model(row, codex_home=None):
    """(done, model) for one log row; done stops the scan."""
    if row.get("type") == "thread.started" and row.get("thread_id"):
        return True, codex_model(str(row["thread_id"]), codex_home)
    return False, None
