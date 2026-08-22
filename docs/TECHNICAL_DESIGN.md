# Technical Design — PII Masking Engine

Companion to [README.md](../README.md). The README says *how to use* the system; this document
explains *how and why it works*, what each design decision costs, and what should be improved
before production hardening. Everything here refers to the `maskroom` package
(`FinancialPrivacyEngine` in `engine.py`; policy in `rules.py`, recognizers in
`recognizers.py`, file pipelines in `excel.py` / `pdf.py`).

---

## 1. Architecture

One shared **detection layer** feeds two **output pipelines** with opposite reversibility
guarantees:

```
                      ┌──────────────────────────────┐
                      │   analyze_text()             │
                      │   Presidio Analyzer          │
                      │   + custom recognizers       │
                      │   + context enhancement      │
                      │   + false-positive filters   │
                      └───────┬──────────────┬───────┘
                              │              │
              ┌───────────────▼───┐   ┌──────▼──────────────┐
              │ Excel pipeline    │   │ PDF pipeline        │
              │ pseudonymize      │   │ redact              │
              │ (reversible,      │   │ (irreversible,      │
              │  precision-first) │   │  recall-first)      │
              └───────────────────┘   └─────────────────────┘
```

The two pipelines deliberately **err in opposite directions**:

- Pseudonymized Excel output is meant for analytics, so false positives destroy value —
  the pipeline favors *precision* (score thresholds, context gating, amount/date policies).
- A redacted PDF that leaks one name has failed completely — the pipeline favors *recall*
  (document-wide propagation, name-part expansion, over-redaction tolerated).

A single detection layer keeps the two consistent: every false-positive fix (e.g. "a number
is never a PERSON") automatically applies to both.

---

## 2. Detection layer

### 2.1 Recognizer strategy: patterns + context, NER as one input among several

Detection is a **stack**, not a single model:

| Layer | Examples | Failure mode it covers |
|---|---|---|
| Checksum/validated patterns | credit card (Luhn), phones (`phonenumbers`, region LK) | format lookalikes (invalid cards, fictional 555 numbers are correctly *not* masked) |
| Plain regex patterns | old NIC `\d{9}[VvXx]`, structured account ids | formats no NER model knows |
| Context-gated patterns | new 12-digit NIC, bare 9–18 digit accounts, EPF refs, passports | ambiguous values that are only PII near the right words |
| Statistical NER (spaCy) | names, locations | anything without a fixed format |
| Lexical name heuristic | capitalized word after *Mr/Dr/overseer/manager/…* | NER's blind spot on non-Western names |

**Context gating** is the load-bearing idea for structured data. Presidio's
`LemmaContextAwareEnhancer` adds ~0.35 to a match's score when a context word appears nearby.
Ambiguous patterns are registered *below* the masking threshold (0.3 vs `min_score` 0.6) so
they only fire when context lifts them: a bare `104512345678` masks in an "Account No" column
and stays untouched in an "Order ID" column — with zero per-dataset configuration.

**Pros**
- Explainable: every mask decision traces to a named recognizer + score.
- Tunable without retraining; new ID formats are a 5-line `PatternRecognizer`.
- Checksum validation gives near-zero false positives for cards/phones.

**Cons / risks**
- Context vocabularies are curated English word lists; a Sinhala-language header
  (`ජා.හැ. අංකය`) provides no boost. Multi-language support requires parallel word lists
  and a multi-language NLP engine.
- Pattern recall is only as good as the catalog: unseen ID formats (e.g. `MRN-908123`)
  pass through silently. There is no "unknown identifier" detector.
- The two NER models disagree at the margins; scores are not calibrated across them.
- Presidio's context enhancer matches context words as **substrings** of the surrounding
  tokens (`ring` ⊂ `string`, `line` ⊂ `online`). Context vocabularies must therefore avoid
  short words; a header like "Raw String" once boosted a phone match via `ring`. Keep
  context words ≥4 chars and specific.

### 2.2 False-positive filter chain

`analyze_text` post-filters analyzer output, in order:

1. **Letter-less NER labels dropped** — `PERSON`/`LOCATION`/`NRP` on text with no letters is
   model noise (the transformer tags bare salary figures as PERSON; observed on real data).
2. **Role-ending "names" dropped** — a PERSON whose final word is a role/honorific
   (*Hon. Attorney*) is a title fragment.
3. **Date policy** — `DATE_TIME` survives only per policy (`birth`/`all`/`none`); digit-only
   runs additionally require birth context *and* date-like length, because NER routinely
   labels arbitrary numbers as dates (`1919`, `71829`, six-digit salaries). Dropping the
   date label also lets the correct recognizer (e.g. `FINANCIAL_ACCOUNT`) win the overlap.
4. **Location policy** — `LOCATION` survives per policy (`address`/`all`/`none`). The
   default keeps bare place names (a city shared by a million people is an analysis
   dimension, not an identifier) and masks street-level addresses: spans carrying a
   house/box number or street word, or preceded by one (*PO Box 14370 Salem* — NER labels
   only *Salem*). *Address* columns are always masked under `address`. Measured effect:
   LOCATION findings on the public-directory sheet 190 → 74, no other entity affected.
5. **Token-overlap skip** — spans overlapping an existing `TOK_…` are ignored, which makes
   re-runs idempotent (verified: second pass changes 0 cells).

Filter order matters: each filter removes *labels*, not *text*, so an over-broad DATE match
being dropped can expose a better-typed match underneath rather than causing a leak.

### 2.3 NER model choice

Benchmarked on real data (see README for the full table): `en_core_web_trf` wins clearly on
name recall (6/6 vs 3/6 hard-name micro-test; 56/60 vs 45/60 on real all-caps
`SURNAME, FIRST M` records) at 4–35× CPU runtime. Both models produce **zero leaks on the PDF
test corpus** because the role-title recognizer and document-wide propagation compensate for
NER misses.

**Recommendation:** `lg` for spreadsheets (pattern recognizers dominate there), `trf` for
name-heavy free text. If GPU is available, `trf` becomes viable as the default.

---

## 3. Excel pipeline (`pseudonymize_excel`)

### 3.1 Flow

1. **Column context harvest** — the sheet's header row is auto-detected (first top row with
   ≥2 short header-like cells); each column's context is the words of its header cell plus
   the cell above it (two-row headers). Context comes from labels, never data; only when no
   header row is found does it fall back to harvesting the top 8 rows.
2. **Per-cell analysis** — strings analyzed with full entity set; numeric cells stringified
   and analyzed against identifier-pattern entities only (`NUMERIC_CELL_ENTITIES`), never NER.
3. **Span-level replacement** — Presidio's `AnonymizerEngine` with a per-entity custom
   operator calls `generate_token`; the anonymizer resolves overlapping matches. Only the
   matched span is replaced, so free-text cells keep their non-PII content.
4. **Vault update** — tokens map to originals; tokens from numeric cells are recorded so
   restore can rebuild cell types exactly.

### 3.2 Design decisions, pros and cons

**Deterministic tokens** — `TOK_<ENTITY>_<SHA-256(value+salt)[:8]>`, prefix extended on
collision.

- *Pros:* referential integrity — the same customer gets the same token across sheets,
  files, and runs, so joins and group-bys on masked columns still work. No state needed at
  masking time beyond the salt.
- *Cons:* determinism is also the main cryptographic weakness — see §5.

**Column-context harvesting**

- *Pros:* makes tabular detection work at all (a bare dashed SSN scores 0.5, below
  threshold, until the header lifts it); no per-dataset config.
- *Cons:* assumes a header row in the top 12 rows. Multi-table sheets (second table),
  merged header cells, or transposed layouts (fields as rows) get no or weak context.
  A row-context fallback for transposed sheets is a cheap future addition.

**Numeric-cell entity restriction**

- *Pros:* a salary/quantity can never become a PERSON or DATE; card numbers stored as
  numbers are still caught via Luhn.
- *Cons:* an identifier stored numerically that needs NER-style reasoning is missed —
  acceptable, since no such case has a sound NER signal anyway.

**Whole-value tokens (no format preservation)** — `TOK_CREDIT_CARD_…` doesn't look like a
card number.

- *Pros:* unambiguous, collision-safe, visibly masked.
- *Cons:* breaks downstream format validators and layout-sensitive consumers, and token
  length differs from the original (column widths, fixed-width exports). If format-preserving
  pseudonyms are needed, FPE (e.g. FF3-1) or per-entity fake-value generation (Presidio's
  `fake` operators) can replace `generate_token` per entity type without touching the
  pipeline.

### 3.3 Restore

`depseudonymize_excel` walks cells, replaces tokens via vault, converts values whose token is
in `numeric_tokens` back to int/float. Verified round-trip: 0 of 264 cells differ (value *and*
type) on the LK corpus; 0 diffs on the generic corpus. Unknown tokens are counted and warned
about (wrong vault detection).

---

## 4. PDF pipeline (`redact_spatial_pdf`)

### 4.1 Why two passes

Single-pass (detect-and-redact per page) fails in two documented ways:

1. NER catches a name once but not its other mentions (case captions, footers).
2. A name never caught on page N but caught on page M leaks on page N.

Pass 1 therefore builds a **document-wide snippet set**; pass 2 redacts *every occurrence of
every snippet on every page*. Between the passes, two recall expanders run:

- **Name-part propagation** — words (≥4 chars, capitalized, not a role word) from detected
  PERSON spans become snippets themselves, catching bare surnames.
- **Capitalized-run expansion** — a capitalized word-run containing any known name word
  becomes a PERSON snippet (`Murshida Shiyam` is fully removed even though NER only ever saw
  `Shiyam` inside another name).

*Pros:* zero name leaks across the whole test corpus, including a real Supreme Court judgment
and a real 1888 scan.
*Cons:* deliberate over-redaction — role words adjacent to names inside a run get swept in;
common-word surnames would propagate aggressively (mitigated by the stopword list, not
eliminated). Acceptable for redaction, wrong for pseudonymization — which is why Excel does
not use propagation.

### 4.2 Coordinate mapping: word-sequence matching, not substring search

The naive approach (`page.search_for(snippet)`) failed twice on real documents:

1. `search_for` is substring-based: a false-positive snippet "LA" blacked out the middle of
   "LANKA" and destroyed adjacent content.
2. Geometry-based whole-word validation (rect-vs-word-box overlap ratios) broke on a real
   1888 scan where Tesseract emits word boxes spanning multiple lines — valid redactions were
   vetoed by unrelated words from neighboring lines.

Current design: snippets are tokenized and matched against the page's **word sequence**
(edge punctuation and possessives normalized, casefolded); redaction rects are the matched
words' own boxes. `search_for` survives only as a fallback for values embedded inside merged
OCR tokens, gated to snippets ≥5 chars.

*Pros:* immune to substring collisions and box-geometry pathologies; handles line wraps
naturally (each word carries its own rect).
*Cons:* a value split across a hyphenated line break won't word-match (falls back to
`search_for`, which handles it); token normalization is Latin-script-centric.

### 4.3 OCR integration

Pages with images but <30 chars of text get a Tesseract textpage (`get_textpage_ocr`,
300 dpi, tessdata auto-discovered). The same textpage serves pass 1 text, pass 2 word boxes,
and `apply_redactions(images=PDF_REDACT_IMAGE_PIXELS)` physically erases the matched pixels.
OCR rects are padded ±2pt horizontally because Tesseract boxes sit tighter than glyphs.

Two lessons encoded in the implementation:

- PyMuPDF textpages weak-reference their `Page`; page objects must be materialized once and
  kept alive for the whole run or the second access crashes.
- OCR errors defeat validating recognizers: `.lk` misread as `.Ik` makes Presidio's
  TLD-validated email recognizer reject the whole address — hence the loose email fallback
  pattern. The general principle: **for redaction, prefer over-matching patterns over
  validating ones.**

*Cons / open risk:* OCR quality bounds everything. There is currently **no confidence
reporting** — a page whose OCR is garbage is silently under-redacted. See §7.

---

## 5. Security analysis

### 5.1 What the system does and does not claim

Masked output is **pseudonymized, not anonymized** (GDPR Art. 4(5) terms). Detection recall
is below 100% by construction; treat outputs as personal data with reduced risk, not as
de-identified data, unless a human review confirms otherwise.

### 5.2 Deterministic-token weaknesses

- **Known-plaintext guessing:** tokens are `SHA-256(value + salt)`. Anyone who knows the salt
  can confirm a guessed value offline (NICs and phone numbers are enumerable spaces). The
  salt is therefore key material, not a tuning constant.
  **Recommendation:** move to `HMAC-SHA256(key, value)` with a key from a secrets manager —
  same determinism, standard key handling, no low-entropy-salt footgun.
- **Frequency analysis:** determinism preserves value frequencies; a dominant token in a
  "City" column is guessable from public statistics. If linkability across files isn't
  needed, random tokens (vault-only mapping) remove this channel.
- **Entity type disclosure:** `TOK_US_SSN_…` reveals *what kind* of value was present.
  Usually acceptable (and useful for audit); use a generic prefix if not.

### 5.3 Vault handling

The vault is a plaintext JSON re-identification key. Minimum bar for production: encrypt at
rest (age/KMS envelope), store separately from masked outputs, restrict read access, and log
restore operations. The engine deliberately does not implement this — key management belongs
to the deployment, not the library.

### 5.4 Residual quasi-identifiers

The default policy keeps analytical fields (dates, amounts). Kept fields can re-identify in
combination (admission date + rare diagnosis in a small population). For small-population or
high-sensitivity datasets, run `--dates all` and consider suppressing rare categorical values
— outside this engine's current scope.

---

## 6. Performance characteristics

| Workload | Cost driver | Measured (CPU) |
|---|---|---|
| Excel | one analyzer call per non-empty cell | ~250-cell LK workbook: 3.8 s (lg) / 141 s (trf) |
| PDF (native) | one analyzer call per page + word matching | 11-page judgment: 5.8 s (lg) / 24 s (trf) |
| PDF (scanned) | Tesseract dominates (~5–15 s/page at 300 dpi) | 2-page 1888 scan: ~25–35 s either model |

Known inefficiencies (deliberate simplicity, worth fixing at scale):

1. ~~No result caching~~ — *done (2026-08-21)*: a per-run cache keyed on
   `(value, numeric?, column)` analyzes each distinct cell value once, and cells that cannot
   be PII (strings < 3 chars, numbers < 8 digits) skip the analyzer. Measured on the real
   directory workbook: 4.7 s → 1.3 s (lg), 39 s → 10 s (trf), outputs byte-identical.
2. **No batching** — spaCy is far faster on batched documents (`nlp.pipe`); Presidio's
   `BatchAnalyzerEngine` exists for exactly this.
3. **Single-threaded** — sheets and PDF pages are embarrassingly parallel.
4. Pass 2 word-matching is O(pages × snippets × words); fine at current scale, indexable
   (first-token hash map) if snippet counts grow.

---

## 7. Recommendations (prioritized)

> Full discussion of items 1–4 and 6 below — problem, pros/cons, options, execution
> order — is in [`ROADMAP.md`](../ROADMAP.md).

**P1 — before production use**
1. Replace salted-hash tokens with **HMAC-SHA256** and a managed key (§5.2).
2. **Encrypt the vault** at rest and separate its storage/permissions from outputs (§5.3).
3. **OCR confidence reporting**: emit per-page mean word confidence from Tesseract; flag
   pages below threshold for human review instead of silently under-redacting.

**P2 — quality and throughput**
4. `BatchAnalyzerEngine` batching (value cache done); parallelize per sheet/page.
5. ~~Column-rule mode~~ — *done*: header rules + value-profile rules + table segmentation
   (see README §Detection stack 3–4).
6. **Sinhala/Tamil support**: `tesseract-ocr-sin/-tam` language packs, multi-language
   context word lists, and a multi-lingual NER model — required for real Sri Lankan
   government documents, which are largely trilingual.

**P3 — scope growth**
7. DOCX/CSV/JSON input support (the detection layer is already format-agnostic).
8. ~~Regression suite over the test corpus~~ — *done*: `tests/` (stress answer key,
   round-trips, PDF leak checks, text rules); wire `pytest` into CI.
9. Structured run reports (JSON: entities found, counts per type, pages OCR'd, confidence)
   for compliance evidence.

---

## 8. Change log of design-shaping incidents

Real-data testing drove most of the design. The incidents worth remembering:

| Incident | Design consequence |
|---|---|
| Headers like "Email Address" masked as PERSON | score threshold + context gating in Excel path |
| Whole cells destroyed for one embedded entity | span-level replacement via AnonymizerEngine |
| Phones/SSNs unmasked in bare cells | column-header context harvesting |
| `1919` hotline and salaries masked as dates | digit-run date rules → evolved into the date policy |
| Case caption names split one-per-line defeated NER | whitespace normalization before analysis |
| "LA" false positive chewed through "LANKA" | whole-word validation → word-sequence matching |
| 1888 scan: tall OCR boxes vetoed valid redactions | abandoned geometry checks for token matching |
| `.lk` OCR-misread as `.Ik` rejected by email validator | loose (non-validating) email fallback |
| `overseer Jayasuriya` never tagged by lg NER | role-titled-name recognizer |
| trf tagged salary figures as PERSON | letter-less NER filter |
| Cryptic / Sinhala headers gave rules nothing to match | value-profile rules (validated patterns over column values) |
| First table's `Name` rule ran into the second table on the same sheet | sheet segmentation by header-like rows |
| 10-digit order id passed the UK NHS checksum | irrelevant country recognizers disabled at startup |
| Presidio compiles patterns IGNORECASE → caps-name heuristic matched "Name, NIC" | `global_regex_flags` without IGNORECASE; header cells skipped |
| Same value numeric in one sheet, text in another → restore typed both as numbers | per-cell numeric record in the vault |
| `Salem` ×81 / `Colombo` masked in a directory — the analysis dimension destroyed | location policy: addresses by default, bare place names kept |

The meta-lesson: every one of these was invisible on synthetic data and obvious on the first
real document of its kind. Keep the real-document corpus in CI (recommendation #8).
