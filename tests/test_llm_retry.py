"""The API layer against a fake client: no network, no key."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from workup.checks import run_prechecks
from workup.docs import load_case_documents
from workup.llm import request_workup
from workup.pipeline import DOCS_DIR, load_cases
from workup.rules import get_rule
from workup.schema import Action, EvidencePointer, RequirementAssessment, Status, Workup


def _good_workup(rule):
    reqs = [RequirementAssessment(requirement_id=r.id, status=Status.missing, pointers=[], reasoning="none")
            for r in rule.requirements]
    return Workup(reason_code_summary="s", requirements=reqs, recommended_action=Action.accept_liability,
                  action_justification="j", evidence_to_request=[], rationale="r", caveats=[],
                  model_confidence="high")


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)  # each item: Exception to raise, or a Workup to return
        self.calls = 0

    def parse(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(stop_reason="end_turn", parsed_output=item,
                               usage=SimpleNamespace(model_dump=lambda: {"input_tokens": 1, "output_tokens": 1}),
                               model_dump=lambda **k: {"model": "fake"})


def _bad_json_error():
    try:
        Workup.model_validate_json('{"reason_code_summary": "***')
    except ValidationError as e:
        return e


def _setup(case_id="CB-2025-0004"):
    case = load_cases()[case_id]
    rule = get_rule(case.scheme, case.reason_code)
    return case, rule, run_prechecks(case, rule), load_case_documents(case.merchant_evidence_documents, DOCS_DIR)


def test_unparseable_answer_is_retried_once_then_succeeds():
    case, rule, pre, docs = _setup()
    fake = SimpleNamespace(messages=FakeMessages([_bad_json_error(), _good_workup(rule)]))
    res = request_workup(case, rule, pre, docs, client=fake)
    assert fake.messages.calls == 2
    assert res.attempts == 2 and res.validation_problems == []
    assert res.problems_per_attempt[0][0].startswith("unparseable answer")


def test_unparseable_twice_raises_and_never_returns():
    case, rule, pre, docs = _setup()
    fake = SimpleNamespace(messages=FakeMessages([_bad_json_error(), _bad_json_error()]))
    with pytest.raises(RuntimeError, match="unparseable answer twice"):
        request_workup(case, rule, pre, docs, client=fake)


def test_structural_problem_gets_one_corrective_round_trip():
    case, rule, pre, docs = _setup()
    bad = _good_workup(rule)
    bad.requirements[0].status = Status.satisfied  # satisfied with no pointer: fails validate_pointers
    fake = SimpleNamespace(messages=FakeMessages([bad, _good_workup(rule)]))
    res = request_workup(case, rule, pre, docs, client=fake)
    assert fake.messages.calls == 2 and res.validation_problems == []
    assert any("no pointers" in p for p in res.problems_per_attempt[0])
