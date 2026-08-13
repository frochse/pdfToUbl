"""The file of registered company names, and what it tolerates."""

import json

from pdfinvoice import kvk


def _clear():
    kvk.names.cache_clear()


def test_a_bundled_number_resolves_to_its_registered_name():
    _clear()
    assert kvk.registered_name("77731964") == "Garagebedrijf Roos B.V."


def test_a_number_nobody_wrote_down_resolves_to_nothing():
    _clear()
    assert kvk.registered_name("99999999") is None
    assert kvk.registered_name(None) is None
    assert kvk.registered_name("") is None


def test_a_number_is_matched_however_it_is_printed():
    """"K.v.K. 7773 1964" and "77731964" are the same registration."""
    _clear()
    assert kvk.registered_name("7773 1964") == "Garagebedrijf Roos B.V."
    assert kvk.registered_name("77.73.19.64") == "Garagebedrijf Roos B.V."


def test_your_own_file_is_merged_over_the_bundled_one(tmp_path, monkeypatch):
    own = tmp_path / "kvk-names.json"
    own.write_text(json.dumps({
        "12345678": "Voorbeeld Holding B.V.",
        "77731964": "Garagebedrijf Roos B.V. (gestopt)",
    }))
    monkeypatch.setenv(kvk.NAMES_ENV, str(own))
    _clear()

    assert kvk.registered_name("12345678") == "Voorbeeld Holding B.V."
    assert kvk.registered_name("77731964") == "Garagebedrijf Roos B.V. (gestopt)"
    _clear()


def test_a_broken_name_file_does_not_stop_an_invoice_from_being_read(
    tmp_path, monkeypatch
):
    """The name is an improvement on what the page says, never a reason to
    fail: a half-written file falls back to the bundled names."""
    broken = tmp_path / "kvk-names.json"
    broken.write_text('{"12345678": ')
    monkeypatch.setenv(kvk.NAMES_ENV, str(broken))
    _clear()

    assert kvk.registered_name("12345678") is None
    assert kvk.registered_name("77731964") == "Garagebedrijf Roos B.V."
    _clear()


def test_entries_that_are_not_names_are_skipped(tmp_path, monkeypatch):
    odd = tmp_path / "kvk-names.json"
    odd.write_text(json.dumps({
        "11111111": "",
        "22222222": None,
        "not a number": "Voorbeeld BV",
        "33333333": "  Spaties BV  ",
    }))
    monkeypatch.setenv(kvk.NAMES_ENV, str(odd))
    _clear()

    assert kvk.registered_name("11111111") is None
    assert kvk.registered_name("22222222") is None
    assert kvk.registered_name("33333333") == "Spaties BV"
    _clear()
