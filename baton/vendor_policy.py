"""Shared vendor availability and quota/preference policy for reviews and analysis.

Role-specific author exclusions, model choices and budgets remain with the caller.
Strict consumers require fresh authenticated capacity; workloads retain their existing
probe-and-retry behavior when observations are missing.
"""
from dataclasses import dataclass
import json
from pathlib import Path

from . import capacity, quota


@dataclass(frozen=True)
class Availability:
    reason: str = ''
    detail: str = ''
    stale: bool = False


def cooldown(state, vendor, now):
    try:
        value = (Path(state) / f'{vendor}-cooldown-until').read_text().splitlines()[0]
    except (OSError, IndexError):
        return ''
    return f'cooldown until epoch {value}' if value.isdecimal() and now < int(value) else ''


def availability(state, settings, vendor, now, *, strict=False, leased=False):
    if not leased and capacity.busy(state, vendor):
        return Availability('vendor_capacity', 'vendor capacity reserved')
    if why := cooldown(state, vendor, now):
        return Availability('vendor_cooldown', why)
    try:
        stale = quota.stale(state, vendor, now, settings.quota_stale_minutes)
    except (ValueError, TypeError, AttributeError):
        return Availability('quota_stale', 'quota observations invalid', True)
    if not strict:
        return Availability(stale=stale)
    if not quota.credential_current(state, vendor, now):
        return Availability('credential_unavailable', 'credential unavailable')
    snap = quota.load(state, vendor) or {}
    if not isinstance(snap, dict):
        return Availability('quota_stale', 'quota observations invalid', True)
    if not quota.fresh(state, vendor, now, settings.quota_stale_minutes):
        return Availability('quota_stale', 'quota observations stale or unavailable', True)
    _, blocked = quota.assess(snap, getattr(settings, f'quota_cap_{vendor}'), settings.quota_five_hour_max, now)
    return Availability('quota_exhausted', blocked) if blocked else Availability()


def choose(state, settings, pool, candidates, now, *, prefer=(), current='', extras=False):
    """Quota pace within the pool; preferences break close ties. For review-style
    selection, extras keep the existing preference-before-pool-pace behavior.
    Candidates must already pass role-specific eligibility and availability checks.
    """
    candidates = list(dict.fromkeys(candidates))
    snapshots = {v: quota.load(state, v) for v in candidates}
    caps = {v: getattr(settings, f'quota_cap_{v}') for v in candidates}
    if not extras:
        return quota.pick(candidates, snapshots, caps, settings.quota_five_hour_max, now, current, prefer)
    pooled = [v for v in candidates if v in pool]
    best, summary = quota.pick(pooled, snapshots, caps, settings.quota_five_hour_max, now, current, prefer)
    eligible = [v for v in candidates if not quota.assess(
        snapshots[v], caps[v], settings.quota_five_hour_max, now)[1]]
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
