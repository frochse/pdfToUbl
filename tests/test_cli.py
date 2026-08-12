import json

from pdfinvoice.cli import main


def test_json_output_single_file(nl, capsys):
    assert main([str(nl), "-f", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["invoice_number"] == "2026-0042"
    assert data["total_gross"] == 2571.86
    assert len(data["lines"]) == 3


def test_multiple_files_produce_csv_rows(samples, capsys):
    # Only the generated fixtures: real invoices dropped into samples/ may be
    # scans with no text layer, which are a read failure rather than a row.
    generated = sorted(samples.glob("invoice_*.pdf"))
    argv = [str(p) for p in generated] + ["-f", "csv", "--date-order", "dmy"]

    assert main(argv) == 0
    rows = capsys.readouterr().out.strip().splitlines()
    assert rows[0].startswith("source_file,invoice_number")
    assert len(rows) == 1 + len(generated)


def test_text_output_lists_lines(en, capsys):
    assert main([str(en)]) == 0
    out = capsys.readouterr().out
    assert "Invoice number    : INV-2026-118" in out
    assert "Consulting services" in out


def test_raw_text_dumps_extracted_text(nl, capsys):
    assert main([str(nl), "--raw-text"]) == 0
    assert "Factuurnummer: 2026-0042" in capsys.readouterr().out


def test_missing_file_exits_nonzero(tmp_path, capsys):
    assert main([str(tmp_path / "nope.pdf")]) == 1
    assert "no such file" in capsys.readouterr().err


def test_strict_flags_warnings(tmp_path, capsys):
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"%PDF-1.4\n%%EOF\n")
    assert main([str(empty)]) == 1  # unreadable/no text
    capsys.readouterr()


def test_output_file(nl, tmp_path):
    target = tmp_path / "out.json"
    assert main([str(nl), "-f", "json", "-o", str(target)]) == 0
    assert json.loads(target.read_text())["supplier"]["name"] == "Axual B.V."
