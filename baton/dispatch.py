"""One agent run, any vendor, start to settled (phase 4 prerequisite, #276): the body of
roles.sh's agent_run.

Checks the vendor can run, counts the run, opens its telemetry record, runs the CLI
(baton.agents) with the exporters set on that child only, and settles how it ended
(baton.settle). Prints JSON for the bash caller, which keeps `defer_vendor` (it
exits the item's subshell):
    status   how it ended; "deferred" when the vendor cannot run now; "" for rejected requests
    outcome  the RESULT:/REVIEW:/SPEC:/... line
    reply    the agent's final message (untrusted: callers strip markers)
    model    the actual Claude or Codex model when reported
    defer    why the item must yield, or ""
Exit code: 0 ok, 1 failed, 75 deferred.
"""

from . import session
import argparse
import json
import os
from pathlib import Path
import sys
import time

from . import agents, capacity, hooks as telemetry, notify, settle, vendors
from .common import LEASE_REASON, Request

# Claude Code writes its prompt cache with a one-hour lifetime by default, at 2x the input price
# against 1.25x for five minutes. These roles finish in minutes with no long wait between model calls
# (all 11 Claude fix runs up to 2026-11-01 idled 22 s or less). dev runs long test suites between
# calls, and a five-minute cache would expire and be written again.
FIVE_MINUTE_CACHE_ROLES = ("spec_review", "spec", "review", "screenshot_review", "fix")


SECRET_ROLES = ("dev", "fix")


@session.scoped
def configure(vendor, role, env):
    """(model, minutes, suffix, Request options) from the runner's configuration."""
    env = session.resolve(env)
    runtime = env.runtime
    models = env.config.models
    followup = (
        (role in ("spec_review", "spec") and runtime.spec_followup)
        or (role == "review" and runtime.review_followup)
        or (role == "fix" and runtime.fix_followup)
    )
    route = models.route(role, vendor, followup=followup)
    settings = env.config.vendor(vendor)
    if vendor == "claude":
        return route.model, settings.max_minutes, "", {
            "fallback_models": " ".join(settings.fallback_models),
            "budget": str(getattr(models, "budget_" + role)),
            "repo": str(env.dirs.clone), "subagent_model": route.subagent_model,
            "context_dir": str(env.dirs.ctx), "effort": route.effort}
    if vendor == "cursor":
        return route.model, settings.max_minutes, "", {}
    return route.model, settings.max_minutes, "-codex", {
        "effort": route.effort, "fallback_models": " ".join(settings.fallback_models),
        "add_dirs": list(map(str, env.caches)), "shots_dir": runtime.shots_dir,
        "skills_dir": env.skills}


def secret_overrides(env, role):
    """The repository's secret keys, read from its secret env file, for the runs that write code
    (dev, fix). The file is read per run and never enters the shared tool environment."""
    repo = session.resolve(env).repo
    if role not in SECRET_ROLES or not repo.secret_env_keys:
        return {}
    try:
        lines = repo.secret_env_file.read_text().splitlines()
    except FileNotFoundError:
        return {}
    found = {}
    for line in lines:
        key, sep, value = line.partition('=')
        if sep and key.strip() in repo.secret_env_keys and value.strip():
            found[key.strip()] = value.strip().strip('"\'')
    return found


@session.scoped
def dispatch(vendor, role, prompt, tag, cwd, settings="", output="", env=None, hooks=None):
    env = session.resolve(env)
    why = (f"unknown vendor {vendor}" if vendor not in agents.ADAPTERS
           else env.config.admission_error(vendor, role))
    if why:
        outcome = f"RESULT: failed {why}"
        print(outcome, file=sys.stderr)
        return {"rc": 1, "status": "", "outcome": outcome, "reply": None,
                "model": "", "defer": "", "log": ""}
    with capacity.lease(env.config.paths.state, vendor) as available:
        if not available:
            return {"rc": 75, "status": "deferred", "outcome": "", "reply": None,
                    "model": "", "defer": LEASE_REASON}
        return _dispatch(vendor, role, prompt, tag, cwd, settings, output, env, hooks)


def _dispatch(vendor, role, prompt, tag, cwd, settings, output, env, hooks):
    # Cooldowns and run counts are the machine's; the run's log is its repository's.
    state, logs = str(env.config.paths.state), str(env.dirs.logs)
    cooldown_hours = int(env.config.scheduling.cooldown_hours or agents.COOLDOWN_HOURS)

    def result(rc, status, outcome, reply=None, model="", defer="", log=""):
        return {"rc": rc, "status": status, "outcome": outcome, "reply": reply, "model": model, "defer": defer, "log": log}

    if why := vendors.vendor_blocked(state, vendor):
        return result(75, "deferred", "", defer=why)
    settle.count_run(state, vendor)
    model, minutes, suffix, options = configure(vendor, role, env)
    if vendor == "claude":
        options["settings"] = settings
    elif vendor == "codex":
        options["output"] = output
    if outcome := agents.preflight(vendor):
        deadline = str(int(time.time()) + cooldown_hours * 3600)
        rc, outcome, defer = settle.settle(state, vendor, "auth", 1, "", tag, deadline, outcome, credential=True)
        return result(rc, "auth", outcome, defer=defer)
    run_log = Path(logs, f"{time.strftime('%F-%H%M%S')}-{tag}{suffix}.jsonl")
    notify.log(f"start {tag} ({vendor} {role}, {model or 'default'}) in {cwd} (log {run_log})")
    hooks = hooks or telemetry.from_env(env, issue=env.runtime.issue, pr=env.runtime.pr, round=env.runtime.round)
    run = hooks.begin(vendor, role, minutes, requested_model=model)
    child_overrides = hooks.child_env(vendor, role, run)
    if vendor == "claude" and role in FIVE_MINUTE_CACHE_ROLES:
        child_overrides["FORCE_PROMPT_CACHING_5M"] = "1"
    child_overrides.update(secret_overrides(env, role))
    request = Request(vendor=vendor, role=role, prompt=prompt, cwd=Path(cwd), log=run_log, minutes=minutes,
                      model=model, otel=vendor == "codex" and bool(run), env=child_overrides, **options)
    try:
        ran = agents.run(request, cooldown_hours)
    except Exception as error:
        notify.log(f"end {tag} adapter rc=1, no result")
        outcome = f"RESULT: failed baton rc=1 (log {run_log})"
        rc, outcome, defer = settle.settle(state, vendor, "failure", 1, str(run_log), tag, "", outcome)
        hooks.end(run, "failure", str(run_log))
        return result(rc, "failure", outcome, "", defer=defer, log=str(run_log))
    if ran.status == "busy":
        ran.cooldown_until = int(time.time()) + env.config.models.busy_minutes * 60
    if ran.killed:
        notify.log(f"killed {tag} after {minutes} min")
    identity = f"claude_model={ran.model or 'unknown'} " if vendor == "claude" else ""
    notify.log(f"end {tag} rc={ran.rc} {identity}{ran.summary}")
    rc, outcome, defer = settle.settle(state, vendor, ran.status, ran.rc, str(run_log), tag,
                                       "" if ran.cooldown_until is None else str(ran.cooldown_until),
                                       ran.outcome, credential=True)
    hooks.end(run, ran.status, str(run_log))
    return result(rc, ran.status, outcome, ran.reply, ran.model, defer, str(run_log))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--settings", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    out = dispatch(args.vendor, args.role, sys.stdin.read(), args.tag, args.cwd, args.settings, args.output)
    print(json.dumps(out, ensure_ascii=False))
    return out["rc"]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"dispatch: {exc}", file=sys.stderr)
        sys.exit(1)