# Technologies & Techniques

Companion to [README.md](README.md) (usage) and [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md)
(architecture trade-offs). This document catalogs every technology in the stack and every
detection/masking technique the engine uses, with enough detail to understand, debug, or
replace each one.

---

## Part I — Technology stack

### Microsoft Presidio (`presidio-analyzer`, `presidio-anonymizer`)

The detection and replacement framework everything is built on.

- **`AnalyzerEngine`** orchestrates a *registry of recognizers* over input text. Each
  recognizer returns `RecognizerResult(entity_type, start, end, score)` spans; the engine
  merges results from all recognizers, applies the context enhancer, and filters by
  `score_threshold`. We use it with its default recognizer set *plus* six custom recognizers
  (Part II) and interrogate/modify its registry at startup (e.g. removing the stock phone
  recognizer to re-register it with Sri Lankan region support).
- **`PatternRecognizer` / `Pattern`** — declarative regex recognizers with a per-pattern
  base score and optional `context` word list. All custom format detection (NIC, passports,
  accounts, loose email, role-titled names) uses these; no custom recognizer classes were
  needed.
- **`LemmaContextAwareEnhancer`** (default enhancer) — boosts a match's score by ~0.35 when
  a recognizer's context word appears near the match (or in the externally supplied context,
  see "column-header context" in Part II). This is the mechanism that lets us register
  ambiguous patterns *below* the masking threshold so they only fire in the right setting.
- **`AnonymizerEngine`** — applies operators to analyzer spans, resolving overlapping
  matches deterministically before replacement. We use the `custom` operator with a lambda
  per entity type, closing over our token generator — so span replacement, overlap conflict
  resolution, and offset bookkeeping are Presidio's problem, not ours.
- Version in use: `presidio-analyzer 2.2.364`.

### spaCy (`en_core_web_lg`, `en_core_web_trf`) + spacy-transformers + PyTorch

Statistical NER — the only component that can find *unformatted* PII (names, places).

- **`en_core_web_lg`** (default): CNN-based pipeline with static word vectors. Fast
  (~ms/sentence), weaker on non-Western and unconventionally formatted names. Presidio's
  `SpacyNlpEngine` maps spaCy labels to Presidio entities (PERSON→PERSON, GPE/LOC→LOCATION,
  NORP→NRP, DATE→DATE_TIME).
- **`en_core_web_trf`** (optional): RoBERTa-base transformer pipeline, selected via
  `NlpEngineProvider(nlp_configuration=...)` and exposed as `--nlp-model en_core_web_trf`.
  Runs on CPU via a CPU-only PyTorch build (installed from the `download.pytorch.org/whl/cpu`
  index to avoid the multi-GB CUDA distribution). Benchmarked: clearly better name recall
  (6/6 vs 3/6 hard-name micro-test; 56/60 vs 45/60 on real all-caps records) at 4–35×
  runtime. Also *noisier* in one specific way — it will tag bare numbers as PERSON — which
  is why the letter-less-NER filter exists (Part II).
- Both models are interchangeable at runtime; nothing else in the pipeline changes.

### `phonenumbers` (via Presidio's `PhoneRecognizer`)

Google's libphonenumber port: parses and *validates* phone numbers per region rather than
pattern-matching them. We re-register `PhoneRecognizer` with
`supported_regions=("LK", "US", "GB", "IN")` so `+94 71 234 5678` and `0112345678` validate,
and extend its context vocabulary with landline words (`landline, fixed, line, tel, fax…`).
Validation is why fictional `555-01xx` numbers and short hotlines (`1919`, `119`) are
correctly *not* masked.

### openpyxl

Excel I/O. Used for cell-level read/write (`iter_rows`, `iter_cols`), preserving workbook
structure while replacing only cell values. Notable behaviors we handle:

- Numeric cells arrive as `int`/`float` (stringified for analysis; `bool` excluded since
  `bool` subclasses `int`), date cells as `datetime` (handled in rule columns via `str()`).
- CSV-imported "numbers" are often *text* cells — the reason salary strings once masked as
  dates and why the date policy exists.
- `read_only=True` mode is used for previews in the web UI (streams rows without loading
  the full workbook).

### PyMuPDF (`pymupdf` / `fitz`)

All PDF work: text extraction, coordinate search, redaction, rasterization, OCR bridge.

- `page.get_text("text"/"words")` — page text and word boxes `(x0,y0,x1,y1,word,...)`.
- `page.search_for(needle, textpage=...)` — maps a string to physical rects (used only as
  a fallback; substring semantics burned us, see Part II word-sequence matching).
- `page.add_redact_annot(rect, fill=(0,0,0))` + `page.apply_redactions(images=
  PDF_REDACT_IMAGE_PIXELS)` — removes the character bytes under the rect and *erases the
  matching pixels inside embedded images*, so scanned PII is destroyed, not covered.
- `page.get_textpage_ocr(language, dpi, full=True, tessdata=...)` — renders the page and
  runs Tesseract, returning a textpage whose coordinates map back to page space; the same
  textpage serves text extraction, word boxes, and search.
- Gotcha encoded in the code: a textpage weak-references its `Page`; page objects must be
  materialized once (`pages = [doc[i] for i in ...]`) and kept alive for the whole run.

### Tesseract OCR (system binary, v4.1.1)

Invoked through PyMuPDF for image-only pages (detected as: <30 chars of native text but
images present). English `tessdata` auto-discovered from `TESSDATA_PREFIX` or standard
paths. Operational characteristics that shaped the design: word boxes sit tighter than
glyphs (we pad redaction rects ±2pt), boxes can span lines on skewed scans (why geometric
validation was abandoned), and character confusions (`.lk`→`.Ik`) defeat validating
recognizers (why the loose email pattern exists).

### hashlib (SHA-256) — token generation

Tokens are `TOK_<ENTITY>_<SHA256(value+salt)[:8].upper()>`, prefix extended on collision
with a different value. Deterministic by design (referential integrity across sheets and
runs). The salt comes from `PII_TOKEN_SALT`. Security posture and the recommended upgrade
to HMAC-SHA256 with managed keys are covered in TECHNICAL_DESIGN §5.

### Flask — web UI backend

Localhost-only single-file app (`webui/app.py`): multipart upload → engine invocation →
JSON response (stats, findings, base64 page renders / capped sheet previews) → per-run
artifact directory (`webui/runs/<id>/`) served for download. The engine is imported
directly — no subprocess, no queue; a `trf` run blocks its request (fine for a local tool).

### Vanilla HTML/CSS/JS — web UI frontend

No framework, no build step. Design-token CSS (`:root` variables + `html[data-theme=light]`
override block) for the dark/light themes, Google Fonts (Archivo + IBM Plex Mono),
`fetch` + FormData for the API. Previews are capped server-side (80 rows × 14 cols,
8 PDF pages) to keep the DOM small.

---

## Part II — Techniques

### 1. Layered detection (patterns → checksums → context → NER → lexical heuristics)

No single detector is trusted for everything. The layers, and what each uniquely covers:

| Technique | Implementation | Uniquely catches |
|---|---|---|
| Checksum validation | Luhn (cards), libphonenumber (phones) | rejects format lookalikes; near-zero FP |
| Anchored regex | old NIC `\d{9}[VvXx]`, `[A-Z]{2,4}-\d{3,6}-\d{2,4}` ids | formats with distinctive shapes |
| Context-gated regex | new NIC, bare digit accounts, EPF refs, passports | ambiguous values, disambiguated by nearby words |
| Statistical NER | spaCy lg/trf | names, places, free-form dates |
| Lexical heuristic | role/honorific + Capitalized word | names NER doesn't know (non-Western names) |

### 2. Context enhancement with harvested column headers

Presidio's context enhancer normally looks at words *around* a match in the text. A
spreadsheet cell has no surrounding words — so the engine harvests words from the top rows
of each column (`_column_context`, 8 rows, 10 words) and passes them as external `context`
to `analyze()`. Effect: a dashed SSN (base score 0.5) or bare phone (0.4) crosses the 0.6
threshold in a column headed "SSN / Tax ID" or "Fixed Line" and stays unmasked elsewhere.
This is the documented Presidio approach for tabular data, generalized to any workbook.

### 3. Header-based column rules (wholesale column masking)

Above detection entirely: if the sheet's header row has a column named like an identifier
(`name`/`officer name`, `address`, `email`, `NIC`, `passport`, `account no`, `DOB`…),
every cell below the header is tokenized without analysis — 100% recall independent of
value format (fixes all-caps `SURNAME, FIRST M` columns that defeat both NER models).
Precision guards: the header row is auto-located (first top row with ≥2 short header-like
strings) so data cells can't create rules; org-name headers (`department name`, `company
name`, `sheet name`…) are deny-listed; null markers (`-`, `N/A`) are skipped; `DOB` rules
respect the date policy. Ordered rule table: specific types (email, phone, NIC…) match
before the broad name/address rules.

### 4. Deterministic pseudonymization with a reversible vault

`generate_token` maps equal inputs to equal tokens (salted SHA-256), so joins across
sheets/files survive masking. The vault (`{token: value}` + `numeric_tokens` set) makes
Excel masking exactly reversible — values *and* cell types (a bank account returns as a
number, an NIC stays text). Free text is reversed by regex-substituting tokens
(`depseudonymize_text`). PDF redaction is deliberately *not* reversible; there the vault is
an audit log.

### 5. Span-level replacement via anonymizer operators

Cells and free text are never replaced wholesale on detection (only column *rules* do
that): analyzer spans are handed to `AnonymizerEngine` with per-entity custom operators, so
`"…my SSN is 456-11-8920, card ending 4312…"` keeps every non-PII character. The anonymizer
also resolves overlapping detections (highest score wins), which is what assigns the final
entity label to a token.

### 6. False-positive filtering as a pipeline stage

All analyzer output passes through ordered filters in `analyze_text` (shared by Excel and
PDF paths — an earlier bug where `1919` survived in Excel but was redacted in PDFs came
from *not* sharing them):

1. letter-less PERSON/LOCATION/NRP dropped (a number is not a name — trf failure mode);
2. PERSON ending in a role word dropped (`Hon. Attorney` is a title, not a person);
3. date policy: DATE_TIME kept only per `birth`/`all`/`none`, with digit-only runs
   additionally requiring birth context + date-like length (kills `1919`, `71829`,
   6-digit salaries);
4. token-overlap skip (idempotent re-runs).

### 7. Two-pass, document-wide PDF redaction

Pass 1 detects over *whitespace-normalized* full-page text (normalization defeats
one-word-per-line captions that break NER). Detected values become a document-wide snippet
set, expanded by two recall techniques:

- **name-part propagation** — words of detected PERSON spans (≥4 chars, capitalized,
  non-role) become snippets, catching bare surnames anywhere in the document;
- **capitalized-run expansion** — any capitalized word-run containing a known name word
  becomes a PERSON snippet (`Murshida Shiyam` fully removed although NER only ever saw
  `Shiyam` in another name).

Pass 2 removes *every occurrence of every snippet on every page*. Rationale: for redaction,
one missed mention is total failure, so recall beats precision; over-redaction is accepted.

### 8. Word-sequence coordinate matching

Mapping text back to page coordinates uses token matching, not substring search: each
snippet is split into edge-punctuation/possessive-normalized, casefolded tokens and matched
against the page's word sequence; the matched words' own boxes become the redaction rects.
This is immune to the two observed failure classes — substring collisions (`LA` inside
`LANKA`) and Tesseract's line-spanning word boxes vetoing valid matches via geometry
checks. `search_for` remains only as a ≥5-char fallback for values inside merged OCR
tokens.

### 9. OCR-aware redaction

Scanned pages get a Tesseract textpage at 300 dpi; all of pass 1/pass 2 then runs against
OCR text and OCR word boxes transparently. Redaction destroys image pixels
(`PDF_REDACT_IMAGE_PIXELS`), rects are glyph-padded, and OCR-tolerant patterns (loose
email) compensate for character confusions. Verified by *independently re-OCR-ing the
masked output* and string-searching for the planted PII — the only honest leak test for
image documents.

### 10. Policy-driven masking (dates, amounts)

Masking decisions that are policy, not detection, are explicit configuration:
salaries/amounts are never masked as such (numbers must match an identifier pattern);
dates follow `--dates birth|all|none` with `birth` as default (a DOB is a
re-identification quasi-identifier; transaction dates are analytical data). Policies live
in the shared filter stage so every pipeline obeys them identically.

### 11. Real-document adversarial testing

The working method that shaped everything above: after every capability, run it against a
*real* artifact of that class (public-record court judgment, 1888 archive scan, US
open-data payroll) and audit for leaks — by re-extraction, re-OCR, and known-answer
checks. Ten of the engine's current mechanisms trace directly to failures those documents
exposed (inventory in TECHNICAL_DESIGN §8); none were visible on synthetic test data.
