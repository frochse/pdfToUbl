"""PDF text extraction, plus an optional OCR fallback for scanned invoices."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

from .numbers import money_tokens, written_with_decimals

# A PDF text layer carries characters that are not text: NUL from a broken
# encoding, form feeds between pages. They travel into every output format —
# an invoice number of "ORF67LFJ\x000002" in CSV, and XML that no parser will
# read back — so drop them as the text comes in rather than in each writer.
# Tab, newline and carriage return are kept; the layout code needs them.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


@dataclass
class Document:
    """Extracted content of one PDF: text, ruled tables and positioned words."""

    path: Path
    pages: list[str] = field(default_factory=list)
    tables: list[list[list]] = field(default_factory=list)
    # Words grouped into visual rows, per page. Each word is a dict with
    # "text", "x0", "x1", "top" — enough to rebuild columns that the plain
    # text layer loses, because PDF text has no column separators.
    word_rows: list[list[list[dict]]] = field(default_factory=list)
    ocr_used: bool = False
    # Height of the tallest band that is covered by a picture and holds no
    # words at all. See `_image_only_band`.
    image_only_band: float = 0.0

    @property
    def text(self) -> str:
        return "\n".join(self.pages)

    @property
    def lines(self) -> list[str]:
        return [ln.rstrip() for ln in self.text.splitlines() if ln.strip()]


# A band this tall (in points, ~3 cm) that is picture and holds no words is
# not a margin: it is printed matter the text layer does not carry.
HIDDEN_TEXT_BAND = 90.0


def extract(path: Path, ocr: str = "auto") -> Document:
    """Read `path` into a Document.

    ocr: "auto" runs OCR when the PDF carries (almost) no text layer, and when
    it carries one but hides part of the page in a picture; "never" disables
    it, "always" forces it.
    """
    doc = _extract_with_pdfplumber(path)
    if ocr == "never":
        return doc

    # An invoice printed on scanned stationery has a text layer for everything
    # the accounting package filled in, and none for the letterhead: the KvK,
    # VAT and bank details are part of the page picture. Rescanning the whole
    # page would put the item table through OCR too and misread amounts that
    # were already perfect, so only the picture is read, with --redo-ocr.
    if ocr == "always" or _no_values_in(doc.text):
        ocred = _ocr(path, redo=False)
    elif doc.image_only_band >= HIDDEN_TEXT_BAND:
        ocred = _ocr(path, redo=True)
    else:
        return doc

    if ocred is None:
        return doc
    ocred.ocr_used = True
    # Point back at the real PDF: the OCR'd copy lives in a temporary
    # directory that is already gone, and callers read this path again
    # to embed the source in the UBL.
    ocred.path = path
    return ocred


def _no_values_in(text: str) -> bool:
    """Whether the text layer holds nothing worth parsing.

    An empty one is the obvious case. The other is a page generated from a
    template, which comes back as its own printed headings — "Omschrijving
    Bedrag", "Totaal" — with every value it was filled in with unreadable, so
    that the invoice looks like an invoice and says nothing. An amount with
    cents in it is the test: every invoice prints at least one, and a text
    layer that carries none is not carrying the invoice either.
    """
    return len(text.strip()) < 40 or not any(
        written_with_decimals(token) for token, _ in money_tokens(text)
    )


def _extract_with_pdfplumber(path: Path) -> Document:
    doc = Document(path=path)
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            doc.pages.append(clean_text(page.extract_text(x_tolerance=1.5) or ""))
            for table in page.extract_tables() or []:
                doc.tables.append([
                    [clean_text(cell) if isinstance(cell, str) else cell
                     for cell in row]
                    for row in table
                ])
            words = page.extract_words(x_tolerance=1.5, y_tolerance=2,
                                       keep_blank_chars=False)
            for word in words:
                word["text"] = clean_text(word["text"])
            doc.word_rows.append(group_into_rows(words))
            doc.image_only_band = max(
                doc.image_only_band, _image_only_band(page, words)
            )
    return doc


def _image_only_band(page, words: list) -> float:
    """The tallest horizontal band of this page that is picture and no words.

    A letterhead can be part of the page image rather than the text layer, and
    then nothing about the supplier is readable. The gap it leaves is the only
    sign: a stretch of page with a picture behind it and not one word on it.
    """
    images = [(image["top"], image["bottom"]) for image in page.images]
    if not images:
        return 0.0

    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for top, bottom in sorted((w["top"], w["bottom"]) for w in words):
        if top > cursor:
            gaps.append((cursor, top))
        cursor = max(cursor, bottom)
    gaps.append((cursor, float(page.height)))

    return max(
        (
            min(gap_bottom, bottom) - max(gap_top, top)
            for gap_top, gap_bottom in gaps
            for top, bottom in images
        ),
        default=0.0,
    )


def group_into_rows(words: list, tolerance: float = 3.0) -> list[list[dict]]:
    """Group words into visual rows by their vertical position."""
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if rows and abs(rows[-1][0]["top"] - word["top"]) <= tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    return rows


# Dutch first, since that is what these invoices are, then English for the ones
# printed in it. Dropped when the language packs are not installed.
OCR_LANGUAGES = "nld+eng"


def _ocr(path: Path, redo: bool = False) -> Document | None:
    """Run ocrmypdf if it is installed; return None when unavailable.

    `redo` keeps the text the PDF already has and reads only the parts of the
    page that are picture. There is no falling back to --force-ocr when it
    fails: rasterising a page whose text layer is exact would trade a missing
    letterhead for misread amounts.
    """
    if not shutil.which("ocrmypdf"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ocr.pdf"
        base = ["ocrmypdf", "--redo-ocr" if redo else "--force-ocr", "--quiet"]
        for command in (base + ["-l", OCR_LANGUAGES], base):
            proc = subprocess.run(command + [str(path), str(out)],
                                  capture_output=True)
            if proc.returncode == 0 and out.exists():
                return _extract_with_pdfplumber(out)
        return None


def ocr_available() -> bool:
    return shutil.which("ocrmypdf") is not None
