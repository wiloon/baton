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

from . import agents, capacity, gitcontrol, hooks as telemetry, notify, settle, tiers, vendors
from .common import LEASE_REASON, Request



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
    tier = tiers.step(env, role, runtime.issue, runtime.pr)
    route = models.route(role, vendor, followup=followup, tier=tier)
    settings = env.config.vendor(vendor)
    return agents.ADAPTERS[vendor].configure(role, route, settings, env)


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
    adapter = agents.ADAPTERS[vendor]
    # Read once, from the caller's hooks too when only they name the PR; configure sees the same tier.
    pr = env.runtime.pr or str(getattr(hooks, "pr", "") or "")
    env = tiers.carry(env, role, env.runtime.issue, pr)
    tier = tiers.step(env, role, env.runtime.issue, pr)
    model, minutes, suffix, options = configure(vendor, role, env)
    options = adapter.attach_dispatch_inputs(options, settings=settings, output=output)
    if outcome := agents.preflight(vendor):
        deadline = str(int(time.time()) + cooldown_hours * 3600)
        rc, outcome, defer = settle.settle(state, vendor, "auth", 1, "", tag, deadline, outcome, credential=True)
        return result(rc, "auth", outcome, defer=defer)
    run_log = Path(logs, f"{time.strftime('%F-%H%M%S')}-{tag}{suffix}.jsonl")
    notify.log(f"start {tag} ({vendor} {role}, {model or 'default'}) in {cwd} (log {run_log})")
    hooks = hooks or telemetry.from_env(env, issue=env.runtime.issue, pr=env.runtime.pr, round=env.runtime.round)
    run = hooks.begin(vendor, role, minutes, requested_model=model, tier=tier)
    child_overrides = hooks.child_env(vendor, role, run)
    child_overrides.update(adapter.run_env(role, run))
    child_overrides.update(secret_overrides(env, role))
    request = Request(vendor=vendor, role=role, prompt=prompt, cwd=Path(cwd), log=run_log, minutes=minutes,
                      model=model, otel=adapter.request_otel(run), env=child_overrides, **options)
    # No run may change what git executes for the runner afterwards (config, hooks).
    git_control = gitcontrol.snapshot(cwd)
    try:
        ran = agents.run(request, cooldown_hours)
    except Exception as error:
        if tampered := gitcontrol.restore(git_control):
            notify.log(f"{tag}: {vendor} changed git control files; restored: {', '.join(tampered)}")
        notify.log(f"end {tag} adapter rc=1, no result tier={tier}")
        outcome = f"RESULT: failed baton rc=1 (log {run_log})"
        rc, outcome, defer = settle.settle(state, vendor, "failure", 1, str(run_log), tag, "", outcome)
        hooks.end(run, "failure", str(run_log))
        return result(rc, "failure", outcome, "", defer=defer, log=str(run_log))
    if tampered := gitcontrol.restore(git_control):
        notify.log(f"{tag}: {vendor} changed git control files; restored: {', '.join(tampered)}")
        if ran.status == "success":
            # Its work can't be trusted either: fail the run, whatever it reported.
            ran.status, ran.rc, ran.reply = "failure", 1, ""
            ran.outcome = "RESULT: failed changed the repository's git configuration or hooks (restored)"
    if ran.status == "busy":
        ran.cooldown_until = int(time.time()) + env.config.models.busy_minutes * 60
    if ran.killed:
        notify.log(f"killed {tag} after {minutes} min")
    identity = adapter.end_line_identity(ran.model)
    notify.log(f"end {tag} rc={ran.rc} {identity}{ran.summary} tier={tier}")
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