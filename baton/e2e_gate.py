"""The one run an issue asks the runner for: its `## E2E gate` section.

The section is one line: targets, then options, such as `e2e/a.spec.ts --repeat-each=15`.
"none" or no section means no gate, and so does a repository with no gate hook. An issue body is
text agents can revise, and the line reaches a command, so every token must match the
repository's patterns (`repo.gate_target`, then `repo.gate_option`) and anything else is refused.
"""
import re

from . import md


def parse(body, repo):
    """The gate hook's arguments, "" for no gate; ValueError when the section is malformed."""
    if repo.gate_hook is None:
        return ""
    text = md.section("E2E gate", body).strip()
    if not text:
        return ""
    line = text.strip("`").strip()
    tokens = line.split()
    count = len(tokens)
    if repo.gate_option:
        while count and re.fullmatch(repo.gate_option, tokens[count - 1]):
            count -= 1
    targets = tokens[:count]
    if "\n" in line or not targets or not all(re.fullmatch(repo.gate_target, token) for token in targets):
        raise ValueError("the `## E2E gate` section must be one line of targets and then options, in the forms "
                         f"this repository allows: {text!r}")
    return " ".join(tokens)
