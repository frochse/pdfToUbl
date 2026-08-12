"""PDF text extraction, plus an optional OCR fallback for scanned invoices."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

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

    @property
    def text(self) -> str:
        return "\n".join(self.pages)

    @property
    def lines(self) -> list[str]:
        return [ln.rstrip() for ln in self.text.splitlines() if ln.strip()]


def extract(path: Path, ocr: str = "auto") -> Document:
    """Read `path` into a Document.

    ocr: "auto" runs OCR only when the PDF carries (almost) no text layer,
    "never" disables it, "always" forces it.
    """
    doc = _extract_with_pdfplumber(path)

    needs_ocr = ocr == "always" or (ocr == "auto" and len(doc.text.strip()) < 40)
    if needs_ocr:
        ocred = _ocr(path)
        if ocred is not None:
            ocred.ocr_used = True
            # Point back at the real PDF: the OCR'd copy lives in a temporary
            # directory that is already gone, and callers read this path again
            # to embed the source in the UBL.
            ocred.path = path
            return ocred
    return doc


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
    return doc


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


def _ocr(path: Path) -> Document | None:
    """Run ocrmypdf if it is installed; return None when unavailable."""
    if not shutil.which("ocrmypdf"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ocr.pdf"
        base = ["ocrmypdf", "--force-ocr", "--quiet"]
        for command in (base + ["-l", OCR_LANGUAGES], base):
            proc = subprocess.run(command + [str(path), str(out)],
                                  capture_output=True)
            if proc.returncode == 0 and out.exists():
                return _extract_with_pdfplumber(out)
        return None


def ocr_available() -> bool:
    return shutil.which("ocrmypdf") is not None
