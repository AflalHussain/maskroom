# Roadmap — evaluated options and recommendations

Companion to `docs/TECHNICAL_DESIGN.md` §7. That section lists *what* should be done; this
document records the *discussion* behind each item — the problem it addresses in our
actual pipeline, the pros and cons, what it does **not** solve, and the recommended
approach — so the decisions can be revisited without re-deriving them.

Items are presented in the order they were discussed. The recommended execution order is
at the end.

---

## 1. `BatchAnalyzerEngine` batching

### What it is
`pseudonymize_excel` currently calls `analyzer.analyze()` once per unique cell value
(after the value cache and pre-filters). Each call runs the full spaCy pipeline on a tiny
string. Presidio's `BatchAnalyzerEngine` (backed by spaCy `nlp.pipe()`) accepts a list of
strings, so the model processes them as a stream instead of one tokenizer/tagger/NER
invocation each.

### Where the time goes
- `en_core_web_lg`: per-call overhead is small; NER dominates. The cache + pre-filters
  already cut Excel runtime ~4×. Batching gain estimated at 1.5–2× (to be measured).
- `en_core_web_trf`: every call pays a transformer forward pass on a near-empty input and
  wastes the batch dimension. This is why trf is 5–35× slower on sheets. Batching is what
  makes trf practical for Excel; expect several-fold improvement.

### Pros
- Large speedup for trf on sheets; moderate for lg; enables `n_process` on multi-core hosts.
- Fits the existing structure: values are already deduplicated per segment, so the change
  is "collect unique texts → analyze batch → map results back" inside the detection branch.
- No accuracy change — same recognizers, same scores.

### Cons
- `context` (column-header words, +0.35 boost) is per call. Batch mode takes one context
  per batch, so batching must be **per column**; wide-but-short sheets get small batches.
- `pseudonymize_text` couples analyze → filter → anonymize per cell. It must be split into
  analyze-many / anonymize-one. Moderate refactor; the stress key and round-trip tests
  cover regressions.
- Memory: thousands of cells through trf on CPU spikes RAM. Cap batch size (~256).
- One malformed string must not poison a batch — wrap with a fallback to single calls.
- The PDF path gains nothing (already one or two document-wide calls).

### Recommendation
Implement batch-per-column in `pseudonymize_excel` with the single-call path kept as
fallback; benchmark lg and trf on `Real_World_Directory.xlsx` and `PII_Stress_Test.xlsx`
before/after. **Priority: medium.** It is a performance feature that matters mainly if
trf becomes routine for spreadsheets (the name-recall benchmark argues for it on
name-heavy data). It is also a prerequisite for item 4 stage 2.

---

## 2. OCR confidence reporting for scans

### The problem
On a scanned PDF the whole detection chain sits on Tesseract's output. If OCR reads
`Nimal Perera` as `Nimal Pcrera` or `853421234V` as `8534212З4V`, the recognizers never
fire and the page comes back "clean" with zero findings. There is currently **no signal**
distinguishing "no PII here" from "OCR too poor to tell". The re-OCR leak verification
only catches the opposite case (redacted but still readable); it cannot catch what was
never read.

### The feature
Tesseract emits per-word confidence (0–100). We would:
- compute per-page mean/median confidence and the share of words below a threshold (~60);
- surface it in the report and the web UI as a page badge — *OCR quality: good /
  degraded / poor* — with a "review manually" flag;
- optionally (separate decision) loosen detection on poor pages.

### Pros
- Turns a silent failure into a visible one. For a compliance workflow this is the key
  property: reviewers know which pages need a manual look.
- Cheap: confidences are already computed and discarded. No extra OCR pass.
- Gives a quantitative basis for tuning DPI and preprocessing (300 vs 400 dpi,
  binarisation) using the existing scanned samples.
- Makes "0 leaks" claims on scans honest and qualified.

### Cons
- Tesseract confidence is a heuristic, not a probability: old typefaces can be
  "confident but wrong"; clean digit tables can score low while correct. It is an
  indicator; UI wording must say so.
- Thresholds are arbitrary and must be calibrated on a small sample set.
- PyMuPDF's OCR textpage exposes confidence less conveniently than
  `pytesseract.image_to_data`; the stats may need a parallel `pytesseract` call (extra
  dependency, possibly a second OCR run unless the redactor is moved onto the same data —
  cleaner but a larger change).
- Auto-loosening detection on poor pages raises false positives. Keep it opt-in.
- Does not fix bad OCR; only exposes it. Real fixes (deskew, despeckle, adaptive
  threshold, better OCR engine) are a separate, larger item.

### Recommendation
Do the reporting only: `pytesseract.image_to_data` on the same 300-dpi pixmap, per-page
`{mean_conf, low_conf_ratio, words}` in the report, badge + manual-review flag in the UI
above a calibrated threshold. Skip auto-loosening. **Priority: high** — small, zero risk
to detection, and it closes a blind spot in what "verified clean" means.

---

## 3. HMAC-based tokens + encrypted vault

Two separate weaknesses in what happens when *output* leaves our hands.

### 3a. Tokens: `SHA256(value + salt)[:8]` → HMAC

**Weakness.** The suffix is a plain salted hash. If the salt leaks (env var visible in
process environments, shell history, systemd units), anyone can confirm guesses offline:
hash a candidate NIC, compare to the token. NICs, phones and dates have tiny keyspaces, so
a leaked salt lets the whole masked file be brute-forced **without the vault**. String
concatenation also has boundary ambiguity (`"ab"+"c"` vs `"a"+"bc"`) — not exploitable
here, but an audit finding.

**Fix.** `HMAC-SHA256(key, entity || "\x00" || value)`, key ≥ 32 random bytes, truncated
to 8 hex chars with the existing collision extension. Including the entity type matches
existing vault semantics (same string under two entity types → two tokens).

- Pros: the correct primitive for keyed pseudonyms; audit-clean; one-line change in the
  token function; determinism preserved so cross-file joins on tokens still work.
- Cons: changes every token → incompatible with existing vaults/masked files. Needs a
  `scheme` field in the vault and a legacy fallback for restore. Key management remains
  the real problem — HMAC does not help if the key still sits in plaintext in an env var.

### 3b. Vault: plaintext JSON → encrypted

**Weakness.** The vault *is* the PII: every original value against its token, in clear
text, written to `webui/runs/<id>/` and wherever the CLI user points it, downloadable per
run, retained indefinitely.

**Options.**

| | Approach | Pros | Cons |
|---|---|---|---|
| 1 | Symmetric local key — AES-256-GCM (`cryptography`), key file 0600 or KMS | Simple, fast, fits on-prem "sovereign" model | Server-side secret to protect |
| 2 | Per-run passphrase — key via Argon2id/scrypt | No server-side secret | Lost passphrase = unrestorable, by design |
| 3 | Asymmetric — encrypt to a recipient public key | Operator can never restore; strongest separation of duties | Most ceremony |

- Pros (any): vault at rest is no longer a single-file disclosure; masked files can be
  handed to an LLM pipeline with re-identification locked; the web run directory becomes
  far less dangerous.
- Cons: key management — a lost key is permanent; encrypted vaults cannot be eyeballed
  when debugging; restore path needs a key/passphrase input (UX + secret-handling
  surface); new dependency.

### What neither fixes
- Tokens are deterministic, so frequency analysis still works (the most common
  `TOK_PERSON_*` in a payroll is inferable). Inherent to pseudonymization vs.
  anonymization — the trade-off that keeps masked data analyzable.
- Detection misses still leak; the security layer does not change detection quality.
- Run retention: `webui/runs/` needs a TTL cleanup regardless.

### Recommendation
Do both before anything production-facing, in this order: (1) HMAC tokens with
`scheme: "hmac-v1"` in the vault and legacy fallback; (2) encrypted vault, option 1
(AES-GCM, key file outside the repo, path via `PII_VAULT_KEY_FILE`) plus option 2 as a CLI
flag for ad-hoc use; (3) run-directory TTL and no vault in the default UI download.
**Priority: highest** if any masked output or the web UI leaves the dev box — about one
day's work against a real exposure.

---

## 4. Sinhala / Tamil support (NER + OCR)

### What already works
- Structured identifiers are script-independent: NICs, phones, emails, passports, cards,
  account refs are digits/ASCII regardless of document language. Value-profile rules and
  regex recognizers fire identically in a Sinhala-headed sheet.
- Column rules extend trivially: add `නම|பெயர்` (name), `ලිපිනය|முகவரி` (address),
  `දුරකථන|தொலைபேசி` (phone) to `COLUMN_RULES` header patterns — covers bilingual
  government/telco sheets with Sinhala headers and Latin/numeric data.

### What breaks
- Names and addresses **written in Sinhala/Tamil script**. spaCy ships no Sinhala or
  Tamil model; `en_core_web_*` returns no entities, so `නිමල් පෙරේරා` passes through
  untouched. This is the real gap.
- OCR: Tesseract 4.1 has `sin`/`tam` traineddata but accuracy on Sinhala is markedly worse
  than English (ligatures, touching glyphs); mixed-script pages need `-l eng+sin+tam`,
  which slows OCR 2–3× and slightly degrades English.
- Presidio context enhancement is English-lemma based; Sinhala header words give no
  boost (column rules bypass this, which is why they matter more here).

### NER options

| | Option | Pros | Cons |
|---|---|---|---|
| A | Multilingual transformer NER (e.g. `Davlan/xlm-roberta-base-ner-hrl`, XLM-R large CoNLL) via Presidio `TransformersNlpEngine` | No Sinhala training data; plugs into Presidio; also lifts English name recall | Zero-shot Sinhala quality unvalidated (likely 60–80% names, worse on addresses); ~1 GB, trf-class speed → item 1 is a prerequisite; needs a test corpus we do not have |
| B | Fine-tune on Sri Lankan data | Best accuracy; addresses learnable | Weeks of annotation with native speakers; ongoing maintenance; a research project |
| C | Gazetteer / lexicon recognizers — Sinhala/Tamil given names, surnames, honorifics (මයා/මිය, திரு/திருமதி), place names, "honorific + next token" pattern | Deterministic, fast, no model; mirrors the existing role-titled PERSON pattern | Misses unlisted names; lists need curation; no generalisation |
| D | Translate-then-detect | Reuses everything | Span alignment back is unreliable; name translation lossy; heavy dependency. Not recommended |

### OCR options
- Enable `eng+sin+tam` only when a page's script suggests it (Unicode range check on an
  English-only first pass, or Tesseract OSD) to avoid slowing English-only scans.
- Expect lower confidence on Sinhala — exactly where item 2's reporting earns its keep.
- Tesseract 4.1 ceiling on Sinhala is mediocre; Tesseract 5 `sin` best-models are better,
  PaddleOCR/Surya better still but heavier. Defer engine changes until real scanned
  Sinhala samples exist to measure against.

### Pros of doing it
- It is the target market: any Sri Lankan government, bank or telco dataset will contain
  Sinhala/Tamil names or addresses, and today they leak silently.
- Column rules + honorific/gazetteer patterns (C) capture much of the value cheaply.

### Cons
- No test corpus — the first step must be assembling real or realistic samples; synthetic
  Sinhala needs a native speaker to check.
- Model options multiply runtime/memory and complicate the UI's model choice.
- Mixed-script text ("Nimal" next to "නිමල්") yields two tokens for one person;
  cross-script linking is unsolved and probably out of scope.
- Latin-centric assumptions in the code need a Unicode audit: `\b`, `[A-Z]` patterns,
  `_norm_token` in the PDF word matcher, the ≤3-letter PERSON filter. Sinhala combining
  marks will break naive word-boundary matching in the redactor.

### Recommendation — staged
1. **Now, cheap:** Sinhala/Tamil header patterns (now: `column_header` regexes in `locales/lk.yaml`); honorific-triggered
   PERSON patterns for both scripts; Unicode audit of the PDF word matcher; OCR language
   auto-selection. Build a small bilingual test set (extend the stress generator; team
   sanity check).
2. **Then, measured:** trial option A on that test set with item 1 batching in place.
   Adopt only above a real bar (≥85% name recall on the bilingual set); otherwise stay
   with rules + gazetteer.
3. **Only on customer demand:** option B fine-tuning.

**Priority: medium**, after items 3 and 2 — unless the first production data is known to
be Sinhala-script, in which case stage 1 moves to the front.

---

## Recommended execution order

| Step | Item | Why first | Effort |
|---|---|---|---|
| 1 | 3 — HMAC tokens + encrypted vault + run TTL | Real exposure today, not a missing feature | ~1 day |
| 2 | 2 — OCR confidence reporting | Closes a blind spot in "verified clean"; tiny | ~½ day |
| 3 | 4 stage 1 — Sinhala/Tamil rules, gazetteer, OCR langs, Unicode audit | Cheap share of the target-market value | ~1 day |
| 4 | 1 — batching | Enabler for trf on sheets and for step 5 | ~1 day |
| 5 | 4 stage 2 — multilingual NER trial | Needs steps 3 and 4 | measured trial |

*Discussed and recorded 2026-08-22.*

---

# Reassessment for the cloud-LLM use case (2026-08-22)

The stated purpose is to pseudonymize/redact data **before sending it to cloud LLMs
(Anthropic, Gemini, OpenAI)** and to restore identities in the answers that come back.
This changes the threat model in two ways and re-orders the list above.

## What changes

1. **Every detection miss is a disclosure.** A missed name no longer sits on our disk; it
   reaches a third party's infrastructure (logs, and training unless a zero-retention
   agreement exists). Recall is the dominant metric; over-masking is cheap because an LLM
   reasons fine over a token. Consequences: `en_core_web_trf` should be the default for
   free text (it won name recall decisively) → batching (item 1) becomes high priority;
   OCR confidence (item 2) should *block* sending of poor pages, not just badge them;
   Sinhala/Tamil stage 1 (item 4) rises because Sinhala-script names leak silently.

2. **The response path does not exist yet.** We restore Excel files, but there is no
   `depseudonymize_text()` for an LLM reply, and LLMs mangle tokens (lowercasing,
   splitting `TOK_PERSON_3f9a1c22` into `TOK PERSON 3f9a1c22`, dropping hex characters,
   inventing tokens never issued). Needed: tolerant restore (case- and
   separator-insensitive, bounded near-match on the id, report of unresolved tokens) and
   a deliberate token design:

   | Option | Pros | Cons |
   |---|---|---|
   | Current hash tokens `TOK_PERSON_3f9a1c22` | Deterministic across files and sessions; unambiguous | BPE-unfriendly; hex gets mangled; ugly in prose; no grammar/gender cues |
   | Sequential placeholders `[PERSON_1]` | Robust in LLM output; short | Unstable across files unless the vault is session-shared; collides when runs merge |
   | Realistic surrogates (Presidio fake-data operator) | Natural pronouns/grammar; least mangled; readable output | May match a real person; LLM may "correct" names; restore by exact string is fragile; hard to audit surrogate vs leak |

   Recommendation: keep the deterministic hash as the *vault identity*, emit a compact
   word-like surface form (e.g. `PERSON_a7k3`) with tolerant restore. Revisit surrogates
   after the basics.

3. **HMAC + encrypted vault — reframed.** The vault and salt never leave the premises, so
   the brute-force scenario is an internal risk, not a cloud one. Still do both, but the
   urgent vault question for this use case is **lifecycle**: a vault must span an LLM
   conversation (prompt → answer → follow-ups), be shared across all files in that
   session, then be retired; plus TTL on `webui/runs/`.

4. **PDF redaction is the wrong tool for LLM input.** Black boxes destroy information the
   LLM needs and cannot be restored. The right path is extract text (native/OCR) →
   pseudonymize as text → send the text, keeping the spatial redactor for cases where a
   PDF must be shared as a document. New output mode; extraction and detection already
   exist; layout-preserving text for tables is the main work.

5. **Quasi-identifiers are a real residual risk.** An LLM given a whole payroll with
   department, title, salary, hire date and address can infer "the Managing Director"
   with the name masked. Keeping salaries/dates unmasked remains right for analytics, but
   docs and UI must state pseudonymization ≠ anonymization, and an optional **strict
   profile** should also mask job titles, organisation names and full addresses.

6. **Operating procedure (outside the code):** zero-data-retention / no-training terms
   with each provider; never a vault, salt or key in a prompt; an audit log per request
   (masked-file hash + findings report) — the structured run report (design doc item 9)
   moves up for this reason.

## Revised execution order

| # | Item | Change |
|---|---|---|
| 1 | Response restore: tolerant `depseudonymize_text`, LLM-friendly token surface form, unresolved-token report | **done 2026-09-08** (`unmask_text`; token surface form kept as `TOK_…`, the tolerant matcher absorbs the mangling) |
| 2 | OCR confidence with block-on-poor | up |
| 3 | Batching → trf default for free text | up (medium → high) |
| 4 | PDF text-mode output for LLM input | **done 2026-09-08** (`pseudonymize_pdf_text`, `pdf_mode=text`) |
| 5 | Sinhala/Tamil stage 1 | up |
| 6 | Session vault lifecycle + HMAC + encryption + run TTL | lifecycle + per-session HMAC salt + TTL **done 2026-09-08** (`maskroom/session.py`); vault encryption at rest still open |
| 7 | Strict profile for quasi-identifiers; structured run reports for audit | new / promoted |
| 8 | Sinhala/Tamil stage 2 (multilingual NER) | unchanged |
