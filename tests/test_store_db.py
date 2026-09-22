"""Store bootstrap: URL resolution, SQLite settings, schema versioning."""
import os

from sqlalchemy import inspect, select, text

from maskroom.store import db as db_mod
from maskroom.store.schema import schema_meta


def test_normalize_url():
    assert db_mod.normalize_url("postgres://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert db_mod.normalize_url("postgresql://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert db_mod.normalize_url("postgresql+psycopg://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert db_mod.normalize_url("sqlite:///x.db") == "sqlite:///x.db"


def test_default_url_follows_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MASKROOM_DATABASE_URL", raising=False)
    monkeypatch.setenv("MASKROOM_DATA_DIR", str(tmp_path))
    assert db_mod.default_url() == "sqlite:///" + os.path.join(str(tmp_path), "maskroom.db")
    assert not db_mod.is_configured()          # no URL, no file yet
    monkeypatch.setenv("MASKROOM_DATABASE_URL", "postgres://u:p@h/d")
    assert db_mod.default_url() == "postgresql+psycopg://u:p@h/d"
    assert db_mod.is_configured()


def test_file_sqlite_settings_and_schema(tmp_path):
    engine = db_mod.make_engine(f"sqlite:///{tmp_path}/sub/maskroom.db")
    db_mod.init_schema(engine)
    with engine.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert c.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert c.execute(select(schema_meta.c.version)).scalar() == db_mod.SCHEMA_VERSION
    assert {"sessions", "vault_entries", "audit_records", "policy_deny_terms",
            "policy_allow_terms", "policy_regex_rules", "policy_revisions"} <= set(
        inspect(engine).get_table_names())
    engine.dispose()


def test_init_schema_is_idempotent(db):
    db_mod.init_schema(db)
    with db.connect() as c:
        assert c.execute(select(schema_meta)).all().__len__() == 1


def test_connect_caches_per_url():
    a = db_mod.connect("sqlite://")
    b = db_mod.connect("sqlite://")
    assert a is b
