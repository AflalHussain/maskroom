"""Relational store for Maskroom's runtime state.

Three things live here, each behind a small class with the same methods the
web app called when they were files: session vaults (SessionStore), the audit
trail (AuditLog) and the admin rules overlay (PolicyStore). Uploaded and masked
files stay on disk; the database holds what needs querying, retention and
sharing between processes.

SQLite is the zero-config default (a file under MASKROOM_DATA_DIR). Set
MASKROOM_DATABASE_URL to a Postgres URL for production. Nothing in this package
is touched inside the masking loop: every store call happens at a request
boundary, so database latency never sits on the per-token path.
"""
from .db import connect, dispose_all, make_engine, init_schema, default_url, is_configured
from .schema import metadata
from .sessions import Session, SessionStore
from .audit import AuditLog, INDEX_FIELDS
from .policy import PolicyStore
from .users import (ROLES, ApiKeyStore, LoginCodeStore, LoginSessionStore, RunStore, UserStore,
                    normalize_email, role_allows)

__all__ = ["connect", "dispose_all", "make_engine", "init_schema", "default_url",
           "is_configured", "metadata", "Session", "SessionStore", "AuditLog",
           "INDEX_FIELDS", "PolicyStore", "ROLES", "ApiKeyStore", "LoginCodeStore", "LoginSessionStore",
           "RunStore", "UserStore", "normalize_email", "role_allows"]
