"""Render a cited page as an image with the quote highlighted, for the analyst UI.

The quote is located among pdfplumber's word boxes with the same normalisation the verifier uses, and the
matching words get a box. If it cannot be located the page is still shown, without a box. Read-only:
nothing here touches the prompt or the cache.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pdfplumber

from .docs import normalize

_PUNCT = re.compile(r"[^\w\s]")
HIGHLIGHT_FILL = (255, 214, 0, 80)
HIGHLIGHT_STROKE = (214, 130, 0)


def _flatten(tokens: list[str]) -> tuple[str, list[int]]:
    """Join tokens with single spaces; return the string and, per character, the index of its token."""
    joined, owner = [], []
    for i, tok in enumerate(tokens):
        if joined:
            joined.append(" ")
            owner.append(-1)
        joined.append(tok)
        owner.extend([i] * len(tok))
    return "".join(joined), owner


def locate_words(words: list[dict], quote: str) -> list[int]:
    """Indices of the page words that spell the quote, or [] when it cannot be located.

    Same two passes as the verifier: exact normalised text on word boundaries, then with punctuation
    turned into spaces. A quote the verifier accepted is therefore almost always located here too; the
    exception is a word that pdfplumber splits differently in `extract_words` than in `extract_text`.
    """
    q = normalize(quote)
    if not q:
        return []
    toks = [normalize(w["text"]) for w in words]
    for strip in (False, True):
        if strip:
            q_ = re.sub(r"\s+", " ", _PUNCT.sub(" ", q)).strip()
            t_ = [re.sub(r"\s+", " ", _PUNCT.sub(" ", t)).strip() for t in toks]
        else:
            q_, t_ = q, toks
        if not q_:
            continue
        joined, owner = _flatten(t_)
        m = re.search(r"(?<!\w)" + re.escape(q_) + r"(?!\w)", joined)
        if m:
            return sorted({owner[k] for k in range(m.start(), m.end()) if owner[k] >= 0})
    return []


def _line_boxes(words: list[dict], idx: list[int]) -> list[tuple[float, float, float, float]]:
    """One box per text line rather than one per word, so the highlight reads as a phrase."""
    lines: dict[int, list[dict]] = {}
    for i in idx:
        lines.setdefault(round(words[i]["top"]), []).append(words[i])
    return [(min(w["x0"] for w in ws) - 1.5, min(w["top"] for w in ws) - 1.5,
             max(w["x1"] for w in ws) + 1.5, max(w["bottom"] for w in ws) + 1.5) for ws in lines.values()]


def render_page(pdf_path: Path, page_number: int, quote: str | None = None, resolution: int = 96) -> tuple[bytes, bool]:
    """PNG bytes of the page and whether the quote was located and highlighted."""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_number - 1]
        image = page.to_image(resolution=resolution)
        found = False
        if quote:
            words = page.extract_words()
            idx = locate_words(words, quote)
            if idx:
                image.draw_rects(_line_boxes(words, idx), fill=HIGHLIGHT_FILL, stroke=HIGHLIGHT_STROKE, stroke_width=2)
                found = True
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue(), found
