from datetime import date

import pdfinvoice
from pdfinvoice.model import Invoice
from pdfinvoice.parser import (_assign_vat_numbers, _clean_party_name,
                               _customer_vat_numbers,
                               _find_iban, _find_vat_ids,
                               _guess_supplier_block, _items_from_text,
                               _letterhead_pieces, _looks_like_labelled_field,
                               _overlapping_segment, _segments,
                               _split_name_from_address, _strip_trailing_label)
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


def test_unprefixed_excl_btw_is_a_net_total_not_a_vat_amount():
    """A garage invoice sets its totals block without naming the total:

        Excl. BTW    2.777,75
        Totaal BTW     583,33
        Te betalen   3.361,08

    "Excl. BTW" says BTW, so it used to be read as the VAT amount, and the net
    total was then derived as gross - net: the two ended up swapped.
    """
    inv = pdfinvoice.parse(_doc(
        "Factuurnummer : 26001434\n"
        "Factuurdatum : 22-07-2026\n"
        "Excl. BTW 2.777,75\n"
        "Totaal BTW 583,33\n"
        "Te betalen € 3.361,08\n"
    ))
    assert inv.total_net == 2777.75
    assert inv.total_tax == 583.33
    assert inv.total_gross == 3361.08


def test_incl_btw_on_its_own_is_the_gross_total():
    inv = pdfinvoice.parse(_doc(
        "Factuurnummer : B1\n"
        "Factuurdatum : 01-02-2026\n"
        "Excl. 21% BTW 100,00\n"
        "Incl. BTW 121,00\n"
    ))
    assert (inv.total_net, inv.total_gross) == (100.0, 121.0)


def test_tax_rate_is_derived_when_the_invoice_never_prints_a_percent_sign():
    """A VAT summary table puts the rate in a column, without its "%"."""
    inv = pdfinvoice.parse(_doc(
        "Factuurnummer : B2\n"
        "Factuurdatum : 01-02-2026\n"
        "Btwcode % Grondslag BTW Bedrag\n"
        "1 21,00 € 2.777,75 € 583,33\n"
        "Excl. BTW 2.777,75\n"
        "Totaal BTW 583,33\n"
        "Te betalen € 3.361,08\n"
    ))
    assert inv.tax_rate == 21.0


def test_a_blended_rate_is_not_mistaken_for_a_real_one():
    """Two rates on one invoice divide out to a rate nobody charges."""
    inv = pdfinvoice.parse(_doc(
        "Factuurnummer : B3\n"
        "Factuurdatum : 01-02-2026\n"
        "Excl. BTW 1.000,00\n"
        "Totaal BTW 155,00\n"
        "Te betalen € 1.155,00\n"
    ))
    assert inv.tax_rate is None


def _positioned_doc(rows) -> Document:
    """A Document from rows of (text, x0, x1) words, with their positions."""
    from pathlib import Path

    word_rows = [
        [{"text": text, "x0": x0, "x1": x1, "top": 14.0 * index,
          "bottom": 14.0 * index + 9} for text, x0, x1 in row]
        for index, row in enumerate(rows)
    ]
    pages = ["\n".join(" ".join(text for text, _, _ in row) for row in rows)]
    return Document(path=Path("columns.pdf"), pages=pages, word_rows=[word_rows])


# The field table of a fuel card invoice: six headings, and a row of values
# under them that is one short — nothing was filled in for Debiteurnummer.
_FIELD_TABLE = [
    [("Contractnr", 25.5, 76.6), ("Debiteurnummer", 120.5, 200.4),
     ("Factuurdatum", 235.3, 301.9), ("Vervaldatum", 327.0, 386.9),
     ("Bladnr", 438.9, 470.5), ("Factuurnr", 510.4, 557.0)],
    [("5000156089", 25.5, 75.6), ("31-07-2026", 255.9, 301.9),
     ("07-08-2026", 340.6, 386.7), ("1", 453.1, 458.1), ("/", 460.5, 463.0),
     ("1", 465.5, 470.5), ("N06683352", 510.4, 556.9)],
]


def test_a_label_used_as_a_column_heading_takes_the_value_below_it():
    """The columns are right-aligned — "Factuurnr" ends at 557.0 and
    "N06683352" at 556.9 — so the value is matched on overlap, not on where
    it starts."""
    inv = pdfinvoice.parse(_positioned_doc(_FIELD_TABLE))

    assert inv.invoice_number == "N06683352"
    assert inv.invoice_date == date(2026, 7, 31)
    assert inv.due_date == date(2026, 8, 7)


def test_a_heading_with_nothing_under_it_stays_empty():
    """Debiteurnummer heads an empty column; the contract number beside it
    belongs to Contractnr and must not slide over."""
    inv = pdfinvoice.parse(_positioned_doc(_FIELD_TABLE))

    assert inv.customer_number is None


def test_a_row_of_label_and_value_pairs_is_not_a_row_of_headings():
    """"Factuurnummer : 26001434" over "Factuurdatum : 22-07-2026" — the line
    below is the next field, not this one's value."""
    inv = pdfinvoice.parse(_positioned_doc([
        [("Factuurnummer", 25, 100), (":", 104, 107), ("26001434", 112, 160)],
        [("Factuurdatum", 25, 100), (":", 104, 107), ("22-07-2026", 112, 170)],
    ]))

    assert inv.invoice_number == "26001434"
    assert inv.invoice_date == date(2026, 7, 22)


def test_one_datum_column_does_not_make_an_item_table_a_field_table():
    """An item table heads a column "Datum" and the row below opens with a
    date. Two headings are needed before a row is read as fields."""
    inv = pdfinvoice.parse(_positioned_doc([
        [("Acceptatiepunt", 26, 85), ("Stand", 158, 190), ("Datum", 207, 240),
         ("Product", 300, 345)],
        [("De Haan Almere", 26, 85), ("47,90", 158, 190),
         ("28-07-26", 207, 240), ("Euro 95", 300, 345)],
        [("Factuurdatum", 25, 100), (":", 104, 107), ("31-07-2026", 112, 170)],
    ]))

    assert inv.invoice_date == date(2026, 7, 31)


# The item table of a fuel card invoice. Its heading runs over two lines —
# "Eenh" above "Prijs" is one word, eenheidsprijs — and the quantity column
# stands in front of the product, not behind it.
_FUEL_TABLE = [
    [("V", 158.4, 164.4), ("Km", 167.2, 180.7), ("Eenh", 394.3, 415.3),
     ("Bedrag", 505.3, 536.3), ("EUR", 538.8, 557.8)],
    [("Acceptatiepunt", 25.5, 85.0), ("A", 158.4, 164.4), ("Stand", 167.2, 190.7),
     ("Datum", 206.8, 233.3), ("Eenheden", 249.2, 290.2),
     ("Product", 316.6, 347.5), ("Prijs", 394.3, 411.8), ("BTW", 436.9, 456.8),
     ("%", 461.1, 469.1), ("excl.", 516.2, 536.2), ("BTW", 538.7, 559.2)],
    [("De", 25.5, 35.7), ("Haan", 38.0, 57.1), ("Almere", 59.3, 84.6),
     ("J", 157.2, 161.2), ("28-07-26", 206.8, 238.8), ("47,90", 249.2, 269.2),
     ("Ltr", 290.6, 300.0), ("Euro", 316.6, 333.4), ("95", 335.7, 344.6),
     ("1,938", 394.3, 414.3), ("21,00", 449.1, 469.1), ("92,82", 539.2, 559.2)],
]


def test_a_quantity_column_in_front_of_the_product_is_read_as_the_quantity():
    """47,90 litres at 1,938 makes 92,82. Counted off from the right, the
    price becomes the quantity and the VAT rate becomes the price."""
    line = pdfinvoice.parse(_positioned_doc(_FUEL_TABLE)).lines[0]

    assert line.description == "Euro 95"
    assert line.quantity == 47.90
    assert line.unit_price == 1.938
    assert line.tax_rate == 21.0
    assert line.amount == 92.82


def test_a_heading_split_over_two_lines_is_read_as_one():
    """"Bedrag EUR" over "excl. BTW" heads the amount column; without the line
    above, the table has no amount column and is not read at all."""
    from pdfinvoice.parser import _match_column_header

    columns = _match_column_header(
        [{"text": t, "x0": a, "x1": b} for t, a, b in _FUEL_TABLE[1]],
        [{"text": t, "x0": a, "x1": b} for t, a, b in _FUEL_TABLE[0]],
    )

    assert columns is not None and "amount" in columns
    # "Eenh" over the price column is the other half of "eenheidsprijs", not a
    # second quantity heading: the lower line names the fields.
    assert columns["quantity"][0] < columns["description"][0]


# A list of fields, two columns wide: labels left, values right. The supplier
# is named outright, and its address is the value column continued downwards.
_NAMED_SUPPLIER = [
    [("Debiteurnummer", 45, 130), ("18288583", 132, 166)],
    [("Leverancier", 299, 344), ("KPN B.V.", 390, 424)],
    [("Factuuradres frochse@yahoo.com", 45, 208),
     ("Wilhelminakade 123", 390, 465)],
    [("Contractant FROC Holding B.V.", 45, 202),
     ("3072 AP Rotterdam", 390, 465)],
    [("Land In Zicht 9", 130, 188), ("KvK nummer", 299, 349),
     ("27124701", 390, 424)],
    [("Klantnummer", 45, 130), ("7073386584", 132, 177)],
]


def test_an_invoice_that_names_its_supplier_is_believed():
    """Which block on the page is the sender is a guess; "Leverancier" is not."""
    inv = pdfinvoice.parse(_positioned_doc(_NAMED_SUPPLIER))

    assert inv.supplier.name == "KPN B.V."
    assert inv.supplier.address == ["Wilhelminakade 123", "3072 AP Rotterdam"]


def test_the_supplier_address_ends_where_the_next_label_begins():
    """"27124701" under the address is the KvK value, and says nothing about
    itself — only the label beside it does. The line above holds someone
    else's e-mail and must not end the block either."""
    inv = pdfinvoice.parse(_positioned_doc(_NAMED_SUPPLIER))

    assert "27124701" not in " ".join(inv.supplier.address)
    assert inv.supplier.coc_number == "27124701"


def test_the_addressee_is_read_from_the_same_kind_of_label():
    """"Aan | FROC Holding B.V." is the same two-column shape as the supplier,
    and the address under it belongs to the customer."""
    inv = pdfinvoice.parse(_positioned_doc(
        [[("Aan", 45, 61), ("FROC Holding B.V.", 130, 202),
          ("Factuurdatum 16 juli 2026", 299, 433)],
         [("Land In Zicht 9", 130, 188), ("Factuurnummer 151138572", 299, 426)],
         [("1316VJ ALMERE", 130, 192)]] + _NAMED_SUPPLIER
    ))

    assert inv.customer.name == "FROC Holding B.V."
    assert inv.customer.address == ["Land In Zicht 9", "1316VJ ALMERE"]


def test_an_e_mail_address_behind_a_billing_label_is_not_the_customer():
    """KPN labels an e-mail address "Factuuradres". No company is called
    frochse@yahoo.com, so the label is passed over."""
    inv = pdfinvoice.parse(_positioned_doc(_NAMED_SUPPLIER))

    assert inv.customer.name != "frochse@yahoo.com"


def test_the_debtor_number_wins_from_the_customer_number():
    """Both are printed and they are not the same number: the debtor number is
    the account the invoice is booked against."""
    inv = pdfinvoice.parse(_positioned_doc(_NAMED_SUPPLIER))

    assert inv.customer_number == "18288583"


def test_the_grand_total_wins_from_the_subtotals_printed_above_it():
    """A fuel card bills per vehicle, so several lines say "Totaal" before the
    one that means it. Net plus VAT settles which is which."""
    inv = pdfinvoice.parse(_doc(
        "Factuurnr : N06683352\n"
        "Factuurdatum : 31-07-2026\n"
        "Totaal kenteken: 1XHK91 92,82\n"
        "Totaal contract: 5000156089 92,82\n"
        "Subtotaal EUR 92,82\n"
        "BTW 21,00% over 92,82 EUR 19,49\n"
        "TOTAAL EUR 112,31\n"
    ))
    assert (inv.total_net, inv.total_tax, inv.total_gross) == (92.82, 19.49, 112.31)
    assert not any("do not add up" in w for w in inv.warnings)


def test_a_customer_vat_heading_claims_the_number_on_the_line_below():
    """"BTW nummer klant:" is a heading with nothing after it; the number
    stands below it, beside the customer's name. Read as the supplier's, it
    swaps the two parties and books the invoice to the wrong account."""
    inv = Invoice()
    lines = [
        "BTW nummer klant:",
        "FROC Holding B.V. NL812913309B01",
        "BTW nr - NL004010462B01 KVK-/ Ondernemingsnr. - 39037382",
    ]
    _assign_vat_numbers(inv, lines, _find_vat_ids("\n".join(lines)))

    assert inv.supplier.vat_number == "NL004010462B01"
    assert inv.customer.vat_number == "NL812913309B01"


def test_a_footer_that_runs_two_register_names_together_still_yields_the_kvk():
    """"KVK-/ Ondernemingsnr. - 39037382": 21 characters of label and dashes
    stand between the word and the number."""
    inv = pdfinvoice.parse(_doc(
        "Factuurdatum : 31-07-2026\n"
        "BTW nr - NL004010462B01 KVK-/ Ondernemingsnr. - 39037382 "
        "IBAN - NL96ABNA0401863042\n"
    ))
    assert inv.supplier.coc_number == "39037382"


def test_a_register_number_behind_a_place_name_is_still_found():
    inv = pdfinvoice.parse(_doc("K.v.K. Amsterdam nr. 60895344\n"))

    assert inv.supplier.coc_number == "60895344"


def test_an_abbreviated_header_does_not_leave_its_full_stop_on_the_name():
    """"T.a.v. Frenk Ochse" — the label match stops at the "v", and the dot
    that follows used to become the first character of the name."""
    inv = pdfinvoice.parse(_doc(
        "Factuurdatum : 31-07-2026\n"
        "T.a.v. Frenk Ochse\n"
        "Land in Zicht 9\n"
        "1316VJ ALMERE\n"
    ))
    assert inv.customer.name == "Frenk Ochse"


def test_the_company_above_a_ter_attentie_van_line_is_the_customer():
    """The invoice is billed to the company; the person is who to hand it to.
    Read the other way round, Exact holds a debtor that does not exist."""
    inv = pdfinvoice.parse(_doc(
        "Factuurdatum : 31-07-2026\n"
        "FROC Holding B.V.\n"
        "T.a.v. Frenk Ochse\n"
        "Land in Zicht 9\n"
        "1316VJ ALMERE\n"
    ))
    assert inv.customer.name == "FROC Holding B.V."
    assert inv.customer.contact_name == "Frenk Ochse"
    assert inv.customer.address == ["Land in Zicht 9", "1316VJ ALMERE"]


def test_a_person_billed_directly_stays_the_customer():
    """No company above the attention line: the person is the party, and
    naming them as their own contact would be nonsense."""
    inv = pdfinvoice.parse(_doc(
        "Factuurdatum : 31-07-2026\n"
        "T.a.v. Frenk Ochse\n"
        "Land in Zicht 9\n"
    ))
    assert inv.customer.name == "Frenk Ochse"
    assert inv.customer.contact_name is None


def test_no_party_name_opens_with_punctuation():
    assert _clean_party_name(". Frenk Ochse") == "Frenk Ochse"
    assert _clean_party_name("- Voorbeeld BV") == "Voorbeeld BV"
    assert _clean_party_name("Voorbeeld B.V.") == "Voorbeeld B.V."
    assert _clean_party_name(".") is None
    assert _clean_party_name(None) is None


def test_a_customer_label_without_the_word_vat_claims_nothing():
    """"Debiteurnummer 1487" would otherwise take whatever number follows it."""
    assert _customer_vat_numbers(
        ["Debiteurnummer 1487", "BTW nr NL004010462B01"]
    ) == []


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


def _column_doc(rows) -> Document:
    """A Document whose rows carry positions, one segment per row."""
    from pathlib import Path

    words = [
        [{"text": text, "x0": x0, "x1": x0 + 6 * len(text), "top": top}]
        for text, x0, top in rows
    ]
    return Document(
        path=Path("letterhead.pdf"),
        pages=["\n".join(text for text, _, _ in rows)],
        word_rows=[words],
    )


# The letterhead of an invoice printed on scanned stationery, as OCR returns
# it: contact details around a company name that is a logo and stays unread.
_STATIONERY = [
    ("Omroepweg 15, 1324 KT Almere | Tel. 036 534 65 50", 31, 10),
    ("Iban: NL50 INGB 0006 8780 69 | Bic: INGBNL2A", 31, 22),
    ("K.V.K 77731964 | BTW Nr. NL 861114681B01", 31, 34),
    ("Info@garageroos.nl | www.garageroos.nl", 31, 46),
    ("Dhr. F. Ochse", 87, 100),
    ("Land in Zicht 9", 87, 112),
    ("1316 VJ ALMERE", 87, 124),
    ("Factuurnummer : 26001434", 25, 150),
    ("Factuurdatum : 22-07-2026", 25, 162),
]


def test_a_letterhead_without_a_name_is_still_the_supplier():
    """The company name is a logo, so OCR reads the address around it and not
    the name. Reporting that address as the supplier's is what keeps the
    addressee below it from being taken for the company."""
    inv = pdfinvoice.parse(_column_doc(_STATIONERY))

    assert inv.supplier.address == ["Omroepweg 15, 1324 KT Almere"]
    assert inv.supplier.coc_number == "77731964"
    assert inv.supplier.vat_number == "NL861114681B01"
    assert inv.supplier.iban == "NL50INGB0006878069"
    assert inv.supplier.bic == "INGBNL2A"


def test_the_addressee_is_the_customer_when_nothing_labels_it():
    """A Dutch invoice prints no "Factuuradres": the customer is the block in
    the window-envelope position, above the labelled fields."""
    inv = pdfinvoice.parse(_column_doc(_STATIONERY))

    assert inv.customer.name == "Dhr. F. Ochse"
    assert inv.customer.address == ["Land in Zicht 9", "1316 VJ ALMERE"]


def _unregistered(rows):
    """The same letterhead, under a KvK number no name file knows."""
    return [(text.replace("77731964", "99999999"), x0, top)
            for text, x0, top in rows]


def test_a_logo_only_name_stays_empty_until_the_number_is_written_down():
    """The name is drawn, not typed, and nothing left on the page is it — the
    web address only resembles it. An empty field that names the number to add
    beats a plausible guess: "garageroos.nl" is Garagebedrijf Roos B.V."""
    inv = pdfinvoice.parse(_column_doc(_unregistered(_STATIONERY)))

    assert inv.supplier.name is None
    assert any("add KvK 99999999 to the name file" in w for w in inv.warnings)


def test_a_printed_company_name_is_used_when_the_register_has_none():
    printed = [("Voorbeeld BV", 31, 10), ("www.garageroos.nl", 31, 22)]
    rows = _unregistered(printed + _STATIONERY[4:])
    inv = pdfinvoice.parse(_column_doc(rows))

    assert inv.supplier.name == "Voorbeeld BV"
    assert not any("name file" in w for w in inv.warnings)


def test_the_registered_name_beats_what_the_invoice_prints():
    """The garage trades as Roos; the register books it as Garagebedrijf Roos
    B.V., and that is the name Exact has to see."""
    inv = pdfinvoice.parse(_column_doc(_STATIONERY))

    assert inv.supplier.name == "Garagebedrijf Roos B.V."
    # Derived from the web address it is not, so it is not reported as such.
    assert not any("web address" in w for w in inv.warnings)


def test_replacing_a_printed_name_with_the_registered_one_is_reported():
    printed = [("Garage Roos", 31, 10)] + _STATIONERY[1:]
    inv = pdfinvoice.parse(_column_doc(printed))

    assert inv.supplier.name == "Garagebedrijf Roos B.V."
    assert any("replaced by the registered name" in w for w in inv.warnings)


def test_a_name_that_differs_only_in_punctuation_is_not_reported():
    printed = [("Garagebedrijf Roos BV", 31, 10)] + _STATIONERY[1:]
    inv = pdfinvoice.parse(_column_doc(printed))

    assert inv.supplier.name == "Garagebedrijf Roos B.V."
    assert not any("replaced by" in w for w in inv.warnings)


def test_letterhead_contact_details_are_not_part_of_the_address():
    assert _letterhead_pieces(
        "Omroepweg 15, 1324 KT Almere | Tel. 036 534 65 50"
    ) == ["Omroepweg 15, 1324 KT Almere"]
    assert _letterhead_pieces("Iban: NL50 INGB 0006 8780 69 | Bic: INGBNL2A") == []
    assert _letterhead_pieces("Voorbeeld BV") == ["Voorbeeld BV"]


def test_an_ocr_misread_iban_is_repaired_when_the_checksum_settles_it():
    """Tesseract reads the check digits of "NL50 INGB ..." as letters."""
    assert _find_iban("Iban: NLSO INGB 0006 8780 69") == "NL50INGB0006878069"
    # Nothing a swap can make valid: left unread rather than guessed at.
    assert _find_iban("Iban: NLXX QQQQ 1111 2222 33") is None
    assert _find_iban("IBAN NL91 ABNA 0417 1643 00") == "NL91ABNA0417164300"


def test_a_second_column_label_ends_the_value():
    """"Factuurnummer : 26001434 Kenteken : 1-XHK-91" is two columns, and the
    gap between them does not always survive text extraction."""
    assert _strip_trailing_label("26001434 Kenteken : 1-XHK-91") == "26001434"
    assert _strip_trailing_label("1487 APK geldig tot : 22 aug 2027") == "1487"
    assert _strip_trailing_label("26001434") == "26001434"


def test_a_labelled_field_is_not_a_company_name():
    assert _looks_like_labelled_field("Date of issue June 17, 2026")
    assert _looks_like_labelled_field("Invoice number ORF67LFJ0002")
    assert not _looks_like_labelled_field("Anthropic, PBC")
    assert not _looks_like_labelled_field("Elasticsearch BV")
