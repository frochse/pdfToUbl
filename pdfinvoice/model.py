"""Plain data model for a parsed invoice."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional


@dataclass
class Party:
    name: Optional[str] = None
    address: List[str] = field(default_factory=list)
    vat_number: Optional[str] = None
    coc_number: Optional[str] = None
    email: Optional[str] = None
    iban: Optional[str] = None
    bic: Optional[str] = None


@dataclass
class LineItem:
    description: str = ""
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    tax_rate: Optional[float] = None
    amount: Optional[float] = None


@dataclass
class Invoice:
    source_file: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    order_number: Optional[str] = None
    customer_number: Optional[str] = None
    payment_reference: Optional[str] = None
    currency: Optional[str] = None
    supplier: Party = field(default_factory=Party)
    customer: Party = field(default_factory=Party)
    lines: List[LineItem] = field(default_factory=list)
    total_net: Optional[float] = None
    total_tax: Optional[float] = None
    total_gross: Optional[float] = None
    tax_rate: Optional[float] = None
    ocr_used: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, list):
                return [convert(v) for v in value]
            return value

        return convert(asdict(self))
