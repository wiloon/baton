#!/usr/bin/env python3
"""Quota snapshots of the pool vendors, read from what each run reports, and the pick.

Neither subscription has a public quota API. Each run's own output carries it:
Claude Code's stream-json emits rate_limit_event, and Codex writes rate_limits
into its session file (~/.codex/sessions/.../rollout-*-<thread id>.jsonl).
Cursor's output carries none: refresh_cursor asks the dashboard API its IDE uses
(undocumented; a change only leaves the snapshot unknown), which costs no usage.
  python -m baton.quota record VENDOR LOG   merge LOG into $STATE/quota-VENDOR.json
  python -m baton.quota probed VENDOR       note a probe attempt
  python -m baton.quota refresh cursor      read Cursor's usage now and print it
  python -m baton.quota credential VENDOR RESULT  record credential health
  python -m baton.quota stale VENDOR        exit 0 for a stale snapshot
  python -m baton.quota pick [--current V] [--prefer "V ..."] VENDOR...
                               print the vendor to use and a one-line summary; exit 1 if none
"""

from . import agents, session
import glob
import json
from contextlib import contextmanager
import fcntl
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import tempfile
import urllib.request

WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 24 * 3600}
TIE = 0.05   # scores this close count as equal: preference, then pool order decides
CURSOR_USAGE = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
# Where cursor-agent >= 2026.11.19 keeps its login on Linux (secret-tool attributes).
CURSOR_SECRET = ("service", "cursor-credentials", "account", "cursor-user")


def _json_lines(lines):
    for line in lines:
        try:
            yield json.loads(line)
        except (ValueError, TypeError):
            continue


def parse_claude(lines):
    """Windows from the last rate_limit_event: {name: {used, resets_at}}."""
    windows = {}
    for event in _json_lines(lines):
        if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
            continue
        info = event.get("rate_limit_info") or {}
        for name, window in (info.get("unifiedWindows") or {}).items():
            if "utilization" in window:
                windows[name] = {"used": float(window["utilization"]), "resets_at": int(window["resetsAt"])}
        if info.get("status") == "rejected" and info.get("rateLimitType"):
            windows[info["rateLimitType"]] = {"used": 1.0, "resets_at": int(info.get("resetsAt") or 0)}
    return windows


def _find_rate_limits(value):
    if isinstance(value, dict):
        if isinstance(value.get("rate_limits"), dict):
            return value["rate_limits"]
        for child in value.values():
            found = _find_rate_limits(child)
            if found:
                return found
    return None


def parse_codex(lines, codex_home):
    """Windows from the last rate_limits in the run's session file (or in the log itself)."""
    lines = list(lines)
    sources = [lines]
    for event in _json_lines(lines):
        if isinstance(event, dict) and event.get("type") == "thread.started" and event.get("thread_id"):
            for path in glob.glob(os.path.join(codex_home, "sessions", "**", f"rollout-*-{event['thread_id']}.jsonl"),
                                  recursive=True):
                sources.append(Path(path).read_text(errors="replace").splitlines())
    windows = {}
    for source in sources:
        for event in _json_lines(source):
            limits = _find_rate_limits(event)
            if not limits:
                continue
            for key in ("primary", "secondary"):
                window = limits.get(key)
                if not window or window.get("used_percent") is None:
                    continue
                name = "five_hour" if (window.get("window_minutes") or 0) <= 24 * 60 else "seven_day"
                windows[name] = {"used": float(window["used_percent"]) / 100, "resets_at": int(window["resets_at"])}
    return windows


def parse_cursor(payload):
    """The weekly window of a GetCurrentPeriodUsage answer: {name: {used, resets_at}}."""
    try:
        used = float(payload["planUsage"]["totalPercentUsed"]) / 100
        return {"seven_day": {"used": used, "resets_at": int(payload["billingCycleEnd"]) // 1000}}
    except (KeyError, TypeError, ValueError):
        return {}


def _fetch_cursor(token):
    request = urllib.request.Request(CURSOR_USAGE, data=b"{}", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Connect-Protocol-Version": "1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


class CursorLoginUnreadable(ValueError):
    """auth.json records a Cursor login, but its access token can't be read."""


def _secret_lookup():
    """The credential cursor-agent keeps in the Secret Service (Linux), or None."""
    try:
        found = subprocess.run(["secret-tool", "lookup", *CURSOR_SECRET], capture_output=True, text=True,
                               timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return found.stdout if found.returncode == 0 else None


def cursor_token(auth, lookup=_secret_lookup):
    """Cursor's access token; None when this machine has no Cursor login.

    cursor-agent 2026.11.19 moved the login into the OS secret store and left only
    {"storedIn": "os-secret-store"} in auth.json; older versions keep accessToken there.
    Raises CursorLoginUnreadable when a login is recorded but no token comes back, so the
    snapshot can't silently freeze at its last value."""
    try:
        stored = json.loads(Path(auth).read_text())
    except OSError:
        return None
    except ValueError as error:
        raise CursorLoginUnreadable(f"cursor auth.json is not JSON: {error}") from None
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
            raise CursorLoginUnreadable("cursor login is in the OS secret store, but secret-tool returned no "
                                        f"access token ({' '.join(CURSOR_SECRET)})")
        return token
    raise CursorLoginUnreadable("cursor auth.json has neither accessToken nor storedIn")


def refresh_cursor(state, now, auth=None, fetch=_fetch_cursor, lookup=_secret_lookup):
    """Record Cursor's usage; {} when it isn't logged in here. Raises when the login can't be
    read (recording probe_failed) or the API fails."""
    try:
        token = cursor_token(auth or Path.home() / ".config/cursor/auth.json", lookup)
    except CursorLoginUnreadable:
        credential(state, 'cursor', 'probe_failed', now)
        raise
    if token is None:
        return {}
    windows = parse_cursor(fetch(token))
    if windows:
        record(state, "cursor", windows, now)
        credential(state, 'cursor', 'authenticated', now)
    return windows


def _path(state, vendor):
    return Path(state, f"quota-{vendor}.json")


def load(state, vendor):
    try:
        return json.loads(_path(state, vendor).read_text())
    except (OSError, ValueError):
        return None


def _save(state, vendor, snap):
    atomic_json(_path(state, vendor), snap)


def atomic_json(path, value):
    """Readers see a complete generation; writers never share a temporary file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name + '.', delete=False) as out:
        temporary = Path(out.name)
        try:
            json.dump(value, out)
            out.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def locked(state, vendor):
    """Short storage lock, independent of the lease held during model execution."""
    path = Path(state) / 'quota-locks' / vendor
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def record(state, vendor, windows, now):
    with locked(state, vendor):
        snap = load(state, vendor) or {"windows": {}}
        for window in snap.setdefault("windows", {}).values():
            window.setdefault("observed_at", 0)
        for name, window in windows.items():
            if now >= snap['windows'].get(name, {}).get('observed_at', 0):
                snap['windows'][name] = dict(window, observed_at=now)
        if windows:
            snap['observed_at'] = max(now, snap.get('observed_at', 0))
        _save(state, vendor, snap)


def fresh(state, vendor, now, minutes):
    """A probe attempt or a successful call without quota data is not an observation."""
    snap = load(state, vendor) or {}
    required = set(agents.ADAPTERS[vendor].QUOTA_WINDOWS)
    try:
        windows = snap.get('windows') or {}
        return required <= windows.keys() and all(
            type(w.get('used')) in (int, float) and math.isfinite(w['used']) and 0 <= w['used'] <= 1
            and 0 <= now - w.get('observed_at', 0) <= minutes * 60
            and w.get('resets_at', 0) > now for w in (windows[n] for n in required))
    except (TypeError, AttributeError):
        return False


def record_log(state, vendor, log, now):
    """Merge the quota a run's LOG reported into VENDOR's snapshot (Claude and Codex only)."""
    lines = Path(log).read_text(errors="replace").splitlines() if Path(log).exists() else []
    if vendor == "claude":
        windows = parse_claude(lines)
    elif vendor == "codex":
        windows = parse_codex(lines, os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    else:
        return
    record(state, vendor, windows, now)


def mark_probed(state, vendor, now):
    with locked(state, vendor):
        snap = load(state, vendor) or {"windows": {}}
        snap["probed_at"] = max(now, snap.get('probed_at', 0))
        _save(state, vendor, snap)


def credential(state, vendor, result, now):
    """Persist the latest credential check; auth expiry stays visible until success."""
    if vendor not in ("claude", "codex", "cursor") or result not in (
            "authenticated", "auth_expired", "probe_failed"):
        raise ValueError("invalid credential result")
    path = Path(state, f"credential-{vendor}.json")
    with locked(state, vendor):
        try:
            previous = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            previous = {}
        if now >= previous.get('observed_at', 0):
            atomic_json(path, {"result": result, "observed_at": now})


def credential_current(state, vendor, now):
    try:
        row = json.loads((Path(state) / f'credential-{vendor}.json').read_text())
        return row.get('result') == 'authenticated' and 0 <= now - row.get('observed_at', 0) <= 86400
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def credential_expired(state, vendor):
    """The latest credential check found VENDOR logged out (a failed auth, not a failed probe)."""
    try:
        return json.loads((Path(state) / f'credential-{vendor}.json').read_text()).get('result') == 'auth_expired'
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def stale(state, vendor, now, minutes):
    snap = load(state, vendor) or {}
    seen = max(snap.get("observed_at") or 0, snap.get("probed_at") or 0)
    return now - seen > minutes * 60


def _used(snap, name, now):
    window = ((snap or {}).get("windows") or {}).get(name)
    if not window or window.get("resets_at", 0) <= now:
        return None, None
    return window["used"], window["resets_at"]


def assess(snap, cap, five_hour_max, now):
    """(score, reason): lower score = more headroom; reason set when the vendor must not run."""
    five, _ = _used(snap, "five_hour", now)
    week, week_reset = _used(snap, "seven_day", now)
    if five is not None and five >= five_hour_max:
        return None, f"5h {five:.0%} >= {five_hour_max:.0%}"
    if week is not None and week >= cap:
        return None, f"week {week:.0%} >= cap {cap:.0%}"
    score = 0.0
    if week is not None:
        # Share of the runner's cap used, minus the share of the week gone: > 0 runs ahead of pace.
        elapsed = min(1.0, max(0.0, 1 - (week_reset - now) / WINDOW_SECONDS["seven_day"]))
        score = week / cap - elapsed
    if five is not None:
        score += max(0.0, five - 0.7)
    return score, None


def horizon(snap, cap, five_hour_max, now):
    """How long the vendor's quota stops it (ADR 0002): "long" while its week is at the cap,
    "short" while only its 5-hour window is full, "" when quota does not stop it."""
    five, _ = _used(snap, "five_hour", now)
    week, _ = _used(snap, "seven_day", now)
    if week is not None and week >= cap:
        return "long"
    if five is not None and five >= five_hour_max:
        return "short"
    return ""


def preference_order(vendors, prefer=()):
    """VENDORS with the PREFER ones first, in PREFER's order; the rest keep their order."""
    first = [v for v in dict.fromkeys(prefer or ()) if v in vendors]
    return [*first, *(v for v in vendors if v not in first)]


def pick(vendors, snapshots, caps, five_hour_max, now, current=None, prefer=()):
    scored, parts = {}, []
    for vendor in vendors:
        score, reason = assess(snapshots.get(vendor), caps.get(vendor, 0.95), five_hour_max, now)
        if reason:
            parts.append(f"{vendor}=blocked({reason})")
        else:
            scored[vendor] = score
            parts.append(f"{vendor}={score:+.2f}")
    summary = " ".join(parts)
    if not scored:
        return None, summary
    if current in scored:
        return current, summary
    best = min(scored.values())
    return next(v for v in preference_order(vendors, prefer)
                if v in scored and scored[v] <= best + TIE), summary


def main(argv):
    cfg = session.resolve().config
    state = str(cfg.paths.state)
    now = time.time()
    command, args = argv[0], argv[1:]
    if command == "record":
        record_log(state, *args, now)
        return 0
    if command == "probed":
        mark_probed(state, args[0], now)
        return 0
    if command == "refresh":
        if reason := cfg.admission_error("cursor"):
            print(reason, file=sys.stderr)
            return 1
        print(json.dumps(refresh_cursor(state, now)))
        return 0
    if command == "credential":
        credential(state, args[0], args[1], now)
        return 0
    if command == "stale":
        return 0 if stale(state, args[0], now, int(session.resolve().config.quota.quota_stale_minutes)) else 1
    if command == "pick":
        current, prefer = None, ()
        while args[:1] in (["--current"], ["--prefer"]):
            if args[0] == "--current":
                current = args[1]
            else:
                prefer = args[1].split()
            args = args[2:]
        caps = {v: cfg.cap(v) for v in args}
        chosen, summary = pick(args, {v: load(state, v) for v in args}, caps,
                               float(cfg.quota.quota_five_hour_max), now, current,
                               prefer)
        print(chosen or "")
        print(summary)
        return 0 if chosen else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
