"""Session vault store: persistence, per-session salts, engine binding, TTL."""
import os
import time

from maskroom.session import SessionStore


def test_create_persist_reload(tmp_path):
    store = SessionStore(str(tmp_path), base_salt="s")
    s = store.create()
    s.vault["TOK_PERSON_AAAAAAAA"] = "Nimal"
    s.save()
    assert os.path.isfile(s.vault_path)
    fresh = SessionStore(str(tmp_path), base_salt="s")
    again = fresh.get(s.id)
    assert again is not None and again.vault == {"TOK_PERSON_AAAAAAAA": "Nimal"}
    assert again.salt == s.salt


def test_unknown_and_invalid_ids(tmp_path):
    store = SessionStore(str(tmp_path), base_salt="s")
    assert store.get("nope") is None
    assert store.get("../../etc/passwd") is None
    assert store.get("a" * 16) is None


def test_delete_and_sweep(tmp_path):
    store = SessionStore(str(tmp_path), ttl=1, base_salt="s")
    a, b = store.create(), store.create()
    assert store.delete(a.id) and store.get(a.id) is None
    b.last_used = time.time() - 10
    assert store.sweep() == [b.id]
    assert store.get(b.id) is None


def test_bind_swaps_vault_and_salt(tmp_path, make_engine):
    engine = make_engine()
    engine.vault["TOK_PERSON_11111111"] = "engine-own"
    own_salt = engine.salt
    store = SessionStore(str(tmp_path), base_salt="s")
    s1, s2 = store.create(), store.create()
    with store.bind(engine, s1):
        t1 = engine.generate_token("Nimal Perera", "PERSON")
        assert engine.salt == s1.salt
    with store.bind(engine, s2):
        t2 = engine.generate_token("Nimal Perera", "PERSON")
    # same value -> same token within a session, different across sessions
    with store.bind(engine, s1):
        assert engine.generate_token("Nimal Perera", "PERSON") == t1
    assert t1 != t2
    assert s1.vault == {t1: "Nimal Perera"} and s2.vault == {t2: "Nimal Perera"}
    # engine state restored afterwards
    assert engine.vault == {"TOK_PERSON_11111111": "engine-own"} and engine.salt == own_salt
    # persisted
    assert SessionStore(str(tmp_path), base_salt="s").get(s1.id).vault == {t1: "Nimal Perera"}
    assert s1.calls == 2
