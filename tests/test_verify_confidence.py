import json
from pathlib import Path

import pytest

from workup.checks import run_prechecks
from workup.confidence import assess, coverage
from workup.docs import Document, Page, load_case_documents
from workup.pipeline import ARTIFACTS, DOCS_DIR, load_cases
from workup.rules import get_rule
from workup.schema import Action, EvidencePointer, RequirementAssessment, Status, Workup
from workup.verify import quote_in_text, rationale_conflicts, verify_workup

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
    docs = [_doc("a.pdf", "tracking shows delivered to the cardholder on 3 March")]
    ptr = [EvidencePointer(document="a.pdf", page=1, quote="delivered to the cardholder")]
    w = _workup([_req(1, Status.satisfied, ptr), _req(2, Status.not_applicable), _req(3, Status.satisfied, ptr),
                 _req(4, Status.satisfied, ptr)])
    met, sat, req, applicable = coverage(rule, verify_workup(w, docs))
    assert met and sat == 3 and req == 3 and applicable == 3


def test_any_two_rule_met_with_two_satisfied():
    rule = get_rule("mastercard", "4837")
    docs = [_doc("a.pdf", "AVS check (full address) Y Full match; CVV M; 3DS authenticated")]
    ptr = [EvidencePointer(document="a.pdf", page=1, quote="AVS check (full address) Y Full match")]
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


def test_rationale_conflict_flags_identifiers_but_ignores_plain_hyphenated_words():
    w = _workup([_req(1, Status.missing)], action=Action.request_more_evidence,
                ask=["The POD image POD-9051-img", "the pre-renewal notice"])
    w.rationale = "The signed proof POD-9051-img confirms delivery under the non-refundable pre-renewal terms."
    conflicts = rationale_conflicts(w)
    assert len(conflicts) == 1 and "pod-9051-img" in conflicts[0]


def test_rationale_conflict_silent_when_nothing_is_requested():
    w = _workup([_req(1, Status.satisfied, [EvidencePointer(document="a.pdf", page=1, quote="q")])])
    w.rationale = "POD-9051-img confirms delivery."
    assert rationale_conflicts(w) == []


def test_request_more_evidence_with_most_of_the_file_missing_is_medium():
    """Case 8 shape: 1 of 4 satisfied, three missing outright. Asking is unlikely to close that gap."""
    c = CASES["CB-2025-0008"]
    rule = get_rule(c.scheme, c.reason_code)
    pre = run_prechecks(c, rule)
    docs = [_doc("terms.pdf", "subscriptions auto-renew monthly")]
    ptr = [EvidencePointer(document="terms.pdf", page=1, quote="subscriptions auto-renew monthly")]
    w = _workup([_req(1, Status.satisfied, ptr), _req(2, Status.missing), _req(3, Status.missing),
                 _req(4, Status.missing)], action=Action.request_more_evidence, ask=["logs"])
    a = assess(rule, pre, w, verify_workup(w, docs))
    assert a.tier == "medium"
    assert any("most of the file" in r for r in a.reasons)


# --------------------------------------------------- robustness to a bad model answer


def test_satisfied_without_any_pointer_is_downgraded():
    w = _workup([_req(1, Status.satisfied, [])])
    v = verify_workup(w, [])[0]
    assert v.effective_status is Status.partial and v.downgraded
    assert "no evidence pointer" in v.note


def test_subset_of_requirements_counts_missing_ones_and_flags_it():
    """Visa 13.1 needs all four; the model returned only requirement 1."""
    rule = get_rule("visa", "13.1")
    docs = [_doc("a.pdf", "tracking RM1 delivered to SW4 7QR")]
    ptr = [EvidencePointer(document="a.pdf", page=1, quote="delivered to SW4 7QR")]
    w = _workup([_req(1, Status.satisfied, ptr)])
    met, sat, req, _ = coverage(rule, verify_workup(w, docs))
    assert not met and sat == 1 and req == 4
    c = CASES["CB-2025-0001"]
    a = assess(rule, run_prechecks(c, rule), w, verify_workup(w, docs))
    assert a.tier == "needs_review"
    assert any("not assessed by the model" in r for r in a.reasons)


def test_unknown_requirement_id_is_ignored_not_counted():
    rule = get_rule("mastercard", "4837")
    docs = [_doc("a.pdf", "AVS check (full address) Y Full match")]
    ptr = [EvidencePointer(document="a.pdf", page=1, quote="AVS check (full address) Y Full match")]
    w = _workup([_req(1, Status.satisfied, ptr), _req(9, Status.satisfied, ptr),
                 _req(2, Status.missing), _req(3, Status.missing), _req(4, Status.missing)])
    met, sat, req, _ = coverage(rule, verify_workup(w, docs))
    assert not met and sat == 1 and req == 2


def test_leftover_validation_problems_force_needs_review():
    c = CASES["CB-2025-0003"]
    rule = get_rule(c.scheme, c.reason_code)
    w = _workup([_req(i, Status.missing) for i in range(1, 5)], action=Action.accept_liability)
    a = assess(rule, run_prechecks(c, rule), w, verify_workup(w, []),
               validation_problems=["requirement 1 points to unknown document 'ghost.pdf'"])
    assert a.tier == "needs_review" and any("structural validation" in r for r in a.reasons)


def test_amount_mismatch_escalates_a_represent():
    c = CASES["CB-2025-0001"].model_copy(deep=True)
    c.chargeback_amount.value = 999.0
    rule = get_rule(c.scheme, c.reason_code)
    pre = run_prechecks(c, rule)
    assert pre.amount_mismatch
    docs = load_case_documents(c.merchant_evidence_documents, DOCS_DIR)
    ptr = [EvidencePointer(document="CB-2025-0001_delivery_confirmation.pdf", page=1, quote="Tracking number: RM98421144GB")]
    w = _workup([_req(1, Status.satisfied, ptr), _req(2, Status.not_applicable), _req(3, Status.satisfied, ptr),
                 _req(4, Status.satisfied, ptr)])
    a = assess(rule, pre, w, verify_workup(w, docs))
    assert a.tier == "needs_review" and any("amount differs" in r for r in a.reasons)


def test_quote_needs_minimum_substance():
    page = "Delivery failed. ECI 02. cannot confirm. Consignment TF-9051 collected."
    assert not quote_in_text("a", page)
    assert not quote_in_text("not", page)
    assert quote_in_text("ECI 02", page)                 # two words with a digit: a code, not a fragment
    assert not quote_in_text("Delivery failed", page)    # two plain words: could be anywhere
    assert quote_in_text("Delivery failed. ECI 02", page)
    assert quote_in_text("TF-9051", page)                # an identifier is substance on its own


def test_identifier_regex_skips_plain_words_and_pure_numbers_and_ignores_case_ids():
    w = _workup([_req(1, Status.missing)], action=Action.request_more_evidence,
                ask=["POD image POD-9051-img for txn_7745MN", "pre-renewal notice dated 2025-04-20", "INV48213"])
    w.rationale = "Non-refundable pre-renewal terms; POD-9051-img and INV48213 confirm; txn_7745MN; 2025-04-20."
    found = rationale_conflicts(w, ignore={"txn_7745MN"})
    idents = [f.split("'")[1] for f in found]
    assert "pod-9051-img" in idents and "inv48213" in idents
    assert "txn_7745mn" not in idents          # case record id, always legitimate to mention
    assert not any("refundable" in i or "renewal" in i for i in idents)
    assert "2025-04-20" not in idents          # dates are not evidence identifiers
