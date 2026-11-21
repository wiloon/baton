"""Unattended runs cannot answer a skill's confirmation step; the prompts say who does."""
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from conftest import full_text
from baton import checks, config, initialization, prompts, session

BIN = Path(__file__).resolve().parent.parent / "bin"


def text(name):
    """The shipped template with its partials expanded and the checks heading filled in."""
    expanded = prompts.expand_partials((BIN / name).read_text(), (BIN / "partials",), SimpleNamespace(prompt_dir=None))
    return expanded.replace("{{CHECKS_HEADING}}", checks.HEADING)


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


def test_fix_prompts_route_description_fixes_through_the_runner():
    for name in ("fix-prompt.md", "codex-fix-prompt.md"):
        prompt = text(name)
        assert "`{{PR_DESCRIPTION_FILE}}`" in prompt, name
        assert "`/tmp/baton-fix-{{PR}}.md.body`" in prompt, name
        assert "adopt a description review point" in prompt, name
        assert "make that description inaccurate" in prompt, name
        assert "keeping the task relation" in prompt, name
        assert "`TASK: complete` or `TASK: partial`" in prompt, name
        assert "allowed only for issue #{{ISSUE}}" in prompt, name
        assert "Mention any other issue by its number without a keyword" in prompt, name


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


# --- #28: an agent tries before it claims a limitation, and runs the terminal checks itself ---

TRY_FIRST = "try it once and quote the command and its exact error"
RUN_IT = "run it yourself and report its output"
UNPROVEN = "without the quoted command and its exact error is Must fix"


@pytest.mark.parametrize("name", ["dev-prompt.md", "fix-prompt.md", "codex-fix-prompt.md", "cursor-fix-prompt.md"])
def test_authors_try_before_claiming_a_limit_and_run_terminal_checks(name):
    prompt = text(name)
    assert TRY_FIRST in prompt
    assert RUN_IT in prompt


@pytest.mark.parametrize("name", ["review-prompt.md", "codex-review-prompt.md", "cursor-review-prompt.md"])
def test_reviewers_flag_an_unproven_limitation(name):
    assert UNPROVEN in text(name)


@pytest.mark.parametrize("name", ["spec-prompt.md", "codex-spec-prompt.md", "spec-review-prompt.md"])
def test_spec_prompts_keep_the_gate_line_and_the_owners_manual_checks(name):
    prompt = text(name)
    assert "`## E2E gate` is exactly one line" in prompt or "`## E2E gate` must be exactly one line" in prompt
    assert "`## Checks to verify`" in prompt and "The runner refuses an accept" in prompt


# --- #38: the verify-fix agent keeps to simple commands and retries a refused one ---

def test_the_verify_fix_prompt_gives_the_shell_rules_and_the_retry_on_denial():
    prompt = text("verify-fix-prompt.md")
    assert "one simple command per call, with no `;`, `&&`, `||`, pipes or `cd dir && …`" in prompt
    assert "retry the step as a simpler command or with the Read and Grep tools" in prompt
    assert "Only a denial that starts with `baton guard:` is final" in prompt
    assert "{{FAILURE}}" in prompt and "with the Read tool, not the shell" in prompt



def test_the_spec_author_is_told_a_denied_command_is_no_reason_to_fail():
    """#53: one denied `wc` and the reviser ended the round with RESULT: failed."""
    text = (BIN / "spec-prompt.md").read_text()
    assert "A denied command blocks only that command" in text
    assert "never a reason for `RESULT: failed`" in text


# --- shared partials (#94) ---------------------------------------------------------------

def first_line(name):
    return (BIN / "partials" / f"{name}.md").read_text().splitlines()[0].replace("{{CHECKS_HEADING}}", checks.HEADING)


@pytest.mark.parametrize("name, role_partial", [
    ("dev-prompt.md", None),
    ("fix-prompt.md", "fix-reply"), ("codex-fix-prompt.md", "fix-reply"), ("cursor-fix-prompt.md", "fix-reply"),
    ("review-prompt.md", "checks-review"), ("codex-review-prompt.md", "checks-review"),
    ("cursor-review-prompt.md", "checks-review"),
    ("spec-prompt.md", "checks-keep"), ("codex-spec-prompt.md", "checks-keep"),
])
def test_every_vendors_prompt_carries_the_shared_rules_under_the_new_heading(tmp_path, name, role_partial):
    out = prompts.render(BIN / name, [], env=other_repo(tmp_path))
    assert "{{>" not in out and "{{CHECKS_HEADING}}" not in out and "Manual checks" not in out
    assert first_line("checks-rules") in out
    if role_partial:
        assert first_line(role_partial) in out

    custom = tmp_path / "custom"
    (custom / "partials").mkdir(parents=True)
    (custom / "partials" / "checks-rules.md").write_text("Our own rules for ## {{CHECKS_HEADING}}.\n")
    overridden = prompts.render(BIN / name, [], env=other_repo(custom, prompt_dir=str(custom)))
    assert "Our own rules for ## Checks to verify." in overridden and first_line("checks-rules") not in overridden
    if role_partial:
        assert first_line(role_partial) in overridden


def test_a_caller_value_that_looks_like_a_partial_stays_data(tmp_path):
    out = prompts.render(BIN / "dev-prompt.md", [("RESUME_NOTE", "{{> checks-rules}}")], env=other_repo(tmp_path))
    assert "{{> checks-rules}}" in out.splitlines()


def test_an_unknown_partial_is_an_error(tmp_path):
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "scan-prompt.md").write_text("Scan.\n{{> nope}}\n")
    with pytest.raises(ValueError, match="unknown prompt partial"):
        prompts.render(BIN / "scan-prompt.md", [], env=other_repo(tmp_path, prompt_dir=str(custom)))
