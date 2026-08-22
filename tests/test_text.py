"""Text-level behaviour: recognizers, filters, date policy, idempotency."""
import pytest


def test_lk_identifiers_and_name(engine):
    masked, changed = engine.pseudonymize_text(
        "Call Nimal Perera on phone 077-1234567, NIC 853421234V")
    assert changed
    assert "Nimal" not in masked and "077-1234567" not in masked and "853421234V" not in masked
    assert "TOK_PERSON_" in masked and "TOK_PHONE_NUMBER_" in masked and "TOK_LK_NIC_" in masked


def test_tokens_are_deterministic(make_engine):
    a = make_engine().generate_token("Nimal Perera", "PERSON")
    b = make_engine().generate_token("Nimal Perera", "PERSON")
    assert a == b and a.startswith("TOK_PERSON_")


def test_idempotent_rerun(engine):
    once, _ = engine.pseudonymize_text("Email kumari.s@lankamail.lk for details")
    twice, changed = engine.pseudonymize_text(once)
    assert twice == once and not changed


def test_depseudonymize_text_roundtrip(make_engine):
    e = make_engine()
    text = "Contact Ruwan Bandara, NIC 912345678V, at 071-2345678."
    masked, _ = e.pseudonymize_text(text)
    assert e.depseudonymize_text(masked) == text


def test_codes_are_not_people(engine):
    for value in ("EMP-100", "PRD-5521", "Max"):
        _, changed = engine.pseudonymize_text(value)
        assert not changed, value


def test_amounts_and_plain_dates_stay(engine):
    for value in ("Salary 165624 paid on 2024-03-15", "Invoice dated 12 March 2024 for 1919"):
        _, changed = engine.pseudonymize_text(value)
        assert not changed, value


def test_birth_dates_masked_by_default(engine):
    masked, changed = engine.pseudonymize_text("Date of birth: 15/03/1985")
    assert changed and "1985" not in masked


@pytest.mark.parametrize("policy,expect_changed", [("all", True), ("none", False)])
def test_date_policy(make_engine, policy, expect_changed):
    e = make_engine(dates=policy)
    _, changed = e.pseudonymize_text("Meeting on 15 March 2024 at 10am")
    assert changed is expect_changed


def test_inverted_caps_name(engine):
    masked, changed = engine.pseudonymize_text("JAYAWARDENA, SANDUNI D   Finance")
    assert changed and "JAYAWARDENA" not in masked


def test_header_like_caps_not_inverted_name(engine):
    # "Name, NIC" must not match the inverted-caps name pattern as a whole
    # (Presidio's default IGNORECASE once made it do so). NER may still tag
    # the lone word; sheet header cells are skipped for that reason.
    engine.pseudonymize_text("Name, NIC")
    assert "Name, NIC" not in [f["text"] for f in engine._last_findings]
