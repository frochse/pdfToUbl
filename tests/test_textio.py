"""Extraction, and the decision to reach for OCR."""

from types import SimpleNamespace

from pdfinvoice.textio import HIDDEN_TEXT_BAND, _image_only_band


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
