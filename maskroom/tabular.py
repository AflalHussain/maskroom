"""CSV/TSV and plain-text/JSON masking.

CSV and TSV are masked "properly": the rows are loaded into an in-memory
workbook and run through the same engine as Excel (`_pseudonymize_workbook`),
so they get header/column rules, value-profile detection and per-column
context — not a naive free-text pass that would miss bare identifiers.

Plain text and JSON are masked as free text (one pass of pseudonymize_text),
mirroring how restore handles them.
"""
import csv

import openpyxl


class TabularMixin:
    @staticmethod
    def _delimiter_for(path, override=None):
        if override:
            return override
        return "\t" if path.lower().endswith((".tsv", ".tab")) else ","

    def pseudonymize_csv(self, input_path, output_path, delimiter=None):
        """Mask a CSV/TSV column-aware and write it back with the same
        delimiter. Values are text (CSV has no types), so detection runs
        through the column-rule / value-profile / context path."""
        delimiter = self._delimiter_for(input_path, delimiter)
        with open(input_path, newline="", encoding="utf-8-sig", errors="replace") as f:
            rows = list(csv.reader(f, delimiter=delimiter))
        lengths = [len(r) for r in rows]

        wb = openpyxl.Workbook()
        ws = wb.active
        for r in rows:
            ws.append(["" if c is None else str(c) for c in r])
        self._pseudonymize_workbook(wb)

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=delimiter)
            for i, row in enumerate(ws.iter_rows()):
                vals = ["" if c.value is None else c.value for c in row]
                if i < len(lengths):
                    vals = vals[:lengths[i]]  # keep each row's original width
                w.writerow(vals)
        print(f"[Success] {'TSV' if delimiter == chr(9) else 'CSV'} saved to: {output_path}")

    def pseudonymize_text_file(self, input_path, output_path):
        """Mask a plain-text / JSON file as free text."""
        with open(input_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        masked, _ = self.pseudonymize_text(text)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(masked)
        print(f"[Success] Text saved to: {output_path}")
