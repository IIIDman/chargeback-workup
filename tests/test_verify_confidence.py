import json
from pathlib import Path

import pytest

from workup.checks import run_prechecks
from workup.confidence import assess, coverage
from workup.docs import Document, Page, load_case_documents
from workup.pipeline import ARTIFACTS, DOCS_DIR, load_cases
from workup.rules import get_rule
from workup.schema import Action, EvidencePointer, RequirementAssessment, Status, Workup
from workup.verify import quote_in_text, verify_workup

CASES = load_cases()


def _doc(name, text, kind="pdf"):
    if kind == "image":
        return Document(name=name, kind="image", image_b64="x", media_type="image/png")
    return Document(name=name, kind="pdf", pages=[Page(1, text)])


def _workup(reqs, action=Action.represent, conf="high", ask=None):
    return Workup(reason_code_summary="s", requirements=reqs, rationale="r", recommended_action=action,
                  action_justification="j", evidence_to_request=ask or [], caveats=[], model_confidence=conf)


def _req(i, status, pointers=None):
    return RequirementAssessment(requirement_id=i, status=status, pointers=pointers or [], reasoning="")


# ------------------------------------------------------------------------------- verify


def test_quote_matching_tolerates_dashes_whitespace_and_punctuation():
    page = "DELIVERED — signed by WHITFORD\n24 Mar 2025 11:47"
    assert quote_in_text("DELIVERED - signed by WHITFORD 24 Mar 2025 11:47", page)
    assert quote_in_text("delivered signed by whitford", page)  # punctuation-stripped fallback
    assert not quote_in_text("signed by SMITH", page)
    assert not quote_in_text("", page)


def test_satisfied_without_verified_quote_is_downgraded():
    docs = [_doc("a.pdf", "the parcel was delivered on 3 May")]
    w = _workup([_req(1, Status.satisfied, [EvidencePointer(document="a.pdf", page=1, quote="signed for on 3 May")])])
    v = verify_workup(w, docs)[0]
    assert v.pointers[0].verified is False
    assert v.effective_status is Status.partial and v.downgraded


def test_image_pointer_is_unverifiable_not_downgraded():
    docs = [_doc("shot.png", "", kind="image")]
    w = _workup([_req(1, Status.satisfied, [EvidencePointer(document="shot.png", page=1, quote="DELIVERED")])])
    v = verify_workup(w, docs)[0]
    assert v.pointers[0].verified is None
    assert v.effective_status is Status.satisfied and not v.downgraded and v.image_only


# --------------------------------------------------------------------------- confidence


def test_coverage_logic_all_treats_not_applicable_as_met():
    rule = get_rule("visa", "13.1")
    docs = [_doc("a.pdf", "x y z")]
    ptr = [EvidencePointer(document="a.pdf", page=1, quote="x y")]
    w = _workup([_req(1, Status.satisfied, ptr), _req(2, Status.not_applicable), _req(3, Status.satisfied, ptr),
                 _req(4, Status.satisfied, ptr)])
    met, sat, req, applicable = coverage(rule, verify_workup(w, docs))
    assert met and sat == 3 and req == 3 and applicable == 3


def test_any_two_rule_met_with_two_satisfied():
    rule = get_rule("mastercard", "4837")
    docs = [_doc("a.pdf", "avs y cvv m 3ds authenticated")]
    ptr = [EvidencePointer(document="a.pdf", page=1, quote="avs y")]
    w = _workup([_req(1, Status.satisfied, ptr), _req(2, Status.satisfied, ptr), _req(3, Status.missing),
                 _req(4, Status.missing)])
    met, sat, req, _ = coverage(rule, verify_workup(w, docs))
    assert met and sat == 2 and req == 2


def test_represent_without_rule_met_is_needs_review():
    c = CASES["CB-2025-0004"]
    rule = get_rule(c.scheme, c.reason_code)
    pre = run_prechecks(c, rule)
    w = _workup([_req(i, Status.missing) for i in range(1, 5)], action=Action.represent)
    a = assess(rule, pre, w, verify_workup(w, []))
    assert a.tier == "needs_review"
    assert any("rule is not met" in r for r in a.reasons)
    assert any("AVS and CVV" in r for r in a.reasons)


def test_non_representable_forces_accept_and_overrides_model():
    c = CASES["CB-2025-0010"]
    rule = get_rule(c.scheme, c.reason_code)
    pre = run_prechecks(c, rule)
    w = _workup([], action=Action.represent)
    a = assess(rule, pre, w, [])
    assert a.final_action is Action.accept_liability and a.action_overridden
    assert a.tier == "medium"


def test_prechecks_do_not_warn_when_they_support_the_recommendation():
    """Case 4: AVS/CVV both failed. That is why accept_liability is right, so it must not raise the tier."""
    c = CASES["CB-2025-0004"]
    rule = get_rule(c.scheme, c.reason_code)
    pre = run_prechecks(c, rule)
    w = _workup([_req(i, Status.missing) for i in range(1, 5)], action=Action.accept_liability, conf="medium")
    a = assess(rule, pre, w, verify_workup(w, []))
    assert a.tier == "high"
    assert not any("AVS" in r for r in a.reasons)


def test_accept_liability_with_partial_requirements_needs_review():
    """Conceding a case that partly works costs the merchant money: the analyst should look."""
    c = CASES["CB-2025-0002"]
    rule = get_rule(c.scheme, c.reason_code)
    pre = run_prechecks(c, rule)
    docs = [_doc("a.pdf", "delivered to M1 7DR")]
    ptr = [EvidencePointer(document="a.pdf", page=1, quote="delivered to M1 7DR")]
    w = _workup([_req(1, Status.partial, ptr), _req(2, Status.not_applicable), _req(3, Status.satisfied, ptr),
                 _req(4, Status.missing)], action=Action.accept_liability)
    a = assess(rule, pre, w, verify_workup(w, docs))
    assert a.tier == "needs_review"
    assert any("although requirement" in r for r in a.reasons)


def test_image_only_support_forces_needs_review():
    c = CASES["CB-2025-0006"]
    rule = get_rule(c.scheme, c.reason_code)
    pre = run_prechecks(c, rule)
    docs = [_doc("photo.png", "", kind="image")]
    ptr = [EvidencePointer(document="photo.png", page=1, quote="delivered 18 Apr")]
    w = _workup([_req(1, Status.satisfied, ptr), _req(2, Status.partial, ptr), _req(3, Status.missing)],
                action=Action.request_more_evidence, ask=["correspondence"])
    a = assess(rule, pre, w, verify_workup(w, docs))
    assert a.tier == "needs_review"
    assert any("image only" in r for r in a.reasons)


def test_clean_case_is_high():
    c = CASES["CB-2025-0003"]
    rule = get_rule(c.scheme, c.reason_code)
    pre = run_prechecks(c, rule)
    docs = load_case_documents(c.merchant_evidence_documents, DOCS_DIR)
    ptr = [EvidencePointer(document="CB-2025-0003_avs_cvv_log.pdf", page=1, quote="AVS check (full address) Y Full match")]
    w = _workup([_req(1, Status.satisfied, ptr), _req(2, Status.satisfied, ptr), _req(3, Status.missing),
                 _req(4, Status.missing)])
    a = assess(rule, pre, w, verify_workup(w, docs))
    assert a.tier == "high" and a.final_action is Action.represent


@pytest.mark.skipif(not (ARTIFACTS / "CB-2025-0001.json").exists(), reason="no cached workup")
def test_cached_case_1_end_to_end_offline():
    """Exercise verify + assess on the real cached model output without any API call."""
    c = CASES["CB-2025-0001"]
    rule = get_rule(c.scheme, c.reason_code)
    pre = run_prechecks(c, rule)
    docs = load_case_documents(c.merchant_evidence_documents, DOCS_DIR)
    w = Workup.model_validate(json.loads((ARTIFACTS / "CB-2025-0001.json").read_text())["workup"])
    v = verify_workup(w, docs)
    assert all(p.verified for rv in v for p in rv.pointers)
    a = assess(rule, pre, w, v)
    assert a.final_action is Action.represent
