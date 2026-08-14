"""Rule-based invoice parsing: no network calls, no model inference."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import kvk
from . import patterns as P
from .model import Invoice, LineItem, Party
from .numbers import (
    amount_tokens,
    detect_currency,
    money_tokens,
    parse_amount,
    parse_date,
    strip_dates,
    written_with_decimals,
)
from .textio import Document

_PERCENT = re.compile(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%")
_TOTAL_WORDS = re.compile(
    r"\b(sub\s*total|subtotaal|total|totaal|vat|btw|tax|mwst|amount\s*due|"
    r"te\s*betalen|balance|netto|gesamt|grondslag|summe)\b",
    re.IGNORECASE,
)
# The separator between a header and the name after it. The full stop is in
# here for the abbreviations: "t.a.v." ends in one, and \b makes the label
# match stop at the "v", so without this the name starts ". Frenk Ochse".
_HEADER_SEPARATOR = r"\b[\s.:\-–]*"
# "T.a.v. Frenk Ochse" names a person to hand the invoice to, inside a block
# addressed to a company. Whoever is named here is the contact, and the line
# above is the party — reading it the other way round bills a person for what
# a company bought, and Exact then holds a debtor that does not exist.
_ATTENTION_HEADERS = re.compile(
    r"^\s*(ter\s*attentie\s*van|attention(?:\s*of)?|t\.?\s?a\.?\s?v\.?|attn?\.?|"
    rf"c/o|care\s*of|z\.?\s?h\.?\s?a\.?){_HEADER_SEPARATOR}(.*)$",
    re.IGNORECASE,
)
_CUSTOMER_HEADERS = re.compile(
    r"^\s*(bill(?:ed)?\s*to|invoice\s*to|sold\s*to|ship\s*to|customer|client|"
    r"factuuradres|factu(?:ur|reren)\s*aan|geadresseerde|t\.?a\.?v\.?|"
    rf"rechnungsempf(?:ä|ae)nger|kunde){_HEADER_SEPARATOR}(.*)$",
    re.IGNORECASE,
)
# Who the invoice is addressed to. "Ship To" is not here on purpose: it is the
# delivery address, and it sits beside the billing one on invoices with both.
_BILLING_HEADERS = re.compile(
    r"^\s*(bill(?:ed)?\s*to|invoice\s*to|sold\s*to|"
    r"factuuradres|factu(?:ur|reren)\s*aan|geadresseerde|t\.?a\.?v\.?|"
    rf"rechnungsempf(?:ä|ae)nger){_HEADER_SEPARATOR}(.*)$",
    re.IGNORECASE,
)
_HEADER_NOISE = re.compile(
    r"^\s*(invoice|factuur|rechnung|tax\s*invoice|credit\s*note|creditnota|"
    r"page\s*\d|pagina\s*\d)\b[\s:#\-]*\d*\s*$",
    re.IGNORECASE,
)

# Column headers of the item table, per field.
COLUMN_HINTS = {
    "quantity": r"^(qty|quantity|aantal|eenheden|menge|anz\.?|uren|hours|"
                r"units?)$",
    "unit_price": r"^(unit|price|prijs|stukprijs|tarief|rate|preis|"
                  r"unitprice|per)$",
    "tax_rate": r"^(vat|btw|tax|mwst|vat%|btw%)$",
    "amount": r"^(amount|total|totaal|bedrag|betrag|line|subtotal|subtotaal|"
              r"sum)$",
    "description": r"^(description|omschrijving|artikel|item|items|product|"
                   r"bezeichnung|dienst|werkzaamheden|specificatie)$",
}


def parse(doc: Document, day_first: bool = True) -> Invoice:
    """Parse an extracted Document into an Invoice."""
    inv = Invoice(source_file=str(doc.path), ocr_used=doc.ocr_used)
    lines = doc.lines

    inv.currency = detect_currency(doc.text)
    _parse_scalars(inv, doc, day_first)
    _parse_totals(inv, lines)
    _parse_tax_rate(inv, doc.text)
    _parse_parties(inv, doc)
    inv.lines = _parse_line_items(doc)
    _reconcile(inv)
    return inv


# --- labelled scalar fields -------------------------------------------------


def _parse_scalars(inv: Invoice, doc: Document, day_first: bool) -> None:
    lines = doc.lines
    for field_name, labels in P.LABELS.items():
        value = _find_labelled_value(lines, labels)
        if value is not None:
            _set_scalar(inv, field_name, value, day_first)

    # Only then the headings: a label printed in front of its value is the
    # plainer statement, and this fills in what it did not cover.
    _fields_under_column_headings(inv, doc, day_first)

    if inv.invoice_date is None:
        # Last resort: the first date-looking string anywhere in the document,
        # passing over any that is already another field's. A text layer can
        # break a label apart — "F: actuurdatum: 13-7-2026" is what pdfplumber
        # returns for one invoice — and the first date on the page is then the
        # due date printed the line above, which books the invoice a fortnight
        # into the wrong period. Nothing else being available, the first date
        # is still better than none.
        dates = [date for date in (parse_date(line, day_first=day_first)
                                   for line in lines) if date]
        unclaimed = [date for date in dates if date != inv.due_date]
        if dates:
            inv.invoice_date = (unclaimed or dates)[0]
            inv.warnings.append(
                "invoice_date was not labelled; used first date in document"
            )

    match = P.PAYMENT_REF_RE.search("\n".join(lines))
    if match:
        inv.payment_reference = _clean_identifier(match.group(1))


def _set_scalar(inv: Invoice, field: str, value: str, day_first: bool) -> None:
    if field.endswith("_date"):
        parsed = parse_date(value, day_first=day_first)
        if parsed:
            setattr(inv, field, parsed)
        return
    cleaned = _clean_identifier(value)
    if cleaned:
        setattr(inv, field, cleaned)


# Words in a heading row are grouped into cells at a far smaller gap than
# columns are, so "Invoice date" stays one heading while "Debiteurnummer
# Factuurdatum" falls apart into two.
_CELL_GAP = 8.0


def _fields_under_column_headings(
    inv: Invoice, doc: Document, day_first: bool
) -> None:
    """Fields whose label is a column heading, with the value on the row below.

        Contractnr  Debiteurnummer  Factuurdatum  Vervaldatum  Bladnr  Factuurnr
        5000156089                    31-07-2026    07-08-2026   1 / 1  N06683352

    A heading is only read as one when the cell holds the label and nothing
    else, and when at least two on the row do — otherwise "Factuurnummer :
    26001434" above "Factuurdatum : 22-07-2026" would take the line below as
    its value, and every item table with a "Datum" column would be a header.
    """
    for rows in doc.word_rows:
        for index in range(len(rows) - 1):
            headings = _labelled_columns(rows[index])
            if len(headings) < 2:
                continue
            for field, span in headings.items():
                if getattr(inv, field) is not None:
                    continue
                # Columns like these are right-aligned as often as left, so the
                # value is the cell below that overlaps the heading, not the
                # one that starts where it starts.
                value = _overlapping_segment(rows[index + 1], span, _CELL_GAP)
                if value:
                    _set_scalar(inv, field, value, day_first)


def _labelled_columns(row: List[dict]) -> Dict[str, Tuple[float, float]]:
    """The fields this row names, and the column each one heads."""
    columns: Dict[str, Tuple[float, float]] = {}
    for x0, x1, text in _segments(row, _CELL_GAP):
        field = _field_named_by(text)
        if field and field not in columns:
            columns[field] = (x0, x1)
    return columns


def _field_named_by(text: str) -> Optional[str]:
    """The field a heading names, when the heading is nothing but the label."""
    candidate = text.strip().strip(":#").strip()
    if not candidate:
        return None
    for field, labels in P.LABELS.items():
        if any(re.fullmatch(label, candidate, re.IGNORECASE) for label in labels):
            return field
    return None


def _find_labelled_value(lines: List[str], labels: List[str]) -> Optional[str]:
    """Return the text following the first matching label.

    Falls back to the next line when the label sits alone on its own line,
    which is what two-column invoice headers usually look like — and only
    then. A label at the end of a row of other labels heads a column, and the
    line below it is a row of values, of which only one is this field's. That
    case belongs to `_fields_under_column_headings`, which has the positions
    to tell the values apart.
    """
    for label in labels:
        regex = P.label_value_re(label)
        for i, line in enumerate(lines):
            match = regex.search(line)
            if not match:
                continue
            value = _strip_trailing_label(match.group(1).strip(" :#\t"))
            if value:
                return value
            if line[: match.start()].strip():
                continue  # not alone on its line: a column heading
            if i + 1 < len(lines):
                return lines[i + 1].strip()
    return None


def _strip_trailing_label(value: str) -> str:
    """Cut a value short when the next column's label follows it on one line.

    Two-column headers set "Factuurnummer : 26001434    Kenteken : 1-XHK-91" as
    one line, and the wide gap between the columns does not always survive text
    extraction — so any label that follows, whatever it is called, ends the
    value as surely as a run of spaces does.

    The space before that colon is optional, because the second column is as
    often set tight against it: "Factuurnummer: 26800059 Betaaltermijn: 14
    dagen" is an invoice number and a payment term, and read as one value the
    number goes into Exact with the term stuck to it.

    A column of amounts ends the value too, labelled or not: "Factuurnummer
    30801393 € 126,09" is what the premium is charged under and what it costs.
    Money is what it looks like — a currency sign, or a number with cents —
    and neither of those is ever part of a reference.
    """
    cut = re.split(
        r"\s{3,}|\s+(?=(?:invoice|factuur|date|datum|due|verval|customer|klant|"
        r"order|page|pagina)\b)|"
        r"\s+(?=[€$£])|\s+(?=-?\d[\d.]*[.,]\d{2}\b)|"
        r"\s+(?=[A-Za-z][\w.]*(?:\s+[\w.]+){0,3}\s*:)",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    return cut[0].strip()


def _clean_identifier(value: str) -> Optional[str]:
    value = value.strip(" :#\t")
    value = re.sub(r"^[-–]\s+", "", value)
    value = re.sub(r"\s{2,}.*$", "", value).strip()
    if not value or len(value) > 40:
        return None
    return value


# --- totals -----------------------------------------------------------------

# Buckets found via an explicit wording win over ones found via a bare "total".
_EXPLICIT, _GENERIC = 2, 1

# Lines whose numbers identify something rather than measure money.
_IDENTIFIER_LINE = re.compile(
    r"\bkvk\b|k\.v\.k\.|chamber\s*of\s*commerce|handelsregister|"
    r"registration\s*(?:number|no\.?|nr\.?)|\btin\s*number\b|"
    r"\biban\b|\bbic\b|swift|account\s*(?:no|number)|rekening|"
    r"(?:vat|btw|tax|ust)[\s.-]*(?:id|no\.?|nr\.?|number|nummer|reg)|"
    r"\btel\b|phone|telefoon|\bfax\b|"
    r"payment\s*terms|\bterms\b|betalingstermijn|betaaltermijn",
    re.IGNORECASE,
)


_TOTAL_BUCKETS = ("total_net", "total_tax", "total_gross")


def _parse_totals(inv: Invoice, lines: List[str]) -> None:
    found: Dict[str, List[Tuple[Tuple[int, int], float]]] = {}

    for line in lines:
        clean = strip_dates(line)
        if _IDENTIFIER_LINE.search(clean):  # "KvK 60895344", "VAT no ...", IBAN
            continue
        amounts = money_tokens(clean)
        if not amounts:
            continue

        bucket, priority = _classify_total(clean)
        if bucket is None:
            continue
        # Totals are printed right-aligned, so the rightmost value is the one.
        token, value = amounts[-1]
        # A money-shaped value (with decimals) beats a bare integer, which is
        # more often a reference or a payment term sharing the line.
        rank = (priority, 1 if written_with_decimals(token) else 0)
        found.setdefault(bucket, []).append((rank, value))

    for bucket, value in _choose_totals(found).items():
        setattr(inv, bucket, value)


def _choose_totals(
    found: Dict[str, List[Tuple[Tuple[int, int], float]]]
) -> Dict[str, float]:
    """Pick one amount per bucket out of everything that could be it.

    An invoice can print the same wording several times: a fuel card bills per
    vehicle, so "Totaal kenteken: ... 92,82" and "Totaal contract: ... 92,82"
    stand above "TOTAAL EUR 112,31" and all three read as a grand total. The
    invoice settles it itself — net plus VAT is the gross, and only one
    combination of the candidates does that — so the triple that adds up wins.
    Failing that the last one printed does, the grand total being what a
    totals block builds towards.
    """
    ranked: Dict[str, List[float]] = {}
    for bucket, candidates in found.items():
        best = max(rank for rank, _ in candidates)
        values = [value for rank, value in candidates if rank == best]
        ranked[bucket] = list(dict.fromkeys(values))  # keep order, drop repeats

    options = [ranked.get(bucket) or [None] for bucket in _TOTAL_BUCKETS]
    chosen = [values[-1] for values in options]

    if not _adds_up(*chosen):
        for net in reversed(options[0]):
            for tax in reversed(options[1]):
                for gross in reversed(options[2]):
                    if _adds_up(net, tax, gross):
                        chosen = [net, tax, gross]
                        break
                else:
                    continue
                break
            else:
                continue
            break

    return {
        bucket: value
        for bucket, value in zip(_TOTAL_BUCKETS, chosen)
        if value is not None
    }


def _adds_up(net: Optional[float], tax: Optional[float],
             gross: Optional[float]) -> bool:
    if None in (net, tax, gross):
        return False
    return abs(net + tax - gross) <= 0.02


def _classify_total(line: str) -> Tuple[Optional[str], int]:
    if P.NET_TOTAL_RE.search(line):
        return "total_net", _EXPLICIT
    if P.GROSS_TOTAL_RE.search(line):
        return "total_gross", _EXPLICIT
    if P.TAX_TOTAL_RE.search(line):
        return "total_tax", _EXPLICIT
    if P.GENERIC_TOTAL_RE.search(line):
        return "total_gross", _GENERIC
    return None, 0


def _parse_tax_rate(inv: Invoice, text: str) -> None:
    match = P.TAX_RATE_RE.search(text)
    if match:
        inv.tax_rate = parse_amount(match.group(1) or match.group(2))


# --- parties ----------------------------------------------------------------


def _parse_parties(inv: Invoice, doc: Document) -> None:
    text = doc.text
    lines = doc.lines

    inv.supplier.iban = _find_iban(text)

    _assign_vat_numbers(inv, lines, _find_vat_ids(text))

    coc = P.COC_RE.search(text)
    if coc:
        inv.supplier.coc_number = coc.group(1)

    emails = P.EMAIL_RE.findall(text)
    if emails:
        inv.supplier.email = emails[0]

    inv.supplier.bic = _find_bic(text)

    # The letterhead is a column, and on an invoice that puts fields beside it
    # the flattened text runs the two together ("1016 ED Amsterdam Terms Due on
    # receipt"). Word positions keep them apart; fall back to the plain text
    # when a PDF carries no usable geometry.
    # An invoice that names its sender is believed over any reading of the
    # letterhead, which is a guess about which block on the page is whose.
    supplier_name, supplier_address = _party_block_from_label(doc, _SUPPLIER_HEADERS)
    if not supplier_name:
        supplier_name, supplier_address = _supplier_block_from_columns(doc)
    if not supplier_name and not supplier_address:
        supplier_name, supplier_address = _guess_supplier_block(
            doc.pages[0] if doc.pages else ""
        )
    if not any(_is_address_piece(line) for line in supplier_address):
        # Whatever was read at the top of the page, none of it is an address.
        # The foot of the page is the other place a company states itself, and
        # on stationery the only one: the address stands there beside the KvK
        # number, the VAT number and the bank, which are already taken from it.
        supplier_address = _address_from_footer(doc) or supplier_address

    supplier_name = _registered_or_printed_name(inv, supplier_name)
    if not supplier_name and inv.supplier.coc_number:
        # Printed stationery carries the company name as a logo, which OCR
        # returns as nothing at all. Nothing left on the page is the name —
        # the web address only resembles it — so the field stays empty and
        # says which number to write down to fill it.
        inv.warnings.append(
            f"no supplier name on the invoice; add KvK "
            f"{inv.supplier.coc_number} to the name file to fill it in"
        )
    inv.supplier.name, inv.supplier.address = supplier_name, supplier_address

    # "Date  Billed to" and "Bill To  Ship To" both put the customer in a
    # column that the flattened text runs together with its neighbour, so the
    # geometry is tried first and the plain text is the fallback.
    name, address = _party_block_from_label(doc, _ADDRESSEE_HEADERS)
    coc, contact = None, None
    if name:
        address, contact = _pull_contact(address)
    else:
        name, address, coc, contact = _customer_block_from_columns(doc)
    if not name:
        name, address, contact = _guess_customer_block(lines)
    if not name:
        name, address = _addressee_block_from_columns(
            doc, {supplier_name, *supplier_address}
        )
        address, contact = _pull_contact(address)
    inv.customer.name, inv.customer.address = _clean_party_name(name), address
    inv.customer.contact_name = contact
    inv.supplier.name = _clean_party_name(inv.supplier.name)
    if coc and not inv.customer.coc_number:
        inv.customer.coc_number = coc


def _clean_party_name(name: Optional[str]) -> Optional[str]:
    """No company or person is called ". Frenk Ochse".

    Whatever left punctuation in front of a name — an abbreviated header, a
    bullet, a dash used as a separator — the name itself never opens with it,
    and the leftover travels into the UBL and into the creditor in Exact.
    """
    if not name:
        return name
    cleaned = re.sub(r"^[\s.,:;\-–—•|]+", "", name).strip()
    return cleaned or None


def _registered_or_printed_name(inv: Invoice, printed: Optional[str]) -> Optional[str]:
    """The Handelsregister's name for the supplier, or what the page printed.

    The register is leading. An invoice prints the trade name the garage goes
    by ("Roos"), the register holds the one the creditor is booked under
    ("Garagebedrijf Roos B.V."), and only the second is stable across invoices
    — so where the KvK number resolves, the printed name gives way and the
    substitution is reported rather than made quietly.
    """
    registered = kvk.registered_name(inv.supplier.coc_number)
    if not registered:
        return printed
    if printed and _same_company(printed, registered):
        return registered
    if printed:
        inv.warnings.append(
            f"supplier name {printed!r} replaced by the registered name "
            f"{registered!r} (KvK {inv.supplier.coc_number})"
        )
    return registered


def _same_company(one: str, other: str) -> bool:
    """Whether two spellings differ only in punctuation: "BV" and "B.V."."""
    strip = lambda name: re.sub(r"[^a-z0-9]", "", name.lower())  # noqa: E731
    return strip(one) == strip(other)


# Words further apart than this are in different columns, not the same phrase.
_COLUMN_GAP = 40
_URL_LINE = re.compile(r"^(?:https?://|www\.)|\.(?:com|nl|io|org|co)$", re.IGNORECASE)


def _is_item_column_word(text: str) -> bool:
    """A lone item-table heading such as "Description" or "Omschrijving".

    Splitting a row into columns hands these over one at a time, so the test
    for a whole header row never fires and an address block would run straight
    on into the item table.
    """
    token = text.strip().strip(".:").lower()
    return any(re.match(hint, token) for hint in COLUMN_HINTS.values())


def _looks_like_labelled_field(text: str) -> bool:
    """"Date of issue June 17, 2026" — a field with its label, not a company."""
    return any(
        re.match(rf"\s*(?:{label})\b", text, re.IGNORECASE)
        for labels in P.LABELS.values()
        for label in labels
    )


# Letterheads set several fields on one line, divided by a bar or a bullet:
# "Omroepweg 15, 1324 KT Almere | Tel. 036 534 65 50".
_PIECE_SEPARATOR = re.compile(r"\s*[|•·]\s*|\s{3,}")
# A piece that is a way to reach or register the company, not its address.
_CONTACT_PIECE = re.compile(
    r"\b(tel|telefoon|phone|fax|mob(?:iel)?|gsm|iban|bic|swift|rekening|"
    r"k\.?v\.?k\.?|handelsregister|btw|vat|ust)\b",
    re.IGNORECASE,
)


def _letterhead_pieces(text: str) -> List[str]:
    """The parts of a letterhead line that say who the company is and where.

    Phone, bank and registration numbers are dropped: each already has a field
    of its own, and leaving them in the address puts them in the UBL street.
    """
    pieces = []
    for piece in _PIECE_SEPARATOR.split(text):
        piece = piece.strip(" ,;")
        if not piece or _CONTACT_PIECE.search(piece):
            continue
        if P.EMAIL_RE.search(piece) or _URL_LINE.search(piece):
            continue
        if _find_vat_ids(piece):
            continue
        pieces.append(piece)
    return pieces


# A postcode with its town: "1324 KT Almere", "10115 Berlin".
_POSTCODE_PIECE = re.compile(r"^\d{4,6}\s?[A-Z]{0,2}\s+\w", re.IGNORECASE)


def _is_address_piece(text: str) -> bool:
    return bool(_STREET_START.search(text) or _POSTCODE_PIECE.match(text))


# An invoice that names its sender outright. Worth more than any guess at the
# letterhead: "Van" is deliberately absent, being half the Dutch surnames.
_SUPPLIER_HEADERS = re.compile(
    rf"^\s*(leverancier|supplier|vendor|verkoper|crediteur|lieferant)"
    rf"{_HEADER_SEPARATOR}(.*)$",
    re.IGNORECASE,
)
# And the party it is addressed to. "Aan" is here and not in _BILLING_HEADERS
# because it only reads as a label in a list of fields, where a column of
# labels stands beside a column of values; loose in a page of text it is a
# preposition.
_ADDRESSEE_HEADERS = re.compile(
    rf"^\s*(aan|contractant|bill(?:ed)?\s*to|invoice\s*to|sold\s*to|"
    rf"factuuradres|factu(?:ur|reren)\s*aan|geadresseerde|"
    rf"rechnungsempf(?:ä|ae)nger){_HEADER_SEPARATOR}(.*)$",
    re.IGNORECASE,
)


def _party_block_from_label(
    doc: Document, headers: re.Pattern
) -> Tuple[Optional[str], List[str]]:
    """A party the invoice names outright, and the address under that name.

    The label and the name sit in two columns of a list of fields:

        Leverancier   KPN B.V.
                      Wilhelminakade 123
                      3072 AP Rotterdam
        KvK nummer    27124701

    So the address is the value column continued downwards, and it ends where
    the label column speaks again — not at the first line that looks like an
    identifier, because "27124701" alone does not, and not at the first line
    holding an e-mail, because the block beside this one has one on it.
    """
    for rows in doc.word_rows:
        for index, row in enumerate(rows):
            found = _label_columns(row, headers)
            if found is None:
                continue
            label_span, value_span, name = found

            address: List[str] = []
            for follow in rows[index + 1 : index + 8]:
                if _overlapping_segment(follow, label_span):
                    break  # the next field begins
                text = _overlapping_segment(follow, value_span)
                if text is None:
                    continue
                if _IDENTIFIER_LINE.search(text) or _find_vat_ids(text):
                    break
                address.append(text)
                if len(address) == 4:
                    break
            if name:
                return name, address
    return None, []


def _label_columns(
    row: List[dict], headers: re.Pattern
) -> Optional[Tuple[Tuple[float, float], Tuple[float, float], str]]:
    """The label column, the value column and the name, on a labelled row.

    The name is in the next column along, or behind the label in the same one
    when the two were set close enough to read as one. What comes back has to
    be able to be a party: KPN labels an e-mail address "Factuuradres", and no
    company is called frochse@yahoo.com.
    """
    segments = _segments(row)
    for position, (x0, x1, text) in enumerate(segments):
        match = headers.match(text)
        if not match:
            continue
        inline = _clean_party_name(match.group(2).strip())
        if inline:
            if _is_addressee_line(inline):
                return (x0, x1), (x0, x1), inline
            continue
        if position + 1 < len(segments):
            nx0, nx1, name = segments[position + 1]
            name = _clean_party_name(name)
            if name and _is_addressee_line(name):
                return (x0, x1), (nx0, nx1), name
    return None


def _supplier_block_from_columns(doc: Document) -> Tuple[Optional[str], List[str]]:
    """Letterhead name and address, read from the column they stand in.

    The letterhead is not always at the left margin — plenty of invoices set it
    flush right — so the column is found from the first letterhead-looking run
    of words on page one, and the rest of the block is whatever sits under it.

    The name can be missing altogether: printed stationery often carries it as
    a logo, and OCR reads the address around it but not the drawing. An address
    with no name is still the supplier's, and reporting it as such is what
    keeps the addressee from being taken for the company.
    """
    rows = doc.word_rows[0] if doc.word_rows else []

    start, header, span = None, None, None
    for index, row in enumerate(rows[:20]):
        for x0, x1, text in _segments(row):
            if _HEADER_NOISE.match(text) or not _is_letterhead_line(text):
                continue
            # "Invoice # NL900187910" and "Date of issue June 17, 2026" are
            # labelled fields, not companies.
            if re.search(r"\b(invoice|factuur|rechnung)\b", text, re.IGNORECASE):
                continue
            if _looks_like_labelled_field(text) or _is_item_column_word(text):
                continue
            start, header, span = index, text, (x0, x1)
            break
        if header:
            break
    if header is None:
        return None, []

    pieces = _letterhead_pieces(header)
    named = [piece for piece in pieces if not _is_address_piece(piece)]
    name = named[0] if named else None
    address: List[str] = [piece for piece in pieces if _is_address_piece(piece)]

    for row in rows[start + 1 : start + 10]:
        text = _overlapping_segment(row, span)
        if text is None:
            continue  # a row with nothing in this column, not the end of it
        if (
            _HEADER_NOISE.match(text)
            or _IDENTIFIER_LINE.search(text)
            or P.EMAIL_RE.search(text)
            or _find_vat_ids(text)
            or _TOTAL_WORDS.search(text)
            or _CUSTOMER_HEADERS.match(text)
            or _is_column_header_text(text)
            or _is_item_column_word(text)
            or re.search(r"\b(invoice|factuur|rechnung)\b", text, re.IGNORECASE)
        ):
            break
        if _is_prose_line(text) or _URL_LINE.search(text):
            continue
        address.extend(_letterhead_pieces(text))
        if len(address) >= 4:
            del address[4:]
            break
    return name, address


def _find_bic(text: str) -> Optional[str]:
    """The bank code, however the footer it stands in spells it.

    "BIC code INGB NL 2A" is one code in three pieces, behind a label that
    carries a word of its own. Only what follows the label has its spaces
    closed up, the way a VAT number's are: closing them across the whole line
    would run the registration before it into the label — "AFM 12004671 BIC"
    becomes "AFM12004671BIC" — and the label is then no longer a word. The
    code is read case-sensitively, or "swift van deze rekening" is a bank code.
    """
    label = re.compile(
        r"\b(?:bic|swift)\b\s*(?:code|nummer|nr\.?|no\.?)?\s*[:#]?\s*",
        re.IGNORECASE,
    )
    for match in label.finditer(text):
        tail = text[match.end():match.end() + 40]
        found = P.BIC_RE.match(
            re.sub(r"(?<=[A-Z0-9])[ ]+(?=[A-Z0-9])", "", tail)
        )
        if found:
            return found.group(1).upper()
    return None


# How far up from the bottom of the page the footer reaches. Enough for the
# columns of details and the small print under them.
_FOOTER_ROWS = 8


def _address_from_footer(doc: Document) -> List[str]:
    """The company's address, out of the line of details at the foot of a page.

        035 - 623 89 40
        Vaartweg 81       info@steijnborg.nl  K.v.K. 32039196  Iban NL 24 ...
        1211 JG Hilversum www.steijnborg.nl   AFM 12004671     BIC code INGB ...

    Four columns, of which one is the address. The others each have a field of
    their own already, so only the pieces that look like an address are kept —
    a street with a number on it, a postcode with its town — and the telephone
    number, the website and the registrations are left where they belong.
    """
    rows = doc.word_rows[-1] if doc.word_rows else []
    address: List[str] = []
    for row in rows[-_FOOTER_ROWS:]:
        for _, _, text in _segments(row):
            if _is_prose_line(text):
                continue  # the small print about the terms of business
            for piece in _letterhead_pieces(text):
                if _is_address_piece(piece) and piece not in address:
                    address.append(piece)
    return address[:4]


def _find_vat_ids(text: str) -> List[str]:
    """VAT numbers, whether or not they are printed with spaces in them.

    Both spellings have to be tried. Deleting every space turns
    "VAT number NL123456789B01" into "...numberNL123456789B01", where the
    number no longer starts at a word boundary and is missed; leaving them
    alone misses "NL 8513 8923 5B01", which is one number in four pieces.
    Spaces are only closed up between characters a VAT number can contain, so
    a lower-case label never gets glued to the value.
    """
    joined = re.sub(r"(?<=[A-Z0-9])[ ]+(?=[A-Z0-9])", "", text)
    found: List[str] = []
    # The joined spelling first: it keeps the numbers in the order they are
    # printed, which is what decides whose number is whose.
    for candidate in P.VAT_ID_RE.findall(joined) + P.VAT_ID_RE.findall(text):
        if candidate not in found:
            found.append(candidate)
    return found


# "Customer VAT/TAX No:", "BTW-nummer klant" — a VAT number that is the buyer's.
_CUSTOMER_VAT_LINE = re.compile(
    r"\b(customer|client|buyer|klant|debiteur|koper|kunde)\b", re.IGNORECASE
)
_VAT_WORDED = re.compile(r"\b(btw|vat|tax|mwst|ust)\b", re.IGNORECASE)


def _customer_vat_numbers(lines: List[str]) -> List[str]:
    """The VAT numbers an invoice marks as the buyer's.

    "BTW nummer klant:" is a heading with nothing after it: the number stands
    on the line below, beside the customer's name. So a label with no number on
    it hands the search to the next line — but only a label that says VAT as
    well as customer, or "Debiteurnummer 1487" would claim whatever number
    happens to follow it.
    """
    found: List[str] = []
    for index, line in enumerate(lines):
        if not _CUSTOMER_VAT_LINE.search(line):
            continue
        on_line = _find_vat_ids(line)
        if not on_line and _VAT_WORDED.search(line) and index + 1 < len(lines):
            on_line = _find_vat_ids(lines[index + 1])
        found.extend(on_line)
    return found


def _assign_vat_numbers(inv: Invoice, lines: List[str], vat_ids: List[str]) -> None:
    """Give each party its VAT number.

    Position alone is not enough: an invoice that prints the buyer's number
    ("Customer VAT/TAX No: ...") would otherwise hand it to the supplier and
    send the document to the wrong account in Exact.
    """
    customer_vats = _customer_vat_numbers(lines)
    supplier_vats = [vat for vat in vat_ids if vat not in customer_vats]

    if supplier_vats:
        inv.supplier.vat_number = supplier_vats[0]
    if customer_vats:
        inv.customer.vat_number = customer_vats[0]
    elif len(supplier_vats) > 1:
        inv.customer.vat_number = supplier_vats[1]


def _guess_supplier_block(first_page: str) -> Tuple[Optional[str], List[str]]:
    """The letterhead: the first meaningful line on page one, and the address
    lines directly under it."""
    lines = [line.strip() for line in first_page.splitlines()]
    for index, line in enumerate(lines):
        if not _is_letterhead_line(line):
            continue
        address: List[str] = []
        for follow in lines[index + 1 : index + 8]:
            if (
                not follow
                or _HEADER_NOISE.match(follow)
                or _IDENTIFIER_LINE.search(follow)
                or P.EMAIL_RE.search(follow)
                or _find_vat_ids(follow)
                or _TOTAL_WORDS.search(follow)
                or _CUSTOMER_HEADERS.match(follow)
                or re.search(r"\b(invoice|factuur|rechnung)\b", follow, re.IGNORECASE)
            ):
                break
            # A letterhead can carry a line of prose between the name and the
            # address ("a corporation organized under the laws of ..."), which
            # used to fill the window and push the real address out of it.
            if _is_prose_line(follow):
                continue
            address.append(follow)
            if len(address) == 4:
                break
        return line, address
    return None, []


def _is_prose_line(line: str) -> bool:
    """A sentence rather than an address line: long, wordy and unnumbered."""
    return len(line) > 40 and not re.search(r"\d", line)


def _is_letterhead_line(line: str) -> bool:
    if not line or _HEADER_NOISE.match(line) or _EMPTY_LABEL.match(line):
        return False
    if not 2 <= len(line) <= 60:
        return False
    if not re.search(r"[A-Za-z]{2}", line):
        return False
    return not (P.EMAIL_RE.search(line) or re.match(r"^[\d\s.,/-]+$", line))


# A field that was left blank: a label, a colon and nothing after it. Letterheads
# open with one often enough — "Afdeling:" over the company name — and taken for
# the name it becomes the creditor in Exact, with the company itself pushed down
# into the street and the street into an extra address line.
_EMPTY_LABEL = re.compile(r"^\s*[^:\n]{1,30}:\s*$")


def _guess_customer_block(
    lines: List[str],
) -> Tuple[Optional[str], List[str], Optional[str]]:
    for i, line in enumerate(lines):
        match = _CUSTOMER_HEADERS.match(line)
        if not match:
            continue
        block: List[str] = []
        contact: Optional[str] = None
        inline = match.group(2).strip()
        above = lines[i - 1].strip() if i else ""
        # An attention line stands under the company being billed, so the
        # block starts on the line above it — unless there is nothing there,
        # in which case the person named is who is being billed.
        if _ATTENTION_HEADERS.match(line) and above and _is_addressee_line(above):
            block.append(above)
            contact = _clean_party_name(inline)
        elif inline:
            block.append(inline)
        for follow in lines[i + 1 : i + 6]:
            follow = follow.strip()
            if (
                not follow
                or _HEADER_NOISE.match(follow)
                or _TOTAL_WORDS.search(follow)
                or _CUSTOMER_HEADERS.match(follow)
                or _is_column_header_text(follow)
                or re.search(r"\b(invoice|factuur)\s*(no|nr|number|nummer)",
                             follow, re.IGNORECASE)
            ):
                break
            block.append(follow)

        block, below = _pull_contact(block)
        if block:
            return block[0], block[1:], contact or below
    return None, [], None


def _customer_block_from_columns(
    doc: Document,
) -> Tuple[Optional[str], List[str], Optional[str], Optional[str]]:
    """Read the customer out of the column its header stands in.

    A two-column header flattens to "Date Billed to", and the row under it to
    "27 May 2026 Jan Jansen Voorbeeld BV ...", so neither line can be matched on
    its own. The x-position of the header word says where its column starts,
    and everything at or right of it on the rows below belongs to the customer.

    An attention line is not that header. "T.a.v. Frenk Ochse" stands under the
    company being billed, so the block starts one row higher and the person
    becomes the contact.
    """
    for rows in doc.word_rows:
        for index, row in enumerate(rows):
            found = _customer_column_start(row)
            if found is None:
                continue
            span, inline, attention = found

            block: List[str] = []
            contact: Optional[str] = None
            above = _overlapping_segment(rows[index - 1], span) if index else None
            if attention and above and _is_addressee_line(above):
                # A company, for the attention of a person.
                block.append(above)
                contact = _clean_party_name(inline)
            elif inline:
                # Nothing above it: the person named is who is being billed.
                block.append(inline)

            for follow in rows[index + 1 : index + 6]:
                text = _overlapping_segment(follow, span)
                if (
                    not text
                    # "Factuur" on its own under the address block is the title
                    # of the document, not a line of the address.
                    or _HEADER_NOISE.match(text)
                    or _TOTAL_WORDS.search(text)
                    or _is_column_header_text(text)
                    or _is_item_column_word(text)
                    or _BILLING_HEADERS.match(text)
                    # The block ends where the field table begins, even when
                    # this column holds only its first heading ("Contractnr").
                    or _row_has_labelled_field(follow)
                ):
                    break
                block.append(text)

            block, below = _pull_contact(block)
            if block:
                name, address, coc = _split_name_from_address(block)
                return name, address, coc, contact or below
    return None, [], None, None


def _is_addressee_line(text: str) -> bool:
    """Whether a line can be the company an invoice is addressed to."""
    return not (
        _HEADER_NOISE.match(text)
        or _IDENTIFIER_LINE.search(text)
        or _find_vat_ids(text)
        or P.EMAIL_RE.search(text)
        or _URL_LINE.search(text)
        or _TOTAL_WORDS.search(text)
        or _looks_like_labelled_field(text)
        or _is_column_header_text(text)
        or _is_item_column_word(text)
    )


def _pull_contact(block: List[str]) -> Tuple[List[str], Optional[str]]:
    """Take the attention line out of an address block.

    The person named there is not part of the address and not the party: an
    invoice to "FROC Holding B.V. / T.a.v. Frenk Ochse" is billed to the
    company, for the attention of the person.
    """
    kept: List[str] = []
    contact: Optional[str] = None
    for line in block:
        match = _ATTENTION_HEADERS.match(line)
        person = _clean_party_name(match.group(2)) if match else None
        if person and contact is None:
            contact = person
            continue
        kept.append(line)
    return kept, contact


def _addressee_block_from_columns(
    doc: Document, supplier_lines: set
) -> Tuple[Optional[str], List[str]]:
    """The block the invoice is addressed to, when nothing labels it as such.

    A Dutch invoice usually prints no "Factuuradres" at all: the customer is
    simply the block sitting in the window-envelope position, between the
    letterhead and the labelled fields ("Factuurnummer : ..."). Those fields
    are the anchor — the block ends where they begin — and the block is only
    believed when it holds something that looks like an address, so a slogan
    under a logo is not read as a customer.
    """
    rows = doc.word_rows[0] if doc.word_rows else []
    fields = next(
        (index for index, row in enumerate(rows) if _row_has_labelled_field(row)),
        None,
    )
    if not fields:  # no fields at all, or nothing above them
        return None, []

    segments = _segments(rows[fields - 1])
    if not segments:
        return None, []
    span = (segments[0][0], segments[0][1])

    block: List[str] = []
    for row in reversed(rows[:fields]):
        text = _overlapping_segment(row, span)
        if (
            text is None
            or text in supplier_lines
            or _HEADER_NOISE.match(text)
            or _IDENTIFIER_LINE.search(text)
            or _find_vat_ids(text)
            or P.EMAIL_RE.search(text)
            or _URL_LINE.search(text)
            or _looks_like_labelled_field(text)
        ):
            break
        block.insert(0, text)
        if len(block) == 5:
            break

    if not any(_is_address_piece(line) for line in block):
        return None, []
    name, address, _ = _split_name_from_address(block)
    return name, address


def _row_has_labelled_field(row: List[dict]) -> bool:
    return any(_looks_like_labelled_field(text) for _, _, text in _segments(row))


def _segments(
    row: List[dict], gap: float = _COLUMN_GAP
) -> List[Tuple[float, float, str]]:
    """Split a row into its columns, at the gaps between them."""
    words = sorted(row, key=lambda w: w["x0"])
    if not words:
        return []
    groups, current = [], [words[0]]
    for word in words[1:]:
        if word["x0"] - current[-1]["x1"] > gap:
            groups.append(current)
            current = [word]
        else:
            current.append(word)
    groups.append(current)
    return [
        (g[0]["x0"], g[-1]["x1"], " ".join(w["text"] for w in g)) for g in groups
    ]


def _overlapping_segment(
    row: List[dict], span: Tuple[float, float], gap: float = _COLUMN_GAP
) -> Optional[str]:
    """The column of this row that sits under `span`.

    Columns are matched by overlap rather than by where they start, because a
    right-aligned letterhead starts at a different x on every line.
    """
    low, high = span
    best, widest = None, 0.0
    for x0, x1, text in _segments(row, gap):
        overlap = min(high, x1) - max(low, x0)
        if overlap > widest:
            best, widest = text, overlap
    return best if widest > 0 else None


def _customer_column_start(
    row: List[dict],
) -> Optional[Tuple[Tuple[float, float], str, bool]]:
    """Where the billing address column begins in this row, what is on it, and
    whether that is an attention line rather than the addressee itself.

    "Ship To" is deliberately not a billing header: an invoice that carries
    both prints them side by side, and the delivery address is not who gets
    invoiced.
    """
    for x0, x1, text in _segments(row):
        match = _BILLING_HEADERS.match(text)
        if not match:
            continue
        inline = match.group(2).strip()
        # "Customer # : 199678" is a reference field, not an address block.
        if inline and re.match(r"^[#:\d]", inline):
            continue
        return (x0, x1), inline, bool(_ATTENTION_HEADERS.match(text))
    return None


# A street: one capitalised word followed by a house number. Marks where the
# address starts when a name and an address share a line.
_STREET_START = re.compile(r"\b[A-Z][\w.'-]{2,}\s+\d{1,4}[a-zA-Z]?\b")
# A company register number printed after the address, with nothing to label it.
_TRAILING_NUMBER = re.compile(r"\s(\d{6,10})\s*$")


def _split_name_from_address(
    block: List[str],
) -> Tuple[str, List[str], Optional[str]]:
    """Split "Jan Jansen Voorbeeld BV Voorbeeldstraat 22, 1234AB Utrecht 12345678"."""
    block = list(block)
    coc = None
    trailing = _TRAILING_NUMBER.search(block[-1])
    if trailing:
        coc = trailing.group(1)
        block[-1] = block[-1][: trailing.start()].rstrip(" ,")

    first, rest = block[0], block[1:]
    match = _STREET_START.search(first)
    if match and match.start() > 0:
        name = first[: match.start()].strip(" ,")
        remainder = first[match.start():].strip(" ,")
        if name:
            return name, ([remainder] + rest if remainder else rest), coc
    return first, rest, coc


def _looks_like_iban(candidate: str) -> bool:
    compact = candidate.replace(" ", "")
    return (
        15 <= len(compact) <= 34
        and compact[:2].isalpha()
        and compact[2:4].isdigit()
    )


def _iban_checksum_ok(compact: str) -> bool:
    """ISO 13616 mod-97: the check the two digits after the country exist for."""
    if not (15 <= len(compact) <= 34) or not compact[:2].isalpha():
        return False
    rearranged = compact[4:] + compact[:4]
    if not rearranged.isalnum():
        return False
    digits = "".join(str(int(char, 36)) for char in rearranged)
    return int(digits) % 97 == 1


# Glyphs tesseract swaps for digits. Only the ones it actually confuses: a
# repair that may touch any character would find a passing checksum for
# anything.
_OCR_DIGIT_CONFUSIONS = {
    "O": "0", "Q": "0", "D": "0",
    "I": "1", "L": "1",
    "Z": "2", "S": "5", "G": "6", "T": "7", "B": "8",
}


def _repair_ocr_iban(compact: str) -> Optional[str]:
    """Undo OCR's letter-for-digit slips in an account number, or give up.

    "NL50 INGB 0006 8780 69" comes back from a scanned letterhead as
    "NLSO INGB ...". Which letters were digits is not knowable from the string,
    so every combination is tried and the checksum decides — but a checksum is
    1-in-97 to pass by luck, so a repair is only trusted when the fewest-edit
    one is the only one that passes. Anything else is left unread: a plausible
    but wrong bank account is worse on an invoice than none at all.
    """
    positions = [i for i, char in enumerate(compact)
                 if char in _OCR_DIGIT_CONFUSIONS]
    if not positions or len(positions) > 8:
        return None

    passing: Dict[int, List[str]] = {}
    for mask in range(1, 1 << len(positions)):
        chars = list(compact)
        for bit, index in enumerate(positions):
            if mask >> bit & 1:
                chars[index] = _OCR_DIGIT_CONFUSIONS[compact[index]]
        candidate = "".join(chars)
        if _iban_checksum_ok(candidate):
            passing.setdefault(bin(mask).count("1"), []).append(candidate)

    if not passing:
        return None
    fewest = passing[min(passing)]
    return fewest[0] if len(fewest) == 1 else None


def _find_iban(text: str) -> Optional[str]:
    """The supplier's account number, preferring one whose checksum holds."""
    candidates = [
        match.replace(" ", "").upper()
        for match in P.IBAN_RE.findall(text)
        if _looks_like_iban(match)
    ]
    for candidate in candidates:
        if _iban_checksum_ok(candidate):
            return candidate

    for labelled in P.IBAN_LABEL_RE.findall(text):
        compact = labelled.replace(" ", "").upper()
        if _iban_checksum_ok(compact):
            return compact
        repaired = _repair_ocr_iban(compact)
        if repaired:
            return repaired

    # Nothing validates: fall back to what is printed rather than drop it.
    return candidates[0] if candidates else None


# --- line items -------------------------------------------------------------


def _parse_line_items(doc: Document) -> List[LineItem]:
    """The invoice's own summary first, then ruled tables, then column
    geometry, then a plain-text heuristic."""
    for strategy in (
        _items_from_summary,
        _items_from_tables,
        _items_from_columns,
        _items_from_text,
    ):
        items = strategy(doc)
        if items:
            return items
    return []


# The heading over the block where an invoice says, in a few rows, what the
# pages after it specify. Nothing else is allowed on the line: "Samenvatting
# per kenteken" heads one summary among several, not the invoice's own.
_SUMMARY_HEADING = re.compile(
    r"^\s*(samenvatting|overzicht|recapitulatie|summary|zusammenfassung)\s*$",
    re.IGNORECASE,
)
_CURRENCY_MARK = re.compile(r"^[€$£]$")


def _items_from_summary(doc: Document) -> List[LineItem]:
    """The rows of a summary block, when they add up to the total under them.

        Samenvatting
        Product              Bruto      Netto
        Internet            € 49,73    € 49,73
        Mobiel              € 46,11    € 46,11
        TV                  € 11,88    € 11,88
        Totaal             € 107,72   € 107,72
        Btw 21% over € 107,72          € 22,62
        Totaal inclusief btw          € 130,34

    A telephone bill is pages of specification — subscriptions, discounts,
    calls per destination, the same money once per service and once per
    location — and none of those rows is a line to book. The three above are,
    and the invoice says so itself by adding them up: the block is only read as
    the item table when its rows come to the total printed under them, so a
    summary that is a partial view of the invoice is left alone.
    """
    for index, line in enumerate(doc.lines):
        if not _SUMMARY_HEADING.match(line):
            continue
        items = _summary_block(doc.lines[index + 1 : index + 40])
        if items:
            return items
    return []


def _summary_block(lines: List[str]) -> List[LineItem]:
    """Read rows until the total that closes the block, and check them by it."""
    rows: List[Tuple[str, float]] = []
    for line in lines:
        text, amounts = _summary_row(line)
        # A total word with no amount beside it is a column heading: "Netto"
        # over the column, not the netto total that ends the block.
        if amounts and _TOTAL_WORDS.search(text):
            total = amounts[-1]
            if not rows or abs(sum(amount for _, amount in rows) - total) > 0.02:
                return []
            return [
                LineItem(description=description, amount=amount)
                for description, amount in rows
            ]
        if not amounts or not re.search(r"[A-Za-z]{2}", text):
            continue  # the column headings, or a rule between the rows
        # Bruto beside Netto: the right-hand column is the one that is charged,
        # and the total below decides whether reading it that way was right.
        rows.append((text, amounts[-1]))
    return []


def _summary_row(line: str) -> Tuple[str, List[float]]:
    """A summary row split into what it charges for and the amounts beside it.

    The description ends at the first amount, not at the last: the amounts are
    set in columns with a currency sign in front of each, so peeling them off
    the right leaves "Internet € 49,73 €" behind.
    """
    tokens = strip_dates(line).split()
    cut = len(tokens)
    for index, token in enumerate(tokens):
        if _CURRENCY_MARK.match(token) or amount_tokens(token):
            cut = index
            break
    return " ".join(tokens[:cut]), amount_tokens(" ".join(tokens[cut:]))


def _items_from_tables(doc: Document) -> List[LineItem]:
    items: List[LineItem] = []
    for table in doc.tables:
        if not table or len(table) < 2:
            continue
        mapping = _map_header_cells(table[0])
        if "description" not in mapping or "amount" not in mapping:
            continue
        for row in table[1:]:
            cells = {
                name: _cell_text(row, index) for name, index in mapping.items()
            }
            item = _cells_to_item(cells)
            if item:
                items.append(item)
    return items


def _map_header_cells(header: List) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for index, cell in enumerate(header):
        text = (cell or "").strip().lower()
        if not text:
            continue
        for name, hint in COLUMN_HINTS.items():
            if name in mapping:
                continue
            if re.search(hint.strip("^$"), text):
                mapping[name] = index
                break
    return mapping


def _cell_text(row: List, index: Optional[int]) -> str:
    if index is None or index >= len(row):
        return ""
    value = row[index]
    return value.replace("\n", " ").strip() if isinstance(value, str) else ""


def _items_from_columns(doc: Document) -> List[LineItem]:
    """Rebuild the item table from word x-positions under its column headers.

    Every page is read, not only the first one that yields something. A table
    that does not fit on a page carries on under a repeat of its own header on
    the next, and stopping at the first page drops the rest of the invoice —
    including the credited instalments at the end, which are what makes the
    remaining lines add up to the amount being charged.
    """
    items: List[LineItem] = []
    for rows in doc.word_rows:
        for index, row in enumerate(rows):
            columns = _match_column_header(row, rows[index - 1] if index else None)
            if not columns:
                continue
            items.extend(_read_column_rows(rows[index + 1 :], columns))
            break  # one item table per page; the rest of it is on the next
    return items


def _match_column_header(
    row: List[dict], above: Optional[List[dict]] = None
) -> Optional[Dict[str, Tuple[float, float]]]:
    """Map fields to x-ranges when this row looks like an item-table header.

    A heading can be set over two lines to keep the column narrow — "Eenh"
    above "Prijs", "Bedrag EUR" above "excl. BTW" — so the row above is read
    too, but only for the fields this row does not already name. The lower line
    is the one that counts: "Eenheden" is the quantity, and "Eenh" over the
    price column is the other half of "eenheidsprijs".
    """
    hits = _column_hits(row)
    if above:
        named = {name for name, _ in hits}
        hits += [hit for hit in _column_hits(above) if hit[0] not in named]

    # Three headings, or the two that are a table on their own. "Omschrijving"
    # over "Bedrag" and nothing else is the whole table on an invoice that
    # charges for one thing; asking for a third heading leaves it unread. Two
    # of anything else is too little to go on — "Datum" beside "Bedrag" is a
    # payment history as easily as an item table.
    names = {name for name, _ in hits}
    if "amount" not in names:
        return None
    if len(hits) < 3 and names != {"description", "amount"}:
        return None

    hits.sort(key=lambda pair: pair[1]["x0"])
    columns: Dict[str, Tuple[float, float]] = {}
    for i, (name, word) in enumerate(hits):
        left = 0.0 if i == 0 else (hits[i - 1][1]["x1"] + word["x0"]) / 2
        right = (
            float("inf")
            if i == len(hits) - 1
            else (word["x1"] + hits[i + 1][1]["x0"]) / 2
        )
        columns[name] = (left, right)
    return columns


def _column_hits(row: List[dict]) -> List[Tuple[str, dict]]:
    """The fields this row's words name, one word per field, in reading order."""
    hits: List[Tuple[str, dict]] = []
    for word in row:
        text = word["text"].strip().lower().strip(".:")
        for name, hint in COLUMN_HINTS.items():
            if any(name == found for found, _ in hits):
                continue
            if re.match(hint, text):
                hits.append((name, word))
                break
    return hits


_NUMERIC_COLUMNS = ("quantity", "unit_price", "tax_rate", "amount")


def _read_column_rows(
    rows: List[List[dict]], columns: Dict[str, Tuple[float, float]]
) -> List[LineItem]:
    """Read item rows below a header.

    To the right of the description, numbers are matched to their columns
    right-to-left rather than by x-range: those columns are right-aligned under
    left-aligned headings, so a value sits well right of its own heading and
    often past the next one. Counting from the amount — always the rightmost —
    keeps them straight even when a row omits a quantity or a price.

    That counting only works while every numeric column is to the right of the
    description, and not every table is built that way: a fuel card puts
    "Eenheden" in front of "Product", and counting from the right would then
    shift every field by one. Columns before the description are read by
    position instead, where left-aligned values do sit under their heading.
    """
    ordered = sorted(columns.items(), key=lambda pair: pair[1][0])
    text_start, text_boundary = columns.get("description", (0.0, 0.0))
    before_text = [
        (name, span) for name, span in ordered
        if name in _NUMERIC_COLUMNS and span[1] <= text_start
    ]
    after_text = [
        name for name, span in ordered
        if name in _NUMERIC_COLUMNS and span[0] >= text_boundary
    ]

    items: List[LineItem] = []
    for row in rows:
        text = " ".join(word["text"] for word in row)
        # The totals block ends the table, and every row of it carries the
        # amount it totals. A row that says "BTW hoog tarief" and nothing else
        # is a note under the line above — the tax the line was charged at —
        # and the table goes on beneath it.
        if _TOTAL_WORDS.search(text) and amount_tokens(text):
            break

        words: List[str] = []
        numbers: List[Tuple[str, float]] = []  # the token and where it sits
        slots: List[int] = []
        percents: List[str] = []
        leading: Dict[str, str] = {}
        for word in row:
            token = word["text"]
            centre = (word["x0"] + word["x1"]) / 2
            if _CURRENCY_MARK.match(token):
                continue  # a sign in front of each amount, not part of the name
            if centre < text_boundary:
                column = next(
                    (n for n, (low, high) in before_text if low <= centre < high),
                    None,
                )
                if column is None:
                    words.append(token)
                elif _PERCENT.fullmatch(token) or amount_tokens(token):
                    # "47,90 Ltr" puts the unit beside the quantity, and the
                    # station name a column further left; only numbers count.
                    leading[column] = token
            elif _PERCENT.fullmatch(token):
                percents.append(token)
            elif amount_tokens(token):
                slots.append(len(words))  # where it stands in the description
                numbers.append((token, centre))
            else:
                words.append(token)

        # Counting from the right only says which number is the amount; it does
        # not say that there is one. A description too long for its column runs
        # past the end of it — "17m1 beglazingspakket t.b.v. 88.4 – 40x30x1" —
        # and the measurement in it would be read as the line amount unless the
        # rightmost number is where the amounts are printed. It belongs to the
        # sentence it was taken out of, so it goes back where it came from.
        if numbers and not _within(numbers[-1][1], columns.get("amount")):
            for moved, ((token, _), slot) in enumerate(zip(numbers, slots)):
                words.insert(slot + moved, token)
            numbers = []

        cells = {"description": " ".join(words).strip(), **leading}
        if percents:
            cells["tax_rate"] = percents[-1]

        targets = [n for n in after_text if n != "tax_rate" or not percents]
        for name, (token, _) in zip(reversed(targets), reversed(numbers)):
            cells[name] = token

        item = _cells_to_item(cells)
        if item:
            items.append(item)
        elif items and cells["description"] and not (numbers or percents):
            # A row under the description column with no amount of its own
            # continues the line above it. An insurance premium is charged on
            # one row and then said in six — the policy, what is insured, the
            # registration, the period — and dropping them leaves a line that
            # nobody can place; a specification wrapped over several rows is
            # the same shape.
            items[-1].description = _joined_description(
                items[-1].description, cells["description"]
            )
    return items


# Room for a description that runs on, and a stop before a line item turns into
# a page of prose. UBL carries the name of what was bought, not its datasheet.
_DESCRIPTION_LIMIT = 300


def _joined_description(description: str, continuation: str) -> str:
    if len(description) >= _DESCRIPTION_LIMIT:
        return description
    return f"{description} {continuation}".strip()


def _within(centre: float, span: Optional[Tuple[float, float]]) -> bool:
    """Whether a word sits in a column. No column at all takes anything."""
    return span is None or span[0] <= centre < span[1]


def _cells_to_item(cells: Dict[str, str]) -> Optional[LineItem]:
    description = cells.get("description", "").strip()
    if not description or not re.search(r"[A-Za-z]{2}", description):
        return None
    if _TOTAL_WORDS.match(description):
        return None

    amount = _single_amount(cells.get("amount", ""))
    if amount is None:
        return None

    rate_match = _PERCENT.search(cells.get("tax_rate", ""))
    rate = parse_amount(rate_match.group(1)) if rate_match else None
    if rate is None:
        bare = _single_amount(cells.get("tax_rate", ""))
        rate = bare if bare is not None and 0 <= bare <= 30 else None

    quantity = _single_amount(cells.get("quantity", ""))
    unit_price = _single_amount(cells.get("unit_price", ""))
    return LineItem(
        description=description,
        quantity=quantity,
        unit_price=_price_the_line_agrees_with(
            quantity, unit_price, amount, cells.get("unit_price", "")
        ),
        tax_rate=rate,
        amount=amount,
    )


def _price_the_line_agrees_with(
    quantity: Optional[float],
    unit_price: Optional[float],
    amount: float,
    token: str,
) -> Optional[float]:
    """Re-read a unit price whose comma was taken for a thousands separator.

    Nothing in "1,938" says whether it is one thousand nine hundred and
    thirty-eight or one and a bit — a fuel price per litre is written exactly
    like that. The line says which: 47,90 litres at 1.938 comes to the 92,82
    charged, and at 1938 to ninety-two thousand.
    """
    if unit_price is None or quantity is None or not quantity:
        return unit_price
    tolerance = max(0.02, abs(amount) * 0.001)
    if abs(quantity * unit_price - amount) <= tolerance:
        return unit_price
    as_decimal = parse_amount(token, decimal_comma=True)
    if as_decimal is not None and abs(quantity * as_decimal - amount) <= tolerance:
        return as_decimal
    return unit_price


def _single_amount(text: str) -> Optional[float]:
    values = amount_tokens(text)
    return values[-1] if values else None


def _is_column_header_text(line: str) -> bool:
    hits = 0
    for token in line.lower().split():
        token = token.strip(".:")
        if any(re.match(hint, token) for hint in COLUMN_HINTS.values()):
            hits += 1
    return hits >= 3


def _items_from_text(doc: Document) -> List[LineItem]:
    """Last resort: lines that end in amounts, between header and totals."""
    items: List[LineItem] = []
    started = False
    for line in doc.lines:
        if _is_column_header_text(line):
            started = True
            continue
        if _TOTAL_WORDS.search(line):
            if started:
                break
            continue
        if ":" in line:  # header fields such as "Invoice number: 1"
            continue

        head, priced, rate = _split_trailing_amounts(strip_dates(line))
        if len(priced) < 2 or not re.search(r"[A-Za-z]{3}", head):
            continue
        if not any(written_with_decimals(token) for token, _ in priced):
            continue

        amounts = [value for _, value in priced]
        quantity = unit_price = None
        if len(amounts) >= 3:
            quantity, unit_price, amount = amounts[-3], amounts[-2], amounts[-1]
        else:
            unit_price, amount = amounts
            if unit_price:
                ratio = amount / unit_price
                if abs(ratio - round(ratio)) < 0.01 and 0 < ratio < 1000:
                    quantity = float(round(ratio))

        leading = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*(?:x|stuks?|pcs?)\s+(.*)$", head)
        if leading:
            quantity = quantity or parse_amount(leading.group(1))
            head = leading.group(2)

        items.append(
            LineItem(
                description=head.strip(" .-"),
                quantity=quantity,
                unit_price=unit_price,
                tax_rate=rate,
                amount=amount,
            )
        )
    return items


def _split_trailing_amounts(
    line: str,
) -> Tuple[str, List[Tuple[str, float]], Optional[float]]:
    """Peel amount tokens off the right-hand side of a line."""
    tokens = line.split()
    amounts: List[Tuple[str, float]] = []
    rate: Optional[float] = None

    while tokens:
        token = tokens[-1]
        percent = _PERCENT.fullmatch(token)
        if percent:
            rate = parse_amount(percent.group(1))
            tokens.pop()
            continue
        values = money_tokens(token)
        if not values or len(amounts) >= 4:
            break
        amounts.insert(0, values[-1])
        tokens.pop()

    return " ".join(tokens), amounts, rate


# --- consistency ------------------------------------------------------------


# VAT rates in use across the EU, standard and reduced. A rate derived from the
# totals is only believed when it lands on one of these: on an invoice with more
# than one rate the division yields a blend, and a blend almost never does.
_KNOWN_TAX_RATES = (
    0, 2.1, 2.5, 3, 4, 4.8, 5, 5.5, 6, 7, 7.7, 8, 8.1, 9, 10, 11, 12, 13, 13.5,
    14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 25.5, 27,
)


def _reconcile(inv: Invoice) -> None:
    """Fill in derivable totals and warn about anything that does not add up."""
    net, tax, gross = inv.total_net, inv.total_tax, inv.total_gross

    if net is None and gross is not None and tax is not None:
        inv.total_net = round(gross - tax, 2)
    elif tax is None and gross is not None and net is not None:
        inv.total_tax = round(gross - net, 2)
    elif gross is None and net is not None and tax is not None:
        inv.total_gross = round(net + tax, 2)
    elif gross is None and net is not None and inv.tax_rate is not None:
        inv.total_tax = round(net * inv.tax_rate / 100, 2)
        inv.total_gross = round(net + inv.total_tax, 2)

    net, tax, gross = inv.total_net, inv.total_tax, inv.total_gross
    if inv.tax_rate is None:
        inv.tax_rate = _rate_from_totals(net, tax)

    if None not in (net, tax, gross) and abs(net + tax - gross) > 0.02:
        inv.warnings.append(
            f"totals do not add up: net {net} + tax {tax} != gross {gross}"
        )

    # No warning when the lines do not add up to the net total. A grouped
    # specification prints its subtotals as rows of their own, so the sum is
    # legitimately higher than the invoice total, and the invoice is still
    # right. The UBL side handles it: it only sends detail lines when they
    # agree with the total, so the document Exact receives always adds up.

    for name in ("invoice_number", "invoice_date", "total_gross"):
        if getattr(inv, name) is None:
            inv.warnings.append(f"missing required field: {name}")
    if not inv.lines:
        inv.warnings.append("no invoice lines detected")


def _rate_from_totals(
    net: Optional[float], tax: Optional[float]
) -> Optional[float]:
    """The VAT rate the totals imply, when the invoice never prints it as "21%".

    A VAT summary table sets the rate in a column of its own ("Btwcode % ..."
    over "1 21,00 ..."), where the number carries no percent sign and cannot be
    told from the amounts beside it. Without a rate the UBL tax category goes
    out with no Percent, which NLCIUS requires for a standard-rated invoice.
    """
    if not net or tax is None or net <= 0:
        return None
    rate = min(_KNOWN_TAX_RATES, key=lambda known: abs(known - tax / net * 100))
    if abs(round(net * rate / 100, 2) - tax) > 0.02:
        return None
    return float(rate)
