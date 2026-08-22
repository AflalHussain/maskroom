"""PDF redaction: detected values must not survive in the output text layer
(native PDFs) or in a fresh OCR of the redacted page (scans), and values
that must be kept must still be present."""
import os

import pytest

try:
    import pymupdf as fitz
except ImportError:
    import fitz

CASES = [
    # (file, must_be_gone, must_remain)
    ("PII_Test_Sample_LK.pdf", ["Nimal", "Perera"], ["TELECOM"]),
    ("SC_Judgment_Sample.pdf", [], ["SC"]),
]


def _text_after_redaction(engine, path, scanned):
    doc = fitz.open(path)
    if not scanned:
        return " ".join(p.get_text() for p in doc)
    tessdata = engine._find_tessdata()
    if tessdata is None:
        pytest.skip("tesseract language data not installed")
    return " ".join(p.get_text(textpage=p.get_textpage_ocr(language="eng", dpi=300,
                                                            full=True, tessdata=tessdata))
                    for p in doc)


@pytest.mark.parametrize("name,gone,remain", CASES)
def test_native_pdf_no_leaks(make_engine, data_path, tmp_path, name, gone, remain):
    e = make_engine()
    out = tmp_path / "out.pdf"
    e.redact_spatial_pdf(data_path(name), str(out))
    assert e.report, "no findings at all"
    text = _text_after_redaction(e, str(out), scanned=False)
    # every value the engine decided to redact must be gone from the text layer
    for entry in e.report:
        if len(entry["text"]) >= 5:
            assert entry["text"] not in text, entry
    for g in gone:
        assert g not in text
    for r in remain:
        assert r in text


def test_scanned_pdf_no_leaks(make_engine, data_path, tmp_path):
    e = make_engine()
    out = tmp_path / "out.pdf"
    e.redact_spatial_pdf(data_path("PII_Test_Sample_LK_SCANNED.pdf"), str(out))
    assert any(x["method"] == "detected" for x in e.report)
    text = _text_after_redaction(e, str(out), scanned=True)
    for entry in e.report:
        if entry["entity"] in ("PERSON", "LK_NIC", "PHONE_NUMBER", "EMAIL_ADDRESS") \
                and len(entry["text"]) >= 5:
            assert entry["text"] not in text, entry
