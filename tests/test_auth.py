"""Single sign-on mode: login round trip, roles, service keys, the legacy key,
ownership, CSRF, logout, disabled users, the extension's login redirect."""
import os
import time
from types import SimpleNamespace

import pytest

H = {"X-Requested-With": "maskroom"}
EXT_ID = "lcmdehcdpfddkjgajmlpgfholdekpgio"


@pytest.fixture(scope="module")
def env(tmp_path_factory, db_module):
    os.environ.pop("MASKROOM_API_KEY", None)
    from webui import app as webapp
    from webui.auth import FakeProvider, Identity
    from webui.audit import AuditLog
    from maskroom.store import SessionStore
    runs = str(tmp_path_factory.mktemp("runs"))
    webapp.RUNS = runs
    webapp.audit = AuditLog(db_module, os.path.join(runs, "audit"), ttl_days=0)
    webapp.sessions = SessionStore(db_module, ttl=None, base_salt="t")
    webapp.app.config["TESTING"] = True
    provider = FakeProvider(Identity("Alice@Corp.lk", "Alice", "sub-1"))
    webapp.configure_auth(webapp.app, db_module, mode="oidc", provider=provider,
                          legacy_key=lambda: webapp.API_KEY, admin_key=lambda: None,
                          admin_emails="admin@corp.lk", extension_ids=[EXT_ID])
    yield SimpleNamespace(app=webapp.app, webapp=webapp, provider=provider, db=db_module,
                          Identity=Identity)
    webapp.configure_auth(webapp.app, db_module, mode="off",
                          legacy_key=lambda: webapp.API_KEY, admin_key=lambda: webapp.ADMIN_KEY)


def login(env, email, name="", nxt="/staging"):
    """A fresh client (its own cookie jar) signed in as `email`."""
    c = env.app.test_client()
    env.provider.identity = env.Identity(email, name, "sub-" + email)
    r = c.get("/auth/login", query_string={"next": nxt})
    assert r.status_code == 302 and "/auth/callback?state=" in r.headers["Location"]
    r2 = c.get(r.headers["Location"])
    assert r2.status_code == 302, r2.data
    return c, r2


def test_config_and_me_when_signed_out(env):
    c = env.app.test_client()
    cfg = c.get("/api/config").get_json()
    assert cfg["auth_mode"] == "oidc" and cfg["auth_required"] and cfg["admin_auth_required"]
    r = c.get("/api/me")
    assert r.status_code == 401 and r.get_json()["login_url"] == "/auth/login"
    assert c.post("/api/session", headers=H).status_code == 401
    assert c.get("/").status_code == 200 and c.get("/admin").status_code == 200   # pages are open
    assert c.get("/ext/update.xml").status_code in (200, 503)                       # never 401


def test_login_round_trip_sets_cookie_and_identity(env):
    c, r = login(env, "Alice@Corp.lk", "Alice")
    assert r.headers["Location"] == "/staging"
    cookie = r.headers["Set-Cookie"]
    assert "maskroom_session=" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    me = c.get("/api/me").get_json()
    assert me["principal"]["email"] == "alice@corp.lk" and me["principal"]["role"] == "staff"
    assert me["principal"]["kind"] == "user"


def test_roles_gate_admin_and_auditor_apis(env):
    alice, _ = login(env, "alice@corp.lk")
    assert alice.get("/api/audit").status_code == 403
    assert alice.get("/api/policy").status_code == 403
    admin, _ = login(env, "admin@corp.lk", "Boss")                    # bootstrapped by MASKROOM_ADMIN_EMAILS
    assert admin.get("/api/me").get_json()["principal"]["role"] == "admin"
    assert admin.get("/api/audit").status_code == 200
    assert admin.get("/api/policy").status_code == 200
    users = admin.get("/api/users").get_json()["users"]
    alice_id = next(u["id"] for u in users if u["email"] == "alice@corp.lk")
    r = admin.patch(f"/api/users/{alice_id}", json={"role": "auditor"}, headers=H)
    assert r.status_code == 200 and r.get_json()["user"]["role"] == "auditor"
    assert alice.get("/api/audit").status_code == 200                # role is read live
    assert alice.get("/api/policy").status_code == 403
    assert admin.patch(f"/api/users/{alice_id}", json={"role": "root"}, headers=H).status_code == 400
    me_id = admin.get("/api/me").get_json()["principal"]["id"]
    assert admin.patch(f"/api/users/{me_id}", json={"role": "staff"}, headers=H).status_code == 400
    admin.patch(f"/api/users/{alice_id}", json={"role": "staff"}, headers=H)


def test_csrf_header_required_for_cookie_writes(env):
    alice, _ = login(env, "alice@corp.lk")
    assert alice.post("/api/session").status_code == 403
    r = alice.post("/api/session", headers=H)
    assert r.status_code == 200
    sid = r.get_json()["session_id"]
    assert alice.get(f"/api/session/{sid}").status_code == 200        # GET needs no header
    assert alice.get(f"/api/session/{sid}/vault").status_code == 200


def test_audit_attributes_to_the_signed_in_user(env):
    alice, _ = login(env, "alice@corp.lk")
    r = alice.post("/api/mask", json={"text": "Call Nimal Perera on 0771234567"},
                   headers={**H, "X-Maskroom-User": "spoofed@evil.example"})
    assert r.status_code == 200
    admin, _ = login(env, "admin@corp.lk")
    recs = admin.get("/api/audit", query_string={"action": "mask"}).get_json()["records"]
    assert recs[0]["user"] == "alice@corp.lk"                        # header is ignored


def test_service_keys(env):
    admin, _ = login(env, "admin@corp.lk")
    r = admin.post("/api/keys", json={"name": "gateway", "role": "staff"}, headers=H)
    assert r.status_code == 201
    secret, key_id = r.get_json()["secret"], r.get_json()["key"]["id"]
    assert secret.startswith("mr_")
    svc = env.app.test_client()
    bearer = {"Authorization": f"Bearer {secret}"}
    assert svc.post("/api/session", headers=bearer).status_code == 200   # no CSRF header needed
    assert svc.get("/api/me", headers=bearer).get_json()["principal"] == {
        "kind": "service", "id": key_id, "email": None, "name": "gateway", "role": "staff"}
    assert svc.get("/api/audit", headers=bearer).status_code == 403
    assert svc.post("/api/session", headers={"Authorization": "Bearer mr_wrong"}).status_code == 401
    assert admin.get("/api/keys").get_json()["keys"][0]["name"] == "gateway"
    assert admin.delete(f"/api/keys/{key_id}", headers=H).status_code == 200
    assert svc.post("/api/session", headers=bearer).status_code == 401
    assert admin.delete(f"/api/keys/{key_id}", headers=H).status_code == 404
    assert admin.post("/api/keys", json={"name": ""}, headers=H).status_code == 400


def test_legacy_shared_key_still_works_as_a_service_identity(env, monkeypatch):
    monkeypatch.setattr(env.webapp, "API_KEY", "old-shared")
    c = env.app.test_client()
    r = c.post("/api/mask", json={"text": "NIC 853421234V"}, headers={"X-API-Key": "old-shared"})
    assert r.status_code == 200
    assert c.get("/api/me", headers={"X-API-Key": "old-shared"}).get_json()["principal"]["name"] == "legacy-api-key"
    assert c.post("/api/session", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.post("/api/session?key=old-shared").status_code == 401
    admin, _ = login(env, "admin@corp.lk")
    recs = admin.get("/api/audit", query_string={"action": "mask"}).get_json()["records"]
    assert recs[0]["user"] == "legacy-api-key"


def test_ownership_of_sessions_and_runs(env, data_path):
    alice, _ = login(env, "alice@corp.lk")
    bob, _ = login(env, "bob@corp.lk")
    admin, _ = login(env, "admin@corp.lk")
    sid = alice.post("/api/session", headers=H).get_json()["session_id"]
    assert bob.get(f"/api/session/{sid}").status_code == 404
    assert bob.get(f"/api/session/{sid}/vault").status_code == 404
    assert bob.post("/api/mask", json={"text": "x", "session_id": sid}, headers=H).status_code == 404
    assert bob.delete(f"/api/session/{sid}", headers=H).get_json() == {"deleted": False}
    assert admin.get(f"/api/session/{sid}").status_code == 404        # admins do not read vaults
    assert alice.get(f"/api/session/{sid}").status_code == 200
    # a file run and its downloads
    with open(data_path("PII_Test_Dataset_LK.xlsx"), "rb") as fh:
        r = alice.post("/api/process", data={"file": (fh, "staff.xlsx"), "session_id": sid,
                                             "preview": "false"}, headers=H)
    assert r.status_code == 200, r.data
    run_id, out = r.get_json()["run_id"], r.get_json()["downloads"]["output"]
    assert alice.get(f"/api/download/{run_id}/{out}").status_code == 200
    assert alice.get(f"/api/text/{run_id}/masked.md").status_code == 200
    assert bob.get(f"/api/download/{run_id}/{out}").status_code == 404
    assert bob.get(f"/api/text/{run_id}/masked.md").status_code == 404
    assert admin.get(f"/api/download/{run_id}/{out}").status_code == 404
    # an ownerless session (created with auth off, or before the upgrade) is admin-only
    old = env.webapp.sessions.create(owner_id=None)
    assert bob.get(f"/api/session/{old.id}").status_code == 404
    assert admin.get(f"/api/session/{old.id}").status_code == 200


def test_logout_revokes_the_session(env):
    alice, _ = login(env, "alice@corp.lk")
    assert alice.post("/auth/logout").status_code == 403               # CSRF header
    r = alice.post("/auth/logout", headers=H)
    assert r.status_code == 200 and "maskroom_session=;" in r.headers["Set-Cookie"]
    assert alice.get("/api/me").status_code == 401


def test_disabled_user_is_rejected(env):
    bob, _ = login(env, "bob@corp.lk")
    admin, _ = login(env, "admin@corp.lk")
    bob_id = bob.get("/api/me").get_json()["principal"]["id"]
    assert admin.patch(f"/api/users/{bob_id}", json={"disabled": True}, headers=H).status_code == 200
    assert bob.get("/api/me").status_code == 401                       # existing cookie dies at once
    c = env.app.test_client()
    env.provider.identity = env.Identity("bob@corp.lk", "Bob", "sub-bob")
    r = c.get(c.get("/auth/login").headers["Location"])
    assert r.status_code == 403 and b"disabled" in r.data
    admin.patch(f"/api/users/{bob_id}", json={"disabled": False}, headers=H)


def test_login_failure_is_a_page_not_a_crash(env):
    from webui.auth import AuthError
    c = env.app.test_client()
    env.provider.identity = AuthError("The identity provider returned no email address.")
    r = c.get(c.get("/auth/login").headers["Location"])
    assert r.status_code == 400 and b"no email" in r.data
    assert c.get("/auth/callback?state=stale&code=x").status_code == 400


def test_extension_login_redirects_to_chromiumapp_only(env):
    ok = f"https://{EXT_ID}.chromiumapp.org/done"
    _, r = login(env, "alice@corp.lk", nxt=ok)
    assert r.headers["Location"] == ok
    for bad in ("https://evil.example/", "//evil.example/x", "http://" + EXT_ID + ".chromiumapp.org/",
                "https://other.chromiumapp.org/"):
        _, r = login(env, "alice@corp.lk", nxt=bad)
        assert r.headers["Location"] == "/", bad


def test_sliding_session_expiry(env, monkeypatch):
    alice, _ = login(env, "alice@corp.lk")
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 11 * 3600)
    assert alice.get("/api/me").status_code == 200                     # 11 h later, still valid, extended
    monkeypatch.setattr(time, "time", lambda: now + 11 * 3600 + 11.5 * 3600)
    assert alice.get("/api/me").status_code == 200                     # 11.5 h after the extension
    monkeypatch.setattr(time, "time", lambda: now + 11 * 3600 + 11.5 * 3600 + 13 * 3600)
    assert alice.get("/api/me").status_code == 401                     # idle past 12 h


def test_mode_off_parity_after_reconfigure(env):
    webapp = env.webapp
    webapp.configure_auth(webapp.app, env.db, mode="off", legacy_key=lambda: None, admin_key=lambda: None)
    try:
        c = env.app.test_client()
        assert c.get("/api/config").get_json()["auth_mode"] == "off"
        assert c.post("/api/session").status_code == 200               # no cookie, no CSRF header
        assert c.get("/api/me").get_json() == {"auth_mode": "off", "principal": None}
        assert c.get("/auth/login").status_code == 404
        assert c.get("/api/audit").status_code == 403                  # admin key unset: disabled
    finally:
        webapp.configure_auth(webapp.app, env.db, mode="oidc", provider=env.provider,
                              legacy_key=lambda: webapp.API_KEY, admin_key=lambda: None,
                              admin_emails="admin@corp.lk", extension_ids=[EXT_ID])


def test_pages_and_shared_script_are_open(env):
    c = env.app.test_client()
    for path in ("/", "/staging", "/admin", "/admin/rules", "/admin/users"):
        r = c.get(path)
        assert r.status_code == 200 and b"/static/auth.js" in r.data, path
    r = c.get("/static/auth.js")
    assert r.status_code == 200 and b"MaskroomAuth" in r.data
    assert c.get("/api/users").status_code == 401
