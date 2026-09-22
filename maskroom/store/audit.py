"""Audit trail of every masking operation, for the admin dashboard.

Each record keeps the original input and the masked output (the admin's
explicit requirement), attributed to the signed-in user's email or a service
key's name (webui/auth.py), with a retention window so raw PII is not kept
forever.

WARNING: this store contains real PII (the original inputs). It must be
readable by auditors and administrators only and sit on protected storage.
Retention (AUDIT_TTL_DAYS) bounds how long originals live; it is not encryption.

Metadata and texts are rows in `audit_records`; files a user uploaded or
received are copied under <files_root>/<id>/input.<ext> and output.<ext>,
with only their names in the row.
"""
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, insert, or_, select

from .schema import audit_records as T

INDEX_FIELDS = ("id", "time", "iso", "action", "user", "session_id", "kind",
                "filename", "by_entity", "entity_total")
_TEXT_COLS = ("user_id", "filename", "action", "session_id", "kind")


def _like(needle):
    """A case-insensitive substring pattern with LIKE metacharacters escaped."""
    esc = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


class AuditLog:
    def __init__(self, db, files_root, ttl_days=90):
        self.db = db
        self.root = files_root
        self.ttl_days = float(ttl_days or 0)
        self._lock = threading.Lock()
        self._last_sweep = 0.0
        os.makedirs(files_root, exist_ok=True)

    # ----------------------------------------------------------- writing
    def record(self, action, user=None, session_id=None, ip="", kind="text",
               input_text=None, output_text=None, input_file=None, output_file=None,
               filename=None, by_entity=None, entity_total=0, unresolved=None, changed=None):
        rid = uuid.uuid4().hex[:16]
        ts = time.time()
        stored = {}
        for role, src in (("input", input_file), ("output", output_file)):
            if src and os.path.isfile(src):
                rec_dir = os.path.join(self.root, rid)
                os.makedirs(rec_dir, exist_ok=True)
                ext = os.path.splitext(src)[1] or ".bin"
                dst = os.path.join(rec_dir, role + ext)
                shutil.copyfile(src, dst)
                stored[role + "_file"] = os.path.basename(dst)
        rec = {
            "id": rid, "time": ts,
            "iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "action": action, "user": user or "unknown", "session_id": session_id or "",
            "ip": ip or "", "kind": kind or "text",
            "input_text": input_text, "output_text": output_text,
            "filename": filename, "by_entity": by_entity or {},
            "entity_total": int(entity_total or 0),
            "unresolved": unresolved or [], "changed": changed, **stored,
        }
        row = dict(rec, user_id=rec["user"])
        del row["user"]
        with self.db.begin() as conn:
            conn.execute(insert(T).values(**row))
        return rec

    # ----------------------------------------------------------- reading
    @staticmethod
    def _index_row(r):
        d = {"id": r.id, "time": r.time, "iso": r.iso, "action": r.action, "user": r.user_id,
             "session_id": r.session_id, "kind": r.kind, "filename": r.filename,
             "by_entity": r.by_entity, "entity_total": r.entity_total}
        d["has_input_file"] = r.input_file is not None
        d["has_output_file"] = r.output_file is not None
        return d

    def list(self, user=None, action=None, since=None, until=None, q=None, limit=50, offset=0):
        where = []
        if user:
            where.append(T.c.user_id.ilike(_like(user), escape="\\"))
        if action:
            where.append(T.c.action == action)
        if since is not None:
            where.append(T.c.time >= since)
        if until is not None:
            where.append(T.c.time <= until)
        if q:
            pat = _like(q)
            where.append(or_(*[T.c[c].ilike(pat, escape="\\") for c in _TEXT_COLS]))
        with self.db.begin() as conn:
            total = conn.execute(select(func.count()).select_from(T).where(*where)).scalar()
            rows = conn.execute(select(T).where(*where)
                                .order_by(T.c.time.desc(), T.c.id.desc())
                                .limit(limit).offset(offset)).all()
        return {"total": total, "offset": offset, "limit": limit,
                "records": [self._index_row(r) for r in rows]}

    def get(self, rid):
        with self.db.begin() as conn:
            r = conn.execute(select(T).where(T.c.id == (rid or ""))).first()
        if r is None:
            return None
        rec = {"id": r.id, "time": r.time, "iso": r.iso, "action": r.action, "user": r.user_id,
               "session_id": r.session_id, "ip": r.ip, "kind": r.kind,
               "input_text": r.input_text, "output_text": r.output_text,
               "filename": r.filename, "by_entity": r.by_entity, "entity_total": r.entity_total,
               "unresolved": r.unresolved, "changed": r.changed}
        if r.input_file:
            rec["input_file"] = r.input_file
        if r.output_file:
            rec["output_file"] = r.output_file
        return rec

    def file_path(self, rid, role):
        rec = self.get(rid)
        if not rec:
            return None
        name = rec.get("input_file" if role == "input" else "output_file")
        if not name:
            return None
        p = os.path.join(self.root, os.path.basename(rid), name)
        return p if os.path.isfile(p) else None

    def stats(self):
        with self.db.begin() as conn:
            total = conn.execute(select(func.count()).select_from(T)).scalar()
            by_action = dict(conn.execute(select(T.c.action, func.count()).group_by(T.c.action)).all())
            by_user = dict(conn.execute(select(T.c.user_id, func.count()).group_by(T.c.user_id)).all())
        return {"total": total, "by_action": by_action, "by_user": by_user,
                "ttl_days": self.ttl_days}

    # -------------------------------------------------------- retention
    def sweep(self):
        """Delete records older than ttl_days. Returns removed ids."""
        if not self.ttl_days:
            return []
        cutoff = time.time() - self.ttl_days * 86400
        with self.db.begin() as conn:
            gone = [r[0] for r in conn.execute(select(T.c.id).where(T.c.time < cutoff))]
            if gone:
                conn.execute(delete(T).where(T.c.id.in_(gone)))
        for rid in gone:
            shutil.rmtree(os.path.join(self.root, rid), ignore_errors=True)
        return gone

    def maybe_sweep(self, interval=60.0):
        """sweep(), but at most once per `interval` seconds per process."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_sweep < interval:
                return []
            self._last_sweep = now
        return self.sweep()
