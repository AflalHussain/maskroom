"""Web UI for the PII masking engine. Run:  pii_env/bin/python webui/app.py"""
import base64
import io
import json
import os
import sys
import time
import uuid

import openpyxl
from flask import Flask, jsonify, request, send_file, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from maskroom import FinancialPrivacyEngine
from maskroom.locale import DEFAULT_LOCALE, available_locales

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

app = Flask(__name__, static_folder="static")


@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE, "static"), "index.html")


@app.get("/api/locales")
def locales():
    return jsonify({"default": DEFAULT_LOCALE, "locales": available_locales()})


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
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded."}), 400
    name = os.path.basename(f.filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in (".xlsx", ".xlsm", ".pdf"):
        return jsonify({"error": f"Unsupported file type {ext!r} — use .xlsx, .xlsm or .pdf."}), 400

    run_id = uuid.uuid4().hex[:12]
    run_dir = os.path.join(RUNS, run_id)
    os.makedirs(run_dir)
    in_path = os.path.join(run_dir, "input" + ext)
    f.save(in_path)

    opts = request.form
    restore = opts.get("restore") == "true"
    try:
        engine = FinancialPrivacyEngine(
            min_score=float(opts.get("min_score", 0.6)),
            entities=[e.strip() for e in opts.get("entities", "").split(",") if e.strip()] or None,
            nlp_model=opts.get("nlp_model") or None,
            dates=opts.get("dates", "birth"),
            locations=opts.get("locations", "address"),
            locale=opts.get("locale") or DEFAULT_LOCALE,
            column_rules=opts.get("column_rules", "true") != "false",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    out_name = ("restored" if restore else "masked") + ext
    out_path = os.path.join(run_dir, out_name)
    t0 = time.time()
    try:
        if restore:
            if ext == ".pdf":
                return jsonify({"error": "PDF redaction is permanent — restore only works for Excel."}), 400
            vf = request.files.get("vault")
            if not vf:
                return jsonify({"error": "Restore needs the vault JSON saved when the file was masked."}), 400
            vpath = os.path.join(run_dir, "vault_in.json")
            vf.save(vpath)
            engine.load_vault(vpath)
            engine.depseudonymize_excel(in_path, out_path)
        elif ext == ".pdf":
            engine.redact_spatial_pdf(in_path, out_path)
        else:
            engine.pseudonymize_excel(in_path, out_path)
    except Exception as e:
        return jsonify({"error": f"Processing failed: {e}"}), 500
    elapsed = time.time() - t0

    vault_path = os.path.join(run_dir, "vault.json")
    if not restore:
        engine.save_vault(vault_path)

    findings = engine.report
    by_entity = {}
    for e in findings:
        by_entity[e["entity"]] = by_entity.get(e["entity"], 0) + 1

    resp = {
        "run_id": run_id,
        "filename": name,
        "kind": "pdf" if ext == ".pdf" else "excel",
        "mode": "restore" if restore else ("redact" if ext == ".pdf" else "pseudonymize"),
        "elapsed_s": round(elapsed, 1),
        "vault_entries": len(engine.vault),
        "findings": findings,
        "by_entity": dict(sorted(by_entity.items(), key=lambda kv: -kv[1])),
        "downloads": {"output": out_name,
                      "vault": "vault.json" if not restore else None},
    }
    if ext == ".pdf":
        resp["before"] = pdf_preview(in_path)
        resp["after"] = pdf_preview(out_path)
    else:
        resp["before"] = excel_preview(in_path)
        resp["after"] = excel_preview(out_path)

    with open(os.path.join(run_dir, "result.json"), "w") as fh:
        json.dump({k: v for k, v in resp.items() if k not in ("before", "after")}, fh)
    return jsonify(resp)


@app.get("/api/download/<run_id>/<path:fname>")
def download(run_id, fname):
    safe = os.path.basename(fname)
    path = os.path.join(RUNS, os.path.basename(run_id), safe)
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404
    return send_file(path, as_attachment=True, download_name=safe)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5170, debug=False)
