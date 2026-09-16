"""Masking Word/PowerPoint (.docx/.pptx): text runs are pseudonymized in
place, non-text parts are preserved, and the result restores cleanly."""
import zipfile

from maskroom.restore import unmask_file


def _make_docx(path, body_runs):
    runs = "".join(f"<w:r><w:t>{t}</w:t></w:r>" for t in body_runs)
    doc = ('<?xml version="1.0"?><w:document xmlns:w="w"><w:body><w:p>'
           + runs + "</w:p></w:body></w:document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", doc)
        z.writestr("word/media/image1.png", b"\x89PNGbinary")


def test_docx_masking_and_roundtrip(make_engine, tmp_path):
    e = make_engine()
    src = tmp_path / "letter.docx"
    _make_docx(src, ["Dear Nimal Perera, ", "your NIC 853421234V is on file."])
    masked = tmp_path / "letter_masked.docx"
    e.pseudonymize_office(str(src), str(masked))

    with zipfile.ZipFile(masked) as z:
        xml = z.read("word/document.xml").decode()
        assert z.read("word/media/image1.png") == b"\x89PNGbinary"  # non-text part intact
    assert "Nimal Perera" not in xml and "853421234V" not in xml
    assert "TOK_PERSON_" in xml and "TOK_LK_NIC_" in xml

    # the same engine's vault restores it
    restored = tmp_path / "letter_restored.docx"
    rep = unmask_file(e, str(masked), str(restored))
    with zipfile.ZipFile(restored) as z:
        back = z.read("word/document.xml").decode()
    assert "Dear Nimal Perera," in back and "853421234V" in back
    assert rep["restored"] >= 2 and not rep["unresolved"]


def test_pptx_masking(make_engine, tmp_path):
    e = make_engine()
    src = tmp_path / "deck.pptx"
    slide = ('<?xml version="1.0"?><p:sld xmlns:a="a"><a:t>Kumari Bandara</a:t>'
             '<a:t> — NIC 199085601234</a:t></p:sld>')
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("ppt/slides/slide1.xml", slide)
    masked = tmp_path / "deck_masked.pptx"
    e.pseudonymize_office(str(src), str(masked))
    with zipfile.ZipFile(masked) as z:
        xml = z.read("ppt/slides/slide1.xml").decode()
    assert "Kumari Bandara" not in xml and "199085601234" not in xml and "TOK_PERSON_" in xml


def test_empty_runs_and_entities_are_left_alone(make_engine, tmp_path):
    e = make_engine()
    src = tmp_path / "amp.docx"
    _make_docx(src, ["Total &amp; sum is 42", "  "])  # entity-escaped text, blank run
    out = tmp_path / "amp_masked.docx"
    e.pseudonymize_office(str(src), str(out))
    with zipfile.ZipFile(out) as z:
        xml = z.read("word/document.xml").decode()
    assert "Total &amp; sum is 42" in xml  # no PII, ampersand preserved as a valid entity
