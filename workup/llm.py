"""The single LLM call per case: build the prompt, request a structured Workup, validate, retry once.

Model: claude-opus-5-5 (default) with adaptive thinking. Structured output is enforced by passing the Pydantic
`Workup` class as the response format, so the model cannot return prose or an unknown status value.
Things a JSON schema cannot check (document names, page ranges) are validated after parsing; if they
fail, the call is repeated once with the problems appended so the model can correct itself.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

import anthropic

from .checks import PreChecks
from .docs import Document
from .rules import ReasonCode, rule_as_text
from .schema import Case, Workup, validate_pointers

# Override with WORKUP_MODEL in .env (e.g. claude-opus-5, claude-sonnet-5) to compare models.
MODEL = os.environ.get("WORKUP_MODEL", "claude-opus-5-5")

SYSTEM_PROMPT = """You are preparing a chargeback representment workup for a disputes analyst at a payment
acquirer. The analyst decides; you lay the case out so the decision takes seconds instead of minutes.

You receive: the scheme rule for this reason code (with its compelling-evidence requirements and how many
must be met), the case metadata, deterministic pre-checks computed from that metadata, the issuer's
narrative, and the merchant's evidence documents as page-by-page text (images are attached as images).

Rules for your assessment:
1. Assess each requirement of the rule, in order, one entry per requirement.
   - satisfied: the evidence clearly meets it. partial: something relevant exists but a specific element is
     missing or unproven. missing: nothing in the evidence addresses it. not_applicable: the requirement is
     conditional on something that is not the case here (e.g. a services-only requirement on a physical-goods
     order, a tip clause when no tip was added); say why in the reasoning and give no pointer.
   - Every satisfied or partial assessment needs at least one pointer: the exact document filename, the page,
     and a verbatim quote copied from that page. Never paraphrase inside a quote. If nothing quotable supports
     the status, the status is missing.
2. The pre-checks are facts from the transaction record. Use them: a failed AVS/CVV, a 3DS that was not
   attempted, or a shipping/billing postcode mismatch is not overridden by what a merchant document asserts.
3. Judge evidence by what it proves, not by how it sounds. A merchant's own report, risk score, policy
   document or confident conclusion is not compelling evidence unless it contains the specific facts the
   requirement asks for. Say so explicitly in the reasoning when a document looks relevant but does not meet
   the requirement.
4. A policy or terms document proves what the policy says, not that it was shown to, accepted by, or applied
   to this cardholder. Distinguish policy from proof.
5. Long documents may contain the relevant record on one page among many unrelated ones. Search all pages and
   point to the specific page.
6. If the rule is non-representable, set recommended_action to accept_liability, leave requirements empty,
   and explain briefly. Do not assess evidence.
7. recommended_action:
   - represent: the rule's logic is met (all / any two / any one) with satisfied requirements.
   - request_more_evidence: the merchant has a plausible case but a specific, obtainable item is missing or
     partial. List exactly what to ask for.
   - accept_liability: the requirements cannot be met, or the missing items are not the kind a merchant could
     supply after the fact.
8. Put contradictions between the issuer narrative and the evidence, date or address concerns, and anything
   the analyst should double-check into caveats.
9. Write the rationale as 3-5 sentences in the voice of the analyst, factual, no hedging words, ready to file.
10. The rationale must be consistent with your own assessment. Never state as fact anything a requirement is
    marked partial or missing for, and never cite a document you are asking the merchant to supply. If the
    recommended action is not represent, the rationale states where the case stands and what is unproven,
    rather than arguing a representment.
"""


@dataclass
class LLMResult:
    workup: Workup
    raw_response: dict  # full API response, for the audit log
    usage: dict
    attempts: int
    validation_problems: list[str]  # problems left on the final attempt (should be empty)
    problems_per_attempt: list[list[str]] = None  # what each attempt got wrong, for the audit log


def build_user_content(case: Case, rule: ReasonCode, prechecks: PreChecks, documents: list[Document]) -> list[dict]:
    """Assemble the user turn as content blocks: text for everything, image blocks for PNG evidence."""
    meta = case.model_dump()
    docs_meta = {d.name: d.page_count for d in documents}

    parts: list[dict] = []
    text = [
        "# Scheme rule",
        rule_as_text(rule),
        "",
        "# Case",
        json.dumps({k: v for k, v in meta.items() if k != "merchant_evidence_documents"}, indent=2),
        "",
        "# Pre-checks (computed from the transaction record)",
        *prechecks.as_lines(),
        "",
        "# Merchant evidence documents",
        f"{len(documents)} document(s): " + (", ".join(f"{n} ({p} page(s))" for n, p in docs_meta.items()) or "none"),
        "",
    ]
    parts.append({"type": "text", "text": "\n".join(text)})

    for d in documents:
        if d.kind == "pdf":
            for p in d.pages:
                parts.append({
                    "type": "text",
                    "text": f'<document name="{d.name}" page="{p.number}" of="{d.page_count}">\n{p.text}\n</document>',
                })
        else:
            parts.append({"type": "text", "text": f'<document name="{d.name}" page="1" of="1" kind="image">'})
            parts.append({"type": "image", "source": {"type": "base64", "media_type": d.media_type, "data": d.image_b64}})
            parts.append({"type": "text", "text": "</document>"})

    parts.append({"type": "text", "text": "Produce the representment workup for this case."})
    return parts


def request_workup(case: Case, rule: ReasonCode, prechecks: PreChecks, documents: list[Document],
                   client: anthropic.Anthropic | None = None) -> LLMResult:
    client = client or anthropic.Anthropic()
    case_docs = {d.name: d.page_count for d in documents}
    messages = [{"role": "user", "content": build_user_content(case, rule, prechecks, documents)}]

    attempts = 0
    problems: list[str] = []
    history: list[list[str]] = []
    while True:
        attempts += 1
        response = client.messages.parse(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=messages,
            output_format=Workup,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
        )
        workup: Workup = response.parsed_output
        problems = validate_pointers(workup, case_docs, len(rule.requirements))
        history.append(problems)
        if not problems or attempts >= 2:
            break
        # One corrective round-trip: show the model its own answer and the concrete problems.
        messages = messages + [
            {"role": "assistant", "content": workup.model_dump_json()},
            {"role": "user", "content": "Your workup failed validation:\n- " + "\n- ".join(problems)
             + "\nReturn a corrected workup. Only point to documents and pages that exist in this case."},
        ]

    usage = response.usage.model_dump() if hasattr(response.usage, "model_dump") else dict(response.usage)
    return LLMResult(
        workup=workup,
        raw_response=response.model_dump(mode="json", warnings=False),  # ParsedMessage carries extra fields
        usage=usage,
        attempts=attempts,
        validation_problems=problems,
        problems_per_attempt=history,
    )
