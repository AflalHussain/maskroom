"""Web UI + text/JSON API for the PII masking engine.
Run:  pii_env/bin/python webui/app.py

Pages:   /            file masking studio (Excel/PDF, preview, vault download)
         /staging     LLM staging area: mask text and files, copy them into
                      Claude (or any chat app), paste the reply back to unmask
API:     see docs in README.md "LLM staging" — /api/session, /api/mask,
         /api/unmask, /api/process, /api/download

Engines are built once per option set and share one spaCy load; all analysis
is serialized through the session store's engine lock (spaCy pipelines are
not guaranteed thread-safe). Set MASKROOM_API_KEY to require an X-API-Key
header on every /api route.
"""
import base64
import hmac
import json
import os
import sys
import threading
import time
import uuid

import openpyxl
from flask import Flask, jsonify, request, send_file, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from maskroom import FinancialPrivacyEngine, SessionStore, build_nlp_engine
from maskroom import rules
from maskroom.locale import DEFAULT_LOCALE, available_locales
from maskroom.restore import SUPPORTED_EXTS as RESTORE_EXTS, unmask_file

try:
    import pymupdf as fitz
except ImportError:
    import fitz

BASE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(BASE, "runs")
os.makedirs(RUNS, exist_ok=True)

MAX_PREVIEW_ROWS = 80
MAX_PREVIEW_COLS = 14
MAX_PREVIEW_PAGES = 8
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "200000"))
MAX_MARKDOWN_ROWS = 2000
API_KEY = os.environ.get("MASKROOM_API_KEY")
TTL_HOURS = float(os.environ.get("SESSION_TTL_HOURS", "24"))

app = Flask(__name__, static_folder="static")
sessions = SessionStore(os.path.join(RUNS, "sessions"), ttl=TTL_HOURS * 3600 or None)

# ---------------------------------------------------------------- engines
_nlp_engines = {}
_engines = {}
_build_lock = threading.Lock()


def _bool(v, default=True):
    if v is None or v == "":
        return default
    return str(v).lower() not in ("false", "0", "no", "off")


def engine_options(opts):
    """Normalized engine options from a form or JSON mapping."""
    entities = opts.get("entities") or ""
    if isinstance(entities, str):
        entities = [e.strip() for e in entities.split(",") if e.strip()]
    return {
        "min_score": float(opts.get("min_score") or 0.6),
        "entities": tuple(entities) or None,
        "nlp_model": opts.get("nlp_model") or None,
        "dates": opts.get("dates") or "birth",
        "locations": opts.get("locations") or "address",
        "locale": opts.get("locale") or DEFAULT_LOCALE,
        "column_rules": _bool(opts.get("column_rules"), True),
    }


def get_engine(options):
    """A warm engine for this option set. Engines share the spaCy model
    for their nlp_model, so a new option combination costs only recognizer
    setup, not a model load."""
    key = tuple(sorted(options.items()))
    eng = _engines.get(key)
    if eng is not None:
        return eng
    with _build_lock:
        eng = _engines.get(key)
        if eng is None:
            model = options["nlp_model"]
            nlp = _nlp_engines.get(model)
            if nlp is None:
                nlp = _nlp_engines[model] = build_nlp_engine(model)
            kw = dict(options, entities=list(options["entities"]) if options["entities"] else None)
            eng = _engines[key] = FinancialPrivacyEngine(nlp_engine=nlp, **kw)
    return eng


# ------------------------------------------------------------------ auth
@app.before_request
def check_api_key():
    if not API_KEY or not request.path.startswith("/api/") or request.path == "/api/config":
        return None
    given = request.headers.get("X-API-Key") or request.args.get("key") or ""
    if not hmac.compare_digest(given, API_KEY):
        return jsonify({"error": "Missing or invalid API key (X-API-Key header)."}), 401
    return None


@app.after_request
def no_store(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ----------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE, "static"), "index.html")


@app.get("/staging")
def staging():
    return send_from_directory(os.path.join(BASE, "static"), "staging.html")


@app.get("/api/config")
def config():
    return jsonify({"auth_required": bool(API_KEY), "default_locale": DEFAULT_LOCALE,
                    "locales": available_locales(), "session_ttl_hours": TTL_HOURS,
                    "preamble": rules.LLM_TOKEN_PREAMBLE, "max_text_chars": MAX_TEXT_CHARS,
                    "restore_exts": list(RESTORE_EXTS)})


@app.get("/api/locales")
def locales():
    return jsonify({"default": DEFAULT_LOCALE, "locales": available_locales()})


# -------------------------------------------------------------- sessions
def _session_or_error(session_id, create=False):
    """(session, error_response). Sweeps idle sessions as a side effect."""
    sessions.sweep()
    if not session_id:
        if create:
            return sessions.create(), None
        return None, (jsonify({"error": "session_id is required."}), 400)
    sess = sessions.get(session_id)
    if sess is None:
        return None, (jsonify({"error": "Unknown or expired session."}), 404)
    return sess, None


@app.post("/api/session")
def session_create():
    sessions.sweep()
    return jsonify(sessions.create().info())


@app.get("/api/session/<session_id>")
def session_info(session_id):
    sess, err = _session_or_error(session_id)
    return err or jsonify(sess.info())


@app.delete("/api/session/<session_id>")
def session_delete(session_id):
    return jsonify({"deleted": sessions.delete(session_id)})


@app.get("/api/session/<session_id>/vault")
def session_vault(session_id):
    sess, err = _session_or_error(session_id)
    if err:
        return err
    with sess.lock:
        sess.save()
    return send_file(sess.vault_path, as_attachment=True,
                     download_name=f"vault-{session_id}.json")


# ------------------------------------------------------------------ text
def _json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"error": "Send a JSON object body."}), 400)
    text = data.get("text")
    if not isinstance(text, str):
        return None, (jsonify({"error": '"text" (string) is required.'}), 400)
    if len(text) > MAX_TEXT_CHARS:
        return None, (jsonify({"error": f"Text is longer than {MAX_TEXT_CHARS} characters."}), 413)
    return data, None


@app.post("/api/mask")
def mask_text():
    """{text, session_id?, min_score?, dates?, locations?, locale?, entities?,
    nlp_model?} -> {session_id, masked, changed, findings, vault_entries,
    preamble, elapsed_s}. Without a session_id a new session is created."""
    data, err = _json_body()
    if err:
        return err
    sess, err = _session_or_error(data.get("session_id"), create=True)
    if err:
        return err
    try:
        engine = get_engine(engine_options(data))
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    t0 = time.time()
    with sessions.bind(engine, sess):
        masked, changed = engine.pseudonymize_text(data["text"])
        findings = list(engine._last_findings)
        reverse = {v: k for k, v in engine.vault.items()}
        for f in findings:
            f["token"] = reverse.get(f["text"].strip())
        entries = len(engine.vault)
    return jsonify({
        "session_id": sess.id, "masked": masked, "changed": changed,
        "findings": findings, "vault_entries": entries,
        "preamble": rules.LLM_TOKEN_PREAMBLE, "elapsed_s": round(time.time() - t0, 2),
    })


@app.post("/api/unmask")
def unmask_text():
    """{text, session_id} -> {text, restored, fuzzy, unresolved}."""
    data, err = _json_body()
    if err:
        return err
    sess, err = _session_or_error(data.get("session_id"))
    if err:
        return err
    engine = get_engine(engine_options({}))
    with sessions.bind(engine, sess):
        restored, report = engine.unmask_text(data["text"])
    return jsonify({"session_id": sess.id, "text": restored, **report})


# ----------------------------------------------------------------- files
def excel_preview(path, limit_sheets=6):
    """Sheets as row-lists of strings, capped for the browser."""
    wb = openpyxl.load_workbook(path, read_only=True)
    sheets = []
    for name in wb.sheetnames[:limit_sheets]:
        ws = wb[name]
        rows = []
        for r, row in enumerate(ws.iter_rows(values_only=True)):
            if r >= MAX_PREVIEW_ROWS:
                break
            rows.append(["" if v is None else str(v) for v in row[:MAX_PREVIEW_COLS]])
        sheets.append({"name": name, "rows": rows,
                       "truncated": ws.max_row > MAX_PREVIEW_ROWS
                                    or (ws.max_column or 0) > MAX_PREVIEW_COLS})
    wb.close()
    return sheets


def excel_markdown(path, out_path):
    """Render a (masked) workbook as Markdown tables, one section per sheet,
    so it can be pasted into a chat without needing code execution."""
    wb = openpyxl.load_workbook(path, read_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"## Sheet: {ws.title}\n")
        rows = []
        for r, row in enumerate(ws.iter_rows(values_only=True)):
            if r >= MAX_MARKDOWN_ROWS:
                parts.append(f"\n_(truncated after {MAX_MARKDOWN_ROWS} rows)_\n")
                break
            cells = ["" if v is None else str(v).replace("|", "\\|").replace("\n", " ")
                     for v in row]
            if any(cells):
                rows.append("| " + " | ".join(cells) + " |")
        if rows:
            width = rows[0].count("|") - 1
            parts.append(rows[0])
            parts.append("|" + "---|" * width)
            parts.extend(rows[1:])
        parts.append("")
    wb.close()
    text = "\n".join(parts)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def pdf_preview(path, dpi=90):
    doc = fitz.open(path)
    pages = []
    for i in range(min(len(doc), MAX_PREVIEW_PAGES)):
        png = doc[i].get_pixmap(dpi=dpi).tobytes("png")
        pages.append(base64.b64encode(png).decode())
    n = len(doc)
    doc.close()
    return {"pages": pages, "total": n}


@app.post("/api/process")
def process():
    """Multipart: file (+ options). Optional session_id joins the run to a
    session vault; pdf_mode=text|redact (default redact); restore=true with
    either a vault file or a session_id reverses a masked workbook;
    preview=false skips the before/after previews."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded."}), 400
    name = os.path.basename(f.filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in (".xlsx", ".xlsm", ".pdf"):
        return jsonify({"error": f"Unsupported file type {ext!r} — use .xlsx, .xlsm or .pdf."}), 400

    opts = request.form
    restore = opts.get("restore") == "true"
    pdf_text = ext == ".pdf" and opts.get("pdf_mode", "redact") == "text"
    want_preview = _bool(opts.get("preview"), True)
    session_id = opts.get("session_id") or None
    sess = None
    if session_id or opts.get("session") == "true":
        sess, err = _session_or_error(session_id, create=not restore)
        if err:
            return err
    try:
        engine = get_engine(engine_options(opts))
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    run_id = uuid.uuid4().hex[:12]
    run_dir = os.path.join(RUNS, run_id)
    os.makedirs(run_dir)
    in_path = os.path.join(run_dir, "input" + ext)
    f.save(in_path)

    if restore:
        out_name = "restored" + ext
    elif pdf_text:
        out_name = "masked.md"
    else:
        out_name = ("masked" if ext != ".pdf" else "redacted") + ext
    out_path = os.path.join(run_dir, out_name)
    text_name = None
    t0 = time.time()

    # A run always has a scratch session so per-run state (vault/report) is
    # isolated; a real session makes the vault shared across the conversation.
    scratch = sess or sessions.create()
    try:
        with sessions.bind(engine, scratch):
            if restore:
                if ext == ".pdf":
                    return jsonify({"error": "PDF redaction is permanent — restore only works for Excel."}), 400
                vf = request.files.get("vault")
                if vf:
                    vpath = os.path.join(run_dir, "vault_in.json")
                    vf.save(vpath)
                    engine.load_vault(vpath)
                elif not sess:
                    return jsonify({"error": "Restore needs the vault JSON saved when the "
                                             "file was masked, or the session it was masked in."}), 400
                engine.depseudonymize_excel(in_path, out_path)
            elif pdf_text:
                engine.pseudonymize_pdf_text(in_path, out_path)
            elif ext == ".pdf":
                engine.redact_spatial_pdf(in_path, out_path)
            else:
                engine.pseudonymize_excel(in_path, out_path)
                text_name = "masked.md"
                excel_markdown(out_path, os.path.join(run_dir, text_name))
            findings = list(engine.report)
            vault_entries = len(engine.vault)
            if not restore:
                engine.save_vault(os.path.join(run_dir, "vault.json"))
    except Exception as e:
        return jsonify({"error": f"Processing failed: {e}"}), 500
    finally:
        if scratch is not sess:
            sessions.delete(scratch.id)
    elapsed = time.time() - t0

    by_entity = {}
    for e in findings:
        by_entity[e["entity"]] = by_entity.get(e["entity"], 0) + 1

    resp = {
        "run_id": run_id,
        "session_id": sess.id if sess else None,
        "filename": name,
        "kind": "pdf" if ext == ".pdf" else "excel",
        "mode": "restore" if restore else ("text" if pdf_text else
                                          ("redact" if ext == ".pdf" else "pseudonymize")),
        "elapsed_s": round(elapsed, 1),
        "vault_entries": vault_entries,
        "findings": findings,
        "by_entity": dict(sorted(by_entity.items(), key=lambda kv: -by_entity[kv[0]])),
        "downloads": {"output": out_name,
                      "vault": "vault.json" if not restore else None,
                      "text": out_name if pdf_text else text_name},
    }
    if want_preview:
        if ext == ".pdf":
            resp["before"] = pdf_preview(in_path)
            resp["after"] = pdf_preview(out_path) if not pdf_text else None
        else:
            resp["before"] = excel_preview(in_path)
            resp["after"] = excel_preview(out_path)

    with open(os.path.join(run_dir, "result.json"), "w") as fh:
        json.dump({k: v for k, v in resp.items() if k not in ("before", "after")}, fh)
    return jsonify(resp)


@app.post("/api/unmask-file")
def unmask_file_route():
    """Multipart: file + session_id. Restores tokens inside a file the LLM
    produced (Excel, Word, PowerPoint, Markdown/CSV/text) using the session
    vault. Returns the report and a download name; never changes the vault."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded."}), 400
    name = os.path.basename(f.filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in RESTORE_EXTS:
        return jsonify({"error": f"Unsupported file type {ext!r} — supported: "
                                 f"{', '.join(RESTORE_EXTS)}."}), 415
    sess, err = _session_or_error(request.form.get("session_id"))
    if err:
        return err
    engine = get_engine(engine_options({}))
    run_id = uuid.uuid4().hex[:12]
    run_dir = os.path.join(RUNS, run_id)
    os.makedirs(run_dir)
    in_path = os.path.join(run_dir, "input" + ext)
    f.save(in_path)
    out_name = "restored" + ext
    t0 = time.time()
    try:
        with sessions.bind(engine, sess):
            report = unmask_file(engine, in_path, os.path.join(run_dir, out_name))
    except Exception as e:
        return jsonify({"error": f"Restore failed: {e}"}), 500
    resp = {"run_id": run_id, "session_id": sess.id, "filename": name,
            "elapsed_s": round(time.time() - t0, 2), **report,
            "downloads": {"output": out_name}}
    with open(os.path.join(run_dir, "result.json"), "w") as fh:
        json.dump(resp, fh)
    return jsonify(resp)


@app.get("/api/download/<run_id>/<path:fname>")
def download(run_id, fname):
    safe = os.path.basename(fname)
    path = os.path.join(RUNS, os.path.basename(run_id), safe)
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404
    return send_file(path, as_attachment=True, download_name=safe)


@app.get("/api/text/<run_id>/<path:fname>")
def run_text(run_id, fname):
    """A run's text output as JSON (for copy-to-clipboard in the UI)."""
    safe = os.path.basename(fname)
    path = os.path.join(RUNS, os.path.basename(run_id), safe)
    if not os.path.isfile(path) or not safe.endswith((".md", ".txt")):
        return jsonify({"error": "File not found"}), 404
    with open(path, encoding="utf-8") as fh:
        return jsonify({"text": fh.read()})


if __name__ == "__main__":
    # Local default: loopback only. Containers/PaaS set HOST=0.0.0.0 and PORT.
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "5170")), debug=False)
