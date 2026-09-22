"""Rules overlay in the database: live tables, revisions, YAML interchange."""
import os

import yaml
from sqlalchemy import select

from maskroom import overlay
from maskroom.store import PolicyStore, db as db_mod
from maskroom.store.schema import policy_revisions

BODY = {
    "deny_terms": [{"term": "Project Halo"}, {"term": "Operation Kite", "entity": "CODENAME"}],
    "allow_terms": ["Nimal Perera", "Maskroom"],
    "regex_rules": [{"name": "emp", "entity": "EMPLOYEE_ID", "regex": r"EMP-\d{6}",
                     "score": 0.9, "context": ["staff"]}],
}


def test_empty_store_is_empty_overlay(db):
    store = PolicyStore(db)
    assert store.load() == overlay.validate(None)
    assert store.latest_revision_id() == 0
    assert store.current() == (overlay.validate(None), 0)


def test_save_preserves_order_and_records_revision(db):
    store = PolicyStore(db)
    norm, rev = store.save(BODY, user_id="alice")
    assert norm == overlay.validate(BODY) and rev == 1
    assert store.load() == norm
    assert [d["term"] for d in store.load()["deny_terms"]] == ["Project Halo", "Operation Kite"]
    with db.connect() as c:
        row = c.execute(select(policy_revisions)).one()
    assert row.user_id == "alice" and row.fingerprint == overlay.fingerprint(norm)
    assert row.snapshot == norm
    # saving the empty overlay still leaves a revision behind
    _norm, rev2 = store.save(None)
    assert rev2 == 2 and store.load() == overlay.validate(None)


def test_overlay_module_defaults_to_the_store(db, monkeypatch):
    """overlay.load()/save() with no path talk to the configured database."""
    monkeypatch.setattr(db_mod, "_engines", {db_mod.default_url(): db})
    saved = overlay.save({"allow_terms": ["Maskroom"]})
    assert overlay.load() == saved
    assert PolicyStore(db).latest_revision_id() == 1


def test_no_database_means_no_side_effects(monkeypatch, tmp_path):
    monkeypatch.delenv("MASKROOM_DATABASE_URL")
    monkeypatch.setenv("MASKROOM_DATA_DIR", str(tmp_path))
    assert overlay.load() == overlay.validate(None)
    assert not os.path.exists(tmp_path / "maskroom.db")


def test_admin_cli_import_export(db, monkeypatch, tmp_path, capsys):
    from maskroom.admin import main
    monkeypatch.setattr(db_mod, "_engines", {db_mod.default_url(): db})
    src = tmp_path / "rules.yaml"
    src.write_text(yaml.safe_dump(BODY))
    assert main(["policy", "import", str(src), "--user", "ops"]) == 0
    assert "revision 1: deny=2 allow=2 regex=1" in capsys.readouterr().out
    assert PolicyStore(db).load() == overlay.validate(BODY)

    out = tmp_path / "export.yaml"
    assert main(["policy", "export", str(out)]) == 0
    assert "wrote" in capsys.readouterr().out
    assert overlay.load(path=str(out)) == overlay.validate(BODY)
    assert main(["policy", "export"]) == 0
    assert yaml.safe_load(capsys.readouterr().out) == overlay.validate(BODY)

    assert main(["db", "init"]) == 0
    assert "schema ready" in capsys.readouterr().out
