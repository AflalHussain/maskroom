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
    Column("owner_id", String(32)),               # principal id; NULL = created with auth off (v1 rows)
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

# ---------------------------------------------------------------- identity
# Who may do what. Users come from single sign-on (one row per email, created
# on first login); roles are managed here, never mapped from the provider.
users = Table(
    "users", metadata,
    Column("id", String(32), primary_key=True),            # "usr_" + 12 hex
    Column("email", String(255), nullable=False, unique=True),   # stored lower-cased
    Column("name", String(255), nullable=False, server_default=""),
    Column("role", String(16), nullable=False, server_default="staff"),
    Column("disabled", Boolean, nullable=False, server_default=false()),
    Column("created", Float, nullable=False),
    Column("last_login", Float),
    Column("provider_sub", String(255)),
)

# A browser or extension login. The cookie carries a random token; the row id
# is its SHA-256, so a copy of the database cannot forge a session.
login_sessions = Table(
    "login_sessions", metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("created", Float, nullable=False),
    Column("expires", Float, nullable=False),
    Column("last_seen", Float, nullable=False),
    Column("ip", String(64), nullable=False, server_default=""),
    Column("id_token", Text),   # for RP-initiated logout (id_token_hint); None when absent
    Index("ix_login_sessions_expires", "expires"),
    Index("ix_login_sessions_user", "user_id"),
)

# A one-time code handed to a native client (the desktop helper) at the end of
# a browser sign-in, redeemed once within a minute for a login session. Same
# hashing rule as login_sessions.
login_codes = Table(
    "login_codes", metadata,
    Column("id", String(64), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("created", Float, nullable=False),
    Column("expires", Float, nullable=False),
    Column("ip", String(64), nullable=False, server_default=""),
    Column("id_token", Text),
    Index("ix_login_codes_expires", "expires"),
)

# Named credentials for scripts, gateways and MCP servers. Only the hash is kept.
api_keys = Table(
    "api_keys", metadata,
    Column("id", String(32), primary_key=True),            # "key_" + 12 hex
    Column("name", String(80), nullable=False),
    Column("key_hash", String(64), nullable=False, unique=True),
    Column("role", String(16), nullable=False, server_default="staff"),
    Column("created_by", String(255)),
    Column("created_at", Float, nullable=False),
    Column("last_used", Float),
    Column("revoked_at", Float),
)

# One row per file run so downloads can be limited to the run's owner.
runs = Table(
    "runs", metadata,
    Column("run_id", String(32), primary_key=True),
    Column("owner_id", String(32)),
    Column("session_id", String(64)),
    Column("created", Float, nullable=False),
    Index("ix_runs_created", "created"),
)
