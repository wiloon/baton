"""Strong retrospective candidates and a nonblocking reservation over shared policy."""
from contextlib import contextmanager
from dataclasses import dataclass

from . import capacity, vendor_policy


@dataclass(frozen=True)
class Analyst:
    vendor: str
    model: str
    effort: str


def candidates(config):
    allowed = {*config.quota.vendor_pool, *config.quota.vendor_extra}
    settings = config.retrospectives
    result = []
    for vendor in ('claude', 'codex', 'cursor'):
        if vendor in allowed:
            model = getattr(settings, f'model_{vendor}')
            effort = ('xhigh' if '-xhigh' in model else 'high') if vendor == 'cursor' else settings.effort
            result.append(Analyst(vendor, model, effort))
    return result



@contextmanager
def reserve(config, state, now, failed=()):
    """Yield (analyst, lease, selection). Try another candidate on a lease race;
    callers get one model invocation, never an unbounded fallback loop.
    """
    choices = {c.vendor: c for c in candidates(config)}
    preferences = dict(config.preferences)
    prefer = preferences.get('retrospective', preferences.get('reviewer', ('claude', 'codex', 'cursor')))
    selection = dict(role='retrospective', strategy='review_preference_and_quota_pace',
                     preference=list(prefer), blocked={}, reason='vendor_disabled')
    ready = []
    for vendor in choices:
        status = vendor_policy.availability(state, config.quota, vendor, now, strict=True)
        if status.reason:
            selection['blocked'][vendor] = status.reason
        else:
            ready.append(vendor)
    # A failed vendor is a last resort for this revision, without resetting budgets.
    alternatives = [v for v in ready if v not in failed]
    ready = alternatives or ready
    while ready:
        chosen, summary = vendor_policy.choose(state, config.quota, config.quota.vendor_pool,
                                               ready, now, prefer=prefer, extras=True)
        selection['quota_summary'] = summary
        if not chosen:
            break
        with capacity.lease(state, chosen) as lease:
            status = vendor_policy.availability(state, config.quota, chosen, now, strict=True,
                                                leased=bool(lease))
            if lease and not status.reason:
                selection.update(chosen=chosen, requested_model=choices[chosen].model, reason='')
                yield choices[chosen], lease, selection
                return
            selection['blocked'][chosen] = status.reason or 'vendor_capacity'
        ready.remove(chosen)
    reasons = list(selection['blocked'].values())
    if reasons:
        selection['reason'] = reasons[0] if len(set(reasons)) == 1 else 'no_analyst_available'
    yield None, None, selection
