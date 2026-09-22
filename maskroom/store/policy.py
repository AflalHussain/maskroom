"""The admin rules overlay as tables, with a revision history.

The live overlay is three tables (deny terms, allow terms, regex rules) that
load() reads back in saved order into the same normalized dict
maskroom.overlay.validate() produces. Every save() also appends a
policy_revisions row (who, when, fingerprint, full snapshot), which is what
lets a second process notice a change (latest_revision_id) and lets an
operator see or restore history.
"""
import time

from sqlalchemy import delete, func, insert, select

from .. import overlay as overlay_mod
from .schema import (policy_allow_terms as T_ALLOW, policy_deny_terms as T_DENY,
                     policy_regex_rules as T_REGEX, policy_revisions as T_REV)


class PolicyStore:
    def __init__(self, db):
        self.db = db

    def _load(self, conn):
        deny = [{"term": r.term, "entity": r.entity}
                for r in conn.execute(select(T_DENY).order_by(T_DENY.c.position))]
        allow = [r.term for r in conn.execute(select(T_ALLOW).order_by(T_ALLOW.c.position))]
        regex = [{"name": r.name, "entity": r.entity, "regex": r.regex,
                  "score": r.score, "context": list(r.context or [])}
                 for r in conn.execute(select(T_REGEX).order_by(T_REGEX.c.position))]
        return overlay_mod.validate({"version": 1, "deny_terms": deny,
                                     "allow_terms": allow, "regex_rules": regex})

    def load(self):
        """The normalized overlay currently in force (empty if never saved)."""
        with self.db.begin() as conn:
            return self._load(conn)

    def latest_revision_id(self):
        with self.db.begin() as conn:
            return conn.execute(select(func.max(T_REV.c.id))).scalar() or 0

    def current(self):
        """(overlay, revision id) read in one transaction."""
        with self.db.begin() as conn:
            rev = conn.execute(select(func.max(T_REV.c.id))).scalar() or 0
            return self._load(conn), rev

    def save(self, data, user_id=None):
        """Validate, replace the live tables and append a revision, atomically.
        Returns (normalized overlay, new revision id). Raises PolicyError."""
        norm = overlay_mod.validate(data)
        with self.db.begin() as conn:
            for t in (T_DENY, T_ALLOW, T_REGEX):
                conn.execute(delete(t))
            if norm["deny_terms"]:
                conn.execute(insert(T_DENY), [dict(position=i, **d)
                                              for i, d in enumerate(norm["deny_terms"])])
            if norm["allow_terms"]:
                conn.execute(insert(T_ALLOW), [{"position": i, "term": t}
                                               for i, t in enumerate(norm["allow_terms"])])
            if norm["regex_rules"]:
                conn.execute(insert(T_REGEX), [dict(position=i, **r)
                                               for i, r in enumerate(norm["regex_rules"])])
            rev = conn.execute(insert(T_REV).values(
                created_at=time.time(), user_id=user_id,
                fingerprint=overlay_mod.fingerprint(norm), snapshot=norm)).inserted_primary_key[0]
        return norm, rev
