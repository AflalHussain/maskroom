"""Tolerant restore of tokens that came back from an LLM (ROADMAP item 1)."""
import pytest


@pytest.fixture
def vault_engine(make_engine):
    e = make_engine()
    e.vault.update({
        "TOK_PERSON_8B584CCF": "Nimal Perera",
        "TOK_PERSON_1A2B3C4D": "Kumari Bandara",
        "TOK_LK_NIC_0F0F0F0F": "853421234V",
        "TOK_US_SSN_ABCDEF01": "123-45-6789",
        "TOK_PHONE_NUMBER_77777777": "077-1234567",
    })
    return e


def test_exact_tokens_restore(vault_engine):
    text, rep = vault_engine.unmask_text("Call TOK_PERSON_8B584CCF on TOK_PHONE_NUMBER_77777777.")
    assert text == "Call Nimal Perera on 077-1234567."
    assert rep["restored"] == 2 and not rep["fuzzy"] and not rep["unresolved"]
    assert "Nimal Perera" in rep["values"]


@pytest.mark.parametrize("mangled", [
    "tok_person_8b584ccf",          # lowercased
    "TOK PERSON 8B584CCF",          # underscores became spaces
    "TOK-PERSON-8B584CCF",          # hyphens
    "TOK\\_PERSON\\_8B584CCF",      # markdown-escaped underscores
    "TOK_PERSON_8B584C",            # truncated id (unique prefix)
    "TOK_PERSON_8B584CCF12",        # extended id
    "Tok_Person_8b584ccf",          # mixed case
])
def test_mangled_tokens_restore(vault_engine, mangled):
    text, rep = vault_engine.unmask_text(f"According to {mangled}, the loan is overdue.")
    assert text == "According to Nimal Perera, the loan is overdue.", mangled
    assert rep["restored"] == 1 and len(rep["fuzzy"]) == 1
    assert rep["fuzzy"][0]["token"] == "TOK_PERSON_8B584CCF"


def test_multiword_entity_types(vault_engine):
    text, rep = vault_engine.unmask_text("nic tok lk nic 0f0f0f0f; ssn TOK US SSN ABCDEF01")
    assert text == "nic 853421234V; ssn 123-45-6789"
    assert rep["restored"] == 2


def test_ambiguous_or_unknown_tokens_are_reported_not_guessed(vault_engine):
    # both PERSON ids start with different chars, so "TOK_PERSON_9" matches nothing
    text, rep = vault_engine.unmask_text("See TOK_PERSON_99999999 and TOK_PERSON_DEADBEEF.")
    assert "Nimal" not in text and "Kumari" not in text
    assert rep["restored"] == 0
    assert len(rep["unresolved"]) == 2


def test_prefix_ambiguity_is_unresolved(make_engine):
    e = make_engine()
    e.vault.update({"TOK_PERSON_ABCDEF12": "A", "TOK_PERSON_ABCDEF34": "B"})
    text, rep = e.unmask_text("TOK_PERSON_ABCDEF")
    assert text == "TOK_PERSON_ABCDEF" and rep["restored"] == 0 and rep["unresolved"]


def test_text_without_tokens_is_untouched(vault_engine):
    src = "Nothing to see here, token-free text with the word stock."
    text, rep = vault_engine.unmask_text(src)
    assert text == src and rep["restored"] == 0 and not rep["unresolved"]


def test_depseudonymize_text_is_tolerant(vault_engine):
    assert vault_engine.depseudonymize_text("tok person 8b584ccf") == "Nimal Perera"


def test_llm_roundtrip_with_real_masking(make_engine):
    e = make_engine()
    masked, _ = e.pseudonymize_text("Contact Ruwan Bandara, NIC 912345678V, at 071-2345678.")
    # simulate a model reply that lowercases and spaces the tokens
    reply = "Summary: " + masked.lower().replace("_", " ")
    restored, rep = e.unmask_text(reply)
    assert "Ruwan Bandara" in restored and "912345678V" in restored and "071-2345678" in restored
    assert not rep["unresolved"]
