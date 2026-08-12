# pdfinvoice

An invoice reader for PDFs, on the command line or in a local browser UI. It
extracts the text layer with
[pdfplumber](https://github.com/jsvine/pdfplumber) and pulls the fields out with
regular expressions and layout geometry, then writes them as text, JSON, CSV or
an SI-UBL 2.0 (NLCIUS) document ready to import into Exact. No LLM, no API key,
no network access — it runs entirely offline and gives the same answer every
time.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"      # drop [dev] if you don't need the tests
```

Then use `.venv/bin/pdfinvoice`, or `python -m pdfinvoice` without installing.
The browser UI needs Flask: it comes with `[dev]`, or install `".[web]"` on its
own.

## Use

```sh
pdfinvoice invoice.pdf                    # human-readable summary
pdfinvoice invoice.pdf -f json            # full record, including line items
pdfinvoice invoices/ -r -f csv -o out.csv # one row per invoice, recursively
pdfinvoice invoice.pdf -f ubl             # OASIS UBL 2.1 Invoice
pdfinvoice invoices/ -r -f ubl -d ubl/    # one .xml per invoice, into ubl/
pdfinvoice invoice.pdf --raw-text         # just the extracted text
```

```
File              : invoice_nl.pdf
Invoice number    : 2026-0042
Invoice date      : 2026-08-12
Due date          : 2026-09-11
Supplier          : Axual B.V.
Supplier VAT      : NL854103576B01
Supplier IBAN     : NL91ABNA0417164300
Customer          : Voorbeeld Holding B.V.
Net total         : 2,125.50 EUR
VAT               : 446.36 EUR
Gross total       : 2,571.86 EUR

Lines:
  - Kafka support uren
    qty 10  @ 125.00  = 1250.00
```

### Options

| Flag | Meaning |
| --- | --- |
| `-f, --format text\|json\|csv\|ubl` | output format (default `text`); `ubl` is SI-UBL 2.0 |
| `-o, --output FILE` | write to a file instead of stdout |
| `-d, --out-dir DIR` | write one file per invoice into `DIR` — the usual way to emit UBL for a batch |
| `-r, --recursive` | recurse into directories |
| `--currency CODE` | currency for UBL when the PDF does not name one (default `EUR`) |
| `--date-order dmy\|mdy` | how to read `03/04/2026` (default `dmy`) |
| `--ocr auto\|never\|always` | OCR scanned PDFs (default `auto`) |
| `--raw-text` | print extracted text instead of parsing |
| `--strict` | exit 2 when any invoice produced warnings |

Exit codes: `0` success, `1` a file could not be read, `2` `--strict` and
warnings were produced.

### As a library

```python
import pdfinvoice

inv = pdfinvoice.read("invoice.pdf")
print(inv.invoice_number, inv.total_gross, inv.warnings)
print(inv.to_dict())
```

## UBL for Exact

`-f ubl` writes **SI-UBL 2.0 (NLCIUS)**, the Dutch CIUS of EN 16931. Exact
Online accepts it on the Peppol path and Exact Globe Next imports it directly.

```xml
<cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:fdc:nen.nl:nlcius:v1.0</cbc:CustomizationID>
<cbc:ProfileID>urn:fdc:peppol.eu:2017:poacc:billing:01:1.0</cbc:ProfileID>
```

The source PDF is embedded as an attachment, because Exact shows no invoice
image for a document that carries none. That makes the XML roughly 1.4× the size
of the PDF.

When the line items read off the page do not add up to the invoice's own net
total — a grouped specification prints its subtotals as rows of their own, so
the rows sum to more than is being charged — the whole amount is sent as a
single line instead. BR-CO-10 requires the lines to sum to
`LineExtensionAmount`, and Exact rejects a document that contradicts itself.
The detail still travels with the invoice, in the embedded PDF.

A credit note (a negative total) is written as a `CreditNote` document with
positive amounts, not as an `Invoice` with type code 381 — Exact multiplies the
latter by −1, which negates the document twice.

NLCIUS asks for more than a PDF always shows. Anything missing is reported on
stderr, named by the rule that will reject it:

```
$ pdfinvoice invoice.pdf -f ubl -o invoice.xml
invoice.pdf: BR-NL-1: a Dutch supplier needs a KvK or OIN number; none was found
invoice.pdf: BR-NL-3: supplier address is missing city, postcode
```

The common ones are `BR-NL-1` (supplier KvK or OIN), `BR-NL-2` (a buyer or order
reference — `NA` is sent when the invoice prints neither), `BR-NL-3` / `BR-NL-4`
(street, city and postcode as separate fields) and `BR-S-02` (supplier VAT
number). A document with warnings is still written; it is Exact that decides.

Element order follows `UBL-Invoice-2.1.xsd` and `UBL-CreditNote-2.1.xsd`, which
is checked by the tests. Before relying on this in production, run the output
through the Dutch Peppol Authority's schematron
([peppolautoriteit-nl/validation](https://github.com/peppolautoriteit-nl/validation),
`schematron/si-ubl-2.0.sch`) — that is the artifact that actually judges it.

## Web UI

```sh
pdfinvoice-web            # serves http://localhost:8000 and opens a browser
```

Drop PDFs onto the page — several at once is fine — and each one comes back as a
card with the summary fields, the line items, the UBL, the JSON and the raw
text, plus whatever warnings the parse produced. Copy or download any panel
individually, or take the whole batch as UBL, CSV or JSON. The date order,
fallback currency and OCR mode are the same settings as on the command line, in
the bar at the top; the OCR control is disabled when `ocrmypdf` is not
installed.

| Flag | Meaning |
| --- | --- |
| `--host ADDR` | interface to bind (default `127.0.0.1`, local only) |
| `--port N` | port (default `8000`) |
| `--no-browser` | do not open a browser window on start |
| `--debug` | enable the Flask reloader |
| `--mail-to ADDRESS` | default recipient for the mail button |

### Mailing an invoice to Exact (macOS)

Each invoice card has a **Mail PDF + UBL** button. It opens a draft in Apple
Mail with both files attached, addressed to whatever is in the *Mailen naar*
field. That field starts empty; `--mail-to` prefills it, which is where the
purchase inbox of your accounting package goes:

```sh
pdfinvoice-web --mail-to inkoop@example.exact.nl
```

The draft is composed and shown, **never sent**: sending is the step that cannot
be taken back, so it stays with whoever is at the keyboard.

`mailto:` cannot carry attachments, so Mail is driven over AppleScript. The
first time, macOS asks whether the terminal may control Mail — allow it under
System Settings → Privacy & Security → Automation. Until then the button
reports what to turn on. The button is hidden on anything that is not macOS.

The browser sends the PDF back for the mail rather than the server keeping it,
so nothing is held between requests. The two attachments are written to one
directory under the system temp dir, which is swept on the next mail — Mail
still needs them on disk after the draft opens, so they cannot be deleted
straight away.

It is a front end for the same code the CLI uses, meant for your own machine:
uploads are capped at 32 MB, parsed inside a temporary directory that is deleted
before the response is sent, and nothing is stored. There is no authentication,
so leave it on `127.0.0.1` unless you have put something in front of it.

Two JSON endpoints back the page, if you want to drive it from a script:
`GET /api/health` and `POST /api/parse` (multipart: `files` repeated, plus
optional `date_order`, `ocr` and `currency`).

## What it extracts

Invoice number, invoice date, due date, order and customer number, payment
reference, currency, supplier (name, VAT number, CoC number, IBAN, BIC, email),
customer (name and address block), line items (description, quantity, unit
price, VAT rate, amount) and the net / VAT / gross totals.

Labels are recognised in English, Dutch and German — see `pdfinvoice/patterns.py`
to add your own wording.

## How it decides

- **Fields** come from labels (`Invoice no`, `Factuurnummer`, …); a label alone
  on a line takes the value from the line below, which is what two-column
  headers look like once the PDF text layer is flattened.
- **Totals** are classified per line rather than looked up by keyword, because
  "Total excl. VAT" is a net total, "Total incl. VAT" is not a VAT amount, and
  "Terms: Net 30" is neither. Explicit wording beats a bare "Total", and a value
  written with decimals beats a bare integer sharing the line.
- **Line items** are read from ruled tables when the PDF has them; otherwise
  from the x-positions of words under the column headers, mapping numbers to
  columns right-to-left (numeric columns are right-aligned under left-aligned
  headers, so they rarely sit under their own header). A plain-text heuristic is
  the last resort.
- **Supplier and customer** are read as columns, not as lines. A letterhead
  beside a field block flattens to `1016 ED Amsterdam Terms Due on receipt`, and
  an address header to `Date Billed to`, so the words' x-positions are used to
  pull each column apart. Columns are matched by overlap rather than by where
  they start, because a flush-right letterhead begins at a different x on every
  line. `Ship To` is never taken as the customer when a billing address exists.
- **Numbers** are read in both `1.234,56` and `1,234.56` conventions. A space is
  never treated as a thousands separator: on an invoice it separates columns.

Every result is checked: net + VAT must equal gross, line amounts must sum to
the net total, and required fields must be present. Anything off shows up in
`warnings` instead of being silently wrong.

## Scanned PDFs

If a PDF has no text layer, install [`ocrmypdf`](https://ocrmypdf.readthedocs.io)
(`brew install ocrmypdf`) and it is used automatically. Without it you get a
clear error rather than an empty result. OCR'd output is flagged with
`ocr_used: true` — check the numbers.

## Limits

This is a rule-based reader, so an unusual layout gets fields wrong rather than
guessing. When you hit one, run `--raw-text` to see what the PDF actually
contains, then extend `pdfinvoice/patterns.py`. `--strict` in a batch job tells
you which files need a human.

## Tests

```sh
.venv/bin/python -m pytest
```

The suite generates its own sample invoices (Dutch, English, and one with a
ruled table) with reportlab, so there is nothing to check in beyond the code.
