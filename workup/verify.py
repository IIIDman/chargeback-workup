"""Quote verification: is every pointer the model returned actually on the page it cites?

A pointer is verified when its normalised quote appears on word boundaries in the normalised page text
(second pass with punctuation turned into spaces) and is substantial enough to mean something. Pointers
into images are `verified=None`: no text to check, the analyst must look. A satisfied requirement with no
verified text pointer is downgraded to partial with a note; a failed match is usually the extractor, not
the model, so nothing is discarded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .docs import Document, normalize
from .schema import Status, Workup

_PUNCT = re.compile(r"[^\w\s]")
MIN_QUOTE_CHARS = 12  # a quote counts as evidence only when it is at least this long AND...
MIN_QUOTE_WORDS = 3   # ...has this many words; shorter is fine only with a digit in it ("ECI 02", "TF-9051")
_IDENT = re.compile(r"(?<!\w)(?=[\w-]*\d)(?=[\w-]*[a-z])[a-z0-9][\w-]{3,}(?!\w)")


def _substantial(q: str) -> bool:
    """Generic fragments ("of the", "delivered") would match almost any page and must not count."""
    words = q.split()
    if len(q) >= MIN_QUOTE_CHARS and len(words) >= MIN_QUOTE_WORDS:
        return True
    if _IDENT.search(q) is not None:
        return True
    return len(words) >= 2 and len(q) >= 6 and any(ch.isdigit() for ch in q)  # "ECI 02", "Total: 54.00"


def _bounded(needle: str, hay: str) -> bool:
    """Substring match on word boundaries: "ORD-551" must not match inside "ORD-5512", nor
    "authorised transaction" inside "unauthorised transaction"."""
    return re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", hay) is not None


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
    def image_count(self) -> int:
        return sum(1 for p in self.pointers if p.verified is None)

    @property
    def image_only(self) -> bool:
        return bool(self.pointers) and all(p.verified is None for p in self.pointers)


def quote_in_text(quote: str, page_text: str) -> bool:
    q, t = normalize(quote), normalize(page_text)
    if not q or not _substantial(q):
        return False
    if _bounded(q, t):
        return True
    # Punctuation becomes a space, never nothing: deleting it would let "12.50" match inside "1,250.00".
    q2 = re.sub(r"\s+", " ", _PUNCT.sub(" ", q)).strip()
    t2 = re.sub(r"\s+", " ", _PUNCT.sub(" ", t))
    return bool(q2) and _bounded(q2, t2)


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

        text_backed = rv.verified_count > 0
        if r.status is Status.satisfied and not text_backed and not rv.image_only:
            rv.effective_status = Status.partial
            rv.downgraded = True
            if not rv.pointers:
                rv.note = "downgraded satisfied -> partial: no evidence pointer was given"
            elif rv.image_count:
                rv.note = ("downgraded satisfied -> partial: the text quotes were not found in the document; "
                           "the remaining support is an image the analyst must view")
            else:
                rv.note = "downgraded satisfied -> partial: none of the cited quotes were found in the document text"
        elif r.status is Status.partial and rv.pointers and not text_backed and not rv.image_only:
            rv.note = "partial, but none of the cited quotes were found in the document text"
        elif rv.image_only and r.status in (Status.satisfied, Status.partial):
            rv.note = "supported by an image only; not text-verifiable, analyst should view it"
        out.append(rv)
    return out


# ----------------------------------------------------------- rationale self-consistency

# Identifier-shaped tokens: letters/digits joined by - or _, at least one digit, at least 5 characters.
# Pure numbers (2025, postcodes' digit runs) and plain hyphenated words (non-refundable) are ignored.
_TOKEN = re.compile(r"\b[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*\b")


def _identifiers(text: str) -> set[str]:
    out = set()
    for m in _TOKEN.finditer(text):
        tok = m.group()
        core = tok.replace("-", "").replace("_", "")
        if len(tok) >= 5 and any(c.isdigit() for c in tok) and not core.isdigit():  # skips dates, pure numbers
            out.add(tok.lower())
    return out


def rationale_conflicts(workup: Workup, ignore: set[str] | None = None) -> list[str]:
    """Flag a rationale that asserts evidence the same workup says is missing.

    Matches identifier-shaped tokens (letters plus digits, e.g. POD-9051-img) that appear both in the
    rationale and in the list of things to request. Case and transaction ids are passed in `ignore`. It
    over-fires when a reference is used as context in the ask, so the message tells the analyst what to
    check and the tier only goes to medium.
    """
    asks = " ".join(workup.evidence_to_request)
    if not asks.strip():
        return []
    skip = {s.lower() for s in (ignore or set())}
    overlap = sorted((_identifiers(workup.rationale) & _identifiers(asks)) - skip)
    return [f"the rationale mentions {ident!r}, which the workup also asks the merchant to supply: check it "
            f"is not claimed as proof we already hold" for ident in overlap]
