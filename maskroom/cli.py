"""Command-line entry point (`maskroom` / `python -m maskroom`)."""
import argparse
import os

from .engine import FinancialPrivacyEngine


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="maskroom",
        description="Pseudonymize Excel workbooks / redact PDFs with Presidio.",
    )
    parser.add_argument("input", help="Input .xlsx or .pdf file")
    parser.add_argument("output", nargs="?", help="Output path (default: <input>_masked.<ext>)")
    parser.add_argument("--vault", help="Vault JSON file: written after masking, "
                        "read when using --restore")
    parser.add_argument("--restore", action="store_true",
                        help="Reverse a masked .xlsx using the vault given via --vault")
    parser.add_argument("--min-score", type=float, default=0.6,
                        help="Minimum detection confidence (default: 0.6)")
    parser.add_argument("--entities", nargs="*", default=None,
                        help="Restrict detection to these entity types (default: all)")
    parser.add_argument("--nlp-model", default=None,
                        help="spaCy model for NER, e.g. en_core_web_trf "
                             "(default: Presidio's en_core_web_lg)")
    parser.add_argument("--dates", choices=["birth", "all", "none"], default="birth",
                        help="Date masking policy: 'birth' masks only birth-linked "
                             "dates (default), 'all' masks every date, 'none' keeps all")
    parser.add_argument("--no-column-rules", action="store_true",
                        help="Disable header-based column rules (mask whole 'Name'/"
                             "'Address'/... columns without per-cell detection)")
    args = parser.parse_args(argv)

    root, ext = os.path.splitext(args.input)
    output = args.output or f"{root}_masked{ext}"

    engine = FinancialPrivacyEngine(min_score=args.min_score, entities=args.entities,
                                    nlp_model=args.nlp_model, dates=args.dates,
                                    column_rules=not args.no_column_rules)

    ext = ext.lower()
    if args.restore:
        if not args.vault:
            parser.error("--restore requires --vault <mapping file>")
        if ext not in (".xlsx", ".xlsm"):
            parser.error("--restore only supports Excel files (PDF redaction is permanent)")
        engine.load_vault(args.vault)
        engine.depseudonymize_excel(args.input, args.output or f"{root}_restored{ext}")
        return

    if ext in (".xlsx", ".xlsm"):
        engine.pseudonymize_excel(args.input, output)
    elif ext == ".pdf":
        engine.redact_spatial_pdf(args.input, output)
    else:
        parser.error(f"Unsupported file type: {ext} (expected .xlsx, .xlsm, or .pdf)")

    if args.vault:
        engine.save_vault(args.vault)


if __name__ == "__main__":
    main()
