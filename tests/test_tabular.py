"""CSV/TSV masking (column-aware, reusing the Excel engine) and text/JSON."""
import csv


def _read(path, delim=","):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f, delimiter=delim))


def test_csv_is_column_aware(make_engine, tmp_path):
    e = make_engine()
    src = tmp_path / "staff.csv"
    src.write_text("Name,NIC,Phone,Salary\n"
                   "Nimal Perera,853421234V,077-1234567,120000\n"
                   "Kumari Bandara,199085601234,071-9876543,98000\n")
    out = tmp_path / "staff_masked.csv"
    e.pseudonymize_csv(str(src), str(out))
    rows = _read(out)
    assert rows[0] == ["Name", "NIC", "Phone", "Salary"]        # header untouched
    assert rows[1][0].startswith("TOK_PERSON_")                  # Name column masked
    assert "853421234V" not in rows[1][1] and rows[1][1].startswith("TOK_")
    assert rows[1][3] == "120000" and rows[2][3] == "98000"      # salary kept (analytical)
    # a raw text pass would miss the plain name "Kumari Bandara"; column rule catches it
    assert rows[2][0].startswith("TOK_PERSON_")


def test_tsv_delimiter_preserved(make_engine, tmp_path):
    e = make_engine()
    src = tmp_path / "d.tsv"
    src.write_text("Name\tNIC\nRuwan Jayawardena\t912345678V\n")
    out = tmp_path / "d_masked.tsv"
    e.pseudonymize_csv(str(src), str(out))
    rows = _read(out, "\t")
    assert rows[0] == ["Name", "NIC"] and rows[1][0].startswith("TOK_PERSON_")
    assert "912345678V" not in open(out, encoding="utf-8").read()


def test_row_widths_preserved(make_engine, tmp_path):
    e = make_engine()
    src = tmp_path / "ragged.csv"
    src.write_text("Name,NIC\nNimal Perera,853421234V\nnote,,extra\n")
    out = tmp_path / "r.csv"
    e.pseudonymize_csv(str(src), str(out))
    rows = _read(out)
    assert len(rows[1]) == 2 and len(rows[2]) == 3


def test_text_file_free_text(make_engine, tmp_path):
    e = make_engine()
    src = tmp_path / "note.txt"
    src.write_text("Call Nimal Perera on 077-1234567 about NIC 853421234V.")
    out = tmp_path / "note_masked.txt"
    e.pseudonymize_text_file(str(src), str(out))
    masked = out.read_text()
    # free-text: values are removed (entity labels may vary; that's why CSV
    # uses the column-aware path instead)
    assert "Nimal Perera" not in masked and "853421234V" not in masked
    assert "077-1234567" not in masked and "TOK_PERSON_" in masked


def test_json_free_text(make_engine, tmp_path):
    e = make_engine()
    src = tmp_path / "c.json"
    src.write_text('{"customer": "Nimal Perera", "nic": "853421234V"}')
    out = tmp_path / "c_masked.json"
    e.pseudonymize_text_file(str(src), str(out))
    masked = out.read_text()
    assert "Nimal Perera" not in masked and "853421234V" not in masked and "TOK_PERSON_" in masked
