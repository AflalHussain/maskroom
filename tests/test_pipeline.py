"""File-masking pipeline: extension dispatch, FileMaskResult, and the
supported-extension contract — exercised directly, with no Flask layer."""
import zipfile

import pytest

from maskroom.pipeline import SUPPORTED_EXTS, FileMaskResult, mask_file


def _make_docx(path, runs):
    body = "".join(f"<w:r><w:t>{t}</w:t></w:r>" for t in runs)
    doc = ('<?xml version="1.0"?><w:document xmlns:w="w"><w:body><w:p>'
           + body + "</w:p></w:body></w:document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", doc)


def test_supported_exts_are_the_masking_set():
    assert set(SUPPORTED_EXTS) == {".xlsx", ".xlsm", ".pdf", ".docx", ".pptx",
                                   ".csv", ".tsv", ".txt", ".json"}


def test_unsupported_ext_raises_before_touching_the_file(make_engine):
    e = make_engine()
    with pytest.raises(ValueError, match="unsupported file type"):
        mask_file(e, "/no/such/file.png", "/tmp/out.png")


def test_excel_dispatch(make_engine, data_path, tmp_path):
    e = make_engine()
    out = tmp_path / "out.xlsx"
    res = mask_file(e, data_path("PII_Test_Dataset_LK.xlsx"), str(out))
    assert isinstance(res, FileMaskResult)
    assert res.kind == "excel" and res.mode == "pseudonymize"
    assert out.exists() and out.stat().st_size > 0
    assert res.findings  # a dataset full of PII produces findings


def test_pdf_redact_dispatch(make_engine, data_path, tmp_path):
    e = make_engine()
    out = tmp_path / "out.pdf"
    res = mask_file(e, data_path("PII_Test_Sample_LK.pdf"), str(out))
    assert res.kind == "pdf" and res.mode == "redact"
    assert out.exists() and out.stat().st_size > 0


def test_pdf_text_mode_dispatch(make_engine, data_path, tmp_path):
    e = make_engine()
    out = tmp_path / "out.md"
    res = mask_file(e, data_path("PII_Test_Sample_LK.pdf"), str(out), pdf_text=True)
    assert res.kind == "pdf" and res.mode == "text"
    assert out.exists() and out.stat().st_size > 0


def test_office_dispatch_masks(make_engine, tmp_path):
    e = make_engine()
    src = tmp_path / "letter.docx"
    _make_docx(src, ["Dear Nimal Perera, ", "your NIC 853421234V is on file."])
    out = tmp_path / "out.docx"
    res = mask_file(e, str(src), str(out))
    assert res.kind == "office" and res.mode == "pseudonymize"
    with zipfile.ZipFile(out) as z:
        xml = z.read("word/document.xml").decode()
    assert "Nimal Perera" not in xml and "TOK_PERSON_" in xml


def test_tabular_dispatch_masks(make_engine, tmp_path):
    e = make_engine()
    src = tmp_path / "staff.csv"
    src.write_text("Name,NIC\nNimal Perera,853421234V\n")
    out = tmp_path / "out.csv"
    res = mask_file(e, str(src), str(out))
    assert res.kind == "tabular" and res.mode == "pseudonymize"
    body = out.read_text()
    assert "Nimal Perera" not in body and "853421234V" not in body


def test_text_dispatch_masks(make_engine, tmp_path):
    e = make_engine()
    src = tmp_path / "rec.json"
    src.write_text('{"name": "Nimal Perera", "nic": "853421234V"}')
    out = tmp_path / "out.json"
    res = mask_file(e, str(src), str(out))
    assert res.kind == "text" and res.mode == "pseudonymize"
    assert "Nimal Perera" not in out.read_text()
