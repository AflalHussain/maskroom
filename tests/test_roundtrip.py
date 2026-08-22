"""mask -> save vault -> fresh engine -> restore must reproduce every cell,
including numeric types, on every workbook in the corpus."""
import openpyxl
import pytest

WORKBOOKS = ["PII_Redaction_Test_Dataset.xlsx", "PII_Test_Dataset_LK.xlsx",
             "Real_World_Directory.xlsx", "PII_Stress_Test.xlsx"]


def _cells(path):
    wb = openpyxl.load_workbook(path)
    return {(ws.title, c.coordinate): c.value
            for ws in wb.worksheets for row in ws.iter_rows() for c in row}


@pytest.mark.parametrize("name", WORKBOOKS)
def test_roundtrip(make_engine, data_path, tmp_path, name):
    masked, vault, restored = (tmp_path / "m.xlsx", tmp_path / "v.json", tmp_path / "r.xlsx")
    e = make_engine()
    e.pseudonymize_excel(data_path(name), str(masked))
    e.save_vault(str(vault))
    assert len(e.report) > 0, "nothing was masked"

    e2 = make_engine()
    e2.load_vault(str(vault))
    e2.depseudonymize_excel(str(masked), str(restored))

    before, after = _cells(data_path(name)), _cells(str(restored))
    diffs = {k: (before.get(k), after.get(k)) for k in set(before) | set(after)
             if before.get(k) != after.get(k)}
    assert diffs == {}, f"{len(diffs)} cells differ, e.g. {list(diffs.items())[:5]}"
