"""Design findings of the screenshot review (#96).

The screenshot reviewer may raise design findings in a `## Design findings` section:

    1. checks 1, 2 — <file>: <measurements>; <cause>; <concrete change>

Each is given a stable id `D<round>.<n>` by the runner. Later screenshot reviews judge every
open id under `## Design answers` (`D1.1. resolved — why` / `D1.1. held — why`). Only a valid
`resolved` closes a finding. Pure: no I/O.
"""

import json
import re

HEADING = re.compile(r"^#{1,6}\s")
SECTION = re.compile(r"^#{2,3}\s+design findings\s*:?\s*$", re.IGNORECASE)
ANSWERS = re.compile(r"^#{2,3}\s+design answers\s*:?\s*$", re.IGNORECASE)
FINDING = re.compile(r"^\s{0,3}(?:[-*+]\s+)?(\d+)\s*[.):]\s+checks?\s+([\d\s,]+?)\s*[—–-]+\s*(\S.*)$", re.IGNORECASE)
ANSWER = re.compile(r"^\s*(?:[-*+]\s+)?(D\d+\.\d+)\s*[.):]?\s*(resolved|held)\b", re.IGNORECASE)


def section(comment, heading):
    """The lines of the section HEADING matches, continuation lines included; None when absent."""
    lines, on, found = [], False, False
    for line in (comment or "").replace("\r", "").split("\n"):
        if HEADING.match(line):
            on = bool(heading.match(line.strip()))
            found = found or on
            continue
        if on:
            lines.append(line)
    return lines if found else None


def parse(comment, round, count):
    """The new findings of the comment as `{id, checks, text}` (checks: 1-based numbers), `[]`
    for `none`; None when the section is missing or any finding has no valid check number
    (no usable verdicts: every check then goes to the owner)."""
    lines = section(comment, SECTION)
    if lines is None:
        return None
    body = [line for line in lines if line.strip()]
    if not body:
        return None
    if len(body) == 1 and re.fullmatch(r"none", re.sub(r"[-*.\s]", "", body[0]).lower()):
        return []
    found = []
    for line in body:
        match = FINDING.match(re.sub(r"[*`_]", "", line))
        if not match:
            if found and line.startswith((" ", "\t")):
                found[-1]["text"] += " " + line.strip()
                continue
            return None
        numbers = [int(n) for n in re.findall(r"\d+", match.group(2))]
        if not numbers or any(not 1 <= n <= count for n in numbers):
            return None
        found.append({"id": f"D{round}.{match.group(1)}", "checks": sorted(set(numbers)),
                      "text": match.group(3).strip()})
    if len({item["id"] for item in found}) != len(found):
        return None
    return found


def answers(comment):
    """{id: "resolved" | "held"} from `## Design answers`; an id given twice is dropped (it
    counts as unanswered)."""
    given, twice = {}, set()
    for line in section(comment, ANSWERS) or ():
        match = ANSWER.match(re.sub(r"[*`_]", "", line))
        if not match:
            continue
        key = match.group(1).upper()
        if key in given:
            twice.add(key)
        given[key] = match.group(2).lower()
    return {key: value for key, value in given.items() if key not in twice}


def settle(open_items, comment):
    """OPEN_ITEMS minus those the comment resolved."""
    given = answers(comment)
    return [item for item in open_items if given.get(item["id"]) != "resolved"]


def with_check_text(found, listed):
    """FOUND with each finding's `checks` as the text of the checks it blocks (LISTED, in order)."""
    return [{**item, "checks": [listed[n - 1] for n in item["checks"]]} for item in found]


def block(open_items):
    """The open design findings block the runner renders: one line per id, `none` when empty."""
    if not open_items:
        return "none"
    return "\n".join(f"{item['id']}. checks {'; '.join(c.replace(chr(10), ' ') for c in item['checks'])}"
                     f" — {item['text']}" for item in open_items)


def blocked(open_items):
    """The check texts the open findings block."""
    return [check for item in open_items for check in item["checks"]]


def dump(open_items):
    return json.dumps(open_items, separators=(",", ":")) if open_items else ""


def load(text):
    try:
        items = json.loads(text) if text else []
    except ValueError:
        return []
    return items if isinstance(items, list) else []
