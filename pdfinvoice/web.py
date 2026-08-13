"""A small local web front end for the parser.

It runs on your machine, holds nothing on disk beyond the request, and talks to
the same `pdfinvoice` code the CLI uses. Uploaded PDFs are parsed in a
temporary directory that is deleted before the response is sent.
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
import threading
import webbrowser
from pathlib import Path
from typing import List

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from . import config, mailer, ubl
from .export import csv_string
from .model import Invoice
from .parser import parse
from .textio import extract, ocr_available

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
# The mail button's recipient. Empty in the source on purpose: the purchase
# inbox of an accounting package accepts whatever is sent to it, so the address
# is a setting and never a literal in a repository. --set-mail-to writes it,
# PDFINVOICE_MAIL_TO overrides it for one run, --mail-to for one command.
DEFAULT_MAIL_TO = ""
MAIL_TO_SETTING = "mail_to"
MAIL_TO_ENV = "PDFINVOICE_MAIL_TO"
# Enough of a check to catch a typed mistake; the mail client judges the rest.
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def configured_mail_to() -> str:
    """The recipient to start from: the environment, else the settings file."""
    return (os.environ.get(MAIL_TO_ENV)
            or config.get(MAIL_TO_SETTING)
            or DEFAULT_MAIL_TO)


def create_app(mail_to: str = DEFAULT_MAIL_TO) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.config["MAIL_TO"] = mail_to

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            ocr_available=ocr_available(),
            mail_available=mailer.mail_available(),
            mail_to=app.config["MAIL_TO"],
        )

    @app.get("/api/health")
    def health():
        return jsonify(status="ok", ocr_available=ocr_available(),
                       mail_available=mailer.mail_available())

    @app.post("/api/email")
    def api_email():
        """Open a draft in Mail with the UBL attached — and only the UBL.

        The PDF is inside it already, base64 in AdditionalDocumentReference,
        and that is where Exact takes the invoice image from. Sent alongside
        as a second attachment it becomes a second document in the purchase
        inbox: one invoice arriving twice.

        The browser sends the UBL back rather than the server keeping it: this
        app holds nothing between requests, and a draft can be asked for long
        after the parse.
        """
        ubl_xml = request.form.get("ubl")
        if not ubl_xml:
            return jsonify(error="de UBL ontbreekt"), 400

        recipient = (request.form.get("recipient") or "").strip()
        if "@" not in recipient:
            return jsonify(error="geen geldig e-mailadres opgegeven"), 400

        stem = Path(secure_filename(request.form.get("stem") or "factuur")).stem
        subject = request.form.get("subject") or f"Factuur {stem}"
        body = request.form.get("body") or (
            "Bijgaand de factuur als UBL. De PDF zit als bijlage in het "
            "UBL-bestand zelf.\n"
        )

        try:
            directory = mailer.outbox()
            attachments = [mailer.write_ubl(directory, ubl_xml, f"{stem}.xml")]
            mailer.compose(recipient, subject, body, attachments)
        except mailer.MailError as exc:
            return jsonify(error=str(exc)), 503
        except Exception as exc:
            return jsonify(error=f"mail aanmaken mislukt: {exc}"), 500

        return jsonify(
            ok=True,
            recipient=recipient,
            attachments=[p.name for p in attachments],
        )

    @app.post("/api/parse")
    def api_parse():
        uploads = [f for f in request.files.getlist("files") if f.filename]
        if not uploads:
            return jsonify(error="er zijn geen bestanden geüpload"), 400

        day_first = request.form.get("date_order", "dmy") != "mdy"
        ocr = request.form.get("ocr", "auto")
        currency = (request.form.get("currency") or "EUR").upper()[:3]
        if ocr not in ("auto", "never", "always"):
            ocr = "auto"

        results: List[dict] = []
        invoices: List[Invoice] = []

        with tempfile.TemporaryDirectory() as tmp:
            for upload in uploads:
                name = secure_filename(upload.filename) or "upload.pdf"
                if not name.lower().endswith(".pdf"):
                    results.append(_failure(name, "geen PDF-bestand"))
                    continue

                path = Path(tmp) / name
                upload.save(path)
                try:
                    doc = extract(path, ocr=ocr)
                except Exception as exc:
                    results.append(_failure(name, f"PDF kon niet gelezen worden: {exc}"))
                    continue

                if not doc.text.strip():
                    hint = "" if ocr_available() else " — installeer ocrmypdf om scans te lezen"
                    results.append(_failure(name, f"geen tekstlaag gevonden{hint}"))
                    continue

                # One malformed invoice must not take down the whole batch.
                try:
                    invoice = parse(doc, day_first=day_first)
                    invoice.source_file = name  # never leak the temp path
                    entry = {
                        "file": name,
                        "ok": True,
                        "invoice": invoice.to_dict(),
                        "ubl": ubl.to_xml(invoice, default_currency=currency,
                                          pdf_bytes=path.read_bytes(),
                                          pdf_name=name),
                        "ubl_warnings": ubl.conformance_warnings(invoice),
                        "addresses": {
                            "supplier": ubl.postal_address(invoice.supplier),
                            "customer": ubl.postal_address(invoice.customer),
                        },
                        "raw_text": doc.text,
                    }
                except Exception as exc:
                    results.append(_failure(name, f"omzetten mislukt: {exc}"))
                    continue

                invoices.append(invoice)
                results.append(entry)

        return jsonify(results=results, csv=csv_string(invoices))

    @app.errorhandler(413)
    def too_large(_):
        limit = MAX_UPLOAD_BYTES // (1024 * 1024)
        return jsonify(error=f"upload is groter dan de limiet van {limit} MB"), 413

    @app.errorhandler(500)
    def server_error(exc):
        # Flask's HTML error page parses as JSON in the browser about as well as
        # it reads: "Unexpected token '<'". Keep /api answering in JSON.
        app.logger.exception("unhandled error")
        if request.path.startswith("/api/"):
            original = getattr(exc, "original_exception", exc)
            return jsonify(error=f"serverfout: {original}"), 500
        return exc

    return app


def _failure(name: str, message: str) -> dict:
    return {"file": name, "ok": False, "error": message}


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pdfinvoice-web",
        description="Serve the local web front end for the PDF invoice reader.",
    )
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to bind (default: 127.0.0.1, local only)")
    ap.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open a browser window on start")
    ap.add_argument("--debug", action="store_true", help="enable Flask reloader")
    ap.add_argument("--mail-to", default=None, metavar="ADDRESS",
                    help="recipient for this run, e.g. the purchase inbox of "
                         "your accounting package (default: the saved one)")
    ap.add_argument("--set-mail-to", metavar="ADDRESS",
                    help="save ADDRESS as the default recipient and exit; it "
                         f"is written to {config.path()}, never to the source")
    ap.add_argument("--show-config", action="store_true",
                    help="print the saved settings and exit")
    args = ap.parse_args(argv)

    if args.set_mail_to is not None:
        return _save_mail_to(args.set_mail_to)
    if args.show_config:
        saved = config.get(MAIL_TO_SETTING) or "(none)"
        print(f"settings file : {config.path()}")
        print(f"mail_to       : {saved}")
        if os.environ.get(MAIL_TO_ENV):
            print(f"{MAIL_TO_ENV}: {os.environ[MAIL_TO_ENV]}  (overrides it)")
        return 0

    mail_to = args.mail_to if args.mail_to is not None else configured_mail_to()
    url = f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}/"
    print(f"pdfinvoice web UI on {url}  (ctrl-c to stop)")
    if mail_to:
        print(f"mail button addressed to {mail_to}")
    if not args.no_browser and not args.debug:
        threading.Timer(0.7, webbrowser.open, args=(url,)).start()

    create_app(mail_to=mail_to).run(
        host=args.host, port=args.port, debug=args.debug
    )
    return 0


def _save_mail_to(address: str) -> int:
    """Write the default recipient, or refuse an address that cannot be one."""
    address = address.strip()
    if address and not _ADDRESS.match(address):
        print(f"not an e-mail address: {address!r}")
        return 2
    written = config.set_value(MAIL_TO_SETTING, address)
    print(f"default recipient {'cleared' if not address else address} "
          f"in {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
