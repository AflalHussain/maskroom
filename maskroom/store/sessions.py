"""Session vaults for the LLM round trip, stored as rows.

A conversation with an LLM spans many masking calls (a prompt, two files,
three follow-ups) and one un-masking pass per reply. All of them must share
one vault so a value re-sent in turn 5 maps to the token issued in turn 1,
and the vault must be findable again later and retired when the
conversation is over. SessionStore keeps one row per session plus one row
per token, derives a per-session token salt (so two sessions never share
tokens), and binds a shared, warm engine to a session for the duration of
one request.

Only what changed is written: the Vault tracks dirty tokens and save() flushes
them in one batched INSERT ... ON CONFLICT DO NOTHING. Entries are append-only
and tokens are deterministic per session salt, so two processes masking the
same value in the same session write the same row and neither loses data. A
per-process cache avoids re-reading the vault on every call; get() re-syncs
it when another process has touched the session since.
"""
import hashlib
import hmac
import os
import threading
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import case, delete, func, select, update

from ..vault import Vault
from ._upsert import _insert_for, insert_ignore
from .schema import sessions as T_SESSIONS, vault_entries as T_ENTRIES


class Session:
    def __init__(self, store, session_id, base_salt, owner_id=None):
        self.store = store
        self.id = session_id
        self.owner_id = owner_id  # principal that created it; None with auth off
        self.vault = Vault()
        self.created = time.time()
        self.last_used = self.created
        self.calls = 0
        self.lock = threading.Lock()
        # Per-session salt: tokens are deterministic within a session but
        # differ between sessions, so a vault or masked text from one
        # conversation says nothing about another.
        self.salt = hmac.new(base_salt.encode("utf-8"), session_id.encode("utf-8"),
                             hashlib.sha256).hexdigest()

    # ----------------------------------------------------------- storage
    def save(self):
        """Persist the session row and any vault entries added since the
        last save. Idempotent: a clean vault writes only the session row."""
        rows = self.vault.dirty_rows()
        with self.store.db.begin() as conn:
            # Another process may have touched the row since we loaded it:
            # keep the newer last_used / higher calls rather than overwrite.
            ins = _insert_for(conn)(T_SESSIONS).values(
                id=self.id, created=self.created, last_used=self.last_used, calls=self.calls,
                owner_id=self.owner_id)  # owner is set once (not in set_): first writer wins
            conn.execute(ins.on_conflict_do_update(index_elements=["id"], set_={
                "last_used": case((T_SESSIONS.c.last_used > ins.excluded.last_used,
                                   T_SESSIONS.c.last_used), else_=ins.excluded.last_used),
                "calls": case((T_SESSIONS.c.calls > ins.excluded.calls,
                               T_SESSIONS.c.calls), else_=ins.excluded.calls)}))
            if rows:
                insert_ignore(conn, T_ENTRIES,
                              [{"session_id": self.id, "token": t, "value": v, "is_numeric": n}
                               for t, v, n in rows],
                              keys=["session_id", "token"])
                numeric = [t for t, _v, n in rows if n]
                if numeric:  # DO NOTHING skips promotions of rows that already existed
                    conn.execute(update(T_ENTRIES)
                                 .where(T_ENTRIES.c.session_id == self.id,
                                        T_ENTRIES.c.token.in_(numeric))
                                 .values(is_numeric=True))
        self.vault.mark_clean()

    def _hydrate(self, conn):
        for token, value, numeric in conn.execute(
                select(T_ENTRIES.c.token, T_ENTRIES.c.value, T_ENTRIES.c.is_numeric)
                .where(T_ENTRIES.c.session_id == self.id)):
            if token not in self.vault:
                self.vault.hydrate(token, value, numeric=bool(numeric))

    @classmethod
    def load(cls, store, session_id, base_salt):
        """The session from the database, or None if there is no such row."""
        with store.db.begin() as conn:
            row = conn.execute(select(T_SESSIONS).where(T_SESSIONS.c.id == session_id)).first()
            if row is None:
                return None
            sess = cls(store, session_id, base_salt, owner_id=row.owner_id)
            sess.created, sess.last_used, sess.calls = row.created, row.last_used, row.calls
            sess._hydrate(conn)
        return sess

    def sync(self):
        """Pick up entries another process added since we cached this
        session (the row count is the cheap tell: entries are never removed
        while a session lives). Returns False if the session no longer exists."""
        with self.store.db.begin() as conn:
            row = conn.execute(select(T_SESSIONS.c.last_used, T_SESSIONS.c.calls)
                               .where(T_SESSIONS.c.id == self.id)).first()
            if row is None:
                return False
            self.last_used = max(self.last_used, row.last_used)
            self.calls = max(self.calls, row.calls)
            n = conn.execute(select(func.count()).select_from(T_ENTRIES)
                             .where(T_ENTRIES.c.session_id == self.id)).scalar()
            if n > len(self.vault):
                self._hydrate(conn)
        return True

    def export(self):
        """The vault as the downloadable JSON document: session fields plus
        Vault.to_dict(). Flushes first so the document includes this
        process's latest entries and any another process wrote."""
        self.save()
        with self.store.db.begin() as conn:
            self._hydrate(conn)
        payload = {"session_id": self.id, "created": self.created,
                   "last_used": self.last_used, "calls": self.calls,
                   **self.vault.to_dict()}
        payload["numeric_cells"] = []  # per-run bookkeeping, never persisted
        return payload

    def info(self):
        return {"session_id": self.id, "created": self.created,
                "last_used": self.last_used, "calls": self.calls,
                "vault_entries": len(self.vault), "owner_id": self.owner_id}


class SessionStore:
    """
    db:        a SQLAlchemy Engine (see maskroom.store.db.connect).
    ttl:       seconds of inactivity after which a session is deleted by
               sweep() (None = never).
    base_salt: secret from which per-session salts are derived. Defaults
               to PII_TOKEN_SALT, like the engine.
    """

    def __init__(self, db, ttl=24 * 3600, base_salt=None):
        self.db = db
        self.ttl = ttl
        self.base_salt = base_salt or os.environ.get("PII_TOKEN_SALT", "EnterpriseRiskManagement2026")
        self._sessions = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0
        # All engines share one analysis lock: spaCy pipelines are not
        # guaranteed thread-safe and the engine keeps per-run state.
        self.engine_lock = threading.RLock()

    @staticmethod
    def valid_id(session_id):
        return isinstance(session_id, str) and 8 <= len(session_id) <= 64 \
            and all(c.isalnum() or c in "-_" for c in session_id)

    def create(self, owner_id=None):
        sess = Session(self, uuid.uuid4().hex[:16], self.base_salt, owner_id=owner_id)
        sess.save()
        with self._lock:
            self._sessions[sess.id] = sess
        return sess

    def get(self, session_id):
        """The session, loading it from the database if needed; None if unknown."""
        if not self.valid_id(session_id):
            return None
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                sess = Session.load(self, session_id, self.base_salt)
                if sess is not None:
                    self._sessions[session_id] = sess
            elif not sess.sync():
                self._sessions.pop(session_id, None)
                return None
            return sess

    def delete(self, session_id):
        with self._lock:
            sess = self._sessions.pop(session_id, None)
        if not self.valid_id(session_id):
            return sess is not None
        with self.db.begin() as conn:
            conn.execute(delete(T_ENTRIES).where(T_ENTRIES.c.session_id == session_id))
            removed = conn.execute(delete(T_SESSIONS).where(T_SESSIONS.c.id == session_id)).rowcount
        return removed > 0 or sess is not None

    def sweep(self):
        """Delete sessions idle for longer than ttl. Returns their ids."""
        if self.ttl is None:
            return []
        cutoff = time.time() - self.ttl
        with self._lock:
            stale = [sid for sid, s in self._sessions.items() if s.last_used < cutoff]
        with self.db.begin() as conn:
            ids = [r[0] for r in conn.execute(select(T_SESSIONS.c.id)
                                              .where(T_SESSIONS.c.last_used < cutoff))]
            ids = list(dict.fromkeys(ids + stale))
            if ids:
                conn.execute(delete(T_ENTRIES).where(T_ENTRIES.c.session_id.in_(ids)))
                conn.execute(delete(T_SESSIONS).where(T_SESSIONS.c.id.in_(ids)))
        with self._lock:
            for sid in ids:
                self._sessions.pop(sid, None)
        return ids

    def maybe_sweep(self, interval=60.0):
        """sweep(), but at most once per `interval` seconds per process."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_sweep < interval:
                return []
            self._last_sweep = now
        return self.sweep()

    @contextmanager
    def bind(self, engine, session):
        """
        Run one request with `engine` working on `session`'s vault and salt.
        The engine's own vault/salt/report are swapped in for the duration
        and restored afterwards; the session is persisted on exit. Holds
        the shared engine lock, so requests are serialized.
        """
        with self.engine_lock, session.lock:
            saved = (engine.vault, engine.salt, engine.report)
            engine.vault = session.vault
            engine.vault.begin_run()  # cell coords are per-workbook, not per-session
            engine.salt = session.salt
            engine.report = []
            try:
                yield engine
            finally:
                session.last_used = time.time()
                session.calls += 1
                (engine.vault, engine.salt, engine.report) = saved
                session.save()
