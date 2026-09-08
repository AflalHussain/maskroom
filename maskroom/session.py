"""Session vaults for the LLM round trip.

A conversation with an LLM spans many masking calls (a prompt, two files,
three follow-ups) and one un-masking pass per reply. All of them must share
one vault so a value re-sent in turn 5 maps to the token issued in turn 1,
and the vault must be findable again later and retired when the
conversation is over. SessionStore keeps one vault per session id on disk,
derives a per-session token salt (so two sessions never share tokens), and
binds a shared, warm engine to a session for the duration of one request.
"""
import hashlib
import hmac
import json
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager


class Session:
    def __init__(self, root, session_id, base_salt):
        self.id = session_id
        self.dir = os.path.join(root, session_id)
        self.vault = {}
        self.numeric_tokens = set()
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
    @property
    def vault_path(self):
        return os.path.join(self.dir, "vault.json")

    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        payload = {"session_id": self.id, "created": self.created,
                   "last_used": self.last_used, "calls": self.calls,
                   "mappings": self.vault, "numeric_tokens": sorted(self.numeric_tokens)}
        tmp = self.vault_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.vault_path)

    @classmethod
    def load(cls, root, session_id, base_salt):
        sess = cls(root, session_id, base_salt)
        with open(sess.vault_path, encoding="utf-8") as f:
            data = json.load(f)
        sess.vault = data.get("mappings", {})
        sess.numeric_tokens = set(data.get("numeric_tokens", []))
        sess.created = data.get("created", sess.created)
        sess.last_used = data.get("last_used", sess.last_used)
        sess.calls = data.get("calls", 0)
        return sess

    def info(self):
        return {"session_id": self.id, "created": self.created,
                "last_used": self.last_used, "calls": self.calls,
                "vault_entries": len(self.vault)}


class SessionStore:
    """
    root:      directory holding one sub-directory per session.
    ttl:       seconds of inactivity after which a session is deleted by
               sweep() (None = never).
    base_salt: secret from which per-session salts are derived. Defaults
               to PII_TOKEN_SALT, like the engine.
    """

    def __init__(self, root, ttl=24 * 3600, base_salt=None):
        self.root = root
        self.ttl = ttl
        self.base_salt = base_salt or os.environ.get("PII_TOKEN_SALT", "EnterpriseRiskManagement2026")
        self._sessions = {}
        self._lock = threading.Lock()
        # All engines share one analysis lock: spaCy pipelines are not
        # guaranteed thread-safe and the engine keeps per-run state.
        self.engine_lock = threading.RLock()
        os.makedirs(root, exist_ok=True)

    @staticmethod
    def valid_id(session_id):
        return isinstance(session_id, str) and 8 <= len(session_id) <= 64 \
            and all(c.isalnum() or c in "-_" for c in session_id)

    def create(self):
        sess = Session(self.root, uuid.uuid4().hex[:16], self.base_salt)
        with self._lock:
            self._sessions[sess.id] = sess
        sess.save()
        return sess

    def get(self, session_id):
        """The session, loading it from disk if needed; None if unknown."""
        if not self.valid_id(session_id):
            return None
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None and os.path.isfile(os.path.join(self.root, session_id, "vault.json")):
                sess = Session.load(self.root, session_id, self.base_salt)
                self._sessions[session_id] = sess
            return sess

    def get_or_create(self, session_id=None):
        return (self.get(session_id) if session_id else None) or self.create()

    def delete(self, session_id):
        with self._lock:
            sess = self._sessions.pop(session_id, None)
        path = os.path.join(self.root, session_id) if self.valid_id(session_id) else None
        if path and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            return True
        return sess is not None

    def sweep(self):
        """Delete sessions idle for longer than ttl. Returns their ids."""
        if self.ttl is None:
            return []
        cutoff = time.time() - self.ttl
        gone = []
        for name in os.listdir(self.root):
            path = os.path.join(self.root, name, "vault.json")
            if not os.path.isfile(path):
                continue
            with self._lock:
                sess = self._sessions.get(name)
            last = sess.last_used if sess else os.path.getmtime(path)
            if last < cutoff:
                self.delete(name)
                gone.append(name)
        return gone

    @contextmanager
    def bind(self, engine, session):
        """
        Run one request with `engine` working on `session`'s vault and salt.
        The engine's own vault/salt/report are swapped in for the duration
        and restored afterwards; the session is persisted on exit. Holds
        the shared engine lock, so requests are serialized.
        """
        with self.engine_lock, session.lock:
            saved = (engine.vault, engine.salt, engine.numeric_tokens,
                     engine.numeric_cells, engine.report)
            engine.vault = session.vault
            engine.salt = session.salt
            engine.numeric_tokens = session.numeric_tokens
            engine.numeric_cells = set()
            engine.report = []
            try:
                yield engine
            finally:
                session.last_used = time.time()
                session.calls += 1
                (engine.vault, engine.salt, engine.numeric_tokens,
                 engine.numeric_cells, engine.report) = saved
                session.save()
