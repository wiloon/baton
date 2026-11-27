"""The route tier of a step (#237, ADR 0009): a per-route record under the repository's state dir,
keyed by the episode (the issue, or the PR without one). Nothing writes a non-standard tier yet.

A step reads its tier once (`carry`) and takes it along in Runtime, so vendor selection, eligibility
checks and dispatch see the same value."""
import json

from . import notify
from .config import TIERED_ROUTES, TIERS
from .quota import atomic_json

STANDARD = "standard"


def episode(issue, pr=""):
    return next((key for key in (str(issue), str(pr)) if key.isdecimal() and int(key) > 0), "")


def path(env, key):
    return env.dirs.state / f"tiers-{key}.json"


def write(env, key, record):
    """Replace the episode's record: {route: tier} over the tiered routes."""
    atomic_json(path(env, key), record)


def read(env, key, route):
    """The recorded tier, else `standard`; reading never fails a step."""
    record = path(env, key)
    try:
        tier = json.loads(record.read_text()).get(route, STANDARD)
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r} for {route}")
    except FileNotFoundError:
        return STANDARD
    except (OSError, ValueError, AttributeError) as error:
        notify.log(f"tier record {record.name} unreadable, using standard: {error}")
        return STANDARD
    return tier


def step(env, route, issue, pr=""):
    """The tier this step carries, else the recorded one. Untiered routes are `standard`."""
    if route not in TIERED_ROUTES:
        return STANDARD
    if route in env.runtime.tiers:
        return env.runtime.tiers[route]
    key = episode(issue, pr)
    return read(env, key, route) if key else STANDARD


def carry(env, route, issue, pr=""):
    """`env` holding the step's tier for `route`; unchanged when it holds one or has no episode."""
    if route not in TIERED_ROUTES or route in env.runtime.tiers or not episode(issue, pr):
        return env
    return env.with_runtime(tiers={**env.runtime.tiers, route: step(env, route, issue, pr)})
