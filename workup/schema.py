"""Data shapes: the input case and the structured workup the LLM must return.

The Workup model is passed to the API as the required output schema, so every field here is a
constraint on the model: enums instead of free text where a fixed vocabulary exists, and a pointer
(document + page + verbatim quote) whenever the model claims a requirement is satisfied or partial.
Runtime validation that needs case context (does this document exist in this case? does this page
exist in that document?) lives in `validate_pointers`, because a JSON schema cannot express it.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

# ------------------------------------------------------------------------------------ input case


class Amount(BaseModel):
    value: float
    currency: str


class Transaction(BaseModel):
    transaction_id: str
    merchant_name: str
    merchant_mcc: str
    transaction_date: str
    amount: Amount
    card_bin_country: str
    avs_result: str | None
    cvv_result: str | None
    three_ds_status: Literal["authenticated", "attempted", "frictionless", "not_attempted"]
    ip_address: str | None
    device_fingerprint: str | None
    billing_address_postcode: str | None
    shipping_address_postcode: str | None


class Case(BaseModel):
    case_id: str
    scheme: Literal["visa", "mastercard"]
    reason_code: str
    reason_code_label: str
    chargeback_date: str
    chargeback_amount: Amount
    transaction: Transaction
    issuer_narrative: str
    merchant_evidence_documents: list[str]


# ------------------------------------------------------------------------------- LLM output


class Status(str, Enum):
    satisfied = "satisfied"
    partial = "partial"
    missing = "missing"
    not_applicable = "not_applicable"  # the requirement does not apply to this transaction type


class Action(str, Enum):
    represent = "represent"
    accept_liability = "accept_liability"
    request_more_evidence = "request_more_evidence"


class EvidencePointer(BaseModel):
    document: str = Field(description="Exact filename of the evidence document, as listed in the case.")
    page: int = Field(description="1-based page number. Images have a single page: 1.")
    quote: str = Field(
        description="Verbatim excerpt copied from that page that supports the assessment. For images, "
        "transcribe the relevant visible text exactly."
    )


class RequirementAssessment(BaseModel):
    requirement_id: int = Field(description="Matches the requirement number in the rule.")
    status: Status
    pointers: list[EvidencePointer] = Field(
        description="Where the evidence is. Must be non-empty when status is satisfied or partial. "
        "Empty when missing or not_applicable."
    )
    reasoning: str = Field(description="One or two sentences: why this status, what is missing if partial.")


class Workup(BaseModel):
    """Field order is deliberate: it is the order the model writes the answer in. The rationale comes after
    the recommended action and the list of evidence still needed, so the filed text is written after the
    model has said what is missing.
    """

    reason_code_summary: str = Field(
        description="Plain-English restatement of what the issuer alleges and what the scheme requires to defend it."
    )
    requirements: list[RequirementAssessment] = Field(
        description="One entry per requirement in the rule, in order. Empty list if the code is non-representable."
    )
    recommended_action: Action
    action_justification: str = Field(description="One line.")
    evidence_to_request: list[str] = Field(
        description="Only when recommended_action is request_more_evidence: specific items to ask the merchant for."
    )
    rationale: str = Field(
        description="3-5 sentences for the file, consistent with the assessment above: it may not state as "
        "fact anything a requirement was marked partial or missing for, and may not cite a document listed "
        "in evidence_to_request. If the action is not represent, describe where the case stands."
    )
    caveats: list[str] = Field(
        description="Anything the analyst should double-check before acting: contradictions, dates, address "
        "mismatches, evidence that looks relevant but is not."
    )
    model_confidence: Literal["high", "medium", "low"] = Field(
        description="Your own confidence in the recommended action. Used as a minor signal only."
    )


# ------------------------------------------------------------------- context-aware validation


def validate_pointers(workup: Workup, case_docs: dict[str, int], n_requirements: int) -> list[str]:
    """Return a list of human-readable problems. Empty list means valid.

    case_docs: {filename: page_count} for the documents of this case.
    """
    problems: list[str] = []
    seen_ids = [r.requirement_id for r in workup.requirements]
    expected_ids = list(range(1, n_requirements + 1))
    if seen_ids != expected_ids:
        problems.append(f"requirements must be exactly ids {expected_ids} in order, got {seen_ids}")
    for r in workup.requirements:
        if r.status in (Status.satisfied, Status.partial) and not r.pointers:
            problems.append(f"requirement {r.requirement_id} is {r.status.value} but has no pointers")
        if r.status in (Status.missing, Status.not_applicable) and r.pointers:
            problems.append(f"requirement {r.requirement_id} is {r.status.value} but has pointers")
        for p in r.pointers:
            if p.document not in case_docs:
                problems.append(f"requirement {r.requirement_id} points to unknown document {p.document!r}")
                continue
            if not (1 <= p.page <= case_docs[p.document]):
                problems.append(
                    f"requirement {r.requirement_id} points to page {p.page} of {p.document}, "
                    f"which has {case_docs[p.document]} page(s)"
                )
            if not p.quote.strip():
                problems.append(f"requirement {r.requirement_id} has an empty quote for {p.document}")
    if workup.recommended_action is not Action.request_more_evidence and workup.evidence_to_request:
        problems.append("evidence_to_request must be empty unless recommended_action is request_more_evidence")
    if workup.recommended_action is Action.request_more_evidence and not workup.evidence_to_request:
        problems.append("request_more_evidence requires a non-empty evidence_to_request list")
    return problems
