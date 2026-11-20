"""baton.check_verdicts: which manual checks the screenshot review cleared (#92)."""
import pytest

from baton import check_verdicts

CHECKS = """- The library at 390 px and 1280 px.
- Hover the Sign in link: it underlines.
  In dark mode too.
- Sign up with a new account and land back on Welcome."""

ITEMS = ["The library at 390 px and 1280 px.",
         "Hover the Sign in link: it underlines.\nIn dark mode too.",
         "Sign up with a new account and land back on Welcome."]


def review(*lines):
    return "## Must fix\nnone\n\n## Checks\n" + "\n".join(lines) + "\n\n## Screenshots\n- a.png — ok\n"


def test_each_list_item_is_one_check_with_its_continuation_lines():
    assert check_verdicts.items(CHECKS) == ITEMS


def test_numbered_and_star_lists_count_too():
    assert check_verdicts.items("1. one\n2) two\n* three") == ["one", "two", "three"]


def test_prose_without_a_list_is_one_check():
    assert check_verdicts.items("Open the page and look.\nBoth themes.") == ["Open the page and look.\nBoth themes."]


def test_none_has_no_checks():
    assert check_verdicts.items("none") == [] and check_verdicts.items("") == []


def test_the_prompt_gets_the_checks_numbered():
    assert check_verdicts.numbered(CHECKS) == (
        "1. The library at 390 px and 1280 px.\n"
        "2. Hover the Sign in link: it underlines.\n   In dark mode too.\n"
        "3. Sign up with a new account and land back on Welcome.")


def test_a_verdict_for_every_check_is_read_in_order():
    comment = review("1. verified", "3. owner — a real sign-up", "2. Verified: the hover shot shows it")
    assert check_verdicts.parse(comment, 3) == ["verified", "verified", "owner"]


@pytest.mark.parametrize("lines", [
    ("1. verified", "2. verified"),                         # one check has no verdict
    ("1. verified", "2. verified", "2. owner", "3. owner"),  # a check answered twice
    ("1. verified", "2. verified", "3. looks fine"),         # not one of the three
    ("1. verified", "2. verified", "3. verified", "4. verified"),  # a check that does not exist
])
def test_an_incomplete_or_ambiguous_list_reads_as_no_verdicts(lines):
    assert check_verdicts.parse(review(*lines), 3) is None


def test_a_review_without_a_checks_section_has_no_verdicts():
    assert check_verdicts.parse("## Must fix\nnone\n\n## Screenshots\n- a.png — ok", 3) is None


def test_the_owner_gets_only_the_checks_not_verified():
    comment = review("1. verified", "2. verified", "3. owner — a real sign-up")
    assert check_verdicts.for_owner(CHECKS, comment) == [ITEMS[2]]


def test_all_verified_leaves_nothing_for_the_owner():
    comment = review("1. verified", "2. verified", "3. verified")
    assert check_verdicts.for_owner(CHECKS, comment) == []


def test_a_broken_verdict_in_a_passed_review_still_goes_to_the_owner():
    comment = review("1. verified", "2. broken — no underline", "3. verified")
    assert check_verdicts.for_owner(CHECKS, comment) == [ITEMS[1]]


def test_without_readable_verdicts_every_check_goes_to_the_owner():
    assert check_verdicts.for_owner(CHECKS, "no checks section") == ITEMS
    assert check_verdicts.for_owner(CHECKS, "") == ITEMS


@pytest.mark.parametrize("lines", [
    ("1. `verified`", "2. **verified**", "3. _owner_ — a real sign-up"),
    ("- 1. verified", "* 2. verified", "3. owner"),
    ("**1.** verified", "2: verified", "3) owner"),
])
def test_markdown_dressing_around_a_verdict_is_ignored(lines):
    assert check_verdicts.parse(review(*lines), 3) == ["verified", "verified", "owner"]


@pytest.mark.parametrize("heading", ["## Checks", "## **Checks**", "### Checks", "## Checks:"])
def test_the_checks_heading_may_vary_a_little(heading):
    comment = review("1. verified", "2. verified", "3. owner").replace("## Checks", heading)
    assert check_verdicts.parse(comment, 3) == ["verified", "verified", "owner"]


def test_numbered_lines_in_other_sections_are_not_verdicts():
    comment = review("1. verified", "2. verified", "3. owner").replace("## Must fix\nnone", "## Must fix\n1. verified")
    assert check_verdicts.parse(comment, 3) == ["verified", "verified", "owner"]
