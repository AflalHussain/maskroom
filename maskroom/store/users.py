"""Who may do what: users, their roles, login sessions, service keys, and run
ownership.

Users arrive through single sign-on; the first login creates the row as
`staff` (or `admin` when bootstrapped), and roles are changed only here, by an
administrator, never mapped from the identity provider. A login session is a
random cookie token whose SHA-256 is the row id; a service key is a
`mr_...` secret whose SHA-256 is stored. Neither plaintext ever touches the
database, so a copy of it cannot forge either.
"""
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, insert, select, update

from .schema import api_keys as T_KEYS, login_sessions as T_LOGINS, runs as T_RUNS, users as T_USERS

ROLES = ("staff", "auditor", "admin")   # each role includes the ones before it
_RANK = {r: i for i, r in enumerate(ROLES)}


def role_allows(have, need):
    return have in _RANK and need in _RANK and _RANK[have] >= _RANK[need]


def normalize_email(email):
    return (email or "").strip().lower()


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass
class User:
    id: str
    email: str
    name: str
    role: str
    disabled: bool
    created: float
    last_login: float | None
    provider_sub: str | None

    def to_dict(self):
        return {"id": self.id, "email": self.email, "name": self.name, "role": self.role,
                "disabled": self.disabled, "created": self.created, "last_login": self.last_login}


@dataclass
class ApiKey:
    id: str
    name: str
    role: str
    created_by: str | None
    created_at: float
    last_used: float | None
    revoked_at: float | None

    def to_dict(self):
        return {"id": self.id, "name": self.name, "role": self.role, "created_by": self.created_by,
                "created_at": self.created_at, "last_used": self.last_used,
                "revoked_at": self.revoked_at}


@dataclass
class LoginSession:
    id: str
    user_id: str
    created: float
    expires: float
    last_seen: float
    ip: str


def _user(row):
    return User(row.id, row.email, row.name, row.role, bool(row.disabled), row.created,
                row.last_login, row.provider_sub)


def _key(row):
    return ApiKey(row.id, row.name, row.role, row.created_by, row.created_at, row.last_used,
                  row.revoked_at)


class UserStore:
    def __init__(self, db):
        self.db = db

    def upsert_login(self, email, name="", sub=None, bootstrap_admin=False):
        """The user for a successful login, created on first sight. Existing
        rows keep their id and role; name, last_login and provider_sub are
        refreshed. bootstrap_admin only applies to a brand-new row."""
        email = normalize_email(email)
        if not email:
            raise ValueError("email is required")
        now = time.time()
        with self.db.begin() as conn:
            row = conn.execute(select(T_USERS).where(T_USERS.c.email == email)).first()
            if row is None:
                uid = "usr_" + uuid.uuid4().hex[:12]
                conn.execute(insert(T_USERS).values(
                    id=uid, email=email, name=name or "", role="admin" if bootstrap_admin else "staff",
                    disabled=False, created=now, last_login=now, provider_sub=sub))
                row = conn.execute(select(T_USERS).where(T_USERS.c.id == uid)).first()
            else:
                values = {"last_login": now}
                if name:
                    values["name"] = name
                if sub:
                    values["provider_sub"] = sub
                conn.execute(update(T_USERS).where(T_USERS.c.id == row.id).values(**values))
                row = conn.execute(select(T_USERS).where(T_USERS.c.id == row.id)).first()
        return _user(row)

    def get(self, user_id):
        with self.db.begin() as conn:
            row = conn.execute(select(T_USERS).where(T_USERS.c.id == (user_id or ""))).first()
        return _user(row) if row else None

    def get_by_email(self, email):
        with self.db.begin() as conn:
            row = conn.execute(select(T_USERS).where(T_USERS.c.email == normalize_email(email))).first()
        return _user(row) if row else None

    def list(self):
        with self.db.begin() as conn:
            rows = conn.execute(select(T_USERS).order_by(T_USERS.c.email)).all()
        return [_user(r) for r in rows]

    def set_role(self, user_id, role):
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r} (one of {', '.join(ROLES)})")
        with self.db.begin() as conn:
            return conn.execute(update(T_USERS).where(T_USERS.c.id == user_id)
                                .values(role=role)).rowcount > 0

    def set_disabled(self, user_id, disabled):
        with self.db.begin() as conn:
            return conn.execute(update(T_USERS).where(T_USERS.c.id == user_id)
                                .values(disabled=bool(disabled))).rowcount > 0


class LoginSessionStore:
    """Cookie sessions with a sliding expiry."""

    def __init__(self, db, hours=12.0):
        self.db = db
        self.ttl = float(hours) * 3600

    def create(self, user_id, ip=""):
        """Returns the plaintext cookie token (never stored)."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.db.begin() as conn:
            conn.execute(insert(T_LOGINS).values(
                id=_sha(token), user_id=user_id, created=now, expires=now + self.ttl,
                last_seen=now, ip=ip or ""))
        return token

    def resolve(self, token):
        """(LoginSession, User) for a live token of an enabled user, else None.
        Slides the expiry forward at most once a minute."""
        if not token:
            return None
        now = time.time()
        with self.db.begin() as conn:
            row = conn.execute(
                select(T_LOGINS.c.id.label("sid"), T_LOGINS.c.user_id, T_LOGINS.c.created.label("s_created"),
                       T_LOGINS.c.expires, T_LOGINS.c.last_seen, T_LOGINS.c.ip,
                       T_USERS.c.email, T_USERS.c.name, T_USERS.c.role, T_USERS.c.disabled,
                       T_USERS.c.created.label("u_created"), T_USERS.c.last_login, T_USERS.c.provider_sub)
                .join_from(T_LOGINS, T_USERS, T_USERS.c.id == T_LOGINS.c.user_id)
                .where(T_LOGINS.c.id == _sha(token))).first()
            if row is None or row.expires < now or row.disabled:
                return None
            expires = row.expires
            if now - row.last_seen > 60:
                expires = now + self.ttl
                conn.execute(update(T_LOGINS).where(T_LOGINS.c.id == row.sid)
                             .values(last_seen=now, expires=expires))
        sess = LoginSession(row.sid, row.user_id, row.s_created, expires, now, row.ip)
        user = User(row.user_id, row.email, row.name, row.role, bool(row.disabled),
                    row.u_created, row.last_login, row.provider_sub)
        return sess, user

    def revoke(self, token):
        with self.db.begin() as conn:
            return conn.execute(delete(T_LOGINS).where(T_LOGINS.c.id == _sha(token or ""))).rowcount > 0

    def revoke_user(self, user_id):
        with self.db.begin() as conn:
            return conn.execute(delete(T_LOGINS).where(T_LOGINS.c.user_id == user_id)).rowcount

    def sweep(self):
        with self.db.begin() as conn:
            return conn.execute(delete(T_LOGINS).where(T_LOGINS.c.expires < time.time())).rowcount


class ApiKeyStore:
    PREFIX = "mr_"

    def __init__(self, db):
        self.db = db

    def create(self, name, role="staff", created_by=None):
        """(ApiKey, plaintext). The plaintext is shown once and never stored."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        plaintext = self.PREFIX + secrets.token_urlsafe(32)
        kid = "key_" + uuid.uuid4().hex[:12]
        now = time.time()
        with self.db.begin() as conn:
            conn.execute(insert(T_KEYS).values(id=kid, name=name, key_hash=_sha(plaintext), role=role,
                                               created_by=created_by, created_at=now))
            row = conn.execute(select(T_KEYS).where(T_KEYS.c.id == kid)).first()
        return _key(row), plaintext

    def authenticate(self, plaintext):
        """The live key for a presented secret, else None. Touches last_used
        at most once a minute."""
        if not plaintext or not plaintext.startswith(self.PREFIX):
            return None
        now = time.time()
        with self.db.begin() as conn:
            row = conn.execute(select(T_KEYS).where(T_KEYS.c.key_hash == _sha(plaintext))).first()
            if row is None or row.revoked_at is not None:
                return None
            last_used = row.last_used
            if not last_used or now - last_used > 60:
                last_used = now
                conn.execute(update(T_KEYS).where(T_KEYS.c.id == row.id).values(last_used=now))
        key = _key(row)
        key.last_used = last_used
        return key

    def list(self):
        with self.db.begin() as conn:
            rows = conn.execute(select(T_KEYS).order_by(T_KEYS.c.created_at.desc())).all()
        return [_key(r) for r in rows]

    def revoke(self, key_id):
        with self.db.begin() as conn:
            return conn.execute(update(T_KEYS).where(T_KEYS.c.id == key_id, T_KEYS.c.revoked_at.is_(None))
                                .values(revoked_at=time.time())).rowcount > 0


MISSING = object()


class RunStore:
    """Who owns each file run, so /api/download only serves the owner."""

    def __init__(self, db):
        self.db = db

    def create(self, run_id, owner_id=None, session_id=None):
        with self.db.begin() as conn:
            conn.execute(insert(T_RUNS).values(run_id=run_id, owner_id=owner_id,
                                               session_id=session_id, created=time.time()))

    def owner_of(self, run_id):
        """The owner id (None for an ownerless run), or MISSING for an unknown run."""
        with self.db.begin() as conn:
            row = conn.execute(select(T_RUNS.c.owner_id).where(T_RUNS.c.run_id == (run_id or ""))).first()
        return MISSING if row is None else row.owner_id

    def sweep(self, older_than_s):
        with self.db.begin() as conn:
            return conn.execute(delete(T_RUNS).where(T_RUNS.c.created < time.time() - older_than_s)).rowcount
