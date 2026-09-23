from pathlib import Path

from workup.checks import run_prechecks
from workup.docs import load_case_documents, normalize
from workup.pipeline import DOCS_DIR, load_cases
from workup.rules import get_rule
from workup.schema import Action, EvidencePointer, RequirementAssessment, Status, Workup, validate_pointers

CASES = load_cases()


def test_prechecks_case_2_postcode_mismatch():
    c = CASES["CB-2025-0002"]
    pre = run_prechecks(c, get_rule(c.scheme, c.reason_code))
    assert pre.postcode_match is False
    assert any("differs" in f for f in pre.flags)


def test_prechecks_case_10_forced_accept():
    c = CASES["CB-2025-0010"]
    pre = run_prechecks(c, get_rule(c.scheme, c.reason_code))
    assert pre.representable is False
    assert pre.forced_action == "accept_liability"


def test_prechecks_case_4_failed_signals():
    c = CASES["CB-2025-0004"]
    pre = run_prechecks(c, get_rule(c.scheme, c.reason_code))
    assert pre.postcode_match is None  # digital goods, no shipping postcode
    assert any("AVS and CVV failed" in f for f in pre.flags)
    assert any("3DS was not attempted" in f for f in pre.flags)


def test_documents_load_with_text_and_images():
    c = CASES["CB-2025-0007"]
    docs = load_case_documents(c.merchant_evidence_documents, DOCS_DIR)
    manifest = docs[0]
    assert manifest.kind == "pdf" and manifest.page_count == 10
    assert "txn_7745MN" in manifest.page_text(8)

    c2 = CASES["CB-2025-0002"]
    docs2 = load_case_documents(c2.merchant_evidence_documents, DOCS_DIR)
    kinds = {d.name: d.kind for d in docs2}
    assert kinds["CB-2025-0002_tracking_screenshot.png"] == "image"
    assert kinds["CB-2025-0002_order_confirmation.pdf"] == "pdf"


def test_normalize_unifies_dashes_and_spacing():
    assert normalize("Royal Mail — Tracking   &\nDelivery") == "royal mail - tracking & delivery"


def _workup(reqs, action=Action.represent, ask=None):
    return Workup(
        reason_code_summary="s", requirements=reqs, rationale="r", recommended_action=action,
        action_justification="j", evidence_to_request=ask or [], caveats=[], model_confidence="high",
    )


def test_validate_pointers_rejects_unknown_document_and_bad_page():
    docs = {"a.pdf": 2}
    w = _workup([
        RequirementAssessment(requirement_id=1, status=Status.satisfied,
                              pointers=[EvidencePointer(document="ghost.pdf", page=1, quote="x")], reasoning=""),
        RequirementAssessment(requirement_id=2, status=Status.partial,
                              pointers=[EvidencePointer(document="a.pdf", page=5, quote="x")], reasoning=""),
    ])
    problems = validate_pointers(w, docs, n_requirements=2)
    assert any("unknown document" in p for p in problems)
    assert any("page 5" in p for p in problems)


def test_validate_pointers_requires_pointer_when_satisfied():
    w = _workup([RequirementAssessment(requirement_id=1, status=Status.satisfied, pointers=[], reasoning="")])
    assert any("no pointers" in p for p in validate_pointers(w, {}, n_requirements=1))


def test_validate_pointers_accepts_clean_workup():
    w = _workup([
        RequirementAssessment(requirement_id=1, status=Status.missing, pointers=[], reasoning="none"),
    ], action=Action.request_more_evidence, ask=["tracking"])
    assert validate_pointers(w, {}, n_requirements=1) == []


def test_validate_pointers_not_applicable_must_have_no_pointer():
    w = _workup([
        RequirementAssessment(requirement_id=1, status=Status.not_applicable,
                              pointers=[EvidencePointer(document="a.pdf", page=1, quote="x")], reasoning="n/a"),
    ])
    assert any("not_applicable but has pointers" in p for p in validate_pointers(w, {"a.pdf": 1}, n_requirements=1))


def test_compare_script_runs_from_any_cwd(tmp_path):
    import subprocess, sys
    script = Path(__file__).parent.parent / "scripts" / "compare.py"
    out = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "action agreement" in out.stdout
