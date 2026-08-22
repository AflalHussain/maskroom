# Sovereign AI — PII Masking Engine

A pseudonymization and redaction engine for Excel workbooks and PDF documents, built on
[Microsoft Presidio](https://microsoft.github.io/presidio/), with first-class support for
**Sri Lankan identifiers** (NIC, +94 phone formats, passports) and **scanned documents via OCR**.

- **Excel** → reversible *pseudonymization*: PII is replaced with deterministic tokens
  (`TOK_US_SSN_8B584CCF`), and a vault file maps every token back to its original value.
- **PDF** → irreversible *redaction*: matched text is blacked out and the underlying
  bytes (or image pixels, for scans) are physically destroyed. The vault serves only as an
  audit log of what was removed.

---

## Contents

1. [How it works](#how-it-works)
2. [Setup](#setup)
3. [Usage](#usage)
4. [Options reference](#options-reference)
5. [The vault](#the-vault)
6. [What gets masked (and what doesn't)](#what-gets-masked-and-what-doesnt)
7. [Choosing the NLP model](#choosing-the-nlp-model)
8. [Test corpus](#test-corpus)
9. [Known limitations](#known-limitations)

Further reading: [`docs/TECHNICAL_DESIGN.md`](docs/TECHNICAL_DESIGN.md) (architecture,
security, performance), [`docs/TECHNOLOGIES.md`](docs/TECHNOLOGIES.md) (techniques explained),
and [`ROADMAP.md`](ROADMAP.md) (evaluated next steps with pros/cons).

## Project layout

```
maskroom/            the engine, installed as a package (`pip install -e .`)
  rules.py           detection policy: column rules, value-profile patterns, validators, entity lists
  recognizers.py     custom Presidio recognizers (financial, names, Sri Lankan identifiers)
  engine.py          FinancialPrivacyEngine: analyzer setup, text detection filters, tokens, vault
  excel.py           Excel pipeline: header/segment detection, column rules, mask & restore
  pdf.py             PDF pipeline: OCR, two-pass detection, spatial redaction
  cli.py             the `maskroom` command
webui/               Flask UI (app.py, static/index.html); uploads land in webui/runs/ (ignored)
tests/               pytest suite; tests/data/ holds the test corpus and the stress answer key
scripts/             gen_stress.py (regenerate the stress workbook), time_excel.py (timing)
docs/                technical design and technologies documents
setup.sh             one-shot environment setup
```

---

## How it works

```mermaid
flowchart TD
    A[Input file] -->|.xlsx / .xlsm| B[Excel pipeline]
    A -->|.pdf| C[PDF pipeline]

    subgraph B_ [Excel pipeline — reversible]
        B --> B1[Collect per-column context words<br/>from header rows]
        B1 --> B2[Analyze each cell<br/>strings + numeric cells]
        B2 --> B3[Filter false positives<br/>date policy, letter-less names, titles]
        B3 --> B4[Replace only the PII spans<br/>with deterministic tokens]
        B4 --> B5[Save masked workbook<br/>+ vault mappings]
    end

    subgraph C_ [PDF pipeline — irreversible]
        C --> C1{Page has a<br/>text layer?}
        C1 -->|yes| C2[Native text]
        C1 -->|no, but has images| C3[Tesseract OCR<br/>at 300 dpi]
        C2 --> C4[Pass 1: detect entities on<br/>whitespace-normalized page text]
        C3 --> C4
        C4 --> C5[Propagate person names document-wide<br/>+ expand capitalized runs]
        C5 --> C6[Pass 2: map every occurrence to<br/>coordinates via word-sequence matching]
        C6 --> C7[Black-out + destroy text bytes<br/>and image pixels]
    end
```

### Detection stack

Every piece of text passes through one shared analysis path (`analyze_text`):

1. **Presidio built-in recognizers** — emails, credit cards (Luhn-validated), SSNs, IBANs,
   URLs, and spaCy NER for names, locations, and dates.
2. **Custom recognizers** registered on top:
   | Entity | Pattern | Notes |
   |---|---|---|
   | `FINANCIAL_ACCOUNT` | `ABC-1234-002`, bare 9–18 digit runs, `A/12345` refs | bare digits only mask near context words (*account, iban, routing, epf…*) |
   | `LK_NIC` | old `853421234V` and new 12-digit format | old format masks on its own; new format needs NIC context |
   | `LK_PASSPORT` | `N1234567` | requires *passport* context |
   | `PHONE_NUMBER` | re-registered with region **LK** + US/GB/IN | validates `+94` and `0XX` formats via `phonenumbers`; context includes landline vocabulary |
   | `EMAIL_ADDRESS` (loose) | OCR-tolerant regex, no TLD validation | catches OCR misreads like `.Ik` for `.lk` |
   | `PERSON` (role-titled) | capitalized word after *Mr/Dr/overseer/manager/…* | catches names statistical NER misses |
3. **Column rules (Excel)** — when a sheet's header row contains a recognizable identifier
   header (*name / officer name / address / NIC / email / phone / passport / account no /
   DOB…*), every cell **below** that header is masked wholesale, with no per-cell detection —
   100% recall regardless of value format (fixes e.g. all-caps `SURNAME, FIRST M` columns).
   The header row is auto-located in the top rows (first row with ≥2 short header-like
   cells), so title/description rows above it are fine. Guards: org-name headers
   (*department name, company name, sheet name…*) are excluded, "no data" markers (`-`,
   `N/A`) are skipped, and data cells can never trigger a rule. Disable with
   `--no-column-rules`. Header rows themselves are never analyzed (labels, not data).
   **Keep rules** go the other way: columns headed *City / Town / District / Province /
   Region / Country / Nationality…* are never analyzed at all — they hold aggregation
   dimensions, not people.
   **Stacked tables** on one sheet are segmented automatically (a header-like row after a
   blank row starts a new table with its own rules); transposed layouts fall back to
   per-cell detection.
4. **Value-profile rules (Excel)** — when ≥90% of a column's values match one
   high-precision, validated identifier pattern (NIC with day-of-year check, email, LK
   phone formats, Luhn-valid cards, structurally valid SSNs, passports), the column is
   treated as that identifier **regardless of its header** — cryptic headers (`C3`),
   Sinhala/Tamil headers, or no header at all. Numeric-stored identifiers are covered.
5. **Context enhancement** — for spreadsheets, words from each column's header rows are fed to
   Presidio's context enhancer, so a bare phone number in a *"Fixed Line"* column scores as it
   would inside a sentence. This is what makes structured-data detection work.
6. **Sri Lankan place gazetteer** — English NER does not know the country's geography and
   labels about half of its towns as people (*Kandy*, *Negombo*, *Dehiwala*, *Badulla*…).
   A "name" made only of known provinces, districts, towns or Colombo suburbs is relabelled
   a location, where the location policy applies (kept by default). *Kandy Perera* is
   still a person. Lone field-label words (*NIC*, *OTP*, *Email*) are never names.
7. **False-positive filters** — NER name/place labels on letter-less text are dropped (a salary
   is not a PERSON); PERSON/NRP spans containing digits (`EMP-100`, `WP CAB-1234`) and lone
   ≤3-letter tokens (`Max`, `Pro`) are dropped; "names" ending in a role word
   (*Hon. Attorney*) are dropped; the date policy (below) governs `DATE_TIME`; spans
   overlapping existing tokens are skipped, making re-runs idempotent. Presidio's
   country-specific recognizers irrelevant to this deployment (UK NHS, AU TFN, SG NRIC, IN
   Aadhaar, IT/ES/PL ids, US driver licence…) are disabled — their checksums fire on random
   numbers.

### Date, location & amount policy

Salaries, prices, and quantities are **never masked as amounts** — numbers only mask when they
match an identifier pattern (account, card, NIC). Dates follow a policy (`--dates`):

| Policy | Behavior |
|---|---|
| `birth` *(default)* | Only dates in a birth context are masked (a *Date of Birth* column, "born on…"). A DOB is a classic re-identification quasi-identifier; ordinary transaction/event dates stay analyzable. |
| `all` | Every detected date is masked (HIPAA-style). |
| `none` | No dates are masked. |

Locations follow the same idea (`--locations`): a city or district is something you analyze
*by*, while a street address points at one household.

| Policy | Behavior |
|---|---|
| `address` *(default)* | Only street-level addresses are masked — values with a house/box number or a street word (road, street, lane, mawatha, avenue, P.O. Box, apt…), plus any *Address* column. Bare city, district and country names (`Colombo`, `Jaffna`, `Sri Lanka`) stay. |
| `all` | Every detected place name is masked (use with the strict/quasi-identifier stance: city + title + DOB can single someone out). |
| `none` | No locations are masked, not even address columns. |

---

## Setup

### Prerequisites

| Requirement | Why | Check |
|---|---|---|
| Python 3.11+ | engine and web UI | `python3 --version` |
| `tesseract-ocr` (system package) | redacting **scanned** PDFs; native-text PDFs and Excel work without it | `tesseract --version` |
| ~1.5 GB disk (≈3 GB with the transformer model) | spaCy models, PyTorch | — |
| Internet access during setup | pip packages and model downloads | — |

On Debian/Ubuntu: `sudo apt install python3.11-venv tesseract-ocr`

### Option A — one-shot script (recommended)

```bash
cd /hms/apps/sovereign-ai/masking
./setup.sh            # core engine + web UI, default NER model (en_core_web_lg)
./setup.sh --trf      # additionally installs the transformer model (CPU PyTorch)
```

The script creates the `pii_env` virtualenv, installs pinned dependencies from
`requirements.txt`, downloads the spaCy model(s), and ends with a self-test that masks a
sample sentence. If it prints `engine OK -> Call TOK_PERSON_… on phone TOK_PHONE_NUMBER_…, NIC TOK_LK_NIC_…`, you're done.

### Option B — manual steps

```bash
cd /hms/apps/sovereign-ai/masking
python3 -m venv pii_env
pii_env/bin/pip install --upgrade pip
pii_env/bin/pip install -r requirements.txt
pii_env/bin/python -m spacy download en_core_web_lg
```

Optional transformer model (better name recall, 4–35× slower on CPU — see
[Choosing the NLP model](#choosing-the-nlp-model)). Install PyTorch from the CPU index
first, otherwise pip pulls the multi-GB CUDA build:

```bash
pii_env/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
pii_env/bin/pip install -r requirements-trf.txt
pii_env/bin/python -m spacy download en_core_web_trf
```

### Configure the token salt

Tokens are derived from `SHA-256(value + salt)`. The salt is what stops anyone from
reconstructing tokens for guessed values, so set your own and keep it secret — the same
salt must be used whenever consistent tokens across files are needed:

```bash
export PII_TOKEN_SALT="choose-a-long-random-secret"
```

Put it in the service environment / shell profile rather than on the command line history.
Without it the engine falls back to a built-in default salt, which is fine for testing only.

### Verify the install

```bash
pii_env/bin/maskroom tests/data/PII_Test_Dataset_LK.xlsx /tmp/check.xlsx --vault /tmp/check_vault.json
# expected: "[Success] Excel saved to: /tmp/check.xlsx (… cells modified)"
pii_env/bin/maskroom tests/data/PII_Test_Sample_LK_SCANNED.pdf /tmp/check.pdf
# expected: "[OCR] Page 1 has no text layer — running OCR" then "[Success] PDF saved …"
pii_env/bin/pytest -q
# expected: all tests pass (~3–5 min on the default model; adds the stress, round-trip and PDF gates)
```

If the second command prints a Tesseract warning instead of `[OCR]`, install `tesseract-ocr`
and re-run — scanned pages are otherwise left **un-redacted** (the engine warns loudly, it
never fails silently).

### Start the web UI

```bash
pii_env/bin/python webui/app.py
# → open http://127.0.0.1:5170
```

First start takes 30 s – 2 min while the NLP stack loads (longer once the transformer stack
is installed); the page answers as soon as the Flask banner appears. The server binds to
localhost only and stops with the terminal that started it. To keep it running permanently,
run it under a process manager — a minimal systemd user unit:

```ini
# ~/.config/systemd/user/maskroom.service
[Unit]
Description=PII masking web UI
[Service]
WorkingDirectory=/hms/apps/sovereign-ai/masking
Environment=PII_TOKEN_SALT=choose-a-long-random-secret
ExecStart=/hms/apps/sovereign-ai/masking/pii_env/bin/python webui/app.py
Restart=on-failure
[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now maskroom
```

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Port 5170 is in use` | a previous server is still running: `pkill -f "webui/app.py"` then restart |
| `OSError: [E050] Can't find model 'en_core_web_lg'` | model download skipped — run the `spacy download` step |
| `[Warning] … Tesseract language data was not found` | install `tesseract-ocr`; or point `TESSDATA_PREFIX` at the tessdata directory |
| `--nlp-model en_core_web_trf` fails to load | transformer extras not installed — run `./setup.sh --trf` |
| Very slow first request | model loading on first use; subsequent requests are fast |
| `ModuleNotFoundError: flask` | web UI deps missing — `pii_env/bin/pip install -r requirements.txt` |

---

## Usage

### Mask an Excel workbook (reversible)

```bash
pii_env/bin/maskroom data.xlsx data_masked.xlsx --vault vault.json
```

### Restore the original from a masked workbook

```bash
pii_env/bin/maskroom data_masked.xlsx data_restored.xlsx --restore --vault vault.json
```

Restore is exact — values *and* cell types (numeric cells return as numbers, text identifiers
like NICs stay text). It requires the vault saved during masking.

### Redact a PDF (irreversible; scans handled automatically)

```bash
pii_env/bin/maskroom document.pdf document_redacted.pdf --vault audit.json
```

Pages without a text layer are OCR'd automatically (`[OCR] Page N has no text layer`).
Redaction destroys the matched text bytes and image pixels — **a redacted PDF cannot be
restored**; the vault is an audit log only.

### Use the transformer model for better name recall

```bash
pii_env/bin/maskroom report.pdf --nlp-model en_core_web_trf
```

### Web UI

```bash
pii_env/bin/python webui/app.py     # then open http://127.0.0.1:5170
```

Upload a workbook or PDF, set every CLI option (threshold, NER model, date policy, entity
whitelist, restore-with-vault), and get: coverage stat tiles, a findings table with every
detection's location, entity type, and confidence score, a highlighted before/after preview
(Excel as tables, PDF as page renders), and download links for the masked file and vault.
The server binds to localhost only — uploaded files and vaults land in `webui/runs/`;
clear that directory as you would any sensitive working data.

### Python API

```python
from maskroom import FinancialPrivacyEngine

engine = FinancialPrivacyEngine(
    min_score=0.6,              # detection confidence threshold
    dates="birth",              # "birth" | "all" | "none"
    locations="address",        # "address" | "all" | "none"
    nlp_model=None,             # or "en_core_web_trf"
    entities=None,              # or a whitelist, e.g. ["PERSON", "LK_NIC"]
)

engine.pseudonymize_excel("in.xlsx", "out.xlsx")
engine.save_vault("vault.json")

engine.redact_spatial_pdf("in.pdf", "out.pdf")

# free-text helpers
masked, changed = engine.pseudonymize_text("Call Nimal on 077-1234567")
original = engine.depseudonymize_text(masked)

# reverse a workbook later
engine2 = FinancialPrivacyEngine()
engine2.load_vault("vault.json")
engine2.depseudonymize_excel("out.xlsx", "restored.xlsx")
```

---

## Options reference

| Flag | Default | Meaning |
|---|---|---|
| `input` | — | `.xlsx`, `.xlsm`, or `.pdf` file |
| `output` | `<input>_masked.<ext>` | output path |
| `--vault FILE` | off | write (mask) or read (`--restore`) the token↔value mapping |
| `--restore` | off | reverse a masked workbook using `--vault` (Excel only) |
| `--min-score F` | `0.6` | minimum detection confidence to mask |
| `--entities E…` | all | restrict to specific entity types |
| `--nlp-model M` | `en_core_web_lg` | spaCy NER model, e.g. `en_core_web_trf` |
| `--dates P` | `birth` | date policy: `birth` / `all` / `none` |
| `--locations P` | `address` | location policy: `address` / `all` / `none` |

---

## The vault

`vault.json` holds `{token: original_value}` mappings plus which tokens came from numeric
cells (so restore preserves cell types).

> **The vault is the re-identification key.** Anyone holding it can reverse the masked file.
> Store it encrypted or in a secrets store, always separated from wherever the masked data
> goes. The masked output is only as protected as this file.

Tokens are deterministic: the same value + same salt always produces the same token, so
referential integrity holds across sheets, files, and runs (joins on a masked column still
work). Changing `PII_TOKEN_SALT` changes all tokens.

---

## What gets masked (and what doesn't)

**Masked:** names (including role-titled: "overseer Jayasuriya"), emails, phone numbers
(LK + international), NIC old/new, passports (with context), bank accounts, routing numbers,
EPF/ETF refs (with context), credit cards (valid Luhn), SSNs, street addresses,
birth dates.

**Kept:** salaries and monetary amounts, postal codes, invoice/case/vehicle numbers,
public hotlines (1919, 119), company registration numbers, transaction/event dates
(under the default policy), city/district/country names (under the default policy), job
titles, department and organization names.

Redaction errs toward over-masking (safe direction); pseudonymization errs toward precision.

---

## Choosing the NLP model

Benchmarked on real data (US public-employee records, a Supreme Court judgment, an 1888
scanned report) — 2026-08-19, CPU only:

| Metric | `en_core_web_lg` (default) | `en_core_web_trf` |
|---|---|---|
| Hard non-Western names in sentences | 3/6 | **6/6** |
| Real all-caps `SURNAME, FIRST M` names | 45/60 | **56/60** |
| Well-formatted directory (names/emails/phones) | 85–86/86 | **86/86** |
| PDF leak tests (judgment, scan) | 0 leaks | 0 leaks |
| Excel runtime (146-row workbook) | **15 s** | 64 s |

**Recommendation:** default `lg` for spreadsheets and bulk jobs (pattern recognizers do most
of the work there); `--nlp-model en_core_web_trf` for name-heavy free-text documents.
Note that both models achieve zero PDF leaks thanks to role-title detection and document-wide
name propagation.

---

## Test corpus

| File | What it exercises |
|---|---|
| `PII_Redaction_Test_Dataset.xlsx` | generic structured/unstructured PII + edge cases |
| `tests/data/PII_Test_Dataset_LK.xlsx` | Sri Lankan formats: NIC, +94 phones, EPF/ETF, LKR salaries |
| `tests/data/PII_Test_Sample_LK.pdf` | native-text letter with embedded LK PII |
| `tests/data/PII_Test_Sample_LK_SCANNED.pdf` | same letter as an image-only scan (OCR path) |
| `tests/data/SC_Judgment_Sample.pdf` | real Supreme Court judgment: dense legal text, repeated names |
| `tests/data/Ceylon_1888_Scan_Sample.pdf` | real 1888 scan: OCR noise, hard typography |
| `tests/data/Real_World_Directory.xlsx` | real public-employee data: normal + all-caps inverted names |
| `tests/data/PII_Stress_Test.xlsx` (+ `_key.json`) | hostile synthetic: cryptic & Sinhala headers, header at row 16, stacked tables, numeric-stored NICs, decoy columns, free text — with a machine-readable answer key |

---

## Known limitations

- **PDF redaction is permanent.** There is no restore for PDFs, by design.
- **OCR quality bounds scan redaction.** Clean scans work well; heavily skewed, low-contrast,
  or handwritten pages produce OCR text no recognizer can match. High-stakes scanned input
  deserves a human review pass.
- **Name columns with no recognizable header** (e.g. a Sinhala `නම` header) rely purely on
  NER; `lg` reaches ~92% on Sri Lankan names there, `trf` does better. Columns headed
  *name* (any casing) are rule-masked at 100%, and a dedicated pattern now catches
  all-caps inverted names (`JAYAWARDENA, SANDUNI D`) in free text.
- **Passports and free-format IDs** (e.g. `MRN-908123`) are only caught when a known pattern
  or context applies. Add a `PatternRecognizer` for any organization-specific ID format —
  see `setup_custom_financial_matchers()` for the template.
- **NER false positives** occasionally survive (a code like `EDGE-01` tagged as a name).
  Over-masking is the safe direction, but review pseudonymized output before analytics use.
