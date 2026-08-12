"""Shared output formatting, used by both the CLI and the web app."""

from __future__ import annotations

import csv
import io
from typing import Iterable, List

from .model import Invoice

CSV_COLUMNS = [
    "source_file",
    "invoice_number",
    "invoice_date",
    "due_date",
    "order_number",
    "customer_number",
    "currency",
    "supplier_name",
    "supplier_vat_number",
    "supplier_iban",
    "customer_name",
    "total_net",
    "total_tax",
    "total_gross",
    "tax_rate",
    "line_count",
    "warnings",
]


def csv_row(inv: Invoice) -> dict:
    return {
        "source_file": inv.source_file,
        "invoice_number": inv.invoice_number or "",
        "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else "",
        "due_date": inv.due_date.isoformat() if inv.due_date else "",
        "order_number": inv.order_number or "",
        "customer_number": inv.customer_number or "",
        "currency": inv.currency or "",
        "supplier_name": inv.supplier.name or "",
        "supplier_vat_number": inv.supplier.vat_number or "",
        "supplier_iban": inv.supplier.iban or "",
        "customer_name": inv.customer.name or "",
        "total_net": money(inv.total_net),
        "total_tax": money(inv.total_tax),
        "total_gross": money(inv.total_gross),
        "tax_rate": money(inv.tax_rate),
        "line_count": len(inv.lines),
        "warnings": "; ".join(inv.warnings),
    }


def money(value) -> str:
    return "" if value is None else f"{value:.2f}"


def write_csv(invoices: Iterable[Invoice], stream) -> None:
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for inv in invoices:
        writer.writerow(csv_row(inv))


def csv_string(invoices: List[Invoice]) -> str:
    buffer = io.StringIO()
    write_csv(invoices, buffer)
    return buffer.getvalue()
