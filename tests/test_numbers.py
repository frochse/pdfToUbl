from datetime import date

import pytest

from pdfinvoice.numbers import (
    amount_tokens,
    detect_currency,
    parse_amount,
    parse_date,
    strip_dates,
    written_with_decimals,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1.234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("1234.56", 1234.56),
        ("1234", 1234.0),
        ("12,00", 12.0),
        ("1.234", 1234.0),      # thousands separator, not a decimal
        ("12.000", 12000.0),
        ("€ 2.571,86", 2571.86),
        ("$1,200.00", 1200.0),
        ("(45,00)", -45.0),
        ("-99.95", -99.95),
        ("45,00-", -45.0),
        ("", None),
        ("n/a", None),
    ],
)
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


def test_amount_tokens_ignores_identifiers_and_percentages():
    line = "Invoice 2026-0042 BTW 21% bedrag 1.250,00"
    assert amount_tokens(line) == [1250.0]


def test_written_with_decimals():
    assert written_with_decimals("345.00")
    assert written_with_decimals("1.234,56")
    assert not written_with_decimals("30")


def test_strip_dates():
    assert "11" not in strip_dates("Te betalen voor 11-09-2026")


@pytest.mark.parametrize(
    "text, day_first, expected",
    [
        ("12-08-2026", True, date(2026, 8, 12)),
        ("2026-08-12", True, date(2026, 8, 12)),
        ("03/04/2026", True, date(2026, 4, 3)),
        ("03/04/2026", False, date(2026, 3, 4)),
        ("25/12/2026", False, date(2026, 12, 25)),  # unambiguous, order ignored
        ("March 4, 2026", True, date(2026, 3, 4)),
        ("12 augustus 2026", True, date(2026, 8, 12)),
        ("4 Mar 2026", True, date(2026, 3, 4)),
        ("31-02-2026", True, None),                 # not a real date
        ("no date here", True, None),
    ],
)
def test_parse_date(text, day_first, expected):
    assert parse_date(text, day_first=day_first) == expected


def test_detect_currency():
    assert detect_currency("Bedragen in EUR") == "EUR"
    assert detect_currency("Total: £8,640.00") == "GBP"
    assert detect_currency("nothing here") is None


def test_three_digits_after_a_comma_are_thousands_unless_told_otherwise():
    """"1,938" is 1938 to an Anglo reader and 1.938 to a Dutch one; nothing in
    the token settles it."""
    assert parse_amount("1,938") == 1938.0
    assert parse_amount("1,938", decimal_comma=True) == 1.938
    # Everything else reads the same either way.
    assert parse_amount("1.234,56", decimal_comma=True) == 1234.56
    assert parse_amount("47,90", decimal_comma=True) == 47.90
    assert parse_amount("1,234,567", decimal_comma=True) == 1234567.0


def test_a_month_and_a_year_are_a_period_and_not_an_amount():
    """"Maandelijkse kosten juli 2026" heads a column of prices; the year is
    four digits of the shape money has."""
    assert "2026" not in strip_dates("Maandelijkse kosten juli 2026 Aantal Totaal")
    assert "2026" not in strip_dates("Verbruik tot en met 30 juni 2026")
    assert strip_dates("Bedrag 2026,00") == "Bedrag 2026,00"
