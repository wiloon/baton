"""The fixer's answers to design findings, the loop id and disagreements (#96), through Review.loop
with the doubles of tests/test_review.py (the fix double posts its reply with the real fix marker)."""
import pytest

from baton import review
from baton.common import Deferred
from test_review import PR, loop, screen_run   # noqa: F401  (loop is a fixture)
from test_review_design import FOUND, answered

REJECT = "## Must fix\nnone\n\n## Design findings\nD1.1. rejected — 24 px is the brand spacing\n"


def test_every_fix_of_a_loop_carries_the_same_loop_id_across_a_deferred_resume(loop):
    loop.reviews, loop.screens = ["pass", "pass", "pass"], ["pass", "pass", "pass"]
    loop.screen_texts = [FOUND, answered("D1.1. held — still 24 px")]
    loop.fix_plans = [75]
    with pytest.raises(Deferred):
        screen_run(loop, MAX_REVIEW_ROUNDS="3")
    loop_id = loop.checkpoint()["loop_id"]
    assert loop_id and loop.fixes[0]["loop_id"] == loop_id
    assert screen_run(loop, MAX_REVIEW_ROUNDS="3") == 0
    assert {f["loop_id"] for f in loop.fixes} == {loop_id}


def test_a_design_only_fix_is_told_which_findings_it_must_answer(loop):
    loop.reviews, loop.screens = ["pass", "pass"], ["pass", "pass"]
    loop.screen_texts = [FOUND, answered("D1.1. resolved — ok")]
    assert screen_run(loop) == 0
    assert loop.fixes[0]["design_ids"] == ["D1.1"] and loop.fixes[0]["design_only"] is True


def test_a_code_must_fix_makes_the_fix_not_design_only(loop):
    loop.reviews, loop.screens = ["changes", "pass"], ["pass", "pass"]
    loop.screen_texts = [FOUND, answered("D1.1. resolved — ok")]
    assert screen_run(loop) == 0
    assert loop.fixes[0]["design_only"] is False


def test_the_fixers_rejection_reaches_the_next_prompt_and_a_held_one_is_a_disagreement(loop):
    loop.reviews, loop.screens = ["pass", "pass"], ["pass", "pass"]
    loop.screen_texts = [FOUND, answered("D1.1. held — still 24 px at 390 px")]
    loop.fix_replies = [(REJECT, None)]
    assert screen_run(loop) == 0
    assert "[fixer answer: rejected — 24 px is the brand spacing]" in loop.agents[3]["prompt"]
    [given] = loop.merge_screenshots
    [item] = given["design"]
    assert item["disagree"] == {"reviewer": "still 24 px at 390 px", "fixer": "24 px is the brand spacing"}


def test_a_rejection_the_reviewer_accepts_closes_the_finding(loop):
    loop.reviews, loop.screens = ["pass", "pass"], ["pass", "pass"]
    loop.screen_texts = [FOUND, answered("D1.1. resolved — the brand spacing is a fair reason")]
    loop.fix_replies = [(REJECT, None)]
    assert screen_run(loop) == 0
    assert loop.merge_screenshots[0]["design"] == []


def test_an_earlier_loops_rejection_is_neither_shown_nor_a_disagreement(loop):
    loop.reviews, loop.screens = ["pass", "pass"], ["pass", "pass"]
    loop.screen_texts = [FOUND, answered("D1.1. held — still 24 px")]
    loop.fix_replies = [(REJECT, "1700000000-old")]
    assert screen_run(loop) == 0
    assert "24 px is the brand spacing" not in loop.agents[3]["prompt"]
    [item] = loop.merge_screenshots[0]["design"]
    assert "disagree" not in item and "answer" not in item


def test_an_answer_given_twice_is_unanswered_and_a_held_finding_stays_open_without_disagreement(loop):
    loop.reviews, loop.screens = ["pass", "pass"], ["pass", "pass"]
    loop.screen_texts = [FOUND, answered("D1.1. held — still 24 px")]
    loop.fix_replies = [(REJECT + "D1.1. rejected — again\n", None)]
    assert screen_run(loop) == 0
    [item] = loop.merge_screenshots[0]["design"]
    assert "disagree" not in item and "answer" not in item


def test_only_a_disagreement_left_goes_to_the_merge_without_more_fix_rounds(loop):
    loop.reviews, loop.screens = ["pass"] * 3, ["pass"] * 3
    loop.screen_texts = [FOUND, answered("D1.1. held — still 24 px")]
    loop.fix_replies = [(REJECT, None)]
    assert screen_run(loop, MAX_REVIEW_ROUNDS="3") == 0
    assert len(loop.fixes) == 1 and loop.merged == [PR] and not loop.labelled("ready-for-human")
    assert loop.merge_screenshots[0]["design"][0]["disagree"]


def test_the_answer_window_takes_this_loops_replies_since_the_previous_screenshot_review():
    def comment(loop_id, number, text):
        return {"author": {"login": "o"}, "body": f"<!-- baton-fix by=codex loop={loop_id} round={number} -->\n{text}"}
    reply = "## Design findings\nD1.1. rejected — r\n"
    second = "## Design findings\nD2.1. adopted — a\n"
    comments = [comment("L", 1, reply), comment("L", 2, second), comment("X", 2, reply),
                {"author": {"login": "o"}, "body": f"<!-- baton-fix by=codex -->\n{reply}"},
                {"author": {"login": "stranger"}, "body": f"<!-- baton-fix by=codex loop=L round=2 -->\n{reply}"}]
    assert review.fix_replies(comments, "o", "L", 1, 3) == [reply, second]
    assert review.fix_replies(comments, "o", "L", 2, 3) == [second]
    assert review.fix_replies(comments, "o", "L", 1, 2) == [reply]
