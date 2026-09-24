"""Merchant evidence loading.

Design choice: extract text ourselves, page by page, rather than sending PDFs to the model as opaque
documents. Reasons: (1) we control exactly what the model saw, (2) every quote the model returns can be
verified verbatim against the extracted text, (3) page-level pointers come for free.

Every page of every PDF in this dataset has a text layer (checked: pdfplumber returns text for all of them).
Scanned PDFs would need an OCR step (e.g. tesseract) inserted in `extract_pages`; that is out of scope here
and noted in the README. Images (PNG) are passed to the model's vision input as-is.
"""
from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


@dataclass
class Page:
    number: int  # 1-based
    text: str


@dataclass
class Document:
    name: str
    kind: str  # "pdf" | "image"
    pages: list[Page] = field(default_factory=list)  # pdf only
    image_b64: str | None = None  # image only
    media_type: str | None = None  # image only

    @property
    def page_count(self) -> int:
        return len(self.pages) if self.kind == "pdf" else 1

    def page_text(self, number: int) -> str:
        for p in self.pages:
            if p.number == number:
                return p.text
        raise KeyError(f"{self.name} has no page {number}")


def normalize(text: str) -> str:
    """Normalise text for quote matching: unicode NFKC, unify dashes/quotes, collapse whitespace, lowercase.

    Used on both sides (document text and model quote) so that a quote still matches when pdf extraction
    inserted odd spacing or the model normalised a dash.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("—", "-").replace("–", "-").replace("−", "-")
    text = text.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"(?<=\w)-[ \t]*\n\s*(?=\w)", "", text)  # re-join words hyphenated across a line break
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def extract_pages(pdf_path: Path) -> list[Page]:
    pages: list[Page] = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            pages.append(Page(number=i, text=text))
    return pages


def load_document(path: Path) -> Document:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return Document(name=path.name, kind="pdf", pages=extract_pages(path))
    if suffix in IMAGE_TYPES:
        data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
        return Document(name=path.name, kind="image", image_b64=data, media_type=IMAGE_TYPES[suffix])
    raise ValueError(f"unsupported evidence file type: {path.name}")


def load_case_documents(filenames: list[str], docs_dir: Path) -> list[Document]:
    docs = []
    for name in filenames:
        path = docs_dir / name
        if not path.exists():
            raise FileNotFoundError(f"evidence file referenced by case but missing: {name}")
        docs.append(load_document(path))
    return docs
