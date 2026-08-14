"""Extraction, and the decision to reach for OCR."""

from types import SimpleNamespace

from pdfinvoice.textio import (HIDDEN_TEXT_BAND, _image_only_band,
                              _no_values_in)


def _page(height, images, words):
    return (
        SimpleNamespace(height=height, images=[
            {"top": top, "bottom": bottom} for top, bottom in images
        ]),
        [{"top": top, "bottom": top + 10} for top in words],
    )


def test_a_letterhead_baked_into_the_page_image_is_found():
    """Stationery scanned into the page: the invoice has a text layer, but its
    first 175 points are picture and carry no word at all."""
    page, words = _page(842, [(0, 842)], words=[175, 190, 205, 400, 600])

    assert _image_only_band(page, words) >= HIDDEN_TEXT_BAND


def test_a_logo_above_the_text_is_not_a_hidden_letterhead():
    """An ordinary digital invoice: a small logo, then text right below it."""
    page, words = _page(842, [(30, 80)], words=[95, 110, 300, 500, 700])

    assert _image_only_band(page, words) < HIDDEN_TEXT_BAND


def test_a_wide_margin_with_no_picture_behind_it_is_just_a_margin():
    page, words = _page(842, [], words=[300, 320, 340])

    assert _image_only_band(page, words) == 0.0


def test_a_template_that_kept_only_its_own_headings_is_read_again():
    """One insurance invoice extracts as the form it was printed from:

        Factuur
        Omschrijving Bedrag
        Totaal
        09375345

    Every value it was filled in with is unreadable, so the page says nothing
    while looking like a page with a text layer. An amount with cents in it is
    what says otherwise — the collection number above is digits, not money.
    """
    assert _no_values_in(
        "Factuur\nOmschrijving Bedrag\nTotaal\n09375345\n"
        "Vaartweg 81 K.v.K. 32039196 Iban NL 24 INGB 0000 1687 56\n"
    )


def test_a_text_layer_with_amounts_in_it_is_left_alone():
    """OCR of a page whose text is already exact trades a missing letterhead
    for misread amounts."""
    assert not _no_values_in(
        "Factuurnummer 26800059\nOmschrijving BTW% Aantal Prijs Subtotaal\n"
        "Product 21% 1 € 3.490,00 € 3.490,00\nTotaal incl. BTW € 1.358,17\n"
    )
