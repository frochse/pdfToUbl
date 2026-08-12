from datetime import date

import pdfinvoice
from pdfinvoice.model import Invoice
from pdfinvoice.parser import (_assign_vat_numbers, _find_vat_ids,
                               _guess_supplier_block, _items_from_text,
                               _looks_like_labelled_field, _overlapping_segment,
                               _segments, _split_name_from_address)
from pdfinvoice.textio import Document


def test_dutch_invoice(nl):
    inv = pdfinvoice.read(nl)

    assert inv.invoice_number == "2026-0042"
    assert inv.invoice_date == date(2026, 8, 12)
    assert inv.due_date == date(2026, 9, 11)
    assert inv.order_number == "PO-99120"
    assert inv.customer_number == "KL-1187"
    assert inv.currency == "EUR"

    assert inv.supplier.name == "Axual B.V."
    assert inv.supplier.vat_number == "NL854103576B01"
    assert inv.supplier.coc_number == "60895344"
    assert inv.supplier.iban == "NL91ABNA0417164300"
    assert inv.supplier.bic == "ABNANL2A"
    assert inv.customer.name == "Voorbeeld Holding B.V."
    assert "1000 AA Amsterdam" in inv.customer.address

    assert (inv.total_net, inv.total_tax, inv.total_gross) == (2125.50, 446.36, 2571.86)
    assert inv.tax_rate == 21.0

    assert [line.description for line in inv.lines] == [
        "Kafka support uren",
        "Streaming platform licentie",
        "Reiskosten",
    ]
    assert inv.lines[0].quantity == 10
    assert inv.lines[0].unit_price == 125.0
    assert inv.lines[0].amount == 1250.0
    assert inv.warnings == []


def test_english_invoice(en):
    inv = pdfinvoice.read(en)

    assert inv.invoice_number == "INV-2026-118"
    assert inv.invoice_date == date(2026, 3, 4)
    assert inv.due_date == date(2026, 4, 3)
    assert inv.currency == "GBP"
    assert inv.customer.name == "Contoso Europe GmbH"
    assert (inv.total_net, inv.total_tax, inv.total_gross) == (7200.0, 1440.0, 8640.0)
    assert inv.lines[1].quantity == 1
    assert inv.lines[1].unit_price == 1200.0
    assert inv.warnings == []


def test_ruled_table_invoice(ruled):
    inv = pdfinvoice.read(ruled, day_first=False)

    assert inv.invoice_number == "GLX-7781"
    assert inv.invoice_date == date(2026, 4, 3)
    assert inv.currency == "USD"
    # "Terms: Net 30" must not be read as a net total.
    assert inv.total_net == 345.0
    assert inv.total_tax == 27.60
    assert inv.total_gross == 372.60
    assert [line.amount for line in inv.lines] == [300.0, 45.0]
    assert inv.lines[0].tax_rate == 8.0
    assert inv.warnings == []


def _doc(text: str) -> Document:
    from pathlib import Path

    return Document(path=Path("memory.pdf"), pages=[text])


def test_text_fallback_reads_item_lines():
    items = _items_from_text(_doc(
        "Description Qty Price Amount\n"
        "Widget 3 10,00 30,00\n"
        "Gadget 1 5,50 5,50\n"
        "Total 35,50\n"
    ))
    assert [i.description for i in items] == ["Widget", "Gadget"]
    assert items[0].quantity == 3
    assert items[0].amount == 30.0


def test_reconcile_derives_missing_total_and_warns():
    inv = pdfinvoice.parse(_doc(
        "Invoice number: A1\n"
        "Invoice date: 01-02-2026\n"
        "Subtotal 100,00\n"
        "VAT 21% 21,00\n"
    ))
    assert inv.total_gross == 121.0          # derived from net + tax
    assert "no invoice lines detected" in inv.warnings


def test_inconsistent_totals_are_flagged():
    inv = pdfinvoice.parse(_doc(
        "Invoice number: A2\n"
        "Invoice date: 01-02-2026\n"
        "Total excl. VAT 100,00\n"
        "VAT 21,00\n"
        "Total incl. VAT 130,00\n"
    ))
    assert any("do not add up" in w for w in inv.warnings)


def test_vat_number_is_found_next_to_a_worded_label():
    """Space-stripping the whole document hid "VAT number NL123456789B01":
    the number no longer began at a word boundary."""
    assert _find_vat_ids("VAT number NL123456789B01") == ["NL123456789B01"]
    assert _find_vat_ids("BTW NL854103576B01") == ["NL854103576B01"]
    # Printed in groups, which is why the spaces were being stripped at all.
    assert _find_vat_ids("BTW/VAT No. NL 9876 5432 1B01") == ["NL987654321B01"]


def test_letterhead_prose_does_not_crowd_out_the_address():
    page = "\n".join([
        "Voorbeeld Diensten S.A.",
        "a corporation organized under the laws of the Republic of Panama",
        "with the registered operations in the Netherlands",
        "Voorbeeldkade 115",
        "1076 EE Amsterdam",
        "the Netherlands",
    ])
    name, address = _guess_supplier_block(page)

    assert name == "Voorbeeld Diensten S.A."
    assert address == ["Voorbeeldkade 115", "1076 EE Amsterdam",
                       "the Netherlands"]


def test_name_and_address_on_one_line_are_split():
    name, address, coc = _split_name_from_address(
        ["Jan Jansen Voorbeeld BV Voorbeeldstraat 22, 1234AB Utrecht 12345678"])

    assert name == "Jan Jansen Voorbeeld BV"
    assert address == ["Voorbeeldstraat 22, 1234AB Utrecht"]
    assert coc == "12345678"


def _word(text, x0, x1):
    return {"text": text, "x0": x0, "x1": x1, "top": 0}


def test_columns_are_split_at_the_gap_between_them():
    row = [_word("Date", 48, 70), _word("Billed", 215, 235), _word("to", 237, 246)]
    assert _segments(row) == [(48, 70, "Date"), (215, 246, "Billed to")]


def test_a_right_aligned_letterhead_still_matches_its_column():
    """Each line of a flush-right block starts at a different x, so columns are
    matched by overlap rather than by where they begin."""
    span = (505, 545)  # the letterhead
    prose = [_word("a", 353, 358), _word("corporation", 358, 400)]
    street = [_word("Voorbeeldkade", 476, 494), _word("115", 494, 534)]
    far_left = [_word("Invoice", 45, 80)]

    assert _overlapping_segment(prose, span) is None
    assert _overlapping_segment(street, span) == "Voorbeeldkade 115"
    assert _overlapping_segment(far_left, span) is None


def test_customer_vat_number_is_not_given_to_the_supplier():
    """"Customer VAT/TAX No: ..." would otherwise be read as the supplier's and
    point the import at the wrong account."""
    inv = Invoice()
    lines = ["BTW/VAT No. NL 9876 5432 1B01", "Customer VAT/TAX No: NL111222333B01"]
    _assign_vat_numbers(inv, lines, ["NL987654321B01", "NL111222333B01"])

    assert inv.supplier.vat_number == "NL987654321B01"
    assert inv.customer.vat_number == "NL111222333B01"


def test_a_labelled_field_is_not_a_company_name():
    assert _looks_like_labelled_field("Date of issue June 17, 2026")
    assert _looks_like_labelled_field("Invoice number ORF67LFJ0002")
    assert not _looks_like_labelled_field("Anthropic, PBC")
    assert not _looks_like_labelled_field("Elasticsearch BV")
