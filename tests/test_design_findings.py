from baton import design_findings


def test_an_empty_or_punctuation_only_design_findings_section_has_no_usable_verdicts():
    assert design_findings.parse("## Design findings", 1, 1) is None
    assert design_findings.parse("## Design findings\n\n## Checks\n1. verified", 1, 1) is None
    assert design_findings.parse("## Design findings\n---\n\n## Checks", 1, 1) is None


def test_none_is_a_usable_empty_verdict():
    assert design_findings.parse("## Design findings\nnone\n", 1, 1) == []
    assert design_findings.parse("## Design findings\n- none.\n", 1, 1) == []
