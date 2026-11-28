"""Strong retrospective candidates and their selection over shared policy."""
from contextlib import contextmanager
from dataclasses import dataclass

from . import agents, capacity, vendor_policy


@dataclass(frozen=True)
class Analyst:
    vendor: str
    model: str
    effort: str


def candidates(config):
    settings = config.retrospectives
    result = []
    for vendor in (*config.pool(), *config.extra()):
        if not config.allows(vendor, "retrospective"):
            continue
        model = config.analyst_model(vendor)
        effort = agents.ADAPTERS[vendor].analyst_effort(model, settings.effort)
        result.append(Analyst(vendor, model, effort))
    return result



@contextmanager
def reserve(config, state, now, failed=()):
    """Yield (analyst, selection). A vendor with a workload running is not chosen (ADR 0001),
    but nothing is held during the analysis: a workload never waits for one. Callers get
    one model invocation, never an unbounded fallback loop.
    """
    choices = {c.vendor: c for c in candidates(config)}
    preferences = dict(config.preferences)
    prefer = preferences.get('retrospective', preferences.get('reviewer', ()))
    prefer = [vendor for vendor in prefer if vendor in choices]
    selection = dict(role='retrospective', strategy='review_preference_and_quota_pace',
                     preference=list(prefer), blocked={}, reason='vendor_disabled')
    ready = []
    for vendor in choices:
        reason = ('vendor_capacity' if capacity.busy(state, vendor) else
                  vendor_policy.availability(state, config.quota, vendor, now, strict=True).reason)
        if reason:
            selection['blocked'][vendor] = reason
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
        # A workload may have started, or the observations changed, while choosing.
        reason = ('vendor_capacity' if capacity.busy(state, chosen) else
                  vendor_policy.availability(state, config.quota, chosen, now, strict=True).reason)
        if not reason:
            selection.update(chosen=chosen, requested_model=choices[chosen].model, reason='')
            yield choices[chosen], selection
            return
        selection['blocked'][chosen] = reason
        ready.remove(chosen)
    reasons = list(selection['blocked'].values())
    if reasons:
        selection['reason'] = reasons[0] if len(set(reasons)) == 1 else 'no_analyst_available'
    yield None, selection
