"""PDF pipeline (PyMuPDF + Tesseract): OCR for scanned pages, two-pass
document-wide detection with name propagation, spatial redaction that
destroys image pixels."""
import glob
import os
import re

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24 preferred import name
except ImportError:
    import fitz

from . import rules


class PdfMixin:
    @staticmethod
    def _norm_token(token):
        """Normalize a word for matching: strip edge punctuation and a
        possessive suffix, casefold."""
        token = token.strip("\"'’‘`.,;:()[]{}<>!?—–-*")
        token = re.sub(r"['’‘`]s$", "", token)
        return token.casefold()

    @staticmethod
    def _find_tessdata():
        """Locate the Tesseract language-data directory for OCR."""
        prefix = os.environ.get("TESSDATA_PREFIX")
        if prefix and os.path.isdir(prefix):
            return prefix
        hits = glob.glob("/usr/share/tesseract-ocr/*/tessdata") \
            + glob.glob("/usr/local/share/tessdata") \
            + glob.glob("/usr/share/tessdata")
        return hits[0] if hits else None

    def _textpage_for(self, page, ocr_dpi=300):
        """
        Return an OCR textpage for scanned pages (image content but no
        usable text layer), or None when the native text layer suffices.
        """
        if len(page.get_text("text").strip()) >= 30:
            return None
        if not page.get_images(full=True):
            return None
        tessdata = self._find_tessdata()
        if tessdata is None:
            print(f"[Warning] Page {page.number + 1} looks scanned but Tesseract "
                  f"language data was not found — install tesseract-ocr. "
                  f"This page will NOT be redacted.")
            return None
        print(f"[OCR] Page {page.number + 1} has no text layer — running OCR")
        return page.get_textpage_ocr(language="eng", dpi=ocr_dpi, full=True,
                                     tessdata=tessdata)

    def _open_pages(self, input_path):
        """Open a PDF and return (doc, pages, textpages, raw_texts). Keep one
        Page object per page alive for the whole run: an OCR textpage
        weak-references its page and dies with it otherwise."""
        doc = fitz.open(input_path)
        pages = [doc[i] for i in range(len(doc))]
        textpages = [self._textpage_for(page) for page in pages]
        raw_texts = [page.get_text("text", textpage=tp) for page, tp in zip(pages, textpages)]
        return doc, pages, textpages, raw_texts

    def _pdf_snippets(self, page_texts):
        """
        Pass 1: document-wide detection over whitespace-normalized page
        texts. Returns {snippet: entity_type} including propagated name
        words and expanded capitalized runs; appends to self.report.
        """
        snippets = {}  # {text: entity_type}
        for page_no, text in enumerate(page_texts, 1):
            for result in self.analyze_text(text):
                snippet = text[result.start:result.end].strip(" .,;:'\"()")
                if len(snippet) >= 3:
                    snippets.setdefault(snippet, result.entity_type)
                    self.report.append(
                        {"where": f"page {page_no}", "method": "detected",
                         "entity": result.entity_type,
                         "score": round(result.score, 2), "text": snippet})

        # Propagate parts of person names (e.g. a surname on its own).
        name_words = set()
        for snippet, entity_type in list(snippets.items()):
            if entity_type != "PERSON":
                continue
            for word in snippet.split():
                word = word.strip(".,;:'\"()")
                if (len(word) >= 4 and word[0].isupper()
                        and word.lower() not in rules.NAME_PROPAGATION_STOPWORDS):
                    snippets.setdefault(word, "PERSON")
                    name_words.add(word.lower())

        # Expand to capitalized word-runs containing a known name word, so
        # "Murshida Shiyam" is fully removed even if NER only ever saw
        # "Shiyam" as part of another person's name.
        run_re = re.compile(r"\b[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)+")
        for text in page_texts:
            for match in run_re.finditer(text):
                run = match.group(0)
                if any(w.strip(".,;:'\"()").lower() in name_words for w in run.split()):
                    snippets.setdefault(run, "PERSON")

        # Report snippets added by propagation/expansion (no analyzer score).
        directly_detected = {e["text"] for e in self.report if e["method"] == "detected"}
        for snippet, entity_type in snippets.items():
            if snippet not in directly_detected:
                self.report.append(
                    {"where": "document", "method": "propagated",
                     "entity": entity_type, "score": None, "text": snippet})
        return snippets

    def pseudonymize_pdf_text(self, input_path, output_path=None):
        """
        Text mode for LLM input: extract each page's text (native layer or
        OCR), detect PII document-wide exactly as the spatial redactor does,
        and replace every occurrence with a reversible vault token instead
        of a black box. Returns the masked text (Markdown, one section per
        page); writes it to output_path when given.
        """
        doc, pages, textpages, raw_texts = self._open_pages(input_path)
        try:
            snippets = self._pdf_snippets([re.sub(r"\s+", " ", t) for t in raw_texts])
        finally:
            doc.close()

        # Longest snippet first so "Nimal Perera" wins over the propagated
        # "Perera". Whitespace inside a snippet may be a line break in the
        # raw text; a token's own body (TOK_..._HEX) is never re-matched
        # because word characters guard both ends.
        patterns = [
            (re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in snippet.split())
                        + r"(?!\w)"), snippet, entity)
            for snippet, entity in sorted(snippets.items(), key=lambda kv: -len(kv[0]))
        ]
        sections = []
        for page_no, text in enumerate(raw_texts, 1):
            for pat, snippet, entity in patterns:
                text = pat.sub(lambda m, s=snippet, e=entity: self.generate_token(s, e), text)
            sections.append(f"## Page {page_no}\n\n{text.strip()}\n")
        masked = "\n".join(sections)
        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(masked)
            print(f"[Success] Masked text saved to: {output_path} "
                  f"({len(snippets)} distinct values tokenized)")
        return masked

    def redact_spatial_pdf(self, input_path, output_path):
        """
        Redact a PDF in two passes. Pass 1 detects entities over each page's
        whitespace-normalized text (layouts that break names across lines
        defeat NER otherwise). Pass 2 blacks out every occurrence of every
        detected value on every page — so a name NER only caught once is
        still removed wherever else it appears — and scrubs the underlying
        character stream. Individual words of detected person names are
        propagated too, catching partial mentions of the same person.
        Scanned pages are OCR'd and the matching image pixels are destroyed,
        not merely covered.
        """
        doc, pages, textpages, raw_texts = self._open_pages(input_path)
        page_texts = [re.sub(r"\s+", " ", t) for t in raw_texts]
        snippets = self._pdf_snippets(page_texts)

        # Pass 2: redact every occurrence of every detected value. Rects
        # come from matching the snippet's tokens against the page's word
        # sequence — geometry-only checks are unreliable on skewed scans
        # where OCR word boxes can span lines.
        redactions = 0
        for page, tp in zip(pages, textpages):
            word_boxes = [(fitz.Rect(w[:4]), w[4])
                          for w in page.get_text("words", textpage=tp)]
            norm_words = [self._norm_token(w) for _, w in word_boxes]

            def word_match_rects(snippet):
                tokens = [t for t in map(self._norm_token, snippet.split()) if t]
                if not tokens:
                    return []
                rects = []
                for i in range(len(norm_words) - len(tokens) + 1):
                    if norm_words[i:i + len(tokens)] == tokens:
                        rects.extend(r for r, _ in word_boxes[i:i + len(tokens)])
                return rects

            def add_redaction(rect):
                if tp is not None:
                    # OCR boxes sit tighter than the printed glyphs; pad a
                    # little so no character fringes survive.
                    rect = fitz.Rect(rect.x0 - 2, rect.y0 - 1,
                                     rect.x1 + 2, rect.y1 + 1)
                page.add_redact_annot(rect, fill=(0, 0, 0))

            for snippet, entity_type in snippets.items():
                rects = word_match_rects(snippet)
                # Fallback for values embedded inside a larger word (native
                # text) or merged OCR tokens: substring search, restricted
                # to substantial snippets so short false positives cannot
                # chew through unrelated words.
                if not rects and len(snippet) >= 5:
                    rects = page.search_for(snippet, textpage=tp)
                for rect in rects:
                    add_redaction(rect)
                    redactions += 1
                if rects:
                    # Record what was removed so an audit trail exists.
                    self.generate_token(snippet, entity_type)
            # PDF_REDACT_IMAGE_PIXELS erases matching pixels inside scanned
            # images, so the PII is destroyed rather than covered.
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

        doc.save(output_path)
        print(f"[Success] PDF saved to: {output_path} ({redactions} regions redacted)")
