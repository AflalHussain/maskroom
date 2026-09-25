"""Users, roles, login sessions, service keys and run ownership."""
import time

import pytest
from sqlalchemy import select

from maskroom.store import (ApiKeyStore, LoginSessionStore, RunStore, UserStore, normalize_email,
                            role_allows)
from maskroom.store.schema import api_keys, login_sessions
from maskroom.store.users import MISSING


def test_roles_are_ordered():
    assert role_allows("admin", "staff") and role_allows("admin", "auditor")
    assert role_allows("auditor", "staff") and not role_allows("auditor", "admin")
    assert role_allows("staff", "staff") and not role_allows("staff", "auditor")
    assert not role_allows("anonymous", "staff")


def test_upsert_login_creates_then_updates(db):
    users = UserStore(db)
    a = users.upsert_login(" Alice@Corp.LK ", name="Alice", sub="sub-1")
    assert a.email == "alice@corp.lk" and a.role == "staff" and not a.disabled
    assert a.id.startswith("usr_")
    time.sleep(0.01)
    again = users.upsert_login("alice@corp.lk", name="Alice P", sub="sub-1", bootstrap_admin=True)
    assert again.id == a.id and again.role == "staff"          # bootstrap only on first sight
    assert again.name == "Alice P" and again.last_login > a.last_login
    assert users.get_by_email("ALICE@corp.lk").id == a.id
    admin = users.upsert_login("boss@corp.lk", bootstrap_admin=True)
    assert admin.role == "admin"
    assert [u.email for u in users.list()] == ["alice@corp.lk", "boss@corp.lk"]
    with pytest.raises(ValueError):
        users.upsert_login("   ")


def test_role_and_disable(db):
    users = UserStore(db)
    u = users.upsert_login("bob@corp.lk")
    assert users.set_role(u.id, "auditor") and users.get(u.id).role == "auditor"
    with pytest.raises(ValueError):
        users.set_role(u.id, "root")
    assert users.set_disabled(u.id, True) and users.get(u.id).disabled
    assert not users.set_role("usr_nope", "admin")


def test_login_sessions(db, monkeypatch):
    users, logins = UserStore(db), LoginSessionStore(db, hours=1)
    u = users.upsert_login("carol@corp.lk", name="Carol")
    tok = logins.create(u.id, ip="10.0.0.1")
    with db.connect() as c:
        row = c.execute(select(login_sessions)).one()
    assert row.id != tok and len(row.id) == 64                  # hashed, not the token
    sess, user = logins.resolve(tok)
    assert user.email == "carol@corp.lk" and sess.ip == "10.0.0.1"
    assert logins.resolve("nope") is None and logins.resolve("") is None
    # sliding expiry: after 50 min it is still valid and gets extended
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 50 * 60)
    sess2, _ = logins.resolve(tok)
    assert sess2.expires > sess.expires
    # 70 min after the last touch: expired
    monkeypatch.setattr(time, "time", lambda: now + 50 * 60 + 70 * 60)
    assert logins.resolve(tok) is None
    monkeypatch.setattr(time, "time", lambda: now)
    tok2 = logins.create(u.id)
    users.set_disabled(u.id, True)
    assert logins.resolve(tok2) is None                          # disabled user
    users.set_disabled(u.id, False)
    assert logins.resolve(tok2) is not None
    removed, id_tok = logins.revoke(tok2)
    assert removed and id_tok is None and logins.resolve(tok2) is None
    assert logins.revoke("never-issued") == (False, None)
    tok_with = logins.create(u.id, id_token="eyJ.fake.idtoken")
    assert logins.resolve(tok_with)[0].id_token == "eyJ.fake.idtoken"
    assert logins.revoke(tok_with) == (True, "eyJ.fake.idtoken")
    tok3 = logins.create(u.id)
    assert logins.revoke_user(u.id) == 2 and logins.resolve(tok3) is None   # tok3 + the expired tok
    assert logins.sweep() == 0
    logins.create(u.id)
    monkeypatch.setattr(time, "time", lambda: now + 3 * 3600)
    assert logins.sweep() == 1                                   # expired rows are removed


def test_api_keys(db):
    keys = ApiKeyStore(db)
    k, plain = keys.create("gateway", role="staff", created_by="admin@corp.lk")
    assert plain.startswith("mr_") and k.id.startswith("key_")
    with db.connect() as c:
        row = c.execute(select(api_keys)).one()
    assert row.key_hash != plain
    got = keys.authenticate(plain)
    assert got and got.id == k.id and got.last_used is not None
    assert keys.authenticate("mr_wrong") is None and keys.authenticate("") is None
    assert keys.authenticate(plain[3:]) is None                  # missing prefix
    assert [x.name for x in keys.list()] == ["gateway"]
    assert keys.revoke(k.id) and not keys.revoke(k.id)
    assert keys.authenticate(plain) is None
    with pytest.raises(ValueError):
        keys.create("", role="staff")
    with pytest.raises(ValueError):
        keys.create("x", role="root")


def test_runs(db):
    runs = RunStore(db)
    runs.create("abc123abc123", owner_id="usr_1", session_id="s1")
    runs.create("ffffffffffff", owner_id=None)
    assert runs.owner_of("abc123abc123") == "usr_1"
    assert runs.owner_of("ffffffffffff") is None
    assert runs.owner_of("nope") is MISSING
    assert runs.sweep(older_than_s=-1) == 2 and runs.owner_of("abc123abc123") is MISSING


def test_normalize_email():
    assert normalize_email("  A@B.C ") == "a@b.c" and normalize_email(None) == ""


def test_admin_cli_users_and_keys(db, monkeypatch, capsys):
    from maskroom.admin import main
    from maskroom.store import db as db_mod
    monkeypatch.setattr(db_mod, "_engines", {db_mod.default_url(): db})
    UserStore(db).upsert_login("dana@corp.lk", name="Dana")
    assert main(["user", "list"]) == 0
    assert "dana@corp.lk" in capsys.readouterr().out
    assert main(["user", "role", "dana@corp.lk", "admin"]) == 0
    assert UserStore(db).get_by_email("dana@corp.lk").role == "admin"
    with pytest.raises(SystemExit):
        main(["user", "role", "nobody@corp.lk", "admin"])
    assert main(["user", "disable", "dana@corp.lk"]) == 0
    assert UserStore(db).get_by_email("dana@corp.lk").disabled
    assert main(["user", "enable", "dana@corp.lk"]) == 0
    assert main(["key", "create", "gateway", "--role", "staff"]) == 0
    out = capsys.readouterr().out
    secret = out.split("secret (shown once): ")[1].strip()
    assert ApiKeyStore(db).authenticate(secret).name == "gateway"
    assert main(["key", "list"]) == 0 and "gateway" in capsys.readouterr().out
    kid = ApiKeyStore(db).list()[0].id
    assert main(["key", "revoke", kid]) == 0 and "revoked" in capsys.readouterr().out
    assert ApiKeyStore(db).authenticate(secret) is None


def test_login_codes_are_single_use_and_short_lived(db, monkeypatch):
    from maskroom.store import LoginCodeStore
    from maskroom.store.schema import login_codes
    users, codes = UserStore(db), LoginCodeStore(db, ttl_s=60)
    u = users.upsert_login("dave@corp.lk", name="Dave")
    code = codes.create(u.id, ip="10.0.0.2", id_token="idt")
    with db.connect() as c:
        row = c.execute(select(login_codes)).one()
    assert row.id != code and len(row.id) == 64                 # hashed, not the code
    assert codes.redeem("nope") is None and codes.redeem("") is None
    assert codes.redeem(code) == (u.id, "10.0.0.2", "idt")
    assert codes.redeem(code) is None                            # single use
    now = time.time()
    stale = codes.create(u.id)
    monkeypatch.setattr(time, "time", lambda: now + 61)
    assert codes.redeem(stale) is None                           # expired
    monkeypatch.setattr(time, "time", lambda: now)
    old = codes.create(u.id)
    monkeypatch.setattr(time, "time", lambda: now + 120)
    codes.create(u.id)                                           # create sweeps expired rows
    with db.connect() as c:
        assert c.execute(select(login_codes)).all().__len__() == 1
    assert codes.redeem(old) is None
