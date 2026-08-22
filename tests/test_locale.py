"""Locale mechanism: bundled locales load, generic works, a second country's
rules take effect, and the policy compiles column/profile rules correctly."""
import os

import pytest

from maskroom.locale import available_locales, build_policy, load_locale_data


def test_bundled_locales():
    locs = available_locales()
    assert "lk" in locs and "in" in locs and not any(k.startswith("_") for k in locs)


def test_template_is_valid_schema():
    import maskroom.locale as L
    d = load_locale_data(os.path.join(L.LOCALES_DIR, "_template.yaml"))
    p = build_policy(os.path.join(L.LOCALES_DIR, "_template.yaml"))
    assert d["code"] == "xx" and p.column_rules[0][0] == "XX_NATIONAL_ID"


def test_lk_policy_shape():
    p = build_policy("lk")
    assert p.column_rules[0][0] == "LK_NIC"            # locale ids before generic rules
    assert [e for e, _, _ in p.profile_patterns][:3] == ["LK_NIC", "LK_PASSPORT", "PHONE_NUMBER"]
    assert "kandy" in p.places and p.is_place("Nuwara Eliya") and not p.is_place("Kandy Perera")
    assert p.address_hint_re.search("Galle Mawatha") and "LK" in p.phone_regions


def test_unknown_locale_and_validator():
    with pytest.raises(ValueError):
        build_policy("zz")


def test_generic_locale(make_engine):
    e = make_engine(locale="generic")
    assert e.locale == "generic"
    masked, _ = e.pseudonymize_text("Call Nimal Perera at nimal@lankamail.lk")
    assert "TOK_PERSON_" in masked and "TOK_EMAIL_ADDRESS_" in masked
    _, changed = e.pseudonymize_text("853421234V")   # LK NIC means nothing here
    assert not changed


def test_india_locale(make_engine):
    e = make_engine(locale="in")
    masked, changed = e.pseudonymize_text("PAN ABCDE1234F, mobile +91 98765 43210, office in Bengaluru")
    assert changed and "TOK_IN_PAN_" in masked and "TOK_PHONE_NUMBER_" in masked
    assert "Bengaluru" in masked                      # place kept under address policy
    _, changed = e.pseudonymize_text("Mumbai")
    assert not changed
    masked, changed = e.pseudonymize_text("Shri Ramesh will attend")
    assert changed and "Ramesh" not in masked         # locale honorific
