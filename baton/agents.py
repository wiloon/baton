"""One interface for running any vendor's CLI agent (ADR-0005, #226).

    run(request) -> RunResult(status, outcome, reply, usage, cooldown_until, log)

Each vendor has an adapter (claude_cli, cursor_cli, codex_cli) that owns its CLI flags,
stream-json parsing, final reply and verdict line, and read-only enforcement. This
module classifies how the run ended. Side effects stay with the bash caller (roles.sh
agent_run): run counts, cooldown files, quota snapshots, Telegram, telemetry, logs.

CLI (JSON on stdout):
    run --vendor V --role R --cwd DIR --log FILE --minutes N [...] < prompt
        exit 0 success, 1 failure/timeout, 75 quota/busy/auth
    preflight --vendor V            exit 1 with the failed outcome when V cannot run
    classify --vendor V --rc N --log FILE [--outcome O]
    deadline TEXT                   the epoch a quota error says it resets
"""
import argparse
import datetime as d
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from zoneinfo import ZoneInfo

from . import claude_cli, codex_cli, cursor_cli
from .common import Request, events, read

ADAPTERS = {"claude": claude_cli, "cursor": cursor_cli, "codex": codex_cli}
STATUSES = ("success", "failure", "timeout", "quota", "busy", "auth")

# Match explicit quota messages, never bare 429/rate-limit/overloaded errors (those are
# BUSY below). Claude
# phrases come from the previous runner. Cursor phrases are conservative guesses until
# captured on the runner host; Codex usage_limit_reached/usage-limit text is present in the local
# CLI binary. Authentication is classified before quota. An auth failure is handled
# like an exhausted quota: the vendor rests COOLDOWN_HOURS (no reset time to read) and
# the item waits for another vendor or the owner's new login.
QUOTA = {
    "claude": r"usage limit|session limit|hit your .*limit|limit reached|out of (extra )?usage|rate_limit_error",
    "cursor": r"usage limit (reached|exceeded)|(monthly|weekly|daily) (usage |request )?limit (reached|exceeded)|(insufficient|exhausted) quota",
    "codex": r"usage_limit_reached|hit your usage limit|usage limit (reached|exceeded)|insufficient_quota",
}
AUTH = (r"authentication (failed|required)|failed to authenticate|unauthorized|invalid[_ ]api[_ ]key|invalid[_ ]token"
        r"|(token|session) (has )?expired|please (log|sign) in|login required|not (logged|signed) in")
COOLDOWN_HOURS = 5
# The vendor's servers are full (Codex "Selected model is at capacity", Claude API 529
# overloaded_error). Not the account's quota and not the task's fault. The run first
# moves to the fallback model (Claude's CLI does it itself, Codex is rerun once); if that
# is full too, the vendor rests for BUSY_MINUTES and the item is retried, with another
# vendor if one is free.
BUSY = r"at capacity|overloaded"
BUSY_MINUTES = 30


@dataclass
class RunResult:
    status: str             # one of STATUSES
    outcome: str            # the RESULT:/REVIEW:/SPEC:/... line; "RESULT: failed ..." when none
    reply: str              # the final message (untrusted: callers strip markers)
    usage: str
    log: str
    rc: int
    killed: bool            # timeout(1) stopped the CLI
    cooldown_until: int | None
    summary: str            # the runner.log end line after "rc=N"
    model: str = ""         # Codex rollout's actual model, never inferred from a requested alias

    @property
    def exit_code(self):
        return {"success": 0, "quota": 75, "busy": 75, "auth": 75}.get(self.status, 1)


def errors(text):
    """Only error/result events and raw non-JSON diagnostics, never assistant text or
    tool output that may discuss quotas or contain untrusted issue content."""
    out = []
    for line, obj in events(text):
        if obj is None:
            out.append(line)
        elif isinstance(obj, dict) and (obj.get("type") in ("error", "turn.failed") or obj.get("error") is not None
                                        or (obj.get("type") == "result" and (obj.get("is_error") is True
                                                                             or obj.get("subtype") != "success"))):
            out.append(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
    return "\n".join(out)


def deadline(text, cooldown_hours=COOLDOWN_HOURS, now=None):
    """The epoch a quota error says it resets: explicit reset/retry fields or wording
    only; anything else waits cooldown_hours."""
    now = time.time() if now is None else now
    value = None
    m = re.search(r'(?:resets_at|reset_at|reset_time)["\s:]+(\d{10,13})\b', text, re.I)
    if m:
        value = int(m[1])
        if value > 10**12:
            value /= 1000
    if value is None:
        m = re.search(r'(?:retry_after(?:_seconds)?["\s:]+|(?:retry|try again|resets?) (?:after|in)\s+)(\d+)\s*(seconds?|secs?|s\b|minutes?|mins?|m\b|hours?|hrs?|h\b|days?|d\b|weeks?|w\b)?', text, re.I)
        if m:
            unit = (m[2] or 'seconds').lower()[0]
            value = now + int(m[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[unit]
    if value is None:
        m = re.search(r'(?:resets?(?:_at|_time)?|try again)\s*["\s:]*(?:at\s+)?(\d{4}-\d\d-\d\d[T ][\d:]+(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?)', text, re.I)
        if m:
            try:
                value = d.datetime.fromisoformat(m[1].replace('Z', '+00:00')).timestamp()
            except ValueError:
                pass
    if value is None:
        # Claude commonly displays "resets 3pm (America/Los_Angeles)".
        m = re.search(r'(?:resets?|try again)\s+(?:at\s+)?(\d{1,2})(?::(\d\d))?\s*(am|pm)?(?:\s+on\s+([A-Za-z]+\s+\d{1,2}(?:,?\s+\d{4})?))?(?:\s*\(([^)]+)\))?', text, re.I)
        if m:
            try:
                zone = ZoneInfo(m[5]) if m[5] else d.datetime.now().astimezone().tzinfo
                current = d.datetime.fromtimestamp(now, zone)
                hour = int(m[1])
                if m[3]:
                    hour = hour % 12 + (12 if m[3].lower() == 'pm' else 0)
                reset = current.replace(hour=hour, minute=int(m[2] or 0), second=0, microsecond=0)
                if m[4]:
                    date_text = m[4].replace(',', '')
                    parsed = None
                    for fmt in ('%b %d %Y', '%B %d %Y', '%b %d', '%B %d'):
                        try:
                            parsed = d.datetime.strptime(date_text, fmt)
                            break
                        except ValueError:
                            continue
                    if parsed is None:
                        raise ValueError('unknown reset date')
                    year = parsed.year if re.search(r'\d{4}', date_text) else current.year
                    reset = reset.replace(year=year, month=parsed.month, day=parsed.day)
                elif reset.timestamp() <= now:
                    reset += d.timedelta(days=1)
                value = reset.timestamp()
            except (ValueError, KeyError):
                pass
    return int(value if value is not None and value > now else now + cooldown_hours * 3600)


def classify(vendor, rc, log_text, outcome="", cooldown_hours=COOLDOWN_HOURS):
    """(status, cooldown_until): timeout, then auth, quota, busy, failure."""
    found = errors(log_text)
    if rc in (124, 137):
        return "timeout", None
    if re.search(AUTH, found, re.I):
        return "auth", int(time.time()) + cooldown_hours * 3600
    if re.search(QUOTA[vendor], found, re.I):
        return "quota", deadline(found, cooldown_hours)
    if re.search(BUSY, found, re.I):
        return "busy", int(time.time()) + BUSY_MINUTES * 60
    if rc != 0 or outcome.startswith("RESULT: failed"):
        return "failure", None
    return "success", None


def started(text):
    """True once a Codex run began a command, edit or message: a rerun could repeat it."""
    return any(isinstance(obj, dict) and obj.get("type") in ("item.started", "item.completed")
               for _, obj in events(text))


def preflight(vendor):
    """None when VENDOR's CLI can run, else the failed outcome (an auth failure)."""
    check = getattr(ADAPTERS[vendor], "preflight", None)
    return check() if check else None


def run(req, cooldown_hours=COOLDOWN_HOURS):
    if outcome := preflight(req.vendor):
        return RunResult("auth", outcome, "", "", str(req.log), 1, False, int(time.time()) + cooldown_hours * 3600, outcome)
    r = ADAPTERS[req.vendor].run(req)
    status, until = classify(req.vendor, r.rc, read(req.log), r.outcome, cooldown_hours)
    if status == "busy" and req.vendor == "codex" and req.fallback and not started(read(req.log)):
        # The first attempt keeps its log beside the rerun's; telemetry reads the rerun's.
        req.log.replace(f"{req.log}.busy")
        req = replace(req, model=req.fallback, fallback_models="")
        r = ADAPTERS[req.vendor].run(req)
        status, until = classify(req.vendor, r.rc, read(req.log), r.outcome, cooldown_hours)
    actual = re.search(r"\bmodel=([^\s]+)", r.summary) if req.vendor == "codex" else None
    return RunResult(status, r.outcome, r.reply, r.usage, str(req.log), r.rc, r.rc in (124, 137), until,
                     r.summary, actual.group(1) if actual else "")


def main(argv=None):
    p = argparse.ArgumentParser(prog="baton.agents")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run one agent; the prompt comes on stdin")
    r.add_argument("--vendor", required=True, choices=ADAPTERS)
    r.add_argument("--role", required=True)
    r.add_argument("--cwd", required=True, type=Path)
    r.add_argument("--log", required=True, type=Path)
    r.add_argument("--minutes", required=True, type=int)
    r.add_argument("--access", choices=("readonly", "write"), default="")
    r.add_argument("--network", choices=("on", "off"), default="")
    r.add_argument("--add-dir", dest="add_dirs", action="append", default=[])
    r.add_argument("--otel", action="store_true")
    for name in ("model", "effort", "fallback-models", "output", "settings", "budget", "subagent-model", "repo", "shots-dir", "skills-dir"):
        r.add_argument(f"--{name}", default="")
    r.add_argument("--cooldown-hours", type=int, default=COOLDOWN_HOURS)
    f = sub.add_parser("preflight")
    f.add_argument("--vendor", required=True, choices=ADAPTERS)
    c = sub.add_parser("classify")
    c.add_argument("--vendor", required=True, choices=ADAPTERS)
    c.add_argument("--rc", required=True, type=int)
    c.add_argument("--log", required=True, type=Path)
    c.add_argument("--outcome", default="")
    c.add_argument("--cooldown-hours", type=int, default=COOLDOWN_HOURS)
    t = sub.add_parser("deadline")
    t.add_argument("text")
    t.add_argument("--cooldown-hours", type=int, default=COOLDOWN_HOURS)
    a = p.parse_args(argv)

    if a.command == "deadline":
        print(deadline(a.text, a.cooldown_hours))
        return 0
    if a.command == "classify":
        status, until = classify(a.vendor, a.rc, read(a.log), a.outcome, a.cooldown_hours)
        print(json.dumps({"status": status, "cooldown_until": until}))
        return 0
    if a.command == "preflight":
        outcome = preflight(a.vendor)
        print(json.dumps({"status": "auth" if outcome else "success", "outcome": outcome or ""}))
        return 1 if outcome else 0
    options = {k: v for k, v in vars(a).items() if k not in ("command", "cooldown_hours")}
    result = run(Request(prompt=sys.stdin.read(), **options), a.cooldown_hours)
    print(json.dumps(asdict(result), ensure_ascii=False))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
