"""Session vault store: persistence, per-session salts, engine binding, TTL."""
import time

from sqlalchemy import func, select

from maskroom.session import SessionStore
from maskroom.store.schema import vault_entries


def _rows(db, sid):
    with db.connect() as c:
        return c.execute(select(func.count()).select_from(vault_entries)
                         .where(vault_entries.c.session_id == sid)).scalar()


def test_create_persist_reload(db):
    store = SessionStore(db, base_salt="s")
    s = store.create()
    s.vault.add("TOK_PERSON_AAAAAAAA", "Nimal")
    s.save()
    assert _rows(db, s.id) == 1
    fresh = SessionStore(db, base_salt="s")
    again = fresh.get(s.id)
    assert again is not None and dict(again.vault.items()) == {"TOK_PERSON_AAAAAAAA": "Nimal"}
    assert again.salt == s.salt


def test_clean_save_writes_nothing_new(db):
    store = SessionStore(db, base_salt="s")
    s = store.create()
    s.vault.add("TOK_A_1", "a"); s.vault.mark_numeric("TOK_A_1")
    s.save()
    assert s.vault.dirty_rows() == []
    s.save()
    assert _rows(db, s.id) == 1
    again = SessionStore(db, base_salt="s").get(s.id)
    assert again.vault.was_numeric(token="TOK_A_1")
    # numeric promotion of an existing row is persisted too
    s.vault.add("TOK_B_2", "b"); s.save()
    s.vault.mark_numeric("TOK_B_2"); s.save()
    assert SessionStore(db, base_salt="s").get(s.id).vault.was_numeric(token="TOK_B_2")


def test_export_shape(db):
    store = SessionStore(db, base_salt="s")
    s = store.create()
    s.vault.add("TOK_A_1", "a")
    doc = s.export()
    assert set(doc) == {"session_id", "created", "last_used", "calls",
                        "mappings", "numeric_tokens", "numeric_cells"}
    assert doc["mappings"] == {"TOK_A_1": "a"} and doc["numeric_cells"] == []


def test_two_processes_share_entries(db):
    """Two stores over one database stand in for two gunicorn workers."""
    a, b = SessionStore(db, base_salt="s"), SessionStore(db, base_salt="s")
    s = a.create()
    other = b.get(s.id)
    other.vault.add("TOK_X_1", "x"); other.last_used = time.time() + 1; other.save()
    s.vault.add("TOK_Y_2", "y"); s.save()
    assert dict(a.get(s.id).vault.items()) == {"TOK_X_1": "x", "TOK_Y_2": "y"}
    assert b.delete(s.id) and a.get(s.id) is None   # deletion is seen through the cache


def test_unknown_and_invalid_ids(db):
    store = SessionStore(db, base_salt="s")
    assert store.get("nope") is None
    assert store.get("../../etc/passwd") is None
    assert store.get("a" * 16) is None
    assert store.delete("../../etc/passwd") is False


def test_delete_and_sweep(db):
    store = SessionStore(db, ttl=1, base_salt="s")
    a, b = store.create(), store.create()
    assert store.delete(a.id) and store.get(a.id) is None
    b.last_used = time.time() - 10
    assert store.sweep() == [b.id]
    assert store.get(b.id) is None
    assert _rows(db, b.id) == 0


def test_maybe_sweep_rate_limits(db):
    store = SessionStore(db, ttl=1, base_salt="s")
    s = store.create()
    s.last_used = time.time() - 10; s.save()
    assert store.maybe_sweep(interval=3600) == [s.id]
    t = store.create(); t.last_used = time.time() - 10; t.save()
    assert store.maybe_sweep(interval=3600) == []      # too soon
    assert store.sweep() == [t.id]


def test_bind_swaps_vault_and_salt(db, make_engine):
    engine = make_engine()
    engine.vault.add("TOK_PERSON_11111111", "engine-own")
    own_salt = engine.salt
    store = SessionStore(db, base_salt="s")
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
    assert dict(s1.vault.items()) == {t1: "Nimal Perera"} and dict(s2.vault.items()) == {t2: "Nimal Perera"}
    # engine state restored afterwards
    assert dict(engine.vault.items()) == {"TOK_PERSON_11111111": "engine-own"} and engine.salt == own_salt
    # persisted
    reloaded = SessionStore(db, base_salt="s").get(s1.id)
    assert dict(reloaded.vault.items()) == {t1: "Nimal Perera"}
    assert s1.calls == 2
