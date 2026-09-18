"""Admin policy console API: admin-key gate, GET/PUT/test, hot reload."""
import json
import os

import pytest


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    os.environ.pop("MASKROOM_API_KEY", None)
    from webui import app as webapp
    from maskroom import overlay as overlay_mod
    runs = str(tmp_path_factory.mktemp("runs"))
    webapp.RUNS = runs
    from webui.audit import AuditLog
    webapp.audit = AuditLog(os.path.join(runs, "audit"), ttl_days=0)
    # isolate the overlay file and start from empty. These are module globals,
    # so save and restore them — otherwise a saved overlay (e.g. an allow-list)
    # leaks into every engine built by a later test module.
    saved = (overlay_mod.DEFAULT_PATH, webapp._overlay, webapp._overlay_fp)
    overlay_mod.DEFAULT_PATH = os.path.join(runs, "custom_policy.yaml")
    webapp._overlay = overlay_mod.load()
    webapp._overlay_fp = overlay_mod.fingerprint(webapp._overlay)
    webapp._engines.clear()
    webapp.ADMIN_KEY = "sekret"
    webapp.app.config["TESTING"] = True
    yield webapp.app.test_client()
    overlay_mod.DEFAULT_PATH, webapp._overlay, webapp._overlay_fp = saved
    webapp._engines.clear()


H = {"X-Admin-Key": "sekret"}


def test_requires_admin_key(client):
    assert client.get("/api/policy").status_code == 401
    assert client.put("/api/policy", json={}).status_code == 401
    assert client.get("/api/policy", headers=H).status_code == 200


def test_get_shows_builtins_and_overlay(client):
    d = client.get("/api/policy", headers=H).get_json()
    assert d["overlay"]["deny_terms"] == []
    assert d["default_entity"] == "CUSTOM_TERM"
    assert d["builtins"]["locale"]["code"]                 # a locale is loaded
    assert isinstance(d["builtins"]["column_rules"], list) and d["builtins"]["column_rules"]


def test_put_validates(client):
    r = client.put("/api/policy", headers=H, json={"regex_rules": [{"name": "x", "regex": "(a+)+"}]})
    assert r.status_code == 400 and "nested quantifiers" in r.get_json()["error"]


def test_put_then_effect_and_audit(client):
    body = {
        "deny_terms": [{"term": "Project Halo", "entity": "CUSTOM_TERM"}],
        "allow_terms": ["Nimal Perera"],
        "regex_rules": [{"name": "emp", "entity": "EMPLOYEE_ID",
                         "regex": r"EMP-\d{6}", "score": 0.9, "context": ["staff"]}],
    }
    r = client.put("/api/policy", headers=H, json=body)
    assert r.status_code == 200
    assert len(r.get_json()["overlay"]["deny_terms"]) == 1

    # GET reflects the saved overlay
    assert client.get("/api/policy", headers=H).get_json()["overlay"]["allow_terms"] == ["Nimal Perera"]

    # the live masking path now honours the new rules (hot reload)
    code = client.post("/api/mask", data=json.dumps({"text": "Project Halo uses EMP-004521 (staff)."}),
                       content_type="application/json")
    masked = code.get_json()["masked"]
    assert "Project Halo" not in masked and "EMP-004521" not in masked

    # allow-list protects a name that would otherwise mask
    m2 = client.post("/api/mask", data=json.dumps({"text": "Contact Nimal Perera today."}),
                     content_type="application/json").get_json()["masked"]
    assert "Nimal Perera" in m2

    # the change was written to the audit trail
    from webui import app as webapp
    rows = webapp.audit.list(action="policy-update")["records"]
    assert rows and rows[0]["by_entity"]["deny_terms"] == 1


def test_test_endpoint_previews_candidate(client):
    # preview an unsaved overlay without persisting it
    body = {"text": "Codename Falcon is secret.",
            "overlay": {"deny_terms": [{"term": "Codename Falcon", "entity": "CUSTOM_TERM"}]}}
    d = client.post("/api/policy/test", headers=H, data=json.dumps(body),
                    content_type="application/json").get_json()
    assert "Codename Falcon" not in d["masked"] and d["changed"]
    assert d["by_entity"].get("CUSTOM_TERM") == 1
    # empty text is a 400
    assert client.post("/api/policy/test", headers=H, data=json.dumps({"text": "  "}),
                       content_type="application/json").status_code == 400
