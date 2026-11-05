"""How an agent run ends, for the bash runner (migration slice 4a, #273): the run count,
the quota snapshot, the cooldown file, runner.log and Telegram, and the quota probe.

Bash keeps the parts that need its own scope: the telemetry hooks and `defer_vendor`'s
`exit 75`. `settle` prints JSON with the new `outcome` and, when the item must yield,
the `defer` reason; its exit code is 0 ok, 1 failed, 75 deferred.
"""

from . import session
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from . import hooks as telemetry, notify, quota
from .common import child_env, read

PROBE_PROMPT = "Reply with the word ok."
# What an expired or missing login looks like in a probe's output (grep -Ei before).
PROBE_AUTH = (r'"error"\s*:\s*"authentication_failed"|Failed to authenticate|OAuth session expired|authentication_error'
              r"|unauthorized|authentication (failed|required)|invalid[_ ]token|token (has )?expired"
              r"|please (log|sign) in|login required|not (logged|signed) in")


def runs_file(state, vendor, day):
    """Runs per vendor and day, for telemetry only; nothing caps them."""
    return Path(state, f"runs-{day}" if vendor == "claude" else f"{vendor}-runs-{day}")


def count_run(state, vendor, day=None):
    path = runs_file(state, vendor, day or time.strftime("%F"))
    try:
        before = int(path.read_text().split()[0])
    except (OSError, IndexError, ValueError):
        before = 0
    path.write_text(f"{before + 1}\n")


def refresh_cursor(state):
    """Cursor's usage comes from its API, not from a run's output; a failure only leaves it
    unknown, which picks treat like before it was visible."""
    try:
        windows = quota.refresh_cursor(state, int(time.time()))
    except Exception as error:
        notify.log(f"quota: refreshing cursor failed: {error}")
        return
    if week := windows.get("seven_day"):
        notify.log(f"quota cursor: week {week['used']:.0%} (resets epoch {week['resets_at']})")


def record_quota(state, vendor, log):
    """Keep the quota the run reported."""
    if vendor == "cursor":
        refresh_cursor(state)
    elif vendor in ("claude", "codex") and log:
        try:
            quota.record_log(state, vendor, log, int(time.time()))
        except Exception:
            notify.log(f"quota: recording {vendor} from {log} failed")


def record_credential(state, vendor, result):
    try:
        quota.credential(state, vendor, result, int(time.time()))
    except Exception:
        notify.log(f"credential: recording {vendor}={result} failed")


def settle(state, vendor, status, rc, log, tag, deadline="", outcome="", credential=False):
    """How a run ended: (exit code, outcome, defer reason). A quota, a busy vendor or an
    expired login rests the vendor until DEADLINE and defers the item (75); another
    failure rewrites OUTCOME as "RESULT: failed VENDOR STATUS ..." (1)."""
    if credential and status in ("auth", "success"):
        record_credential(state, vendor, "auth_expired" if status == "auth" else "authenticated")
    record_quota(state, vendor, log)
    cooldown = Path(state, f"{vendor}-cooldown-until")
    if status == "auth":
        cooldown.write_text(f"{deadline}\n")
        notify.log(f"{tag}: vendor={vendor} outcome=auth rc={rc} retry={deadline} {outcome}")
        notify.notify(f"{tag}: {vendor} auth failure", f"Log {vendor} in again on the runner host, then rm {cooldown} "
                      f"to resume before epoch {deadline}.{f' See {log}' if log else ''}")
        return 75, outcome, f"authentication failed; cooldown until epoch {deadline}"
    if status == "quota":
        cooldown.write_text(f"{deadline}\n")
        notify.log(f"{tag}: vendor={vendor} outcome=quota rc={rc} reset={deadline}")
        notify.notify(f"{tag}: {vendor} quota exhausted", f"Paused until epoch {deadline}; see {log}")
        return 75, outcome, f"quota exhausted; cooldown until epoch {deadline}"
    if status == "busy":
        cooldown.write_text(f"{deadline}\n")
        notify.log(f"{tag}: vendor={vendor} outcome=busy rc={rc} retry={deadline}")
        return 75, outcome, f"model at capacity; cooldown until epoch {deadline}"
    notify.log(f"{tag}: vendor={vendor} outcome={status} rc={rc}")
    if status != "success":
        detail = f": {outcome.removeprefix('RESULT: failed ')}" if outcome else ""
        return 1, f"RESULT: failed {vendor} {status} rc={rc}{detail} (log {log})", ""
    return 0, outcome, ""


def probe_command(vendor, model, directory):
    if vendor == "claude":
        return ["claude", "-p", PROBE_PROMPT, "--model", model, "--max-turns", "1", "--permission-mode", "dontAsk",
                "--tools", "", "--disallowedTools", "mcp__*", "--output-format", "stream-json", "--verbose"], {}
    return ["codex", "exec", "--ignore-user-config", "--ignore-rules", "-s", "read-only", "--skip-git-repo-check",
            "-C", str(directory), "--json", PROBE_PROMPT], {
        "GH_TOKEN": "invalid", "GITHUB_TOKEN": "invalid", "GIT_SSH_COMMAND": "/bin/false"}


def probe(state, logs, vendor, model):
    """One tiny request whose only use is the quota its output reports; the snapshot is
    stale, and the owner's own sessions share the quota. Not counted against the daily
    caps. No repo context or usable credentials; Claude tools/MCP are disabled and
    Codex runs read-only. Cursor's probe is an API read: no agent runs and no usage is spent."""
    if vendor not in ("claude", "codex", "cursor"):
        return
    try:
        quota.mark_probed(state, vendor, int(time.time()))
    except Exception:
        return
    if vendor == "cursor":
        refresh_cursor(state)
        return
    log = Path(logs, f"{time.strftime('%F-%H%M%S')}-quota-probe-{vendor}.jsonl")
    hooks = telemetry.from_env()  # Global overhead, never charged to an issue's task totals.
    probe_run = hooks.begin(vendor, "probe", 2, requested_model=model if vendor == "claude" else "")
    with tempfile.TemporaryDirectory() as directory:
        cmd, env = probe_command(vendor, model, directory)
        with open(log, "w") as out:
            try:
                rc = session.run_process(["timeout", "--kill-after=10", "2m", *cmd], cwd=directory, env=child_env(**env),
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT).returncode
            except OSError as e:
                out.write(f"baton: cannot start {cmd[0]}: {e}\n")
                rc = 127
    hooks.end(probe_run, "success" if rc == 0 else "failure", str(log))
    record_quota(state, vendor, str(log))
    if re.search(PROBE_AUTH, read(log), re.I):
        result = "auth_expired"
    else:
        result = "authenticated" if rc == 0 else "probe_failed"
    record_credential(state, vendor, result)
    notify.log(f"quota probe {vendor} (log {log})")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("count").add_argument("vendor")
    s = sub.add_parser("settle")
    s.add_argument("--vendor", required=True)
    s.add_argument("--status", required=True)
    s.add_argument("--rc", required=True)
    s.add_argument("--log", default="")
    s.add_argument("--tag", required=True)
    s.add_argument("--cooldown-until", default="")
    s.add_argument("--outcome", default="")
    s.add_argument("--credential", action="store_true", help="also record the login's health")
    p = sub.add_parser("probe")
    p.add_argument("vendor")
    args = parser.parse_args(argv)
    env = session.resolve()
    if args.command == "count":
        count_run(str(env.config.paths.state), args.vendor)
    elif args.command == "probe":
        probe(str(env.config.paths.state), str(env.config.paths.logs), args.vendor, env.config.quota.quota_probe_model_claude)
    else:
        rc, outcome, defer = settle(str(env.config.paths.state), args.vendor, args.status, args.rc, args.log, args.tag,
                                    args.cooldown_until, args.outcome, args.credential)
        print(json.dumps({"outcome": outcome, "defer": defer}, ensure_ascii=False))
        return rc
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"settle: {exc}", file=sys.stderr)
        sys.exit(1)
