"""Rule-based PDF invoice reader (no LLM involved)."""

from .model import Invoice, LineItem, Party
from .parser import parse
from .textio import Document, extract

__all__ = ["Invoice", "LineItem", "Party", "Document", "extract", "parse", "read"]
__version__ = "0.1.0"


def read(path, ocr: str = "auto", day_first: bool = True) -> Invoice:
    """Convenience API: path in, parsed Invoice out."""
    from pathlib import Path

    return parse(extract(Path(path), ocr=ocr), day_first=day_first)
