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
ANSWER = re.compile(r"^\s*(?:[-*+]\s+)?(D\d+\.\d+)\s*[.):]?\s*(resolved|held)\b[\s—–:-]*(.*)$", re.IGNORECASE)
REPLY = re.compile(r"^\s*(?:[-*+]\s+)?(D\d+\.\d+)\s*[.):]?\s*(adopted|rejected)\b[\s—–:-]*(.*)$", re.IGNORECASE)


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


def split_section(text):
    """TEXT as (the rest, the `## Design findings` section whole with its heading); the section is
    "" when TEXT has none."""
    rest, kept, on = [], [], False
    for line in (text or "").split("\n"):
        if HEADING.match(line):
            on = bool(SECTION.match(line.strip()))
        (kept if on else rest).append(line)
    return "\n".join(rest), "\n".join(kept).rstrip("\n")


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


def lines_of(lines, pattern):
    """(id, kind, why) of every line of LINES that PATTERN matches."""
    found = []
    for line in lines or ():
        match = pattern.match(re.sub(r"[*`_]", "", line))
        if match:
            found.append((match.group(1).upper(), match.group(2).lower(), match.group(3).strip()))
    return found


def unique(found):
    """{id: (kind, why)} of the ids FOUND names exactly once (a repeated id counts as unanswered)."""
    count = {}
    for key, _, _ in found:
        count[key] = count.get(key, 0) + 1
    return {key: (kind, why) for key, kind, why in found if count[key] == 1}


def judgments(comment):
    """{id: (resolved | held, why)} from `## Design answers`."""
    return unique(lines_of(section(comment, ANSWERS), ANSWER))


def answers(comment):
    """{id: "resolved" | "held"}; an id given twice is dropped."""
    return {key: kind for key, (kind, _) in judgments(comment).items()}


def replies(text):
    """The fixer's `## Design findings` lines of one reply: [(id, adopted | rejected, why)]."""
    return lines_of(section(text, SECTION), REPLY)


def current_answers(texts):
    """The fixer's current answers from the replies TEXTS of one window: {id: (kind, why)}. An id
    answered twice (in one reply or across replies, even identically) counts as unanswered."""
    return unique([line for text in texts for line in replies(text)])


def apply_answers(items, current):
    """ITEMS with each id's current answer from CURRENT; an item with none keeps its previous
    answer, marked earlier (history: it never makes a disagreement)."""
    out = []
    for item in items:
        item = dict(item)
        if item["id"] in current:
            kind, why = current[item["id"]]
            item["answer"] = {"kind": kind, "why": why, "earlier": False}
        elif item.get("answer"):
            item["answer"] = {**item["answer"], "earlier": True}
        out.append(item)
    return out


def judge(items, comment):
    """ITEMS after a screenshot review's judgments: `resolved` closes an item that has no
    disagreement; `held` against a current `rejected` answer makes it one (both positions kept);
    every kept answer then becomes history for the next exchange."""
    given, kept = judgments(comment), []
    for item in items:
        verdict, why = given.get(item["id"], ("", ""))
        if verdict == "resolved" and not item.get("disagree"):
            continue
        answer = item.get("answer")
        if (verdict == "held" and answer and answer["kind"] == "rejected" and not answer["earlier"]
                and not item.get("disagree")):
            item = {**item, "disagree": {"reviewer": why or "held", "fixer": answer["why"] or "rejected"}}
        if answer:
            item = {**item, "answer": {**answer, "earlier": True}}
        kept.append(item)
    return kept


def rejects_all(ids, text):
    """True when the reply TEXT answers every id of IDS exactly once, all `rejected`."""
    given = current_answers([text])
    return bool(ids) and all(given.get(key, ("", ""))[0] == "rejected" for key in ids)


def answer_text(item):
    """The fixer's answer to ITEM for a block or hand-off line; "" when it gave none."""
    answer = item.get("answer")
    if not answer:
        return ""
    label = "earlier fixer answer" if answer.get("earlier") else "fixer answer"
    return f" [{label}: {answer['kind']} — {answer['why']}]" if answer["why"] else f" [{label}: {answer['kind']}]"


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
                     f" — {item['text']}{answer_text(item)}" for item in open_items)


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
