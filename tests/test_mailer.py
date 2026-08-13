import subprocess
from pathlib import Path

import pytest

from pdfinvoice import mailer, web


class _Runner:
    """Stands in for subprocess.run so no real draft is ever opened."""

    def __init__(self, returncode=0, stderr=""):
        self.returncode, self.stderr = returncode, stderr
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, self.returncode, "", self.stderr)


def test_paths_are_passed_as_arguments_not_spliced_into_the_script(tmp_path):
    """A filename can contain a quote, which would otherwise end the string and
    run as AppleScript."""
    awkward = tmp_path / 'factuur "2026" \'s.pdf'
    awkward.write_bytes(b"%PDF-1.4")
    runner = _Runner()

    mailer.compose("a@b.nl", "Onderwerp", "Tekst", [awkward], runner=runner)

    args, kwargs = runner.calls[0]
    assert args[0] == "osascript"
    assert args[1] == "-"  # the script arrives on stdin
    assert args[2:5] == ["a@b.nl", "Onderwerp", "Tekst"]
    assert args[5] == str(awkward.resolve())
    assert "on run argv" in kwargs["input"]
    assert str(awkward) not in kwargs["input"]


def test_a_missing_attachment_is_refused(tmp_path):
    with pytest.raises(mailer.MailError, match="bijlage niet gevonden"):
        mailer.compose("a@b.nl", "s", "b", [tmp_path / "weg.pdf"], runner=_Runner())


def test_a_denied_automation_prompt_is_explained(tmp_path):
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    runner = _Runner(returncode=1, stderr="execution error: Not allowed (-1743)")

    with pytest.raises(mailer.MailError, match="Automatisering"):
        mailer.compose("a@b.nl", "s", "b", [pdf], runner=runner)


def test_the_ubl_is_written_ready_to_attach(tmp_path):
    written = mailer.write_ubl(tmp_path, "<Invoice/>", "F123.xml")

    assert written.name == "F123.xml"
    assert written.read_text(encoding="utf-8") == "<Invoice/>"


def test_the_outbox_sweeps_what_a_previous_run_left(tmp_path, monkeypatch):
    monkeypatch.setattr(mailer.tempfile, "gettempdir", lambda: str(tmp_path))
    stale = tmp_path / "pdfinvoice-mail-oud"
    stale.mkdir()
    (stale / "vorige.pdf").write_bytes(b"x")

    fresh = mailer.outbox()

    assert not stale.exists()
    assert fresh.is_dir() and not list(fresh.iterdir())


# --- the endpoint -----------------------------------------------------------


def _post(client, **fields):
    data = {"ubl": "<Invoice/>", "recipient": "inkoop@example.nl"}
    data.update(fields)
    return client.post("/api/email", data=data,
                       content_type="multipart/form-data")


def test_the_default_recipient_reaches_the_page():
    app = web.create_app(mail_to="inkoop@example.nl")
    with app.test_client() as client:
        assert b'value="inkoop@example.nl"' in client.get("/").data


def test_a_draft_carries_the_ubl_and_nothing_else(monkeypatch, tmp_path):
    """The PDF is inside the UBL; attached again it becomes a second document
    in the purchase inbox, and one invoice arrives twice."""
    seen = {}

    def fake_compose(recipient, subject, body, attachments, **kw):
        seen.update(recipient=recipient, subject=subject,
                    names=[Path(p).name for p in attachments])

    monkeypatch.setattr(web.mailer, "mail_available", lambda: True)
    monkeypatch.setattr(web.mailer, "outbox", lambda: tmp_path)
    monkeypatch.setattr(web.mailer, "compose", fake_compose)

    with web.create_app().test_client() as client:
        response = _post(client, stem="F4353326", subject="Factuur F4353326")

    assert response.status_code == 200
    assert seen["recipient"] == "inkoop@example.nl"
    assert seen["names"] == ["F4353326.xml"]
    assert response.get_json()["attachments"] == ["F4353326.xml"]


def test_a_bad_address_is_refused_before_mail_is_touched(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("Mail should not be touched")

    monkeypatch.setattr(web.mailer, "compose", explode)
    with web.create_app().test_client() as client:
        response = _post(client, recipient="geen-adres")

    assert response.status_code == 400
    assert "e-mailadres" in response.get_json()["error"]


def test_a_mail_failure_answers_in_json(monkeypatch, tmp_path):
    def refuse(*a, **k):
        raise web.mailer.MailError("Mail kon niet gestart worden")

    monkeypatch.setattr(web.mailer, "outbox", lambda: tmp_path)
    monkeypatch.setattr(web.mailer, "compose", refuse)

    with web.create_app().test_client() as client:
        response = _post(client)

    assert response.status_code == 503
    assert response.headers["Content-Type"].startswith("application/json")
    assert "Mail kon niet gestart" in response.get_json()["error"]
