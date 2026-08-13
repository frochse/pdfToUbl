"""Locale-tolerant parsing of amounts and dates found in invoice text."""

from __future__ import annotations

import re
from datetime import date

# Matches "1.234,56", "1,234.56", "1234.56", "1234", optionally negative or
# wrapped in parentheses (accounting negative). A space is deliberately not
# accepted as a thousands separator: on an invoice it separates columns.
AMOUNT_RE = r"-?\(?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?\)?-?"

# The same, anchored, for testing whether a whole whitespace-delimited token
# is an amount ("€1.250,00" yes, "2026-0042" no, "21%" no).
AMOUNT_TOKEN_RE = re.compile(rf"^[€$£]?\s*({AMOUNT_RE})\s*[€$£]?[,;]?$")

_CURRENCY_CHARS = "€$£¥"
_CURRENCY_CODES = {
    "EUR": "EUR",
    "USD": "USD",
    "GBP": "GBP",
    "CHF": "CHF",
    "SEK": "SEK",
    "NOK": "NOK",
    "DKK": "DKK",
    "PLN": "PLN",
    "€": "EUR",
    "$": "USD",
    "£": "GBP",
}


def parse_amount(text: str, decimal_comma: bool = False) -> float | None:
    """Turn a raw amount string into a float, guessing the decimal separator.

    Handles both European (1.234,56) and Anglo (1,234.56) conventions by
    treating the *last* separator as decimal when it is followed by one or two
    digits, and as a thousands separator otherwise.

    Three digits after a comma are the one shape that guess cannot settle:
    "1,938" is 1938 to an Anglo reader and 1.938 to a Dutch one, and a fuel
    price per litre is written exactly like that. `decimal_comma` says to read
    it the Dutch way; the caller has to know something the token does not say.
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    if s.startswith("-") or s.endswith("-"):
        negative = True
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None

    last_dot = s.rfind(".")
    last_comma = s.rfind(",")
    sep_pos = max(last_dot, last_comma)

    if sep_pos == -1:
        digits = s
        decimals = ""
    else:
        tail = s[sep_pos + 1 :]
        three_after_a_lone_comma = (
            decimal_comma
            and s[sep_pos] == ","
            and len(tail) == 3
            and tail.isdigit()
            and s.count(",") == 1
            and "." not in s
        )
        if (len(tail) in (1, 2) and tail.isdigit()) or three_after_a_lone_comma:
            digits = s[:sep_pos]
            decimals = tail
        else:  # separator groups thousands, e.g. "1.234" or "12,000"
            digits = s
            decimals = ""

    digits = re.sub(r"[^\d]", "", digits)
    if not digits and not decimals:
        return None

    value = float(f"{digits or '0'}.{decimals or '0'}")
    return -value if negative else value


_DATE_LIKE = re.compile(r"\b\d{1,4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{2,4}\b")


def money_tokens(text: str) -> list[tuple[str, float]]:
    """Every whitespace-delimited token in `text` that is a plain amount.

    Returns (token, value) pairs: the original token is worth keeping because
    "345.00" and "345" parse to the same number but are not equally likely to
    be money — see `written_with_decimals`.
    """
    found = []
    for token in text.split():
        match = AMOUNT_TOKEN_RE.match(token)
        if not match:
            continue
        value = parse_amount(match.group(1))
        if value is not None:
            found.append((token, value))
    return found


def amount_tokens(text: str) -> list[float]:
    """Values of every amount-shaped token in `text`."""
    return [value for _, value in money_tokens(text)]


def written_with_decimals(token: str) -> bool:
    """True for "345.00" or "1.234,56"; False for a bare "30"."""
    return bool(re.search(r"[.,]\d{1,2}\D*$", token))


def strip_dates(text: str) -> str:
    """Blank out dates so their digits are not mistaken for amounts."""
    return _DATE_LIKE.sub(" ", text)


def detect_currency(text: str) -> str | None:
    """Pick the most plausible currency for a document."""
    for code in ("EUR", "USD", "GBP", "CHF", "SEK", "NOK", "DKK", "PLN"):
        if re.search(rf"\b{code}\b", text):
            return code
    for ch in _CURRENCY_CHARS:
        if ch in text:
            return _CURRENCY_CODES[ch]
    return None


_MONTHS = {
    # English
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    # Dutch
    "mrt": 3, "maart": 3, "mei": 5, "juni": 6, "juli": 7, "augustus": 8,
    "okt": 10, "oktober": 10,
    # German
    "mär": 3, "maerz": 3, "märz": 3, "mai": 5, "dez": 12, "dezember": 12,
    "januar": 1, "februar": 2, "juni ": 6, "oktober ": 10,
}

_NUMERIC_DATE = re.compile(
    r"\b(\d{1,4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{2,4})\b"
)
_TEXT_DATE_DMY = re.compile(
    r"\b(\d{1,2})\s*[-.\s]\s*([A-Za-zÄÖÜäöüé]{3,10})\.?\s*[-.,\s]\s*(\d{4})\b"
)
_TEXT_DATE_MDY = re.compile(
    r"\b([A-Za-z]{3,10})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})\b"
)


def parse_date(text: str, day_first: bool = True) -> date | None:
    """Extract the first date from a string, or None.

    `day_first` disambiguates numeric dates like 03/04/2026; it is ignored when
    one component is unambiguously > 12 or when the month is spelled out.
    """
    if not text:
        return None

    m = _TEXT_DATE_DMY.search(text)
    if m:
        month = _MONTHS.get(m.group(2).lower().rstrip("."))
        if month:
            return _safe_date(int(m.group(3)), month, int(m.group(1)))

    m = _TEXT_DATE_MDY.search(text)
    if m:
        month = _MONTHS.get(m.group(1).lower().rstrip("."))
        if month:
            return _safe_date(int(m.group(3)), month, int(m.group(2)))

    m = _NUMERIC_DATE.search(text)
    if m:
        a, b, c = (int(m.group(i)) for i in (1, 2, 3))
        if a > 31:  # ISO: yyyy-mm-dd
            return _safe_date(a, b, c)
        year = c + 2000 if c < 100 else c
        if a > 12:
            return _safe_date(year, b, a)
        if b > 12:
            return _safe_date(year, a, b)
        return _safe_date(year, b, a) if day_first else _safe_date(year, a, b)

    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None
