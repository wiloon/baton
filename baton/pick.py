"""Pick the vendor for a role, with the stale-quota probes and the log line (migration
slice 4c, #280): `vendors_pick` of roles.sh, for the callers that moved to Python. The
decision is baton.vendors.pick; this runs the probes it asks for, repeats the pick,
and rests the item (Deferred) when no vendor can run."""

from . import session
import os

from . import notify, settle, tiers, vendors
from .common import Deferred, Exhausted
from .config import ROLES, ROLE_ROUTE, ROUTE_ROLE


@session.scoped
def choose(env, role, current, mode, candidates, *, pr="", exclude="", issue="", other_model="", fixed_by="",
           spec_followup=False, pinned=False, avoid_author=""):
    """The vendor for ROLE. MODE: pool, contrib, or review (contrib among vendors that may
    review PR). Deferred("pool", why) when none can run."""
    env = session.resolve(env)
    state, pool = str(env.config.paths.state), list(env.config.pool())
    by, review = ("contrib", True) if mode == "review" else (mode, False)
    kwargs = dict(by=by, review=review, fixed_by=fixed_by, other_model=other_model, spec_followup=spec_followup,
                  pinned=pinned, avoid_author=avoid_author)
    kwargs["pr_state"] = str(env.dirs.state)   # the repository's PR records; cooldowns and quota are global
    vendor_role = ROUTE_ROLE.get(role, role)
    kwargs["tier"] = tiers.step(env, ROLE_ROUTE.get(vendor_role, vendor_role),
                                issue or env.runtime.issue, pr or env.runtime.pr)
    allowed = set(env.config.for_role(vendor_role, pool_only=vendor_role == "reviser"))
    if not allowed and vendor_role not in ROLES:
        allowed = set(env.config.declared())
    candidates = [vendor for vendor in candidates if vendor in allowed]
    if current and current in env.config.declared() and current not in allowed:
        current = ""
    asked, _ = vendors.pick(state, pool, role, candidates, current, pr, exclude.split(), probe_only=True, **kwargs)
    for vendor in asked["probe"]:
        settle.probe(state, str(env.config.paths.logs), vendor, env.config.probe_model(vendor))
    # Only read the task's area (one gh call) when the role has a per-area order.
    area = vendors.task_area(env.repo.gh_repo, pr, issue) if vendors.area_keys(role, env) else ""
    result, rc = vendors.pick(state, pool, role, candidates, current, pr, exclude.split(), area=area, **kwargs)
    if rc == 75:
        raise Deferred("pool", result["reason"], lends_slot=result["lends_slot"])
    if result.get("exhausted"):
        raise Exhausted(role, result.get("reason", ""))
    vendor = result["vendor"]
    try:
        notify.log("selection: " + vendors.selection_details(state, pool, candidates, vendor, role, area=area))
    except (OSError, ValueError, KeyError, TypeError):
        notify.log(f"selection: chosen={vendor} role={role} quota_details=unavailable")
    if result.get("quota_error"):
        notify.log("pool: quota.py failed (rc=2); using pool order")
    if vendor != current and not result.get("silent"):
        if mode == "pool":
            notify.log(f"pool: {role} -> {vendor} (was {current}; {result['reason']})")
        else:
            notify.log(f"contrib: {role} -> {vendor}{f' for PR #{pr}' if pr else ''} (was {current}; {result['reason']})")
    return vendor


@session.scoped
def all_vendors(env=None):
    env = session.resolve(env)
    return vendors.all_vendors(list(env.config.pool()), env.config.extra())
