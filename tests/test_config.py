"""The settings file, and which source of the recipient wins."""

import json

from pdfinvoice import config, web


def _settings_at(tmp_path, monkeypatch, contents=None):
    target = tmp_path / "config.json"
    if contents is not None:
        target.write_text(contents)
    monkeypatch.setenv(config.CONFIG_ENV, str(target))
    monkeypatch.delenv(web.MAIL_TO_ENV, raising=False)
    return target


def test_a_saved_address_becomes_the_default(tmp_path, monkeypatch):
    _settings_at(tmp_path, monkeypatch)
    web.main(["--set-mail-to", "85e5242h@inkoop.exactonline.nl"])

    assert web.configured_mail_to() == "85e5242h@inkoop.exactonline.nl"


def test_nothing_saved_means_no_address_at_all(tmp_path, monkeypatch):
    """The source ships without one: an address here would be an open door
    into someone's bookkeeping."""
    _settings_at(tmp_path, monkeypatch)

    assert web.DEFAULT_MAIL_TO == ""
    assert web.configured_mail_to() == ""


def test_the_environment_overrides_the_saved_address(tmp_path, monkeypatch):
    _settings_at(tmp_path, monkeypatch, json.dumps({"mail_to": "saved@example.nl"}))
    monkeypatch.setenv(web.MAIL_TO_ENV, "run@example.nl")

    assert web.configured_mail_to() == "run@example.nl"


def test_saving_leaves_the_other_settings_alone(tmp_path, monkeypatch):
    target = _settings_at(tmp_path, monkeypatch,
                          json.dumps({"iets_anders": "blijft staan"}))
    web.main(["--set-mail-to", "inkoop@example.nl"])

    assert json.loads(target.read_text()) == {
        "iets_anders": "blijft staan",
        "mail_to": "inkoop@example.nl",
    }


def test_an_address_that_is_not_one_is_refused(tmp_path, monkeypatch):
    target = _settings_at(tmp_path, monkeypatch)

    assert web.main(["--set-mail-to", "geen adres"]) == 2
    assert not target.exists()


def test_the_saved_address_can_be_cleared(tmp_path, monkeypatch):
    _settings_at(tmp_path, monkeypatch, json.dumps({"mail_to": "inkoop@example.nl"}))
    web.main(["--set-mail-to", ""])

    assert web.configured_mail_to() == ""


def test_a_damaged_settings_file_is_not_fatal(tmp_path, monkeypatch):
    """Settings are a convenience, never a precondition for reading invoices."""
    _settings_at(tmp_path, monkeypatch, '{"mail_to": ')

    assert config.load() == {}
    assert web.configured_mail_to() == ""


def test_the_page_is_served_with_the_address_prefilled(tmp_path, monkeypatch):
    _settings_at(tmp_path, monkeypatch)
    app = web.create_app(mail_to="85e5242h@inkoop.exactonline.nl")

    page = app.test_client().get("/").get_data(as_text=True)

    assert 'value="85e5242h@inkoop.exactonline.nl"' in page
