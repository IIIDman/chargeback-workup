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
from pydantic import ValidationError

from .checks import PreChecks
from .docs import Document
from .rules import ReasonCode, rule_as_text
from .schema import Case, Workup, validate_pointers

# Override with WORKUP_MODEL in .env (e.g. claude-opus-5, claude-sonnet-5) to compare models.
MODEL = os.environ.get("WORKUP_MODEL", "claude-opus-5-5")
# Everything about the call that changes what the model does, kept in one place so the cache key can hash it.
CALL_PARAMS = {"max_tokens": 16000, "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}

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
    usage: dict  # totals over all metered attempts
    usage_per_attempt: list[dict]
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
    usages: list[dict] = []  # one per answer the API returned; an unparseable answer leaves none
    unparseable = 0
    while True:
        attempts += 1
        try:
            response = client.messages.parse(
                model=MODEL,
                system=SYSTEM_PROMPT,
                messages=messages,
                output_format=Workup,
                **CALL_PARAMS,
            )
        except ValidationError as e:
            # The SDK could not parse the text as a Workup: a truncated or degenerate answer (seen once on
            # case 6, where the output collapsed into repeated "***" and was cut off). Non-deterministic,
            # so one clean retry of the same request; a second failure is reported, never cached.
            # A truncated answer (stop_reason max_tokens) surfaces here too, because the SDK parses the
            # text before handing the response back; it gets the same single retry.
            unparseable += 1
            history.append([f"unparseable answer: {_first_error(e)}"])
            if unparseable >= 2:
                raise RuntimeError(f"{case.case_id}: the model returned an unparseable answer twice; "
                                   f"not caching. Last error: {_first_error(e)}") from e
            continue
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            raise RuntimeError(
                f"{case.case_id}: model stopped with {response.stop_reason!r} and "
                f"{'no' if response.parsed_output is None else 'a'} parsed workup; not caching this response"
            )
        workup: Workup = response.parsed_output
        usages.append(_usage_dict(response.usage))
        problems = validate_pointers(workup, case_docs, len(rule.requirements))
        history.append(problems)
        if not problems or attempts - unparseable >= 2:
            break
        # One corrective round-trip: show the model its own answer and the concrete problems.
        messages = messages + [
            {"role": "assistant", "content": workup.model_dump_json()},
            {"role": "user", "content": "Your workup failed validation:\n- " + "\n- ".join(problems)
             + "\nReturn a corrected workup. Only point to documents and pages that exist in this case."},
        ]

    return LLMResult(
        workup=workup,
        raw_response=response.model_dump(mode="json", warnings=False),  # ParsedMessage carries extra fields
        usage=sum_usage(usages),
        usage_per_attempt=usages,
        attempts=attempts,
        validation_problems=problems,
        problems_per_attempt=history,
    )


def _first_error(e: ValidationError) -> str:
    try:
        err = e.errors()[0]
        return f"{err.get('type')}: {err.get('msg')}"
    except Exception:  # pragma: no cover
        return str(e).splitlines()[0]


def _usage_dict(usage) -> dict:
    return usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)


_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def sum_usage(usages: list[dict]) -> dict:
    """Token totals over every answer the API returned for this case, so the cost of a corrective
    round-trip is counted. `attempts_metered` says how many answers are in the total."""
    out = {k: sum(int(u.get(k) or 0) for u in usages) for k in _USAGE_KEYS}
    out["attempts_metered"] = len(usages)
    return out
