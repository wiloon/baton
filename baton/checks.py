"""The checks section of a PR or issue body: the one place that knows its name (#94, ADR 0006).

The section lists what must be verified before the PR is done. Agents write HEADING; LEGACY is
the name older bodies carry and stays readable. A body is *well-formed* when it has exactly one
checks section under either name; a missing, duplicated or mixed one never clears the merge gate.
Prompts get the heading through the `{{CHECKS_HEADING}}` template variable. Pure: no I/O.
"""
import re

from . import check_verdicts
from .common import is_author_marker_line

HEADING = "Checks to verify"
LEGACY = "Manual checks"
# Footers a coding tool appends to whatever section ends the body.
ATTRIBUTION = re.compile(r"(🤖 Generated with |co-authored-by:)", re.IGNORECASE)

_HEADING = re.compile(rf"(?i)##[ \t]+(?:{re.escape(HEADING)}|{re.escape(LEGACY)})[ \t]*:?[ \t\r]*")
_NONE = re.compile(r"(none)?")


def is_heading(line):
    """True for a `## ` line that names the checks section, under either name."""
    return bool(_HEADING.fullmatch(line))


def _sections(body):
    """(start, end) line indexes of each checks section in the body's lines: the heading line
    and the first line of the next `## ` heading (or the end)."""
    lines = (body or "").split("\n")
    found, start = [], None
    for i, line in enumerate(lines):
        if line.startswith("## "):
            if start is not None:
                found.append((start, i))
                start = None
            if is_heading(line):
                start = i
    if start is not None:
        found.append((start, len(lines)))
    return lines, found


def _text(lines, span):
    return "\n".join(
        line for line in lines[span[0] + 1:span[1]] if not is_author_marker_line(line)
    ).replace("\r", "").strip("\n")


def well_formed(body):
    """True when the body has exactly one checks section."""
    return len(_sections(body)[1]) == 1


def read(body):
    """The text of the one checks section; None when the body is not well-formed."""
    lines, found = _sections(body)
    return _text(lines, found[0]) if len(found) == 1 else None


def all_blocks(body):
    """The text of every checks section, either name, in body order: the recovery reader for
    where checks must not be lost. Never clears a gate."""
    lines, found = _sections(body)
    return "\n".join(text for text in (_text(lines, span) for span in found) if text.strip())


def items(body):
    """The checks of a well-formed body, one per list item; none when it is not well-formed."""
    text = read(body)
    return _block_items(text) if text is not None else []


def recovery_items(body):
    """The checks of every checks section, for a body of any form."""
    lines, found = _sections(body)
    return [item for span in found for item in _block_items(_text(lines, span))]


def _block_items(text):
    return [] if _is_none(text) else check_verdicts.items(text)


def lines(body):
    """Each non-empty line of every checks section, stripped; none for a section saying none."""
    text = all_blocks(body)
    if _NONE.fullmatch(re.sub(r"[-*.\s]", "", text).lower()):
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def is_none(body):
    """True when the body is well-formed and its section's one non-empty line, tool attribution
    lines aside, is "none" or "none." (#265)."""
    text = read(body)
    return text is not None and _is_none(text)


def _is_none(text):
    answer = [line for line in (re.sub(r"^[-*\s]+", "", line) for line in text.split("\n"))
              if line and not ATTRIBUTION.match(line)]
    return len(answer) == 1 and bool(re.fullmatch(r"none\.?", answer[0].lower()))


def canonicalize(body):
    """The body with its one checks heading line rewritten to HEADING, every line under it
    untouched. A body that is not well-formed is returned as written."""
    lines, found = _sections(body)
    if len(found) != 1:
        return body
    i = found[0][0]
    lines[i] = f"## {HEADING}" + ("\r" if lines[i].endswith("\r") else "")
    return "\n".join(lines)


def set_checks(body, checks):
    """The body with every checks section replaced by one HEADING section holding CHECKS;
    appended when there is none."""
    lines, found = _sections(body.replace("\r\n", "\n"))
    if not found:
        return body.rstrip("\n") + f"\n\n## {HEADING}\n{checks}\n"
    out, at = [], 0
    for n, (start, end) in enumerate(found):
        out += lines[at:start]
        if n == 0:
            out += [f"## {HEADING}", checks, ""]
        at = end
    return "\n".join(out + lines[at:])


def bullets(listed):
    """ITEMS as a markdown list, continuation lines indented under their item."""
    return "\n".join("- " + item.replace("\n", "\n  ") for item in listed)
