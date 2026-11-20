"""Which manual checks the screenshot review cleared (#92, ADR 0006).

The screenshot reviewer gives each item of a PR's checks section one verdict in its
comment's `## Checks` section, numbered as the prompt numbered them:

    1. verified
    2. owner — a real sign-up and redirect

`verified`: the screenshots show the item and it looks right. `broken`: they show it is wrong
(a Must fix). `owner`: no screenshot can show it, or the reviewer is not sure. The form is read
loosely (markdown emphasis, a list marker, `1:`), the content strictly: anything less than one
valid verdict per item reads as no verdicts, and then every item is the owner's. Pure: no I/O.
"""

import re

VERDICTS = ("verified", "broken", "owner")
ITEM = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+(.*)$")
VERDICT = re.compile(r"^\s*(?:[-+]\s+)?(\d+)\s*[.):]\s*(\w+)\b", re.IGNORECASE)
HEADING = re.compile(r"^#{1,6}\s")
CHECKS_HEADING = re.compile(r"^#{2,3}\s+checks\s*:?\s*$", re.IGNORECASE)


def items(checks):
    """The checks of a checks section: one per list item, continuation lines
    included; text with no list is one check; "none" or nothing is no checks."""
    text = (checks or "").replace("\r", "").strip("\n")
    if re.fullmatch(r"(none)?", re.sub(r"[-*.\s]", "", text).lower()):
        return []
    found = [[]]   # text before the first list item joins it
    for line in text.split("\n"):
        match = ITEM.match(line)
        if match and found[-1]:
            found.append([])
        part = (match.group(1) if match else line).strip()
        if part:
            found[-1].append(part)
    return ["\n".join(item) for item in found if item]


def numbered(checks):
    """The checks numbered for the prompt, continuation lines indented under their item."""
    return "\n".join(f"{n}. " + item.replace("\n", "\n   ") for n, item in enumerate(items(checks), 1))


def parse(comment, count):
    """One verdict per check, in order, from the `## Checks` section; None unless every check
    1..COUNT has exactly one of VERDICTS and no other number appears."""
    on, seen = False, {}
    for line in (comment or "").replace("\r", "").split("\n"):
        line = re.sub(r"[*`_]", "", line)   # **1.** `verified`, _owner_: the same verdicts
        if HEADING.match(line):
            on = bool(CHECKS_HEADING.match(line.strip()))
            continue
        match = VERDICT.match(line) if on else None
        if not match:
            continue
        n, verdict = int(match.group(1)), match.group(2).lower()
        if verdict not in VERDICTS or n in seen or not 1 <= n <= count:
            return None
        seen[n] = verdict
    if count == 0 or len(seen) != count:
        return None
    return [seen[n] for n in range(1, count + 1)]


def for_owner(checks, comment):
    """The checks the owner still has to look at: those not `verified`, or all of them when the
    verdicts are unreadable. Checks the owner wrote into the issue are judged like any other."""
    listed = items(checks)
    verdicts = parse(comment, len(listed))
    if verdicts is None:
        return listed
    return [item for item, verdict in zip(listed, verdicts) if verdict != "verified"]
