"""Pick the vendor for a role, with the stale-quota probes and the log line (migration
slice 4c, #280): `vendors_pick` of roles.sh, for the callers that moved to Python. The
decision is baton.vendors.pick; this runs the probes it asks for, repeats the pick,
and rests the item (Deferred) when no vendor can run."""

from . import session
import os

from . import notify, settle, vendors
from .common import Deferred


@session.scoped
def choose(env, role, current, mode, candidates, *, pr="", exclude="", issue="", other_model="", fixed_by=""):
    """The vendor for ROLE. MODE: pool, contrib, or review (contrib among vendors that may
    review PR). Deferred("pool", why) when none can run."""
    env = session.resolve(env)
    state, pool = str(env.config.paths.state), list(env.config.quota.vendor_pool)
    by, review = ("contrib", True) if mode == "review" else (mode, False)
    kwargs = dict(by=by, review=review, fixed_by=fixed_by, other_model=other_model)
    kwargs["pr_state"] = str(env.dirs.state)   # the repository's PR records; cooldowns and quota are global
    asked, _ = vendors.pick(state, pool, role, candidates, current, pr, exclude.split(), probe_only=True, **kwargs)
    for vendor in asked["probe"]:
        settle.probe(state, str(env.config.paths.logs), vendor, env.config.quota.quota_probe_model_claude)
    # Only read the task's area (one gh call) when the role has a per-area order.
    area = vendors.task_area(env.repo.gh_repo, pr, issue) if vendors.area_keys(role, env) else ""
    result, rc = vendors.pick(state, pool, role, candidates, current, pr, exclude.split(), area=area, **kwargs)
    if rc == 75:
        raise Deferred("pool", result["reason"])
    vendor = result["vendor"]
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
    return vendors.all_vendors(list(env.config.quota.vendor_pool), env.config.quota.vendor_extra)
