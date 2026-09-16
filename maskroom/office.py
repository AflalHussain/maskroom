"""Word/PowerPoint masking (.docx/.pptx): pseudonymize the text inside the
package's XML parts in place, preserving all formatting and non-text parts.

This is the mask-side counterpart to restore.unmask_file's office handling.
Each Word text run (<w:t>) and PowerPoint text run (<a:t>) is masked
independently; a caveat follows from that, identical to restore's: an entity
split across two runs (Word often splits on formatting or spellcheck
boundaries) is seen as two fragments and may be missed. Non-text parts
(styles, media, relationships) are copied byte-for-byte.
"""
import re
import zipfile
from xml.sax.saxutils import escape, unescape

# Package parts that hold user-visible text (same set restore.py edits).
OFFICE_TEXT_PARTS = re.compile(
    r"^(word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml"
    r"|ppt/(slides|notesSlides|comments)/[^/]+\.xml"
    r"|docProps/core\.xml)$"
)
# A Word (<w:t>) or PowerPoint (<a:t>) text run.
_TNODE = re.compile(r"(<(?:w|a):t\b[^>]*>)(.*?)(</(?:w|a):t>)", re.DOTALL)

OFFICE_MASK_EXTS = (".docx", ".pptx")


class OfficeMixin:
    def _mask_office_xml(self, xml, where):
        def repl(m):
            inner = unescape(m.group(2))
            if not inner.strip():
                return m.group(0)
            new, changed = self.pseudonymize_text(inner)
            if changed:
                for f in self._last_findings:
                    self.report.append({"where": where, "method": "detected", **f})
            return m.group(1) + escape(new) + m.group(3)
        return _TNODE.sub(repl, xml)

    def pseudonymize_office(self, input_path, output_path):
        """Mask a .docx/.pptx: replace PII inside its text runs with tokens,
        keeping the document's structure and non-text parts intact."""
        with zipfile.ZipFile(input_path) as zin, \
                zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if OFFICE_TEXT_PARTS.match(item.filename):
                    xml = data.decode("utf-8", errors="replace")
                    data = self._mask_office_xml(xml, item.filename).encode("utf-8")
                zout.writestr(item, data)
        print(f"[Success] Office document saved to: {output_path} "
              f"({len(self.report)} spans detected)")
