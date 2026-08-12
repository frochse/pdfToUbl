"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List

from . import ubl
from .export import write_csv
from .model import Invoice
from .parser import parse
from .textio import extract, ocr_available


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pdfinvoice",
        description=(
            "Read invoice data out of PDF files using text extraction and "
            "rules only. No LLM, no network access."
        ),
    )
    ap.add_argument("paths", nargs="+", type=Path,
                    help="PDF files, or directories to scan for *.pdf")
    ap.add_argument("-f", "--format", choices=("text", "json", "csv", "ubl"),
                    default="text",
                    help="output format; ubl is an OASIS UBL 2.1 Invoice "
                         "(default: text)")
    ap.add_argument("-o", "--output", type=Path,
                    help="write to this file instead of stdout")
    ap.add_argument("-d", "--out-dir", type=Path,
                    help="write one file per invoice into this directory; "
                         "the usual way to emit UBL for a batch")
    ap.add_argument("--currency", default="EUR", metavar="CODE",
                    help="currency for UBL output when the PDF does not name "
                         "one (default: EUR)")
    ap.add_argument("--ocr", choices=("auto", "never", "always"), default="auto",
                    help="OCR scanned PDFs via ocrmypdf (default: auto)")
    ap.add_argument("--date-order", choices=("dmy", "mdy"), default="dmy",
                    help="how to read ambiguous dates like 03/04/2026")
    ap.add_argument("--raw-text", action="store_true",
                    help="print the extracted text instead of parsing it")
    ap.add_argument("--strict", action="store_true",
                    help="exit with status 2 if any invoice produced warnings")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="recurse into subdirectories when given a directory")
    return ap


def collect_pdfs(paths: List[Path], recursive: bool) -> List[Path]:
    files: List[Path] = []
    for path in paths:
        if path.is_dir():
            pattern = "**/*.pdf" if recursive else "*.pdf"
            files.extend(sorted(p for p in path.glob(pattern) if p.is_file()))
        else:
            files.append(path)
    return files


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = args.output.open("w", encoding="utf-8") if args.output else sys.stdout

    files = collect_pdfs(args.paths, args.recursive)
    if not files:
        print("no PDF files found", file=sys.stderr)
        return 1

    invoices: List[Invoice] = []
    failures = 0

    try:
        for path in files:
            if not path.exists():
                print(f"{path}: no such file", file=sys.stderr)
                failures += 1
                continue
            try:
                doc = extract(path, ocr=args.ocr)
            except Exception as exc:  # unreadable/corrupt PDF
                print(f"{path}: could not read PDF ({exc})", file=sys.stderr)
                failures += 1
                continue

            if args.raw_text:
                print(f"===== {path} =====", file=out)
                print(doc.text, file=out)
                continue

            if not doc.text.strip():
                hint = "" if ocr_available() else " (install ocrmypdf to OCR scans)"
                print(f"{path}: no text layer found{hint}", file=sys.stderr)
                failures += 1
                continue

            invoices.append(parse(doc, day_first=args.date_order == "dmy"))

        if not args.raw_text:
            if args.out_dir:
                _write_per_invoice(invoices, args)
            else:
                _write(invoices, args.format, out, args)
    finally:
        if args.output:
            out.close()

    if failures:
        return 1
    if args.strict and any(inv.warnings for inv in invoices):
        return 2
    return 0


def _write(invoices: List[Invoice], fmt: str, out, args) -> None:
    if fmt == "ubl":
        for invoice in invoices:
            data, name = _source_pdf(invoice)
            out.write(ubl.to_xml(invoice, default_currency=args.currency,
                                 pdf_bytes=data, pdf_name=name))
            for warning in ubl.conformance_warnings(invoice):
                print(f"{name or invoice.invoice_number}: {warning}",
                      file=sys.stderr)
    elif fmt == "json":
        payload = [inv.to_dict() for inv in invoices]
        json.dump(payload if len(payload) != 1 else payload[0], out, indent=2,
                  ensure_ascii=False)
        out.write("\n")
    elif fmt == "csv":
        write_csv(invoices, out)
    else:
        for index, inv in enumerate(invoices):
            if index:
                out.write("\n")
            out.write(render_text(inv))


def _source_pdf(invoice: Invoice):
    """The PDF this invoice came from, to embed in the UBL.

    Exact shows no invoice image for a document that carries no attachment.
    """
    path = Path(invoice.source_file) if invoice.source_file else None
    if path and path.is_file():
        return path.read_bytes(), path.name
    return None, path.name if path else None


EXTENSIONS = {"ubl": "xml", "json": "json", "csv": "csv", "text": "txt"}


def _write_per_invoice(invoices: List[Invoice], args) -> None:
    """One output file per invoice, named after the invoice number."""
    args.out_dir.mkdir(parents=True, exist_ok=True)
    used = set()
    for invoice in invoices:
        stem = _safe_stem(invoice, used)
        target = args.out_dir / f"{stem}.{EXTENSIONS[args.format]}"
        with target.open("w", encoding="utf-8") as handle:
            _write([invoice], args.format, handle, args)
        print(target, file=sys.stderr)


def _safe_stem(invoice: Invoice, used: set) -> str:
    base = invoice.invoice_number or Path(invoice.source_file or "invoice").stem
    stem = re.sub(r"[^\w.-]+", "_", base).strip("_") or "invoice"
    candidate, suffix = stem, 2
    while candidate in used:
        candidate, suffix = f"{stem}-{suffix}", suffix + 1
    used.add(candidate)
    return candidate


def render_text(inv: Invoice) -> str:
    cur = inv.currency or ""
    rows = [
        ("File", Path(inv.source_file).name if inv.source_file else ""),
        ("Invoice number", inv.invoice_number),
        ("Invoice date", inv.invoice_date),
        ("Due date", inv.due_date),
        ("Order number", inv.order_number),
        ("Customer number", inv.customer_number),
        ("Payment reference", inv.payment_reference),
        ("Supplier", inv.supplier.name),
        ("Supplier VAT", inv.supplier.vat_number),
        ("Supplier IBAN", inv.supplier.iban),
        ("Customer", inv.customer.name),
        ("Currency", inv.currency),
        ("Net total", _money(inv.total_net, cur)),
        ("VAT", _money(inv.total_tax, cur)),
        ("Gross total", _money(inv.total_gross, cur)),
        ("VAT rate", f"{inv.tax_rate:g}%" if inv.tax_rate is not None else None),
    ]

    width = max(len(label) for label, _ in rows)
    out = [f"{label.ljust(width)} : {value}" for label, value in rows
           if value not in (None, "")]

    if inv.lines:
        out.append("")
        out.append("Lines:")
        for item in inv.lines:
            parts = [f"  - {item.description}"]
            detail = []
            if item.quantity is not None:
                detail.append(f"qty {item.quantity:g}")
            if item.unit_price is not None:
                detail.append(f"@ {item.unit_price:.2f}")
            if item.tax_rate is not None:
                detail.append(f"vat {item.tax_rate:g}%")
            if item.amount is not None:
                detail.append(f"= {item.amount:.2f}")
            if detail:
                parts.append("    " + "  ".join(detail))
            out.append("\n".join(parts))

    if inv.ocr_used:
        out.append("")
        out.append("Note: text recovered via OCR; verify the numbers.")
    if inv.warnings:
        out.append("")
        out.append("Warnings:")
        out.extend(f"  ! {w}" for w in inv.warnings)

    return "\n".join(out) + "\n"


def _money(value, currency: str) -> str | None:
    if value is None:
        return None
    return f"{value:,.2f} {currency}".strip()


if __name__ == "__main__":
    raise SystemExit(main())
