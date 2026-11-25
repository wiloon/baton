"""Design findings of the screenshot review (#96), through Review.loop with the agent, gh and fix
doubles of tests/test_review.py."""
import pytest

from baton import review, session
from baton.common import Deferred
from test_review import BIN, PR, loop, screen_run   # noqa: F401  (loop is a fixture)

FOUND = ("## Must fix\nnone\n\n## Nits\nnone\n\n## Design findings\n"
         "1. checks 1 — a.css: bottom 24 px vs top 18 px; unequal optical gap; cut bottom padding to 18 px\n\n"
         "## Checks\n1. verified\n\n## Screenshots\n- a.png — ok")


def answered(answer):
    return ("## Must fix\nnone\n\n## Nits\nnone\n\n## Design findings\nnone\n\n## Design answers\n"
            f"{answer}\n\n## Checks\n1. verified\n\n## Screenshots\n- a.png — ok")


def test_a_passing_screenshot_review_with_a_design_finding_still_goes_to_the_fixer(loop):
    loop.reviews, loop.screens = ["pass", "pass"], ["pass", "pass"]
    loop.screen_texts = [FOUND, answered("D1.1. resolved — now 18 px")]
    assert screen_run(loop) == 0
    assert len(loop.fixes) == 1
    assert "D1.1. checks The library at 390 px and 1280 px. — a.css: bottom 24 px" in loop.fixes[0]["feedback"]
    assert "D1.1. checks The library" in loop.agents[3]["prompt"]   # round 2's screenshot prompt
    [given] = loop.merge_screenshots
    assert given["design"] == [] and not given["design_unusable"] and loop.merged == [PR]
    assert not loop.labelled("ready-for-human")


def test_a_held_finding_stays_open_and_blocks_its_check_at_the_merge(loop):
    loop.reviews, loop.screens = ["pass", "pass"], ["pass", "pass"]
    loop.screen_texts = [FOUND, answered("D1.1. held — still 24 px")]
    assert screen_run(loop) == 0
    [given] = loop.merge_screenshots
    assert [item["id"] for item in given["design"]] == ["D1.1"]


def test_the_last_round_with_only_a_design_finding_is_not_a_review_limit(loop):
    loop.reviews, loop.screens = ["pass"], ["pass"]
    loop.screen_texts = [FOUND]
    assert screen_run(loop, MAX_REVIEW_ROUNDS="1") == 0
    assert not loop.fixes and loop.merged == [PR] and not loop.labelled("ready-for-human")
    [given] = loop.merge_screenshots
    assert given["design"][0]["checks"] == ["The library at 390 px and 1280 px."]


def test_a_screenshot_review_without_a_design_findings_section_has_no_usable_verdicts(loop):
    loop.reviews, loop.screens = ["pass"], ["pass"]
    loop.screen_texts = ["## Must fix\nnone\n\n## Checks\n1. verified"]
    assert screen_run(loop) == 0
    [given] = loop.merge_screenshots
    assert given["design_unusable"] and given["design"] == []


def test_open_findings_survive_a_deferred_resume(loop):
    loop.reviews, loop.screens = ["pass", "pass"], ["pass", "pass"]
    loop.screen_texts = [FOUND, answered("D1.1. resolved — ok")]
    loop.fix_plans = [75]
    with pytest.raises(Deferred):
        screen_run(loop)
    assert "D1.1" in loop.checkpoint()["design_open"]
    assert screen_run(loop) == 0
    assert len(loop.fixes) == 2 and "D1.1. checks" in loop.fixes[1]["feedback"]
    assert loop.merge_screenshots[0]["design"] == []


def test_the_screenshot_and_fix_prompts_carry_the_design_protocol(loop):
    loop.reviews, loop.screens = ["pass"], ["pass"]
    screen_run(loop)
    prompt = loop.agents[1]["prompt"]
    for text in ("UI/UX designer", "device scale factor", "concrete change", "## Design findings", "## Design answers"):
        assert text in prompt
    for name in ("fix", "codex-fix", "cursor-fix"):
        rendered = review.prompts.render(BIN / f"{name}-prompt.md", [], env=session.resolve(loop.env))
        assert "## Design findings" in rendered and "rejected — why" in rendered
