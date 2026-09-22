"""Dialect checks that only mean something on a real server. Run with
MASKROOM_TEST_DATABASE_URL=postgresql+psycopg://maskroom:maskroom@localhost:5432/maskroom
(the `db` service in docker-compose.yml); skipped otherwise. Setting that
variable also runs the rest of the suite against the same server."""
import os

import pytest
from sqlalchemy import select

from maskroom.store import AuditLog, SessionStore
from maskroom.store.schema import vault_entries

pytestmark = pytest.mark.skipif(not os.environ.get("MASKROOM_TEST_DATABASE_URL"),
                                reason="MASKROOM_TEST_DATABASE_URL not set")


def test_dialect_is_postgres(db):
    assert db.dialect.name == "postgresql"


def test_conflict_and_numeric_promotion(db):
    store = SessionStore(db, base_salt="s")
    s = store.create()
    s.vault.add("TOK_A_1", "a"); s.save()
    s.vault.add("TOK_A_1", "a"); s.vault.mark_numeric("TOK_A_1"); s.save()   # no duplicate, flag set
    with db.connect() as c:
        rows = c.execute(select(vault_entries).where(vault_entries.c.session_id == s.id)).all()
    assert len(rows) == 1 and rows[0].is_numeric is True


def test_json_columns_round_trip(db, tmp_path):
    a = AuditLog(db, str(tmp_path), ttl_days=0)
    rec = a.record(action="unmask", user="Ünïcode@corp.lk", input_text="x", output_text="y",
                   by_entity={"PERSON": 2, "LK_NIC": 1}, unresolved=["TOK_X_1"])
    got = a.get(rec["id"])
    assert got["by_entity"] == {"PERSON": 2, "LK_NIC": 1} and got["unresolved"] == ["TOK_X_1"]
    assert a.list(user="ünïcode")["total"] == 1
