"""Prompt templates: replace {{KEY}} placeholders.

    python -m baton.prompts TEMPLATE KEY VALUE [KEY VALUE ...]

The templates name no repository (#326). The watched repository fills in first, from its
configuration: a same-named template in `repo.prompt_dir` replaces the shipped one,
{{REPO_NOTES}} takes `repo.prompt_notes` for this template (its line goes when there are none),
then {{OWNER}}, {{GH_REPO}}, {{CI_COMMAND}} and {{E2E_HOOK}}. The caller's keys follow, one after another in
the order given, as the bash render did. Configuration is trusted and goes first, so text the
caller passes in (an issue, a review) is never expanded as a placeholder of the repository's.
"""
import re
import sys
from pathlib import Path

from . import session

NOTES_LINE = re.compile(r"^[ \t]*\{\{REPO_NOTES\}\}[ \t]*\n?", re.M)


def name_of(template):
    """dev-prompt.md -> dev, codex-spec-prompt.md -> codex-spec."""
    return Path(template).name.removesuffix(".md").removesuffix("-prompt")


def render(template, pairs, env=None):
    repo = session.resolve(env).repo
    template = Path(template)
    if repo.prompt_dir is not None and (repo.prompt_dir / template.name).is_file():
        template = repo.prompt_dir / template.name
    text = template.read_text().rstrip("\n")
    notes = dict(repo.prompt_notes).get(name_of(template), "").strip()
    text = text.replace("{{REPO_NOTES}}", notes) if notes else NOTES_LINE.sub("", text)
    given = dict(pairs)
    base = given.get("BASE", "origin/main")
    for key, value in (("OWNER", repo.owner), ("GH_REPO", given.get("GH_REPO", repo.gh_repo)),
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
