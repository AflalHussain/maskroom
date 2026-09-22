"""Audit store: recording, listing/filtering, files, retention."""
import os
import time

from sqlalchemy import update

from maskroom.store.schema import audit_records
from webui.audit import AuditLog


def test_record_text_and_get(db, tmp_path):
    a = AuditLog(db, str(tmp_path), ttl_days=0)
    rec = a.record(action="mask", user="alice@corp.lk", session_id="s1",
                   input_text="Nimal Perera", output_text="TOK_PERSON_1",
                   by_entity={"PERSON": 1}, entity_total=1, changed=True)
    got = a.get(rec["id"])
    assert got["input_text"] == "Nimal Perera" and got["output_text"] == "TOK_PERSON_1"
    assert got["user"] == "alice@corp.lk" and got["by_entity"] == {"PERSON": 1}
    listing = a.list()
    assert listing["total"] == 1 and listing["records"][0]["id"] == rec["id"]
    # index row carries no raw text
    assert "input_text" not in listing["records"][0]


def test_list_filters(db, tmp_path):
    a = AuditLog(db, str(tmp_path), ttl_days=0)
    a.record(action="mask", user="alice", input_text="x", output_text="y")
    a.record(action="unmask", user="bob", input_text="x", output_text="y")
    a.record(action="mask", user="bob", filename="f.xlsx", input_text="x", output_text="y")
    assert a.list(user="bob")["total"] == 2
    assert a.list(action="unmask")["total"] == 1
    assert a.list(q="f.xlsx")["total"] == 1
    # newest first
    assert a.list()["records"][0]["user"] == "bob"


def test_files_stored_and_retrievable(db, tmp_path):
    a = AuditLog(db, str(tmp_path), ttl_days=0)
    src_in = tmp_path / "in.xlsx"; src_in.write_bytes(b"ORIGINAL")
    src_out = tmp_path / "out.xlsx"; src_out.write_bytes(b"MASKED")
    rec = a.record(action="process", user="u", filename="staff.xlsx", kind="excel",
                   input_file=str(src_in), output_file=str(src_out), by_entity={"PERSON": 2}, entity_total=2)
    assert open(a.file_path(rec["id"], "input"), "rb").read() == b"ORIGINAL"
    assert open(a.file_path(rec["id"], "output"), "rb").read() == b"MASKED"
    row = a.list()["records"][0]
    assert row["has_input_file"] and row["has_output_file"]


def test_retention_sweep(db, tmp_path):
    a = AuditLog(db, str(tmp_path), ttl_days=1)
    src = tmp_path / "in.txt"; src.write_text("x")
    old = a.record(action="mask", user="u", input_text="x", output_text="y", input_file=str(src))
    assert os.path.isdir(tmp_path / old["id"])
    # backdate the record to 2 days ago
    with db.begin() as c:
        c.execute(update(audit_records).where(audit_records.c.id == old["id"])
                  .values(time=time.time() - 2 * 86400))
    fresh = a.record(action="mask", user="u", input_text="x2", output_text="y2")
    gone = a.sweep()
    assert old["id"] in gone and a.get(old["id"]) is None
    assert a.get(fresh["id"]) is not None
    assert not os.path.isdir(tmp_path / old["id"])


def test_stats(db, tmp_path):
    a = AuditLog(db, str(tmp_path), ttl_days=30)
    a.record(action="mask", user="a", input_text="x", output_text="y")
    a.record(action="mask", user="b", input_text="x", output_text="y")
    a.record(action="unmask", user="a", input_text="x", output_text="y")
    st = a.stats()
    assert st["total"] == 3 and st["by_action"]["mask"] == 2 and st["ttl_days"] == 30
    assert set(st["by_user"]) == {"a", "b"}


def test_list_filters_are_escaped_and_ranged(db, tmp_path):
    a = AuditLog(db, str(tmp_path), ttl_days=0)
    a.record(action="mask", user="Alice@Corp.lk", input_text="x", output_text="y")
    t_mid = time.time()
    a.record(action="mask", user="bob", filename="100%_done.xlsx", input_text="x", output_text="y")
    assert a.list(user="alice")["total"] == 1            # case-insensitive
    assert a.list(q="100%")["total"] == 1                # % is literal, not a wildcard
    assert a.list(q="1__%")["total"] == 0                # _ is literal too
    assert a.list(since=t_mid)["total"] == 1 and a.list(until=t_mid)["total"] == 1
    page = a.list(limit=1, offset=1)
    assert page["total"] == 2 and len(page["records"]) == 1 and page["offset"] == 1
    assert "input_file" not in a.get(page["records"][0]["id"])
