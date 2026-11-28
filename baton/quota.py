#!/usr/bin/env python3
"""Quota snapshots of the pool vendors, read from what each run reports, and the pick.

Neither subscription has a public quota API. Each run's own output carries it:
Each adapter parses its own run output (parse_quota_log) or, when its QUOTA_SOURCE is
"api", asks its own API (refresh_quota); this module stores and compares the snapshots.
  python -m baton.quota record VENDOR LOG   merge LOG into $STATE/quota-VENDOR.json
  python -m baton.quota probed VENDOR       note a probe attempt
  python -m baton.quota refresh VENDOR      read the API-sourced vendor's usage now and print it
  python -m baton.quota credential VENDOR RESULT  record credential health
  python -m baton.quota stale VENDOR        exit 0 for a stale snapshot
  python -m baton.quota pick [--current V] [--prefer "V ..."] VENDOR...
                               print the vendor to use and a one-line summary; exit 1 if none
"""

from . import agents, session, statefile
import json
from contextlib import contextmanager
import fcntl
import math
from pathlib import Path
import sys
import time

WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 24 * 3600}
TIE = 0.05   # scores this close count as equal: preference, then pool order decides


def json_lines(lines):
    for line in lines:
        try:
            yield json.loads(line)
        except (ValueError, TypeError):
            continue


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
    statefile.replace(path, json.dumps(value), mode=0o600)


@contextmanager
def locked(state, vendor):
    """Short storage lock: runs of one vendor may be in flight together."""
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
    """Merge the quota a run's LOG reported into VENDOR's snapshot via the adapter parser."""
    adapter = agents.ADAPTERS.get(vendor)
    if adapter is None or adapter.QUOTA_SOURCE != "probe-run":
        return
    lines = Path(log).read_text(errors="replace").splitlines() if Path(log).exists() else []
    record(state, vendor, adapter.parse_quota_log(lines), now)


def mark_probed(state, vendor, now):
    with locked(state, vendor):
        snap = load(state, vendor) or {"windows": {}}
        snap["probed_at"] = max(now, snap.get('probed_at', 0))
        _save(state, vendor, snap)


def credential(state, vendor, result, now):
    """Persist the latest credential check; auth expiry stays visible until success."""
    if vendor not in agents.ADAPTERS or result not in (
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


def pick(vendors, snapshots, caps, five_hour_maxes, now, current=None, prefer=()):
    scored, parts = {}, []
    for vendor in vendors:
        score, reason = assess(snapshots.get(vendor), caps.get(vendor, 0.95),
                               five_hour_maxes[vendor], now)
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
        # The named vendor only, and only an adapter whose quota comes from an API (#252).
        vendor = args[0] if args else ""
        adapter = agents.ADAPTERS.get(vendor)
        if adapter is None or adapter.QUOTA_SOURCE != "api":
            api = " ".join(v for v, a in agents.ADAPTERS.items() if a.QUOTA_SOURCE == "api")
            print(f"quota refresh: expected an API-sourced vendor ({api}), got {vendor or 'none'}",
                  file=sys.stderr)
            return 2
        if reason := cfg.admission_error(vendor):
            print(reason, file=sys.stderr)
            return 1
        print(json.dumps(agents.ADAPTERS[vendor].refresh_quota(state, now)))
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
        limits = {v: cfg.quota.five_hour_max_of(v) for v in args}
        chosen, summary = pick(args, {v: load(state, v) for v in args}, caps,
                               limits, now, current, prefer)
        print(chosen or "")
        print(summary)
        return 0 if chosen else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
