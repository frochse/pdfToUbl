"""Render a parsed Invoice as SI-UBL 2.0 (NLCIUS), for import into Exact.

NLCIUS is the Dutch CIUS of EN 16931. Exact Online accepts it on the Peppol
path, and Exact Globe Next imports it directly. It is stricter than plain UBL
2.1 in ways that matter here:

- the supplier's legal entity needs a KvK or OIN number (BR-NL-1),
- supplier and NL customer addresses need street, city and postcode
  (BR-NL-3, BR-NL-4),
- one of BuyerReference / OrderReference must be present (BR-NL-2),
- PaymentMeans is required once anything is payable (BR-NL-11),
- a credit note uses the CreditNote root, not InvoiceTypeCode 381 (BR-NL-8).
  Exact multiplies a 381 in an Invoice root by -1, so getting this wrong
  negates the document twice.

A PDF is a lossy source, so a document that misses one of those cannot always
be built. `conformance_warnings` reports what is missing rather than emitting
something Exact will silently reject.

Element order in UBL is significant — the schema uses xsd:sequence — and the
order below follows UBL-Invoice-2.1.xsd and UBL-CreditNote-2.1.xsd.
"""

from __future__ import annotations

import base64
import re
from typing import Dict, List, Optional, Tuple
from xml.dom import minidom
from xml.etree import ElementTree as ET

from .model import Invoice, LineItem, Party

INVOICE_NS = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CREDIT_NOTE_NS = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
CAC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

ET.register_namespace("cac", CAC_NS)
ET.register_namespace("cbc", CBC_NS)

# The CIUS this document claims to follow, and the transmission community it is
# meant for. NLCIUS calls ProfileID discouraged "unless the transmission
# community demands a value"; Peppol is that community, and every official
# NLCIUS example carries the Peppol value, so it is always emitted.
CUSTOMIZATION_ID = "urn:cen.eu:en16931:2017#compliant#urn:fdc:nen.nl:nlcius:v1.0"
PROFILE_ID = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"

# 380 = commercial invoice, 381 = credit note (UN/ECE 1001).
INVOICE_TYPE_CODE = "380"
CREDIT_NOTE_TYPE_CODE = "381"
# 30 = credit transfer (UN/ECE 4461); what an IBAN on an invoice means.
PAYMENT_MEANS_CREDIT_TRANSFER = "30"
# C62 = "one" (UN/ECE Recommendation 20), the unit to use when the invoice
# does not say what it is counting.
DEFAULT_UNIT_CODE = "C62"
# Peppol EAS scheme for a Dutch Chamber of Commerce number. 0190 is the OIN,
# used by government bodies; a PDF never tells us which, and KvK is the common
# case for a supplier.
KVK_SCHEME_ID = "0106"
# BR-NL-2 wants a reference even when the invoice carries none.
NO_REFERENCE = "NA"

_NL_POSTCODE = re.compile(r"^(\d{4}\s?[A-Z]{2})\s+(.+)$")
_GENERIC_POSTCODE = re.compile(r"^(\d{4,6})\s+(.+)$")

# A letterhead prints the country as a word on its own line. UBL wants the ISO
# code in cac:Country, and that line is not part of the street.
_COUNTRY_NAMES = {
    "netherlands": "NL", "nederland": "NL", "holland": "NL",
    "belgium": "BE", "belgie": "BE", "belgië": "BE", "belgique": "BE",
    "germany": "DE", "deutschland": "DE", "duitsland": "DE",
    "france": "FR", "frankrijk": "FR",
    "united kingdom": "GB", "great britain": "GB", "england": "GB",
    "united states": "US", "united states of america": "US", "usa": "US",
    "ireland": "IE", "luxembourg": "LU", "luxemburg": "LU",
    "spain": "ES", "spanje": "ES", "italy": "IT", "italie": "IT",
    "austria": "AT", "oostenrijk": "AT", "poland": "PL", "polen": "PL",
    "sweden": "SE", "zweden": "SE", "denmark": "DK", "denemarken": "DK",
    "portugal": "PT", "switzerland": "CH", "zwitserland": "CH",
}


def _country_name_code(line: str) -> Optional[str]:
    return _COUNTRY_NAMES.get(re.sub(r"^the\s+", "", line.strip().lower()))

# XML 1.0 (§2.2) allows only these characters. ElementTree does not enforce it,
# so a form feed between pages or a stray \x02 from a broken encoding — both
# ordinary in a PDF text layer — would otherwise produce a document that no
# parser, including our own pretty-printer, will read back.
_ILLEGAL_XML = re.compile(
    "[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd"
    "\U00010000-\U0010ffff]"
)


def to_xml(
    inv: Invoice,
    default_currency: str = "EUR",
    pretty: bool = True,
    pdf_bytes: Optional[bytes] = None,
    pdf_name: Optional[str] = None,
) -> str:
    """Serialise `inv` as an SI-UBL 2.0 Invoice or CreditNote document.

    `pdf_bytes` embeds the source PDF as an attachment. Exact will not show an
    invoice image for a document that carries none.
    """
    root = build(inv, default_currency=default_currency,
                 pdf_bytes=pdf_bytes, pdf_name=pdf_name)
    # The default namespace differs between the two document types, and
    # register_namespace is global, so it is set per call rather than at import.
    # tostring's default_namespace argument cannot be used instead: UBL's
    # attributes (currencyID, unitCode) are unqualified and it rejects those.
    ET.register_namespace("", CREDIT_NOTE_NS if _is_credit_note(inv) else INVOICE_NS)
    xml = ET.tostring(root, encoding="unicode")
    if pretty:
        xml = minidom.parseString(xml).toprettyxml(indent="  ")
        xml = "\n".join(line for line in xml.splitlines() if line.strip())
        return xml + "\n"
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n"


def build(
    inv: Invoice,
    default_currency: str = "EUR",
    pdf_bytes: Optional[bytes] = None,
    pdf_name: Optional[str] = None,
) -> ET.Element:
    currency = inv.currency or default_currency
    credit = _is_credit_note(inv)
    namespace = CREDIT_NOTE_NS if credit else INVOICE_NS
    root = ET.Element(ET.QName(namespace, "CreditNote" if credit else "Invoice"))

    # UBLVersionID is omitted: UBL-CR-002 asks for it to be absent or exactly
    # "2.1", and absent is what the NLCIUS examples do.
    _cbc(root, "CustomizationID", CUSTOMIZATION_ID)
    _cbc(root, "ProfileID", PROFILE_ID)
    _cbc(root, "ID", inv.invoice_number or "UNKNOWN")
    if inv.invoice_date:
        _cbc(root, "IssueDate", inv.invoice_date.isoformat())
    # CreditNoteType has no DueDate element at all.
    if inv.due_date and not credit:
        _cbc(root, "DueDate", inv.due_date.isoformat())
    _cbc(root, "CreditNoteTypeCode" if credit else "InvoiceTypeCode",
         CREDIT_NOTE_TYPE_CODE if credit else INVOICE_TYPE_CODE)
    _cbc(root, "DocumentCurrencyCode", currency)
    # Exact maps OrderReference to "not used" on the purchase side, so the
    # reference has to travel in BuyerReference to survive the import.
    _cbc(root, "BuyerReference", inv.customer_number or NO_REFERENCE)
    if inv.order_number:
        _cbc(_cac(root, "OrderReference"), "ID", inv.order_number)
    if pdf_bytes:
        _attachment(root, pdf_bytes, pdf_name or "invoice.pdf")

    lines = document_lines(inv)

    _party(root, "AccountingSupplierParty", inv.supplier)
    _party(root, "AccountingCustomerParty", inv.customer)
    _payment_means(root, inv)
    _tax_total(root, inv, lines, currency, credit)
    _monetary_total(root, inv, lines, currency, credit)

    for number, line in enumerate(lines, start=1):
        _line(root, number, line, inv, currency, credit)

    return root


# What a collapsed document is called on the Exact side. The specification the
# amount came from travels with the invoice as the embedded PDF.
SUMMARY_LINE_NAME = "Verrichte werkzaamheden volgens specificatie"


def document_lines(inv: Invoice) -> List[LineItem]:
    """The lines to send, which are not always the lines that were read.

    BR-CO-10 requires the line amounts to sum to LineExtensionAmount, and Exact
    rejects a document where they do not. A grouped specification prints its
    subtotals as rows of their own, so the rows read off the page can add up to
    more than the invoice charges — the invoice is right and the reading is
    incomplete. Rather than send a document that contradicts itself, the whole
    amount goes out as one line and the PDF carries the detail.
    """
    amounts = [line.amount for line in inv.lines]
    if not inv.lines or inv.total_net is None or any(a is None for a in amounts):
        return inv.lines
    if abs(sum(amounts) - inv.total_net) <= 0.02:
        return inv.lines

    return [
        LineItem(
            description=SUMMARY_LINE_NAME,
            quantity=1,
            unit_price=inv.total_net,
            tax_rate=inv.tax_rate,
            amount=inv.total_net,
        )
    ]


def _is_credit_note(inv: Invoice) -> bool:
    return inv.total_gross is not None and inv.total_gross < 0


def _abs(value: Optional[float], credit: bool) -> Optional[float]:
    """Credit notes carry positive amounts; the document type is the sign."""
    if value is None:
        return None
    return abs(value) if credit else value


# --- conformance ------------------------------------------------------------


def postal_address(party: Party) -> Dict[str, Optional[str]]:
    """The address as the document will carry it, field by field.

    What ends up in StreetName / CityName / PostalZone is what BR-NL-3 and
    BR-NL-4 are judged on, and it is not always obvious from the printed lines.
    """
    street, city, postcode, extra = _split_address(party.address)
    return {
        "street": street,
        "additional_street": extra[0] if extra else None,
        "city": city,
        "postcode": postcode,
        "country": _country_of(party),
        "lines": list(party.address),
    }


def conformance_warnings(inv: Invoice, default_currency: str = "EUR") -> List[str]:
    """NLCIUS rules this invoice does not satisfy.

    Each entry names the rule so it can be looked up in the Dutch Peppol
    Authority's schematron, which is the thing that will actually reject it.
    """
    warnings: List[str] = []

    if not inv.invoice_number:
        warnings.append("BR-02: no invoice number was found")
    if not inv.invoice_date:
        warnings.append("BR-03: no invoice date was found")

    supplier, customer = inv.supplier, inv.customer
    if not supplier.name:
        warnings.append("BR-06: no supplier name was found")
    if not customer.name:
        warnings.append("BR-07: no customer name was found")

    if _country_of(supplier) == "NL" and not supplier.coc_number:
        warnings.append(
            "BR-NL-1: a Dutch supplier needs a KvK or OIN number; none was found"
        )
    if not supplier.vat_number:
        warnings.append("BR-S-02: no supplier VAT number was found")

    for role, party in (("supplier", supplier), ("customer", customer)):
        rule = "BR-NL-3" if role == "supplier" else "BR-NL-4"
        if role == "customer" and _country_of(customer) != "NL":
            continue
        street, city, postcode, _ = _split_address(party.address)
        missing = [n for n, v in
                   (("street", street), ("city", city), ("postcode", postcode))
                   if not v]
        if missing:
            warnings.append(
                f"{rule}: {role} address is missing {', '.join(missing)}"
            )
        if not _country_of(party):
            warnings.append(f"BR-09/BR-11: no country code for the {role}")

    if not inv.customer_number and not inv.order_number:
        warnings.append(
            f"BR-NL-2: no buyer or order reference; sent {NO_REFERENCE!r} instead"
        )

    if inv.total_gross is None:
        warnings.append("BR-15: no payable total was found")
    if inv.total_net is None:
        warnings.append("BR-13: no net total was found")
    if inv.total_tax is None:
        warnings.append("BR-CO-14: no VAT total was found")
    if not inv.lines:
        warnings.append("BR-16: no invoice lines were found")

    return warnings


# --- parties ----------------------------------------------------------------


def _party(root: ET.Element, wrapper: str, party: Party) -> None:
    if not any(
        [party.name, party.address, party.vat_number, party.coc_number, party.email]
    ):
        return

    node = _cac(_cac(root, wrapper), "Party")
    if party.name:
        _cbc(_cac(node, "PartyName"), "Name", party.name)
    _postal_address(node, party)
    if party.vat_number:
        scheme = _cac(node, "PartyTaxScheme")
        _cbc(scheme, "CompanyID", party.vat_number)
        _cbc(_cac(scheme, "TaxScheme"), "ID", "VAT")
    if party.name or party.coc_number:
        entity = _cac(node, "PartyLegalEntity")
        if party.name:
            _cbc(entity, "RegistrationName", party.name)
        if party.coc_number:
            # BR-NL-1 requires the scheme to say which register this is.
            _cbc(entity, "CompanyID", party.coc_number).set(
                "schemeID", KVK_SCHEME_ID
            )
    if party.email:
        _cbc(_cac(node, "Contact"), "ElectronicMail", party.email)


def _postal_address(party_node: ET.Element, party: Party) -> None:
    country = _country_of(party)
    if not party.address and not country:
        return

    address = _cac(party_node, "PostalAddress")
    street, city, postcode, extra = _split_address(party.address)
    if street:
        _cbc(address, "StreetName", street)
    # BR-NL-28 discourages AddressLine, so anything left over goes in the one
    # extra street line the schema offers and the rest is dropped.
    if extra:
        _cbc(address, "AdditionalStreetName", extra[0])
    if city:
        _cbc(address, "CityName", city)
    if postcode:
        _cbc(address, "PostalZone", postcode)
    if country:
        _cbc(_cac(address, "Country"), "IdentificationCode", country)


def _split_address(lines: List[str]) -> Tuple[
    Optional[str], Optional[str], Optional[str], List[str]
]:
    """Split free-form address lines into street / city / postcode / rest."""
    street: Optional[str] = None
    city: Optional[str] = None
    postcode: Optional[str] = None
    extra: List[str] = []

    # "Kanaalweg 17L, 3526 KL Utrecht" is one printed line but two fields.
    parts: List[str] = []
    for line in lines:
        parts.extend(piece.strip() for piece in line.split(",") if piece.strip())

    for line in parts:
        match = _NL_POSTCODE.match(line) or _GENERIC_POSTCODE.match(line)
        if _country_name_code(line):
            continue  # belongs in cac:Country, not in the street
        if match and city is None:
            postcode, city = match.group(1), match.group(2)
        elif street is None:
            street = line
        else:
            extra.append(line)
    return street, city, postcode, extra


def _country_of(party: Party) -> Optional[str]:
    """ISO country code, from the VAT number, IBAN prefix or a Dutch postcode."""
    for value in (party.vat_number, party.iban):
        if value and len(value) >= 2 and value[:2].isalpha():
            code = value[:2].upper()
            return "GR" if code == "EL" else code  # EL is the VAT prefix for Greece
    for line in party.address:
        for piece in line.split(","):
            code = _country_name_code(piece)
            if code:
                return code
            if _NL_POSTCODE.match(piece.strip()):
                return "NL"
    return None


def _payment_means(root: ET.Element, inv: Invoice) -> None:
    # BR-NL-11 requires PaymentMeans whenever there is something to pay, even
    # when the invoice never printed an account number.
    payable = inv.total_gross is not None and inv.total_gross > 0
    if not payable and not inv.supplier.iban and not inv.payment_reference:
        return

    means = _cac(root, "PaymentMeans")
    _cbc(means, "PaymentMeansCode", PAYMENT_MEANS_CREDIT_TRANSFER)
    if inv.payment_reference:
        _cbc(means, "PaymentID", inv.payment_reference)
    if inv.supplier.iban:
        account = _cac(means, "PayeeFinancialAccount")
        _cbc(account, "ID", inv.supplier.iban)
        if inv.supplier.bic:
            branch = _cac(account, "FinancialInstitutionBranch")
            _cbc(branch, "ID", inv.supplier.bic)


def _attachment(root: ET.Element, pdf_bytes: bytes, name: str) -> None:
    """Embed the source PDF, which is how Exact shows the invoice image."""
    reference = _cac(root, "AdditionalDocumentReference")
    _cbc(reference, "ID", name)
    binary = _cbc(
        _cac(reference, "Attachment"),
        "EmbeddedDocumentBinaryObject",
        base64.b64encode(pdf_bytes).decode("ascii"),
    )
    binary.set("mimeCode", "application/pdf")
    binary.set("filename", name)


# --- money ------------------------------------------------------------------


def _tax_total(root: ET.Element, inv: Invoice, lines: List[LineItem],
               currency: str, credit: bool) -> None:
    if inv.total_tax is None:
        return

    total = _cac(root, "TaxTotal")
    _amount(total, "TaxAmount", _abs(inv.total_tax, credit), currency)

    for rate, (taxable, tax) in _tax_breakdown(inv, lines).items():
        subtotal = _cac(total, "TaxSubtotal")
        _amount(subtotal, "TaxableAmount", _abs(taxable, credit), currency)
        _amount(subtotal, "TaxAmount", _abs(tax, credit), currency)
        _tax_category(_cac(subtotal, "TaxCategory"), rate)


def _tax_breakdown(inv: Invoice, lines: List[LineItem]) -> Dict[Optional[float], tuple]:
    """Taxable base and tax amount per VAT rate.

    Uses the per-line rates when the lines carry them, and falls back to a
    single subtotal built from the document totals.
    """
    per_rate: Dict[Optional[float], float] = {}
    for line in lines:
        if line.tax_rate is None or line.amount is None:
            per_rate.clear()
            break
        per_rate[line.tax_rate] = per_rate.get(line.tax_rate, 0.0) + line.amount

    if per_rate and inv.total_tax is not None:
        breakdown = {
            rate: (round(base, 2), round(base * rate / 100, 2))
            for rate, base in per_rate.items()
        }
        # Keep the document total authoritative: put any rounding difference
        # on the largest rate group so the subtotals still sum to TaxAmount.
        drift = round(inv.total_tax - sum(tax for _, tax in breakdown.values()), 2)
        if drift:
            rate = max(breakdown, key=lambda r: breakdown[r][0])
            base, tax = breakdown[rate]
            breakdown[rate] = (base, round(tax + drift, 2))
        return breakdown

    taxable = inv.total_net if inv.total_net is not None else 0.0
    return {inv.tax_rate: (taxable, inv.total_tax or 0.0)}


def _tax_category(node: ET.Element, rate: Optional[float]) -> None:
    # S = standard rate, Z = zero rated (UN/ECE 5305).
    _cbc(node, "ID", "Z" if rate == 0 else "S")
    if rate is not None:
        _cbc(node, "Percent", _decimal(rate))
    _cbc(_cac(node, "TaxScheme"), "ID", "VAT")


def _monetary_total(
    root: ET.Element, inv: Invoice, lines: List[LineItem],
    currency: str, credit: bool
) -> None:
    line_total = inv.total_net
    if lines and all(line.amount is not None for line in lines):
        line_total = round(sum(line.amount for line in lines), 2)

    # BR-12 to BR-15 make all four mandatory, and BR-CO-13/15/16 tie them
    # together, so a missing one is filled from whichever total we do have
    # rather than left out and rejected.
    net = inv.total_net if inv.total_net is not None else line_total
    gross = inv.total_gross
    if gross is None and net is not None:
        gross = round(net + (inv.total_tax or 0.0), 2)
    if net is None and gross is not None:
        net = round(gross - (inv.total_tax or 0.0), 2)
    if line_total is None:
        line_total = net

    total = _cac(root, "LegalMonetaryTotal")
    _amount(total, "LineExtensionAmount", _abs(line_total, credit), currency)
    _amount(total, "TaxExclusiveAmount", _abs(net, credit), currency)
    _amount(total, "TaxInclusiveAmount", _abs(gross, credit), currency)
    _amount(total, "PayableAmount", _abs(gross, credit), currency)


def _line(
    root: ET.Element,
    number: int,
    line: LineItem,
    inv: Invoice,
    currency: str,
    credit: bool,
) -> None:
    node = _cac(root, "CreditNoteLine" if credit else "InvoiceLine")
    _cbc(node, "ID", str(number))

    # Exact rejects a quantity of 0, and BR-22 wants the unit named.
    quantity = line.quantity if line.quantity else 1
    element = "CreditedQuantity" if credit else "InvoicedQuantity"
    _cbc(node, element, _decimal(abs(quantity))).set("unitCode", DEFAULT_UNIT_CODE)
    _amount(node, "LineExtensionAmount", _abs(line.amount, credit), currency)

    item = _cac(node, "Item")
    _cbc(item, "Name", line.description or f"Line {number}")
    rate = line.tax_rate if line.tax_rate is not None else inv.tax_rate
    if rate is not None:
        _tax_category(_cac(item, "ClassifiedTaxCategory"), rate)

    # BR-26 makes Price mandatory, and BR-27 forbids a negative one.
    price = line.unit_price
    if price is None and line.amount is not None and quantity:
        price = round(line.amount / quantity, 4)
    if price is None:
        price = 0.0
    _amount(_cac(node, "Price"), "PriceAmount", abs(price), currency)


# --- element helpers --------------------------------------------------------


def _cac(parent: ET.Element, name: str) -> ET.Element:
    return ET.SubElement(parent, ET.QName(CAC_NS, name))


def _cbc(parent: ET.Element, name: str, text: str) -> ET.Element:
    node = ET.SubElement(parent, ET.QName(CBC_NS, name))
    node.text = _ILLEGAL_XML.sub("", text)
    return node


def _amount(
    parent: ET.Element, name: str, value: Optional[float], currency: str
) -> None:
    if value is None:
        return
    node = _cbc(parent, name, f"{value:.2f}")
    node.set("currencyID", currency)


def _decimal(value: float) -> str:
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return text or "0"
