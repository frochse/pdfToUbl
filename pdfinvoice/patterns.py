"""Label vocabulary used to locate fields. English, Dutch and German."""

from __future__ import annotations

import re

# A label is followed by an optional separator (":", "#", "nr.") and the value.
# A worded separator has to look like one: either it carries its full stop, or
# the value starts after a space. Without that, "Invoice no. NRDCH-569080" has
# its "NR" eaten as a separator and returns "DCH-569080".
_SEP = (
    r"\s*(?:[:#]|(?:nr|no|number)\.|(?:nr|no|number)(?=[\s:#]))?\s*[:#]?\s*"
)

LABELS = {
    "invoice_number": [
        r"invoice\s*(?:no\.?|nr\.?|number|#)",
        r"factuur\s*(?:nummer|nr\.?|no\.?|#)",
        r"faktuur\s*(?:nummer|nr\.?)",
        r"rechnungs?\s*(?:nummer|nr\.?)",
        r"document\s*(?:no\.?|number)",
    ],
    "invoice_date": [
        r"invoice\s*date",
        r"date\s*of\s*issue",
        r"issue\s*date",
        r"factuur\s*datum",
        r"datum\s*factuur",
        r"rechnungs?\s*datum",
        r"(?<!due\s)\bdatum\b",
        r"(?<!due\s)\bdate\b",
    ],
    "due_date": [
        r"due\s*date",
        r"payment\s*due",
        r"date\s*due",
        r"pay(?:able)?\s*(?:by|before)",
        r"verval\s*(?:datum|dag)",
        r"vervaldatum",
        r"betaal\s*datum",
        r"betalen\s*(?:voor|vóór|uiterlijk)",
        r"f(?:ä|ae)lligkeitsdatum",
    ],
    "order_number": [
        r"(?:inkoop)?order\s*(?:nummer|nr\.?|no\.?|number|#)",
        r"(?:purchase\s*)?order\s*(?:no\.?|nr\.?|number|#)",
        r"\bp\.?o\.?\s*(?:no\.?|nr\.?|number|#)",
        r"(?:uw\s*)?referentie",
        r"your\s*reference",
    ],
    "customer_number": [
        r"customer\s*(?:no\.?|nr\.?|number|id|#)",
        r"client\s*(?:no\.?|number)",
        r"klant\s*(?:nummer|nr\.?|code)",
        r"debiteuren?\s*(?:nummer|nr\.?)",
        r"kunden\s*(?:nummer|nr\.?)",
    ],
}

# Totals are classified per line rather than looked up by label, because the
# same words appear in several buckets ("Total excl. VAT" is a net total, not a
# grand total, and "Total incl. VAT" is not a VAT amount). Checked in order.
NET_TOTAL_RE = re.compile(
    r"sub\s*total|subtotaal|zwischensumme|grondslag|"
    r"(?:total|totaal|amount|bedrag)[\w.\s]{0,12}?"
    r"(?:excl|ex\.|excluding|exclusief|without|before)|"
    r"\bnet(?:to)?\b(?:\s*(?:amount|total|bedrag|betrag))?",
    re.IGNORECASE,
)
GROSS_TOTAL_RE = re.compile(
    r"(?:total|totaal|amount|bedrag)[\w.\s]{0,12}?(?:incl|including|inclusief)|"
    r"amount\s*due|balance\s*due|grand\s*total|total\s*payable|"
    r"te\s*betalen(?:\s*bedrag)?|gesamt(?:betrag|summe)?|endbetrag",
    re.IGNORECASE,
)
TAX_TOTAL_RE = re.compile(
    r"\bv\.?a\.?t\.?\b|\bbtw\b|\btax\b|omzetbelasting|mehrwertsteuer|"
    r"\bmwst\.?|\bust\.?\b",
    re.IGNORECASE,
)
GENERIC_TOTAL_RE = re.compile(r"\btotal\b|\btotaal\b|\bsumme\b", re.IGNORECASE)

TAX_RATE_RE = re.compile(
    r"(?:vat|btw|tax|mwst|ust)[^%\n]{0,20}?(\d{1,2}(?:[.,]\d{1,2})?)\s*%"
    r"|(\d{1,2}(?:[.,]\d{1,2})?)\s*%\s*(?:vat|btw|tax|mwst|ust)",
    re.IGNORECASE,
)

IBAN_RE = re.compile(
    r"\b([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}[ ]?[A-Z0-9]{1,4})\b"
)
BIC_RE = re.compile(r"\b([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b")
VAT_ID_RE = re.compile(
    r"\b(ATU\d{8}|BE0?\d{9,10}|BG\d{9,10}|CY\d{8}[A-Z]|CZ\d{8,10}|"
    r"DE\d{9}|DK\d{8}|EE\d{9}|EL\d{9}|ES[A-Z0-9]\d{7}[A-Z0-9]|FI\d{8}|"
    r"FR[A-Z0-9]{2}\d{9}|HR\d{11}|HU\d{8}|IE\d[A-Z0-9+*]\d{5}[A-Z]{1,2}|"
    r"IT\d{11}|LT(?:\d{9}|\d{12})|LU\d{8}|LV\d{11}|MT\d{8}|NL\d{9}B\d{2}|"
    r"PL\d{10}|PT\d{9}|RO\d{2,10}|SE\d{12}|SI\d{8}|SK\d{10}|GB\d{9}(?:\d{3})?)\b"
)
# "Registration number" is what a company register id is called on an invoice
# printed in English; on a Dutch invoice that is the KvK number.
COC_RE = re.compile(r"(?:kvk|k\.v\.k\.|chamber\s*of\s*commerce|handelsregister|"
                    r"registration\s*(?:number|no\.?|nr\.?)|"
                    r"company\s*(?:registration|number))"
                    r"[^\d\n]{0,15}(\d{6,10})", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PAYMENT_REF_RE = re.compile(
    r"(?:payment\s*reference|betalingskenmerk|kenmerk|reference)\s*[:#]?\s*"
    r"([A-Z0-9][A-Z0-9\-/ ]{4,25})",
    re.IGNORECASE,
)


def label_value_re(label: str) -> re.Pattern:
    """Regex capturing the free-text value that follows a label on a line."""
    return re.compile(rf"{label}{_SEP}([^\n]*)", re.IGNORECASE)
