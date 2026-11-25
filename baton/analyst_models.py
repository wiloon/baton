"""One source of truth for the retrospective role's approved model families: each adapter's."""
import re

from . import agents


def approved_model(vendor, model, *, requested=False):
    adapter = agents.ADAPTERS.get(vendor)
    if not isinstance(model, str) or adapter is None:
        return False
    family = adapter.ANALYST_REQUEST_FAMILY if requested else adapter.ANALYST_FAMILY
    return bool(re.fullmatch(family, model))
