"""Audit trail of every masking operation, for the admin dashboard.

Each record keeps the original input and the masked output (the admin's
explicit requirement), attributed to the org user id the extension sends
(`X-Maskroom-User`), with a retention window so raw PII is not kept forever.

WARNING: this store contains real PII (the original inputs). It must be
admin-only (see MASKROOM_ADMIN_KEY) and on protected storage. Retention
(AUDIT_TTL_DAYS) bounds how long originals live; it is not encryption.

Layout under <root>/:
  index.jsonl            one compact line per record (metadata, for listing)
  <id>/record.json       the full record (input/output text + metadata)
  <id>/input.<ext>       stored original file (file operations only)
  <id>/output.<ext>      stored masked/restored file (file operations only)
"""
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone

INDEX_FIELDS = ("id", "time", "iso", "action", "user", "session_id", "kind",
                "filename", "by_entity", "entity_total")


class AuditLog:
    def __init__(self, root, ttl_days=90):
        self.root = root
        self.ttl_days = float(ttl_days or 0)
        self.index_path = os.path.join(root, "index.jsonl")
        self._lock = threading.Lock()
        os.makedirs(root, exist_ok=True)

    # ----------------------------------------------------------- writing
    def record(self, action, user=None, session_id=None, ip="", kind="text",
               input_text=None, output_text=None, input_file=None, output_file=None,
               filename=None, by_entity=None, entity_total=0, unresolved=None, changed=None):
        rid = uuid.uuid4().hex[:16]
        ts = time.time()
        rec_dir = os.path.join(self.root, rid)
        os.makedirs(rec_dir, exist_ok=True)
        stored = {}
        for role, src in (("input", input_file), ("output", output_file)):
            if src and os.path.isfile(src):
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
        with open(os.path.join(rec_dir, "record.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        idx = {k: rec[k] for k in INDEX_FIELDS}
        idx["has_input_file"] = "input_file" in stored
        idx["has_output_file"] = "output_file" in stored
        with self._lock:
            with open(self.index_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(idx, ensure_ascii=False) + "\n")
        return rec

    # ----------------------------------------------------------- reading
    def _index(self):
        if not os.path.isfile(self.index_path):
            return []
        rows = []
        with open(self.index_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return rows

    def list(self, user=None, action=None, since=None, until=None, q=None, limit=50, offset=0):
        rows = self._index()
        rows.reverse()  # newest first

        def keep(r):
            if user and user.lower() not in str(r.get("user", "")).lower():
                return False
            if action and r.get("action") != action:
                return False
            if since is not None and r.get("time", 0) < since:
                return False
            if until is not None and r.get("time", 0) > until:
                return False
            if q:
                hay = " ".join(str(r.get(k, "")) for k in
                               ("user", "filename", "action", "session_id", "kind")).lower()
                if q.lower() not in hay:
                    return False
            return True

        filtered = [r for r in rows if keep(r)]
        return {"total": len(filtered), "offset": offset, "limit": limit,
                "records": filtered[offset:offset + limit]}

    def get(self, rid):
        path = os.path.join(self.root, os.path.basename(rid or ""), "record.json")
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

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
        rows = self._index()
        by_action, by_user = {}, {}
        for r in rows:
            by_action[r.get("action")] = by_action.get(r.get("action"), 0) + 1
            by_user[r.get("user")] = by_user.get(r.get("user"), 0) + 1
        return {"total": len(rows), "by_action": by_action, "by_user": by_user,
                "ttl_days": self.ttl_days}

    # -------------------------------------------------------- retention
    def sweep(self):
        """Delete records older than ttl_days. Returns removed ids."""
        if not self.ttl_days:
            return []
        cutoff = time.time() - self.ttl_days * 86400
        rows = self._index()
        keep_rows, gone = [], []
        for r in rows:
            if r.get("time", 0) < cutoff:
                shutil.rmtree(os.path.join(self.root, r["id"]), ignore_errors=True)
                gone.append(r["id"])
            else:
                keep_rows.append(r)
        if gone:
            with self._lock:
                with open(self.index_path, "w", encoding="utf-8") as f:
                    for r in keep_rows:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return gone
