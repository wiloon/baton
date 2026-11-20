"""The checks section of a body: one module, two heading names (#94)."""
import pytest

from baton import checks

OLD, NEW = "Manual checks", "Checks to verify"


def body(*sections):
    return "Closes #1\n\n" + "".join(f"## {heading}\n{text}\n\n" for heading, text in sections) + "## Before release\nnone\n"


@pytest.mark.parametrize("heading", [OLD, NEW])
def test_one_section_under_either_name_is_read(heading):
    text = body((heading, "- a\n- b"))
    assert checks.well_formed(text) and checks.items(text) == ["a", "b"] and checks.read(text) == "- a\n- b"


@pytest.mark.parametrize("line", ["## manual checks", "## CHECKS TO VERIFY:", "## Checks to verify  "])
def test_a_heading_is_exact_apart_from_case_colon_and_space(line):
    assert checks.is_heading(line)


@pytest.mark.parametrize("line", ["## Manual checks to try", "## Checks", "### Checks to verify", "## Checks to verify now"])
def test_a_heading_that_merely_starts_with_the_name_is_not_one(line):
    assert not checks.is_heading(line)


@pytest.mark.parametrize("text", [
    body(), body((NEW, "- a"), (NEW, "- b")), body((OLD, "- a"), (NEW, "- b")), body((OLD, "- a"), (OLD, "- b")),
], ids=["missing", "duplicate", "mixed", "two old"])
def test_a_body_that_is_not_well_formed_has_no_items_and_is_not_none(text):
    assert not checks.well_formed(text) and checks.read(text) is None
    assert checks.items(text) == [] and not checks.is_none(text)


def test_every_block_is_recovered_in_body_order():
    text = body((OLD, "- a"), (NEW, "- b\n  more"))
    assert checks.recovery_items(text) == ["a", "b\nmore"]
    assert checks.all_blocks(text) == "- a\n- b\n  more"
    assert checks.recovery_items(body()) == []


@pytest.mark.parametrize("heading", [OLD, NEW])
def test_none_is_none_under_either_name(heading):
    assert checks.is_none(body((heading, "none"))) and checks.is_none(body((heading, "- None.")))
    assert not checks.is_none(body((heading, "none\n- a"))) and not checks.is_none(body((heading, "")))


def test_canonicalizing_rewrites_only_the_heading_line():
    text = body((OLD, "- a  \n\n  keep this"))
    assert checks.canonicalize(text) == text.replace(f"## {OLD}", f"## {NEW}")
    assert checks.canonicalize(body((NEW, "- a"))) == body((NEW, "- a"))


def test_canonicalizing_leaves_a_body_that_is_not_well_formed_as_written():
    text = body((OLD, "- a"), (NEW, "- b"))
    assert checks.canonicalize(text) == text


def test_setting_replaces_every_section_with_one_and_appends_when_missing():
    text = body((OLD, "- a"), ("Notes", "x"), (NEW, "- b"))
    done = checks.set_checks(text, "- c")
    assert done.count("## Checks to verify") == 1 and "Manual checks" not in done
    assert "- c" in done and "- a" not in done and "- b" not in done and "## Notes\nx" in done
    assert checks.set_checks("Closes #1", "- c") == "Closes #1\n\n## Checks to verify\n- c\n"
