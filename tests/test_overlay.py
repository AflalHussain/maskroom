"""Admin policy overlay: validation, and its effect on the engine."""
import pytest

from maskroom import overlay


# ------------------------------------------------------------- validation
def test_normalize_and_defaults():
    o = overlay.validate({
        "deny_terms": ["Project Halo", {"term": "Acme Corp", "entity": "client_name"}],
        "allow_terms": ["hSenid Mobile", "hSenid Mobile"],  # de-duped
        "regex_rules": [{"name": "emp", "regex": r"EMP-\d{6}"}],
    })
    assert o["deny_terms"][0] == {"term": "Project Halo", "entity": "CUSTOM_TERM"}
    assert o["deny_terms"][1]["entity"] == "CLIENT_NAME"           # upper-cased
    assert o["allow_terms"] == ["hSenid Mobile"]                   # de-duped
    assert o["regex_rules"][0]["entity"] == "CUSTOM_TERM"          # default entity
    assert o["regex_rules"][0]["score"] == 0.85                    # default score


def test_empty_overlay():
    assert overlay.validate(None) == overlay.validate({}) == \
        {"version": 1, "deny_terms": [], "allow_terms": [], "regex_rules": []}


@pytest.mark.parametrize("bad,msg", [
    ({"deny_terms": [{"term": "of"}]}, "too short"),
    ({"regex_rules": [{"name": "x", "regex": "(a+)+"}]}, "nested quantifiers"),
    ({"regex_rules": [{"name": "x", "regex": "([a-z"}]}, "invalid regex"),
    ({"regex_rules": [{"name": "x", "regex": "a", "score": 2}]}, "score"),
    ({"regex_rules": [{"name": "bad name", "regex": "a"}]}, "name"),
    ({"deny_terms": [{"term": "ok term", "entity": "1bad"}]}, "entity"),
    ({"regex_rules": [{"name": "dup", "regex": "a"}, {"name": "dup", "regex": "b"}]}, "duplicate"),
])
def test_rejections(bad, msg):
    with pytest.raises(overlay.PolicyError) as e:
        overlay.validate(bad)
    assert msg in str(e.value)


def test_fingerprint_stable_and_sensitive():
    a = {"deny_terms": ["Alpha Term"]}
    b = {"deny_terms": ["Alpha Term"], "allow_terms": []}
    assert overlay.fingerprint(a) == overlay.fingerprint(b)      # normalized-equal
    assert overlay.fingerprint(a) != overlay.fingerprint({"deny_terms": ["Beta Term"]})


def test_recognizers_grouped_by_entity():
    recs = overlay.recognizers({
        "deny_terms": [{"term": "Alpha One", "entity": "CUSTOM_TERM"},
                       {"term": "Beta Two", "entity": "CUSTOM_TERM"},
                       {"term": "Gamma Three", "entity": "CLIENT_NAME"}],
        "regex_rules": [{"name": "emp", "regex": r"EMP-\d{6}"}],
    })
    names = sorted(r.name for r in recs)
    assert names == ["overlay_deny_client_name", "overlay_deny_custom_term", "overlay_re_emp"]


def test_save_load_roundtrip(tmp_path):
    path = str(tmp_path / "custom_policy.yaml")
    saved = overlay.save({"deny_terms": ["Secret Codename"]}, path=path)
    assert overlay.load(path=path) == saved
    assert overlay.load(path=str(tmp_path / "missing.yaml")) == overlay.validate(None)


# --------------------------------------------------------- engine effect
def test_deny_and_regex_mask(make_engine):
    eng = make_engine(overlay={
        "deny_terms": [{"term": "Project Halo", "entity": "CUSTOM_TERM"}],
        "regex_rules": [{"name": "emp", "entity": "EMPLOYEE_ID",
                         "regex": r"EMP-\d{6}", "score": 0.9, "context": ["staff"]}],
    })
    masked, changed = eng.pseudonymize_text("Project Halo lead is EMP-004521 (staff).")
    assert changed
    assert "Project Halo" not in masked and "EMP-004521" not in masked
    assert "TOK_CUSTOM_TERM_" in masked and "TOK_EMPLOYEE_ID_" in masked


def test_allow_list_protects_a_name(make_engine):
    plain = make_engine()
    m1, _ = plain.pseudonymize_text("Contact Nimal Perera about the file.")
    assert "Nimal Perera" not in m1                      # normally masked

    allowed = make_engine(overlay={"allow_terms": ["Nimal Perera"]})
    m2, _ = allowed.pseudonymize_text("Contact Nimal Perera about the file.")
    assert "Nimal Perera" in m2                          # allow-list wins
