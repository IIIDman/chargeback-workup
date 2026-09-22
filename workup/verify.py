"""Quote verification: does every pointer the model returned actually exist in the document it cites?

The model is asked for verbatim quotes. We check each one against the text we extracted from that page.
A quote we cannot find is not proof of hallucination (extraction can mangle a table), but it is exactly
the thing an analyst should not have to discover by opening the PDF. So:

- a pointer is `verified=True` if its normalised quote is a substring of the normalised page text
  (second attempt with punctuation stripped, to survive extraction quirks);
- pointers into images are `verified=None`: we have no text to check against, the analyst must look;
- a requirement marked satisfied with no verified text pointer is downgraded to partial, and the reason
  is recorded so the UI can show it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .docs import Document, normalize
from .schema import Status, Workup

_PUNCT = re.compile(r"[^\w\s]")


@dataclass
class PointerCheck:
    document: str
    page: int
    quote: str
    verified: bool | None  # None: image, cannot be text-verified
    note: str = ""


@dataclass
class RequirementVerification:
    requirement_id: int
    original_status: Status
    effective_status: Status
    pointers: list[PointerCheck] = field(default_factory=list)
    downgraded: bool = False
    note: str = ""

    @property
    def verified_count(self) -> int:
        return sum(1 for p in self.pointers if p.verified is True)

    @property
    def unverified_count(self) -> int:
        return sum(1 for p in self.pointers if p.verified is False)

    @property
    def image_only(self) -> bool:
        return bool(self.pointers) and all(p.verified is None for p in self.pointers)


def quote_in_text(quote: str, page_text: str) -> bool:
    q, t = normalize(quote), normalize(page_text)
    if not q:
        return False
    if q in t:
        return True
    q2, t2 = _PUNCT.sub("", q), _PUNCT.sub("", t)
    q2, t2 = re.sub(r"\s+", " ", q2).strip(), re.sub(r"\s+", " ", t2)
    return bool(q2) and q2 in t2


def verify_workup(workup: Workup, documents: list[Document]) -> list[RequirementVerification]:
    docs = {d.name: d for d in documents}
    out: list[RequirementVerification] = []
    for r in workup.requirements:
        rv = RequirementVerification(requirement_id=r.requirement_id, original_status=r.status, effective_status=r.status)
        for p in r.pointers:
            doc = docs.get(p.document)
            if doc is None:
                rv.pointers.append(PointerCheck(p.document, p.page, p.quote, False, "document not in case"))
                continue
            if doc.kind == "image":
                rv.pointers.append(PointerCheck(p.document, p.page, p.quote, None, "image: not text-verifiable"))
                continue
            try:
                ok = quote_in_text(p.quote, doc.page_text(p.page))
            except KeyError:
                ok = False
            rv.pointers.append(PointerCheck(p.document, p.page, p.quote, ok, "" if ok else "quote not found on that page"))

        if r.status is Status.satisfied and rv.pointers and rv.verified_count == 0 and not rv.image_only:
            rv.effective_status = Status.partial
            rv.downgraded = True
            rv.note = "downgraded satisfied -> partial: none of the cited quotes were found in the document text"
        elif r.status is Status.partial and rv.pointers and rv.verified_count == 0 and not rv.image_only:
            rv.note = "partial, but none of the cited quotes were found in the document text"
        elif rv.image_only and r.status in (Status.satisfied, Status.partial):
            rv.note = "supported by an image only; not text-verifiable, analyst should view it"
        out.append(rv)
    return out


# ----------------------------------------------------------- rationale self-consistency

# identifier-shaped tokens only: must contain a digit, so "non-refundable" and "pre-renewal" are ignored
_IDENT = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+\b")


def rationale_conflicts(workup: Workup) -> list[str]:
    """Catch a rationale that asserts evidence the same workup says is missing.

    Found on the first full run: case 7's rationale read "the signed proof of delivery POD-9051-img and
    the driver telematics logs confirm service was rendered", while its own assessment marked that
    requirement partial precisely because POD-9051-img had not been supplied, and listed it under
    evidence to request. The rationale is the text that gets filed, so an assertion about evidence we do
    not hold is the most expensive kind of error here.

    The check is a smoke alarm, not a verdict: it matches identifier-shaped tokens carrying a digit that
    appear both in the rationale and in the list of things to ask for. It over-fires when a reference is
    used as context in the ask ("the folio for booking MSP-2025-4488"), so the message tells the analyst
    what to look at rather than asserting an error, and it raises the tier to medium, not needs_review.
    """
    asks = " ".join(workup.evidence_to_request)
    if not asks.strip():
        return []
    in_rationale = {m.group().lower() for m in _IDENT.finditer(workup.rationale)}
    in_asks = {m.group().lower() for m in _IDENT.finditer(asks)}
    overlap = sorted(in_rationale & in_asks)
    return [f"the rationale mentions {ident!r}, which the workup also asks the merchant to supply: check it "
            f"is not claimed as proof we already hold" for ident in overlap]
