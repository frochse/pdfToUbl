"""Generate sample invoice PDFs used by the tests (requires reportlab)."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent / "samples"

DUTCH = [
    (60, 790, "Axual B.V."),
    (60, 776, "Kanaalweg 17L, 3526 KL Utrecht"),
    (60, 762, "KvK: 60895344   BTW: NL854103576B01"),
    (60, 748, "IBAN: NL91 ABNA 0417 1643 00   BIC: ABNANL2A"),
    (60, 710, "FACTUUR"),
    (60, 690, "Factuurnummer: 2026-0042"),
    (60, 676, "Factuurdatum: 12-08-2026"),
    (60, 662, "Vervaldatum: 11-09-2026"),
    (60, 648, "Klantnummer: KL-1187"),
    (60, 634, "Ordernummer: PO-99120"),
    (60, 600, "Factuuradres:"),
    (60, 586, "Voorbeeld Holding B.V."),
    (60, 572, "Postbus 123"),
    (60, 558, "1000 AA Amsterdam"),
    (60, 510, "Omschrijving                          Aantal   Prijs      Bedrag"),
    (60, 492, "Kafka support uren                      10   125,00    1.250,00"),
    (60, 478, "Streaming platform licentie              2   400,00      800,00"),
    (60, 464, "Reiskosten                               1    75,50       75,50"),
    (60, 430, "Totaal excl. BTW                                        2.125,50"),
    (60, 416, "BTW 21%                                                   446,36"),
    (60, 402, "Totaal incl. BTW                                        2.571,86"),
    (60, 370, "Te betalen voor 11-09-2026 o.v.v. betalingskenmerk 2026-0042"),
    (60, 356, "Bedragen in EUR. Vragen? facturen@voorbeeld.nl"),
]

ENGLISH = [
    (60, 790, "Northwind Trading Ltd."),
    (60, 776, "12 Harbour Road, London EC1A 1BB"),
    (60, 762, "VAT No: GB123456789"),
    (60, 730, "INVOICE"),
    (60, 710, "Invoice Number: INV-2026-118"),
    (60, 696, "Invoice Date: March 4, 2026"),
    (60, 682, "Due Date: April 3, 2026"),
    (60, 650, "Bill To:"),
    (60, 636, "Contoso Europe GmbH"),
    (60, 622, "Marienplatz 8, 80331 Munich"),
    (60, 580, "Description                       Qty   Unit Price      Amount"),
    (60, 562, "Consulting services                40       150.00    6,000.00"),
    (60, 548, "Onboarding workshop                 1     1,200.00    1,200.00"),
    (60, 514, "Subtotal                                              7,200.00"),
    (60, 500, "VAT 20%                                               1,440.00"),
    (60, 486, "Total Due                                        GBP 8,640.00"),
    (60, 454, "Payment reference: INV2026118"),
    (60, 440, "IBAN: GB29 NWBK 6016 1331 9268 19"),
]


# A third sample with a real ruled table, which exercises the table-extraction
# path instead of the coordinate path, and uses a US-style date.
TABLE_HEADER = [
    (60, 790, "Globex Corporation"),
    (60, 776, "1 Springfield Plaza, Springfield"),
    (60, 740, "INVOICE"),
    (60, 720, "Invoice #: GLX-7781"),
    (60, 706, "Date: 04/03/2026"),
    (60, 692, "Terms: Net 30"),
    (60, 660, "Bill To: Acme Inc."),
]
TABLE_ROWS = [
    ["Description", "Qty", "Unit Price", "VAT", "Amount"],
    ["Widget assembly", "12", "$25.00", "8%", "$300.00"],
    ["Freight", "1", "$45.00", "8%", "$45.00"],
]
TABLE_FOOTER = [
    (300, 520, "Subtotal: $345.00"),
    (300, 506, "Sales tax 8%: $27.60"),
    (300, 492, "Total Due: $372.60"),
]


def write_table_sample(name: str = "invoice_table.pdf") -> Path:
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    OUT.mkdir(exist_ok=True)
    path = OUT / name
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 10)
    for x, y, text in TABLE_HEADER + TABLE_FOOTER:
        c.drawString(x, y, text)

    table = Table(TABLE_ROWS, colWidths=[160, 40, 80, 40, 80])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
    ]))
    table.wrapOn(c, 400, 200)
    table.drawOn(c, 60, 560)
    c.showPage()
    c.save()
    return path


def write(name: str, rows) -> Path:
    OUT.mkdir(exist_ok=True)
    path = OUT / name
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 10)
    for x, y, text in rows:
        c.drawString(x, y, text)
    c.showPage()
    c.save()
    return path


def main() -> None:
    print(write("invoice_nl.pdf", DUTCH))
    print(write("invoice_en.pdf", ENGLISH))
    print(write_table_sample())


if __name__ == "__main__":
    main()
