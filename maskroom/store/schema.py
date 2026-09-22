"""Table definitions (SQLAlchemy Core, one MetaData).

Portable across SQLite and Postgres: JSON columns hold the small dict/list
fields, timestamps are epoch floats computed in Python (the API has always
returned them that way), and no column is named after a reserved word
(`user` -> user_id, `numeric` -> is_numeric).
"""
from sqlalchemy import (JSON, Boolean, Column, Float, ForeignKey, Index, Integer,
                        MetaData, String, Table, Text)
from sqlalchemy.sql.expression import false

metadata = MetaData()

# Bumped when a table changes shape. Migrations are applied by hand until the
# project adopts Alembic (see docs/adr/0001-database-for-runtime-state.md).
schema_meta = Table(
    "schema_meta", metadata,
    Column("version", Integer, primary_key=True),
    Column("applied_at", Float, nullable=False),
)

sessions = Table(
    "sessions", metadata,
    Column("id", String(64), primary_key=True),   # SessionStore.valid_id: 8..64 chars
    Column("created", Float, nullable=False),
    Column("last_used", Float, nullable=False),
    Column("calls", Integer, nullable=False, server_default="0"),
    Index("ix_sessions_last_used", "last_used"),
)

vault_entries = Table(
    "vault_entries", metadata,
    Column("session_id", String(64), ForeignKey("sessions.id", ondelete="CASCADE"),
           primary_key=True),
    Column("token", String(128), primary_key=True),
    Column("value", Text, nullable=False),
    Column("is_numeric", Boolean, nullable=False, server_default=false()),
)

audit_records = Table(
    "audit_records", metadata,
    Column("id", String(32), primary_key=True),
    Column("time", Float, nullable=False),
    Column("iso", String(40), nullable=False),
    Column("action", String(32), nullable=False),
    Column("user_id", String(255), nullable=False),
    Column("session_id", String(64), nullable=False, server_default=""),
    Column("ip", String(64), nullable=False, server_default=""),
    Column("kind", String(32), nullable=False),
    Column("filename", Text),
    Column("input_text", Text),
    Column("output_text", Text),
    Column("by_entity", JSON, nullable=False),
    Column("entity_total", Integer, nullable=False, server_default="0"),
    Column("unresolved", JSON, nullable=False),
    Column("changed", Boolean),
    Column("input_file", String(64)),
    Column("output_file", String(64)),
    Index("ix_audit_time", "time"),
    Index("ix_audit_user", "user_id"),
    Index("ix_audit_action", "action"),
)

# The admin rules overlay. `position` is the key because list order is part of
# the overlay fingerprint, so load() must return rows in the order saved.
policy_deny_terms = Table(
    "policy_deny_terms", metadata,
    Column("position", Integer, primary_key=True),
    Column("term", Text, nullable=False),
    Column("entity", String(40), nullable=False),
)

policy_allow_terms = Table(
    "policy_allow_terms", metadata,
    Column("position", Integer, primary_key=True),
    Column("term", Text, nullable=False),
)

policy_regex_rules = Table(
    "policy_regex_rules", metadata,
    Column("position", Integer, primary_key=True),
    Column("name", String(40), nullable=False, unique=True),
    Column("entity", String(40), nullable=False),
    Column("regex", Text, nullable=False),
    Column("score", Float, nullable=False),
    Column("context", JSON, nullable=False),
)

policy_revisions = Table(
    "policy_revisions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", Float, nullable=False),
    Column("user_id", String(255)),
    Column("fingerprint", String(16), nullable=False),
    Column("snapshot", JSON, nullable=False),
)
