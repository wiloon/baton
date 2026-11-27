"""The design exchange across two or three rounds of Review.loop (#321; ADR 0005 Consequences and
ADR 0006), with the doubles of tests/test_review.py and the helpers of the other design tests."""
import pytest

from baton import merge, review
from baton.common import Deferred
from test_review import CHECKS_BODY, loop, screen_run   # noqa: F401  (loop is a fixture)
from test_review_answers import REJECT
from test_review_design import FOUND, answered

CHECK = "The library at 390 px and 1280 px."
NO_CHECKS_BODY = "Refs #297\n\n## Checks to verify\nnone\n"
D11 = f"D1.1. checks {CHECK} — a.css: bottom 24 px vs top 18 px; unequal optical gap; cut bottom padding to 18 px"
D12 = f"D1.2. checks {CHECK} — c.css: label 2 px low; baseline off; align to the icon"
D21 = f"D2.1. checks {CHECK} — b.css: gap 4 px; crowded; use 8 px"
TWO_FOUND = FOUND.replace("\n\n## Checks", "\n2. checks 1 — c.css: label 2 px low; baseline off; "
                                          "align to the icon\n\n## Checks")
NEW_FOUND = answered("").replace("## Design findings\nnone",
                                 "## Design findings\n1. checks 1 — b.css: gap 4 px; crowded; use 8 px")
WHY = "24 px is the brand spacing"
HELD = answered("D1.1. held — still 24 px")


def reply(*lines):
    return "## Must fix\nnone\n\n## Design findings\n" + "\n".join(lines) + "\n", None


def rounds(loop, screens, code=("pass", "pass", "pass")):
    loop.reviews, loop.screens = list(code), ["pass"] * len(screens)
    loop.screen_texts = list(screens)


def run(loop, defer=False):
    """Three rounds at most; with DEFER the round-1 fix defers and a second run resumes."""
    if defer:
        loop.fix_plans = [75]
        with pytest.raises(Deferred):
            screen_run(loop, MAX_REVIEW_ROUNDS="3")
        assert loop.checkpoint()["design_open"]
    assert screen_run(loop, MAX_REVIEW_ROUNDS="3") == 0


def shot_prompts(loop):
    return [a["prompt"] for a in loop.agents if a["role"] == "screenshot_review"]


def feedback(loop, round):
    [fix] = [f for f in loop.fixes if str(f["round"]) == str(round)]
    return fix["feedback"]


def merged(loop):
    """(the dict the merge step got, what the real gate leaves to the owner on it)."""
    [given] = loop.merge_screenshots
    return given, merge.Merger.left_for_owner(None, given, CHECKS_BODY)


# --- an incomplete exchange ------------------------------------------------------------------

@pytest.mark.parametrize("defer", [False, True], ids=["straight", "deferred"])
@pytest.mark.parametrize("answers", ["", "D1.1. resolved — ok\nD1.1. resolved — ok", "D9.9. resolved — ok"],
                         ids=["no-line", "twice", "unknown-id"])
def test_a_finding_without_one_valid_judgment_stays_open_in_later_rounds(loop, answers, defer):
    rounds(loop, [FOUND, answered(answers), answered("")])
    run(loop, defer)
    assert f"## Open design findings\n{D11}" in feedback(loop, 2)
    assert D11 in shot_prompts(loop)[2]
    given, left = merged(loop)
    assert [item["id"] for item in given["design"]] == ["D1.1"] and not given["design_unusable"]
    assert "1. verified" in given["comment"] and left == [CHECK]


@pytest.mark.parametrize("defer", [False, True], ids=["straight", "deferred"])
def test_a_later_finding_with_the_same_number_is_not_closed_with_the_earlier_one(loop, defer):
    rounds(loop, [FOUND, NEW_FOUND, answered("D1.1. resolved — now 18 px")])
    run(loop, defer)
    assert D11 in shot_prompts(loop)[2] and D21 in shot_prompts(loop)[2]
    given, left = merged(loop)
    assert [item["id"] for item in given["design"]] == ["D2.1"]
    assert given["design"][0]["checks"] == [CHECK] and left == [CHECK]


def test_a_round_without_screenshot_review_still_gives_its_fixer_the_open_findings(loop):
    rounds(loop, [FOUND, answered("D1.1. resolved — now 18 px")])
    loop.fix_bodies = [NO_CHECKS_BODY, CHECKS_BODY]   # round 2 has no checks, round 3 has them again
    run(loop)
    assert [a["role"] for a in loop.agents] == ["review", "screenshot_review", "review", "review",
                                               "screenshot_review"]
    assert f"## Open design findings\n{D11}" in feedback(loop, 2)
    assert D11 in shot_prompts(loop)[1]
    given, left = merged(loop)
    assert given["design"] == [] and left == []


def test_a_disagreement_survives_a_later_review_that_omits_it(loop):
    rounds(loop, [FOUND, HELD, answered("")], code=("pass", "changes", "pass"))
    loop.fix_replies = [(REJECT, None)]
    run(loop)
    assert len(loop.fixes) == 2
    given, left = merged(loop)
    [item] = given["design"]
    assert item["id"] == "D1.1" and item["disagree"] == {"reviewer": "still 24 px", "fixer": WHY}
    assert left == [CHECK]


# --- only the current answer counts ----------------------------------------------------------

def test_an_earlier_rounds_rejection_makes_no_disagreement(loop):
    rounds(loop, [FOUND, answered(""), HELD])
    loop.fix_replies = [(REJECT, None)]
    run(loop)
    given, left = merged(loop)
    [item] = given["design"]
    assert item["id"] == "D1.1" and "disagree" not in item
    assert "1. verified" in given["comment"] and left == [CHECK]


@pytest.mark.parametrize("verdict, still_open", [("held — still 24 px", ["D1.1"]), ("resolved — ok", [])],
                         ids=["held", "resolved"])
@pytest.mark.parametrize("lines", [("D1.1. adopted — cut to 18 px", "D1.1. rejected — brand"),
                                   ("D1.1. rejected — brand", "D1.1. rejected — brand")],
                         ids=["conflicting", "same-line"])
def test_an_answer_given_twice_after_an_earlier_one_is_unanswered(loop, lines, verdict, still_open):
    rounds(loop, [FOUND, answered(""), answered(f"D1.1. {verdict}")])
    loop.fix_replies = [(REJECT, None), reply(*lines)]
    run(loop)
    assert len(loop.fixes) == 2 and len(loop.fix_replies) == 0
    given, left = merged(loop)
    assert [item["id"] for item in given["design"]] == still_open
    assert not any(item.get("disagree") for item in given["design"])
    assert left == ([CHECK] if still_open else [])


def test_a_rejection_repeated_in_the_current_window_makes_a_disagreement(loop):
    rounds(loop, [FOUND, answered(""), HELD])
    loop.fix_replies = [(REJECT, None), reply("D1.1. rejected — the design system fixes it")]
    run(loop)
    given, left = merged(loop)
    [item] = given["design"]
    assert item["disagree"] == {"reviewer": "still 24 px", "fixer": "the design system fixes it"}
    assert left == [CHECK]


def test_answers_on_both_sides_of_a_round_without_screenshot_review_are_shown(loop):
    rounds(loop, [TWO_FOUND, answered("")])
    loop.fix_bodies = [NO_CHECKS_BODY, CHECKS_BODY]
    loop.fix_replies = [reply(f"D1.1. rejected — {WHY}"), reply("D1.2. adopted — aligned")]
    run(loop)
    assert len(shot_prompts(loop)) == 2
    assert f"{D11} [fixer answer: rejected — {WHY}]" in shot_prompts(loop)[1]
    assert f"{D12} [fixer answer: adopted — aligned]" in shot_prompts(loop)[1]


def test_an_id_answered_on_both_sides_of_a_round_without_screenshot_review_is_unanswered(loop):
    rounds(loop, [FOUND, HELD])
    loop.fix_bodies = [NO_CHECKS_BODY, CHECKS_BODY]
    loop.fix_replies = [(REJECT, None), (REJECT, None)]
    run(loop)
    assert len(shot_prompts(loop)) == 2 and len(loop.fix_replies) == 0
    assert f"{D11}\n" in shot_prompts(loop)[1] and WHY not in shot_prompts(loop)[1]
    given, left = merged(loop)
    [item] = given["design"]
    assert "disagree" not in item and "answer" not in item and left == [CHECK]


def test_a_fix_comment_without_a_loop_id_is_neither_shown_nor_a_disagreement(loop):
    rounds(loop, [FOUND, HELD], code=("pass", "pass"))
    loop.fix_replies = [(REJECT, "")]
    assert screen_run(loop) == 0
    posted = [c["body"] for c in loop.gh.pr["comments"] if c["body"].startswith("<!-- baton-fix ")]
    assert len(posted) == 1 and "loop=" not in posted[0] and WHY in posted[0]
    assert D11 in shot_prompts(loop)[1] and WHY not in shot_prompts(loop)[1]
    given, left = merged(loop)
    [item] = given["design"]
    assert "disagree" not in item and "answer" not in item and left == [CHECK]


def test_a_resume_after_the_posted_reply_keeps_the_loop_id_and_shows_the_answer(loop):
    loop.reviews, loop.screens = ["pass", "pass"], ["pass", "defer", "pass"]
    loop.screen_texts = [FOUND, HELD]
    loop.fix_replies = [(REJECT, None)]
    with pytest.raises(Deferred):
        screen_run(loop, MAX_REVIEW_ROUNDS="3")
    loop_id = loop.checkpoint()["loop_id"]
    [posted] = [c["body"] for c in loop.gh.pr["comments"] if c["body"].startswith("<!-- baton-fix ")]
    assert loop_id and f"loop={loop_id} round=1 " in posted
    assert screen_run(loop, MAX_REVIEW_ROUNDS="3") == 0
    assert {f["loop_id"] for f in loop.fixes} == {loop_id}
    assert f"{D11} [fixer answer: rejected — {WHY}]" in shot_prompts(loop)[-1]
    given, _ = merged(loop)
    assert given["design"][0]["disagree"] == {"reviewer": "still 24 px", "fixer": WHY}


# --- the merge binds the screenshot comment to this run --------------------------------------

def test_the_merge_read_of_an_earlier_loops_comment_gives_no_usable_verdicts(loop, monkeypatch):
    real, reads = review.agent_comment, []

    def stale_at_merge(repo, owner, pr, marker, round):
        if marker != "baton-screenshot-review":
            return real(repo, owner, pr, marker, round)
        reads.append(round)
        if len(reads) == 1:   # design_round reads this run's own comment
            return real(repo, owner, pr, marker, round)
        return answered("D1.1. resolved — an earlier loop's verdict")

    monkeypatch.setattr(review, "agent_comment", stale_at_merge)
    rounds(loop, [FOUND], code=("pass",))
    assert screen_run(loop, MAX_REVIEW_ROUNDS="1") == 0
    assert reads == [1, 1] and not loop.fixes
    given, left = merged(loop)
    assert given["comment"] == "" and given["design_unusable"] is False and left is None
    assert [item["id"] for item in given["design"]] == ["D1.1"]
