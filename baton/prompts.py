"""Prompt templates: replace {{KEY}} placeholders.

    python -m baton.prompts TEMPLATE KEY VALUE [KEY VALUE ...]

The templates name no repository (#326). Shared rules live in `partials/NAME.md` next to the
templates; a `{{> NAME}}` line of the selected template (and only of it) is replaced by that
file first, its lines indented like the marker. The watched repository fills in first, from its
configuration: a same-named template in `repo.prompt_dir` replaces the shipped one,
{{REPO_NOTES}} takes `repo.prompt_notes` for this template (its line goes when there are none),
then {{OWNER}}, {{GH_REPO}}, {{CI_COMMAND}} and {{E2E_HOOK}}. The caller's keys follow, one after another in
the order given, as the bash render did. Configuration is trusted and goes first, so text the
caller passes in (an issue, a review) is never expanded as a placeholder of the repository's.
"""
import re
import sys
from pathlib import Path

from . import checks, session

PARTIAL_LINE = re.compile(r"^([ \t]*)\{\{>[ \t]*([A-Za-z0-9_-]+)[ \t]*\}\}[ \t]*$", re.M)
NOTES_LINE = re.compile(r"^[ \t]*\{\{REPO_NOTES\}\}[ \t]*\n?", re.M)
READING_GUIDANCE = """\nRead narrowly: locate symbols with rg -n or git grep, then read the relevant line ranges and callers. Use rg -l when only filenames are needed. Keep exploratory output to about 100 lines per call; narrow a query that exceeds it and inspect every relevant match. Expand to a whole file or repository-wide search when needed to establish correctness. In follow-up rounds, start with the changed points and diff; reuse facts in the supplied context after checking they still hold.\n"""


def name_of(template):
    """dev-prompt.md -> dev, codex-spec-prompt.md -> codex-spec."""
    return Path(template).name.removesuffix(".md").removesuffix("-prompt")


def expand_partials(text, shipped, repo):
    """TEXT with each `{{> name}}` line replaced by the partial `name.md`: the repository's
    `prompt_dir/partials/` first, then the shipped SHIPPED directories. Partials do not include
    partials, and an unknown one is an error."""
    def partial(match):
        indent, name = match[1], match[2]
        for directory in ((repo.prompt_dir / "partials",) if repo.prompt_dir is not None else ()) + shipped:
            if (directory / f"{name}.md").is_file():
                found = (directory / f"{name}.md").read_text().rstrip("\n")
                return "\n".join(indent + line if line else line for line in found.split("\n"))
        raise ValueError(f"unknown prompt partial {name!r}")
    return PARTIAL_LINE.sub(partial, text)


def render(template, pairs, env=None):
    repo = session.resolve(env).repo
    template = Path(template)
    shipped = (template.parent / "partials", template.resolve().parent / "partials")   # a symlinked template finds them
    if repo.prompt_dir is not None and (repo.prompt_dir / template.name).is_file():
        template = repo.prompt_dir / template.name
    text = expand_partials(template.read_text().rstrip("\n"), shipped, repo)
    notes = dict(repo.prompt_notes).get(name_of(template), "").strip()
    text = text.replace("{{REPO_NOTES}}", notes) if notes else NOTES_LINE.sub("", text)
    given = dict(pairs)
    base = given.get("BASE", "origin/main")
    for key, value in (("OWNER", repo.owner), ("GH_REPO", given.get("GH_REPO", repo.gh_repo)),
                       ("READING_GUIDANCE", READING_GUIDANCE.strip()),
                       ("CHECKS_HEADING", checks.HEADING),
                       ("CI_COMMAND", repo.ci_command.replace("{base}", base)),
                       ("E2E_HOOK", str(repo.e2e_hook or ""))):
        text = text.replace("{{" + key + "}}", value)
    for key, value in pairs:
        text = text.replace("{{" + key + "}}", value)
    return text


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m baton.prompts TEMPLATE [KEY VALUE ...]", file=sys.stderr)
        return 2
    rest = argv[1:]
    sys.stdout.write(render(argv[0], list(zip(rest[::2], rest[1::2]))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
