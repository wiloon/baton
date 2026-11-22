"""Shared vendor availability and quota/preference policy for reviews and analysis.

Role-specific author exclusions, model choices and budgets remain with the caller.
Strict consumers require fresh authenticated capacity; workloads retain their existing
probe-and-retry behavior when observations are missing.
"""
from dataclasses import dataclass
import json
from pathlib import Path

from . import capacity, quota


SHORT, LONG = 'short', 'long'


@dataclass(frozen=True)
class Availability:
    reason: str = ''
    detail: str = ''
    stale: bool = False
    # How long a blocked vendor stays blocked (ADR 0002): SHORT is back within hours, so
    # same-vendor review waits for it; LONG lets same-vendor review run.
    horizon: str = ''


def cooldown_until(state, vendor, now):
    """The epoch VENDOR's cooldown ends at, or None when it is not resting."""
    try:
        value = (Path(state) / f'{vendor}-cooldown-until').read_text().splitlines()[0]
    except (OSError, IndexError):
        return None
    return int(value) if value.isdecimal() and now < int(value) else None


def cooldown(state, vendor, now):
    until = cooldown_until(state, vendor, now)
    return f'cooldown until epoch {until}' if until is not None else ''


def cooldown_horizon(state, settings, vendor, until, now):
    """A cooldown file holds only its end, so its horizon is how far away that end is, except
    after an auth failure: the vendor stays out until someone logs it in again."""
    if quota.credential_expired(state, vendor):
        return LONG
    return SHORT if until - now <= settings.same_vendor_wait_max_minutes * 60 else LONG


def availability(state, settings, vendor, now, *, strict=False, leased=False):
    if not leased and capacity.busy(state, vendor):
        return Availability('vendor_capacity', 'vendor capacity reserved', horizon=SHORT)
    if (until := cooldown_until(state, vendor, now)) is not None:
        return Availability('vendor_cooldown', cooldown(state, vendor, now),
                            horizon=cooldown_horizon(state, settings, vendor, until, now))
    try:
        stale = quota.stale(state, vendor, now, settings.quota_stale_minutes)
    except (ValueError, TypeError, AttributeError):
        return Availability('quota_stale', 'quota observations invalid', True, SHORT)
    if not strict:
        return Availability(stale=stale)
    if not quota.credential_current(state, vendor, now):
        return Availability('credential_unavailable', 'credential unavailable', horizon=LONG)
    snap = quota.load(state, vendor) or {}
    if not isinstance(snap, dict):
        return Availability('quota_stale', 'quota observations invalid', True, SHORT)
    if not quota.fresh(state, vendor, now, settings.quota_stale_minutes):
        return Availability('quota_stale', 'quota observations stale or unavailable', True, SHORT)
    cap = settings.cap_of(vendor)
    five = settings.five_hour_max_of(vendor)
    _, blocked = quota.assess(snap, cap, five, now)
    if not blocked:
        return Availability()
    return Availability('quota_exhausted', blocked,
                        horizon=quota.horizon(snap, cap, five, now))


def choose(state, settings, pool, candidates, now, *, prefer=(), current='', extras=False):
    """Quota pace within the pool; preferences break close ties. For review-style
    selection, extras keep the existing preference-before-pool-pace behavior.
    Candidates must already pass role-specific eligibility and availability checks.
    """
    candidates = list(dict.fromkeys(candidates))
    snapshots = {v: quota.load(state, v) for v in candidates}
    caps = {v: settings.cap_of(v) for v in candidates}
    limits = {v: settings.five_hour_max_of(v) for v in candidates}
    if not extras:
        return quota.pick(candidates, snapshots, caps, limits, now, current, prefer)
    pooled = [v for v in candidates if v in pool]
    best, summary = quota.pick(pooled, snapshots, caps, limits, now, current, prefer)
    eligible = [v for v in candidates if not quota.assess(
        snapshots[v], caps[v], limits[v], now)[1]]
    ordered = quota.preference_order(eligible, prefer)
    chosen = next((v for v in ordered if v not in pool or v == best), None)
    return chosen, summary


def observe_failure(state, vendor, result, now):
    """Persist explicit vendor failures using the existing global gate file formats.
    Called while holding the vendor lease; analysis never emits workload notifications.
    """
    if result['state'] == 'auth':
        quota.credential(state, vendor, 'auth_expired', now)
    until = result.get('cooldown_until')
    if result['state'] in ('auth', 'quota', 'busy') and type(until) is int and until > now:
        path = Path(state) / f'{vendor}-cooldown-until'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(str(until) + '\n')
        temporary.replace(path)
