"""File-format masking pipeline: dispatch a file to the right format walker,
below the web and CLI layers.

The forward-masking counterpart to ``restore.unmask_file``. Like it, this takes
an already-bound engine and explicit paths, so it carries no web/session
coupling: callers own session binding, output naming and vault persistence.
Forward masking only — reversing a masked file is ``restore.unmask_file``.
"""
import os
from dataclasses import dataclass, field

EXCEL_EXTS = (".xlsx", ".xlsm")
PDF_EXTS = (".pdf",)
OFFICE_EXTS = (".docx", ".pptx")
TABULAR_EXTS = (".csv", ".tsv")
TEXT_EXTS = (".txt", ".json")
# The single source of truth for "what forward masking can handle"; the web
# route and the CLI both validate against this.
SUPPORTED_EXTS = EXCEL_EXTS + PDF_EXTS + OFFICE_EXTS + TABULAR_EXTS + TEXT_EXTS


@dataclass
class FileMaskResult:
    """What the caller needs to shape a response, with no re-derivation.

    kind:     excel | pdf | office | tabular | text
    mode:     pseudonymize | redact | text
    findings: per-run detection details (from engine.report).
    """
    kind: str
    mode: str
    findings: list = field(default_factory=list)


def mask_file(engine, in_path, out_path, *, pdf_text=False):
    """Mask the PII in ``in_path``, writing the result to ``out_path``.

    engine:   an already-bound FinancialPrivacyEngine — the caller owns its
              session/vault lifetime, exactly like ``restore.unmask_file``.
    pdf_text: for PDFs, extract and mask the text (Markdown output) instead of
              the default spatial redaction. Ignored for non-PDF inputs.

    Returns a FileMaskResult. Raises ValueError for an unsupported extension.
    """
    ext = os.path.splitext(in_path)[1].lower()
    if ext in PDF_EXTS and pdf_text:
        engine.pseudonymize_pdf_text(in_path, out_path)
        kind, mode = "pdf", "text"
    elif ext in PDF_EXTS:
        engine.redact_spatial_pdf(in_path, out_path)
        kind, mode = "pdf", "redact"
    elif ext in OFFICE_EXTS:
        engine.pseudonymize_office(in_path, out_path)
        kind, mode = "office", "pseudonymize"
    elif ext in TABULAR_EXTS:
        engine.pseudonymize_csv(in_path, out_path)
        kind, mode = "tabular", "pseudonymize"
    elif ext in TEXT_EXTS:
        engine.pseudonymize_text_file(in_path, out_path)
        kind, mode = "text", "pseudonymize"
    elif ext in EXCEL_EXTS:
        engine.pseudonymize_excel(in_path, out_path)
        kind, mode = "excel", "pseudonymize"
    else:
        raise ValueError(f"unsupported file type {ext!r}; supported: {', '.join(SUPPORTED_EXTS)}")
    return FileMaskResult(kind=kind, mode=mode, findings=list(engine.report))
