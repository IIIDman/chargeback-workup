"""Page rendering with quote highlight, checked on the buried-evidence case (a real pointer from the cache)."""
import json

import pdfplumber
import pytest

from workup.pipeline import ARTIFACTS, DOCS_DIR
from workup.render import locate_words, render_page

ART = ARTIFACTS / "CB-2025-0007.json"


def _first_text_pointer():
    art = json.loads(ART.read_text())
    for r in art["workup"]["requirements"]:
        for p in r["pointers"]:
            if p["document"].endswith(".pdf"):
                return p
    raise AssertionError("no text pointer in the cached case 7 workup")


@pytest.mark.skipif(not ART.exists(), reason="no cached workup")
def test_cached_case_7_quote_is_located_on_its_page():
    p = _first_text_pointer()
    with pdfplumber.open(DOCS_DIR / p["document"]) as pdf:
        words = pdf.pages[p["page"] - 1].extract_words()
    idx = locate_words(words, p["quote"])
    assert idx, p["quote"]
    assert " ".join(words[i]["text"] for i in idx).lower().startswith(p["quote"].split()[0].lower().strip('"'))


@pytest.mark.skipif(not ART.exists(), reason="no cached workup")
def test_render_page_returns_png_and_reports_highlight():
    p = _first_text_pointer()
    png, found = render_page(DOCS_DIR / p["document"], p["page"], p["quote"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and found
    png2, found2 = render_page(DOCS_DIR / p["document"], p["page"], "this phrase is on no page at all 4711")
    assert png2[:8] == b"\x89PNG\r\n\x1a\n" and not found2


def test_locate_words_matches_across_punctuation_but_not_inside_identifiers():
    words = [{"text": t, "x0": i * 10, "x1": i * 10 + 8, "top": 0, "bottom": 10}
             for i, t in enumerate(["Ref:", "ORD-5512", "Total:", "£1,250.00", "paid"])]
    assert locate_words(words, "Total: £1,250.00 paid") == [2, 3, 4]
    assert locate_words(words, "ORD-551") == []
    assert locate_words(words, "12.50") == []
