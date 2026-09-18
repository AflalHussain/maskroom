"""The token store: storage, inverse lookup, numeric bookkeeping, the escaped
view, and serialization (incl. the legacy flat form) — all with no engine."""
from maskroom.vault import Vault


def test_add_get_contains_len_iter_items():
    v = Vault()
    v.add("TOK_PERSON_AAAAAAAA", "Nimal Perera")
    v.add("TOK_LK_NIC_BBBBBBBB", "853421234V")
    assert v.get("TOK_PERSON_AAAAAAAA") == "Nimal Perera"
    assert v.get("missing") is None and v.get("missing", "d") == "d"
    assert "TOK_PERSON_AAAAAAAA" in v and "nope" not in v
    assert len(v) == 2
    assert set(v) == {"TOK_PERSON_AAAAAAAA", "TOK_LK_NIC_BBBBBBBB"}
    assert dict(v.items())["TOK_LK_NIC_BBBBBBBB"] == "853421234V"


def test_token_for_inverse_lookup():
    v = Vault()
    v.add("TOK_PERSON_AAAAAAAA", "Nimal Perera")
    assert v.token_for("Nimal Perera") == "TOK_PERSON_AAAAAAAA"
    assert v.token_for("someone else") is None


def test_numeric_cell_wins_token_fallback():
    v = Vault()
    v.add("TOK_LK_NIC_CCCCCCCC", "912345678V")
    v.mark_numeric("TOK_LK_NIC_CCCCCCCC", "Staff!B2")
    # per-cell record present -> cell lookup is authoritative
    assert v.was_numeric(cell="Staff!B2", token="TOK_LK_NIC_CCCCCCCC") is True
    assert v.was_numeric(cell="Staff!Z9", token="TOK_LK_NIC_CCCCCCCC") is False

    # no cell records -> fall back to per-token
    tok_only = Vault()
    tok_only.mark_numeric("TOK_X_1", cell=None)
    assert tok_only.was_numeric(cell="anything", token="TOK_X_1") is True
    assert tok_only.was_numeric(cell="anything", token="TOK_X_2") is False


def test_begin_run_clears_only_cells_not_tokens():
    v = Vault()
    v.mark_numeric("TOK_X_1", "Sheet!A1")
    v.begin_run()
    # cell coords are per-workbook and must not leak across a session's files
    assert v.was_numeric(cell="Sheet!A1", token="TOK_X_1") is True  # falls back to token
    assert v.to_dict()["numeric_cells"] == []
    assert v.to_dict()["numeric_tokens"] == ["TOK_X_1"]


def test_escaped_view_escapes_values_only():
    v = Vault()
    v.add("TOK_ORG_1", "Ben & Jerry's <Ltd>")
    esc = v.escaped()
    assert esc.get("TOK_ORG_1") == "Ben &amp; Jerry's &lt;Ltd&gt;"
    assert v.get("TOK_ORG_1") == "Ben & Jerry's <Ltd>"  # original untouched


def test_to_dict_from_dict_roundtrip_preserves_numeric_cells():
    """Regression: numeric_cells must survive serialization (the file round
    trip a masked workbook relies on to restore numeric types)."""
    v = Vault()
    v.add("TOK_LK_NIC_DDDDDDDD", "199012345678")
    v.mark_numeric("TOK_LK_NIC_DDDDDDDD", "People!C4")
    d = v.to_dict()
    assert d["numeric_cells"] == ["People!C4"]

    back = Vault.from_dict(d)
    assert back.get("TOK_LK_NIC_DDDDDDDD") == "199012345678"
    assert back.was_numeric(cell="People!C4", token="TOK_LK_NIC_DDDDDDDD") is True


def test_from_dict_accepts_legacy_flat_form():
    back = Vault.from_dict({"TOK_PERSON_EEEEEEEE": "Kamal", "TOK_PERSON_FFFFFFFF": "Sunil"})
    assert back.get("TOK_PERSON_EEEEEEEE") == "Kamal"
    assert len(back) == 2


def test_update_from_dict_merges():
    v = Vault()
    v.add("TOK_A_1", "one")
    v.update_from_dict({"mappings": {"TOK_B_2": "two"}, "numeric_tokens": ["TOK_B_2"]})
    assert len(v) == 2 and v.get("TOK_B_2") == "two"
    assert v.was_numeric(token="TOK_B_2") is True
