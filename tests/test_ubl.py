from xml.dom import minidom
from xml.etree import ElementTree as ET

import pdfinvoice
from pdfinvoice import ubl
from pdfinvoice.model import Invoice, LineItem, Party
from pdfinvoice.textio import extract

INVOICE = "{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}"
CREDIT_NOTE = "{urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2}"
CAC = "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}"
CBC = "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}"


def _invoice(**kw) -> Invoice:
    base = dict(
        invoice_number="INV-1",
        supplier=Party(name="Acme B.V.", vat_number="NL854103576B01",
                       coc_number="60895344", iban="NL91ABNA0417164300",
                       address=["Kanaalweg 17L", "3526 KL Utrecht"]),
        customer=Party(name="Klant B.V."),
        customer_number="KL-1187",
        lines=[LineItem(description="Widget", quantity=2, unit_price=5.0,
                        tax_rate=21.0, amount=10.0)],
        total_net=10.0, total_tax=2.1, total_gross=12.1,
    )
    base.update(kw)
    return Invoice(**base)


# --- NLCIUS identification --------------------------------------------------


def test_document_declares_nlcius_and_omits_ubl_version():
    root = ET.fromstring(ubl.to_xml(_invoice()))

    assert root.findtext(f"{CBC}CustomizationID") == (
        "urn:cen.eu:en16931:2017#compliant#urn:fdc:nen.nl:nlcius:v1.0"
    )
    assert root.findtext(f"{CBC}ProfileID") == (
        "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
    )
    # UBL-CR-002: absent, or exactly 2.1. The NLCIUS examples omit it.
    assert root.find(f"{CBC}UBLVersionID") is None


def test_supplier_coc_number_carries_the_kvk_scheme():
    """BR-NL-1: a Dutch supplier needs a KvK or OIN, and the scheme says which."""
    root = ET.fromstring(ubl.to_xml(_invoice()))

    entity = root.find(f"{CAC}AccountingSupplierParty/{CAC}Party/{CAC}PartyLegalEntity")
    company = entity.find(f"{CBC}CompanyID")
    assert company.text == "60895344"
    assert company.get("schemeID") == "0106"


def test_address_is_split_into_street_city_and_postcode():
    """BR-NL-3 wants all three as separate elements, not one address block."""
    address = ET.fromstring(ubl.to_xml(_invoice())).find(
        f"{CAC}AccountingSupplierParty/{CAC}Party/{CAC}PostalAddress")

    assert address.findtext(f"{CBC}StreetName") == "Kanaalweg 17L"
    assert address.findtext(f"{CBC}CityName") == "Utrecht"
    assert address.findtext(f"{CBC}PostalZone") == "3526 KL"
    assert address.findtext(f"{CAC}Country/{CBC}IdentificationCode") == "NL"


def test_buyer_reference_falls_back_when_the_invoice_has_none():
    """BR-NL-2 needs a buyer or order reference; neither is always printed."""
    assert ET.fromstring(ubl.to_xml(_invoice())).findtext(
        f"{CBC}BuyerReference") == "KL-1187"

    bare = _invoice(customer_number=None, order_number=None)
    assert ET.fromstring(ubl.to_xml(bare)).findtext(f"{CBC}BuyerReference") == "NA"
    assert any("BR-NL-2" in w for w in ubl.conformance_warnings(bare))


def test_payment_means_is_present_whenever_something_is_payable():
    """BR-NL-11, which plain EN 16931 does not require."""
    no_bank = _invoice(supplier=Party(name="Acme B.V."))
    means = ET.fromstring(ubl.to_xml(no_bank)).find(f"{CAC}PaymentMeans")

    assert means is not None
    assert means.findtext(f"{CBC}PaymentMeansCode") == "30"


def test_all_four_monetary_totals_are_present():
    """BR-12 to BR-15; a missing one is filled from the totals we do have."""
    total = ET.fromstring(ubl.to_xml(_invoice(total_net=None))).find(
        f"{CAC}LegalMonetaryTotal")

    assert total.findtext(f"{CBC}LineExtensionAmount") == "10.00"
    assert total.findtext(f"{CBC}TaxExclusiveAmount") == "10.00"
    assert total.findtext(f"{CBC}TaxInclusiveAmount") == "12.10"
    assert total.findtext(f"{CBC}PayableAmount") == "12.10"


# --- credit notes -----------------------------------------------------------


def test_credit_note_uses_its_own_root_and_positive_amounts():
    """BR-NL-8. Exact negates a 381 sent in an Invoice root, so this matters."""
    credit = _invoice(total_net=-10.0, total_tax=-2.1, total_gross=-12.1,
                      lines=[LineItem(description="Refund", quantity=1,
                                      unit_price=-10.0, tax_rate=21.0,
                                      amount=-10.0)])
    xml = ubl.to_xml(credit)
    root = ET.fromstring(xml)

    assert root.tag == f"{CREDIT_NOTE}CreditNote"
    assert root.findtext(f"{CBC}CreditNoteTypeCode") == "381"
    assert root.find(f"{CBC}InvoiceTypeCode") is None
    # CreditNoteType has no DueDate element at all.
    assert root.find(f"{CBC}DueDate") is None

    line = root.find(f"{CAC}CreditNoteLine")
    assert line.find(f"{CBC}CreditedQuantity") is not None
    assert root.findtext(f"{CAC}LegalMonetaryTotal/{CBC}PayableAmount") == "12.10"


# --- attachment -------------------------------------------------------------


def test_source_pdf_is_embedded_when_given():
    """Exact shows no invoice image for a document with no attachment."""
    xml = ubl.to_xml(_invoice(), pdf_bytes=b"%PDF-1.4 fake", pdf_name="in.pdf")
    reference = ET.fromstring(xml).find(f"{CAC}AdditionalDocumentReference")

    assert reference.findtext(f"{CBC}ID") == "in.pdf"
    binary = reference.find(f"{CAC}Attachment/{CBC}EmbeddedDocumentBinaryObject")
    assert binary.get("mimeCode") == "application/pdf"
    assert binary.get("filename") == "in.pdf"

    import base64
    assert base64.b64decode(binary.text) == b"%PDF-1.4 fake"


def test_no_attachment_element_without_a_pdf():
    """SI-UBL-2: the document must not carry empty elements."""
    root = ET.fromstring(ubl.to_xml(_invoice()))
    assert root.find(f"{CAC}AdditionalDocumentReference") is None


# --- character handling -----------------------------------------------------


def test_control_characters_do_not_break_the_document():
    """A PDF text layer can carry NUL and form feeds; XML 1.0 forbids them.

    Emitting one produced a document that minidom refused to read back, which
    surfaced as a 500 from the web API.
    """
    inv = _invoice(invoice_number="ORF67LFJ\x000002")

    xml = ubl.to_xml(inv)
    minidom.parseString(xml)  # must round-trip

    assert ET.fromstring(xml).findtext(f"{CBC}ID") == "ORF67LFJ0002"


def test_markup_characters_are_escaped_not_stripped():
    inv = _invoice(customer=Party(name="Smith & Sons <Ltd>"))

    xml = ubl.to_xml(inv)
    assert "&amp;" in xml and "&lt;Ltd&gt;" in xml
    assert "Smith & Sons <Ltd>" in [e.text for e in ET.fromstring(xml).iter()]


# --- the real samples -------------------------------------------------------


def test_real_samples_produce_readable_ubl(samples):
    for pdf in sorted(samples.glob("*.pdf")):
        if not extract(pdf, ocr="never").text.strip():
            continue  # a scan; nothing to render without OCR
        minidom.parseString(ubl.to_xml(pdfinvoice.read(pdf)))


# --- lines that do not add up ----------------------------------------------


def test_lines_are_sent_when_they_agree_with_the_total():
    inv = _invoice()
    assert ubl.document_lines(inv) == inv.lines


def test_lines_that_do_not_add_up_are_sent_as_one_line():
    """A grouped specification prints its subtotals as rows of their own, so
    the rows read off the page can add up to more than the invoice charges.
    BR-CO-10 would reject that, and Exact with it."""
    inv = _invoice(lines=[
        LineItem(description="Werk", quantity=1, unit_price=33.75,
                 tax_rate=21.0, amount=33.75),
        LineItem(description="Subtotaal", quantity=1, unit_price=33.75,
                 tax_rate=21.0, amount=33.75),
    ], total_net=33.75, total_tax=7.09, total_gross=40.84)

    lines = ubl.document_lines(inv)
    assert len(lines) == 1
    assert lines[0].amount == 33.75
    assert lines[0].description == ubl.SUMMARY_LINE_NAME

    # The document Exact receives has to add up: BR-CO-10.
    root = ET.fromstring(ubl.to_xml(inv))
    total = float(root.findtext(
        f"{CAC}LegalMonetaryTotal/{CBC}LineExtensionAmount"))
    line_sum = sum(
        float(line.findtext(f"{CBC}LineExtensionAmount"))
        for line in root.findall(f"{CAC}InvoiceLine")
    )
    assert abs(line_sum - total) < 0.005
