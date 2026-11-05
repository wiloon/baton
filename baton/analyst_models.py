"""One source of truth for the retrospective role's approved model families."""
import re

FAMILIES = {
    'claude': r'(?:opus|claude-opus-[A-Za-z0-9._:-]+)',
    'codex': r'gpt-6(?:\.1)?-sol',
    'cursor': r'grok-4\.7(?:-(?:high|xhigh)(?:-fast)?)?',
}


def approved_model(vendor, model, *, requested=False):
    if not isinstance(model, str) or vendor not in FAMILIES:
        return False
    if not re.fullmatch(FAMILIES[vendor], model):
        return False
    # Cursor advertises explicit reasoning-level IDs; a returned actual identity
    # may be the base Grok name, but a request must never use Auto or an implicit level.
    return not (requested and vendor == 'cursor' and model == 'grok-4.7')
