"""Codex (`codex exec`). Its sandbox enforces read-only and network access; GitHub
credentials are invalid. Codex writes artifacts into a scratch directory inside the
worktree; only the runner copies them out (markers stripped) to the path the caller
named, so only the runner publishes artifacts and markers."""
import os
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
IMAGES = True
SCAN = False
SKIP_SCREENSHOT_AFTER_REVIEW = False
ROUTE_FIELDS = frozenset({"model", "effort"})
EMPTY_MODEL_OK = True
RECORDS_AUTHOR_MODEL = True
DEFAULT_MAX_MINUTES = 60
PREFLIGHT_CHECKS = (("codex", "--version"), ("codex", "login", "status"))
PROMPT_PREFIX = "codex-"
LOG_SUFFIX = "-codex"
OTEL = True
FIVE_MINUTE_CACHE = False
MODEL_LOG_LABEL = ""
GITHUB_VIA_RUNNER = True
DISPATCH_SETTINGS = False
DISPATCH_OUTPUT = True
VERSION_EXECUTABLE = "codex"
PROBE_SENDS_MODEL = False
RUNS_STEM = "codex-runs"
SCREENSHOT_DELIVERY = "Include the full review in your final message, above the verdict line."
# The terminal verdict each role may end with (its final message's last line).
VERDICTS = {
    "review": ("REVIEW: pass", "REVIEW: changes"),
    "screenshot_review": ("REVIEW: pass", "REVIEW: changes"),
    "spec": ("SPEC: revised", "SPEC: human"),
    "fix": ("RESULT: fixed", "RESULT: needs-info"),
    "spec_review": ("SPEC: accept", "SPEC: changes", "SPEC: reject"),
    "dev": ("RESULT: ready",),
}
OTEL_ENDPOINT = "http://127.0.0.1:4318/v1/"
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
            otel += ["-c", f'otel.{key}={{otlp-http={{endpoint="{OTEL_ENDPOINT}{signal}",protocol="json"}}}}']
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


def dispatch_options(env, role, route, settings):
    return {
        "effort": route.effort,
        "fallback_models": " ".join(settings.fallback_models),
        "add_dirs": list(map(str, env.caches)),
        "shots_dir": env.runtime.shots_dir,
        "skills_dir": env.skills,
    }


def telemetry_child_env(run, role, otel_stage):
    if not run:
        return {}
    attrs = f"baton.run_id={run.run_id},baton.role={role}"
    if run.issue:
        attrs += f",baton.issue={run.issue}"
    if run.pr:
        attrs += f",baton.pr={run.pr}"
    if role in otel_stage:
        attrs += f",baton.stage={otel_stage[role]}"
    if run.pipeline_id:
        attrs += f",baton.pipeline_id={run.pipeline_id}"
    return {
        "TRACEPARENT": f"00-{run.trace_id}-{run.span_id}-01",
        "OTEL_RESOURCE_ATTRIBUTES": attrs,
        "OTEL_METRIC_EXPORT_INTERVAL": "1000",
    }


def parse_quota_log(lines, codex_home=None):
    from . import quota
    import os
    home = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return quota.parse_codex(lines, home)


def analyst_effort(model, default):
    return default


def analyst_command(analyst, workspace, env, settings):
    disabled = ('shell_tool', 'unified_exec', 'view_image', 'multi_agent', 'hooks', 'memories',
                'skill_mcp_dependency_install', 'skill_search', 'tool_suggest', 'goals', 'sleep_tool')
    command = ['codex', 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
               '--skip-git-repo-check', '-s', 'read-only', '-m', analyst.model,
               '-c', 'approval_policy="never"', '-c', 'web_search="disabled"',
               '-c', 'features.code_mode.enabled=false', '-c', 'project_doc_max_bytes=0',
               '-c', 'skills.bundled.enabled=false', '-c', 'mcp_servers={}',
               '-c', 'skills.include_instructions=false',
               '-c', f'model_reasoning_effort="{analyst.effort}"', '--json']
    for setting in UNUSED:
        command += ['-c', setting]
    for feature in disabled:
        command += ['--disable', feature]
    return [*command, '-']


def parse_analyst_result(raw, events_fn, code_re, _final_message_fn):
    text = raw.decode(errors='replace')
    rows = [obj for _, obj in events_fn(text) if isinstance(obj, dict)]
    result = next((r for r in reversed(rows) if r.get('type') == 'result'), {})
    contexts = [r['payload'] for r in rows if r.get('type') == 'turn_context' and isinstance(r.get('payload'), dict)]
    actual = 'unknown'
    if contexts and code_re.fullmatch(str(contexts[-1].get('model', ''))):
        actual = contexts[-1]['model']
    messages = [r.get('item', {}).get('text', '') for r in rows
                 if r.get('type') == 'item.completed' and isinstance(r.get('item'), dict)
                 and r['item'].get('type') == 'agent_message']
    reply = messages[-1] if messages else ''
    valid = any(r.get('type') == 'turn.completed' for r in rows) and not any(
        r.get('type') in ('error', 'turn.failed') for r in rows)
    tool_use = any(r.get('type') in ('tool_call', 'tool_use') or
                   (isinstance(r.get('item'), dict) and r['item'].get('type') in
                    ('command_execution', 'file_change', 'mcp_tool_call', 'web_search')) for r in rows)
    return rows, reply, valid, actual, [], tool_use
