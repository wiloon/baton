"""Unattended runs cannot answer a skill's confirmation step; the prompts say who does."""
from pathlib import Path
import re

import pytest

from conftest import full_text
from baton import config, initialization, prompts, session

BIN = Path(__file__).resolve().parent.parent / "bin"


def text(name):
    return (BIN / name).read_text()


def test_dev_prompt_takes_the_test_seams_as_confirmed_and_never_blocks_on_a_skill():
    prompt = text("dev-prompt.md")
    assert "`## Test seams`" in prompt
    assert "Never stop with `RESULT: blocked` because a skill wants a confirmation" in prompt


def test_fix_prompts_answer_the_tdd_seam_confirmation():
    for name in ("fix-prompt.md", "codex-fix-prompt.md"):
        assert "`## Test seams`" in text(name), name


def test_spec_reviewer_asks_for_test_seams_and_authors_add_them_as_decidable():
    assert "**Test seams:**" in text("spec-review-prompt.md")
    for name in ("spec-prompt.md", "codex-spec-prompt.md"):
        assert "**Test seams** point is `decidable`" in text(name), name


def test_dev_prompt_asks_for_as_many_acceptance_items_as_one_pr_holds():
    prompt = text("dev-prompt.md")
    assert "as many of the remaining acceptance items as one reviewable PR holds" in prompt
    assert "Implement the next safely deployable slice" not in prompt


def test_dev_prompt_keeps_closing_keywords_to_its_own_issue():
    assert "To mention another issue, write its number without one" in text("dev-prompt.md")


def test_codex_fix_prompt_routes_description_fixes_through_the_runner():
    prompt = text("codex-fix-prompt.md")
    assert "`{{PR_DESCRIPTION_FILE}}`" in prompt
    assert "`/tmp/baton-fix-{{PR}}.md.body`" in prompt
    assert "You have no GitHub access" in prompt


def test_fix_prompts_answer_nits_in_the_form_the_runner_reads():
    for name in ("fix-prompt.md", "codex-fix-prompt.md", "cursor-fix-prompt.md"):
        assert "`## Nits` section" in text(name), name
        assert "`- <citation> adopted — what you changed`" in text(name), name


def test_spec_review_searches_the_glossary_and_knows_the_e2e_gate():
    prompt = text("spec-review-prompt.md")
    assert "`GLOSSARY.md`, and every ADR" not in prompt
    assert "by searching `GLOSSARY.md`" in prompt
    # The gate is a repository's Playwright convention: it reaches the reviewer through the repo's notes.
    assert "## E2E gate" in prompts.render(BIN / "spec-review-prompt.md", [])


def test_the_implementer_leaves_the_e2e_gate_to_the_runner():
    assert "the runner runs it before pushing" in prompts.render(BIN / "dev-prompt.md", [])


def test_the_repositorys_e2e_hook_fills_its_notes():
    prompt = prompts.render(BIN / "dev-prompt.md", [("WORKTREE", "/wt")])
    assert "{{E2E_HOOK}}" not in prompt and "/hooks/e2e.sh /wt [playwright arguments]" in prompt


# --- #326 slice 1b: the prompts name no repository; the repository's own docs and config fill in ---

TEMPLATES = sorted(p.name for p in BIN.glob("*-prompt.md"))
PROJECT_WORDS = re.compile(r"principles\.md|docs/architecture|\bP-[0-9]|Linear|task ci:changed"
                           r"|Playwright|jsdom|sub-project")


@pytest.mark.parametrize("name", TEMPLATES)
def test_templates_carry_no_project_conventions(name):
    found = {m.group(0) for m in PROJECT_WORDS.finditer(text(name))}
    assert not found, f"{name}: {sorted(found)}"


def other_repo(tmp_path, **extra):
    """A Session for another project, built from the shipped config with a different [[repos]] entry."""
    source = full_text()
    body = '\n'.join([
        '[[repos]]', 'gh_repo = "acme/shop"', 'owner = "alice"', 'agent_milestone = ""', 'ci_fixable_checks = []',
        'scan_areas = []', 'env_files = "~/e"', 'secret_env_file = "~/e/s"', 'secret_env_keys = []', 'tmp_prefixes = []',
        'ci_command = "make check BASE={base}"', f'prompt_dir = "{extra.get("prompt_dir", "")}"',
        'verify_hook = ""', 'e2e_hook = ""', 'gate_hook = ""', 'nightly_hook = ""', 'gate_target = ""', 'gate_option = ""', '',
        '[repos.areas]', '', '[repos.env]', '', '[repos.prompt_notes]', *extra.get("notes", []), '', ''])
    source = re.sub(r"(?ms)^\[\[repos\]\]\n.*?(?=^\[models\])", body, source).replace("scan = true", "scan = false")
    path = tmp_path / "config.toml"
    path.write_text(source)
    (tmp_path / "bin").symlink_to(BIN)
    c = config.load(path, home=tmp_path)
    return session.Session(c, initialization.Runtime(), {}, tmp_path)


def test_render_fills_the_repository_owner_repo_and_ci_command(tmp_path):
    env = other_repo(tmp_path)
    spec = prompts.render(BIN / "spec-prompt.md", [("ISSUE", "7")], env=env)
    fix = prompts.render(BIN / "verify-fix-prompt.md", [("BASE", "abc123")], env=env)
    assert "(`alice`)" in spec and "{{OWNER}}" not in spec and "acme/shop" in spec
    assert "`make check BASE=abc123`" in fix
    assert "{{REPO_NOTES}}" not in spec and "\n\n\n" not in spec


def test_notes_reach_only_their_template_and_data_is_never_expanded(tmp_path):
    env = other_repo(tmp_path, notes=['dev = "Run `bin/smoke {{WORKTREE}}` before you finish."'])
    dev = prompts.render(BIN / "dev-prompt.md", [("WORKTREE", "/w"), ("CONTEXT_FILE", "{{OWNER}}")], env=env)
    review = prompts.render(BIN / "review-prompt.md", [], env=env)
    assert "Run `bin/smoke /w` before you finish." in dev and "Run `bin/smoke" not in review
    assert "`{{OWNER}}` first" in dev    # caller data stays data


def test_a_prompt_dir_overrides_a_template_by_name(tmp_path):
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "scan-prompt.md").write_text("Scan {{GH_REPO}} for {{OWNER}}.\n")
    env = other_repo(tmp_path, prompt_dir=str(custom))
    assert prompts.render(BIN / "scan-prompt.md", [], env=env) == "Scan acme/shop for alice."
    assert prompts.render(BIN / "review-prompt.md", [], env=env).startswith("You are running unattended")
