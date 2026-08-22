"""Core engine: analyzer setup, text-level detection/filters, deterministic
tokens and the vault. File formats live in the Excel and PDF mixins."""
import hashlib
import json
import os
import re

from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

from . import recognizers, rules
from .locale import DEFAULT_LOCALE, build_policy
from .excel import ExcelMixin
from .pdf import PdfMixin


class FinancialPrivacyEngine(ExcelMixin, PdfMixin):
    # Generic policy constants (rules.py); the country-specific part is
    # compiled into self.policy from the locale file.
    TOKEN_RE = rules.TOKEN_RE
    NUMERIC_CELL_ENTITIES = rules.NUMERIC_CELL_ENTITIES
    NULL_MARKERS = rules.NULL_MARKERS
    MIN_NUMERIC_DIGITS = rules.MIN_NUMERIC_DIGITS
    PHONE_CONTEXT = rules.PHONE_CONTEXT

    def __init__(self, salt=None, min_score=0.6, entities=None, language="en",
                 nlp_model=None, dates="birth", locations="address", column_rules=True,
                 locale=DEFAULT_LOCALE):
        """
        salt:      secret used for deterministic tokens. Prefer the
                   PII_TOKEN_SALT environment variable over hardcoding.
        min_score: minimum analyzer confidence required to mask a match.
        entities:  optional list of entity types to detect (None = all).
        nlp_model: spaCy model for NER (None = Presidio's default,
                   en_core_web_lg). "en_core_web_trf" gives noticeably
                   better name recall at a higher runtime cost.
        dates:     date-masking policy. "birth" (default) masks only dates
                   in a birth context (a DOB is a strong quasi-identifier;
                   ordinary transaction/event dates stay analyzable),
                   "all" masks every detected date (HIPAA-style), "none"
                   masks no dates.
        locations: location-masking policy. "address" (default) masks only
                   street-level addresses (house/box numbers, street words)
                   and address columns; bare city/district/country names
                   stay analyzable. "all" masks every detected location,
                   "none" masks none.
        column_rules: mask whole columns from header/value-profile rules
                   (Excel only).
        locale:    country knowledge — a bundled code ("lk", "in"), a path
                   to a locale YAML, or None/"generic" for no country
                   specifics. Default from PII_LOCALE, else "lk".
        """
        if dates not in ("birth", "all", "none"):
            raise ValueError('dates must be "birth", "all", or "none"')
        if locations not in ("address", "all", "none"):
            raise ValueError('locations must be "address", "all", or "none"')
        self.dates = dates
        self.locations = locations
        self.policy = build_policy(locale)
        self.locale = self.policy.code
        self.column_rules = column_rules
        self.salt = salt or os.environ.get("PII_TOKEN_SALT", "EnterpriseRiskManagement2026")
        self.min_score = min_score
        self.entities = entities
        self.language = language
        self.vault = {}  # {token: original_value} — treat as sensitive material
        self.numeric_tokens = set()  # tokens whose source cell was numeric (fallback)
        self.numeric_cells = set()   # "Sheet!A1" cells that were numeric (authoritative)
        self.report = []  # per-run detection details: where/entity/score/text
        self._last_findings = []  # findings of the most recent analyze call
        self.analyzer_calls = 0

        if nlp_model:
            from presidio_analyzer.nlp_engine import NlpEngineProvider
            provider = NlpEngineProvider(nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": language, "model_name": nlp_model}],
            })
            self.analyzer = AnalyzerEngine(nlp_engine=provider.create_engine())
        else:
            self.analyzer = AnalyzerEngine()
        self.anonymizer = AnonymizerEngine()
        recognizers.install(self.analyzer.registry, self.policy)

    # ------------------------------------------------------------ tokens
    def generate_token(self, original_text, entity_type):
        """Deterministic, repeatable token for a value (same input -> same token)."""
        clean_text = str(original_text).strip()
        digest = hashlib.sha256((clean_text + self.salt).encode("utf-8")).hexdigest().upper()

        # Extend the hash prefix on the (rare) collision with a different value.
        length = 8
        token = f"TOK_{entity_type}_{digest[:length]}"
        while token in self.vault and self.vault[token] != clean_text:
            length += 4
            token = f"TOK_{entity_type}_{digest[:length]}"

        self.vault[token] = clean_text
        return token

    # --------------------------------------------------------- detection
    def analyze_text(self, text, entities=None, context=None):
        """
        Run the analyzer and filter out known-spurious matches. Shared by
        the Excel and PDF paths so both apply the same rules.
        """
        self.analyzer_calls += 1
        results = self.analyzer.analyze(
            text=text,
            language=self.language,
            entities=entities if entities is not None else self.entities,
            context=context,
            score_threshold=self.min_score,
        )
        # NER labels (person/place/group) on text with no letters are
        # model noise — the transformer model in particular will happily
        # tag a bare salary figure as PERSON. Pattern-based entities
        # (accounts, cards, ...) are unaffected.
        results = [
            r for r in results
            if r.entity_type not in ("PERSON", "LOCATION", "NRP")
            or any(c.isalpha() for c in text[r.start:r.end])
        ]

        # Names never contain digits: a PERSON span with a digit is a code
        # (EMP-100, WP CAB-1234, PRD-5521) that NER mistook for a name. And
        # a lone token of <=3 letters ("Max", "Pro", "Jan") standing as a
        # whole value is an abbreviation far more often than a person.
        def plausible_person(span):
            if any(c.isdigit() for c in span):
                return False
            words = span.split()
            return not (len(words) == 1 and len(words[0].strip(".,")) <= 3
                        and span.strip() == text.strip())
        results = [
            r for r in results
            if (r.entity_type != "PERSON" or plausible_person(text[r.start:r.end]))
            and not (r.entity_type == "NRP" and any(c.isdigit() for c in text[r.start:r.end]))
        ]

        # Statistical NER does not know local geography (about half of
        # Sri Lanka's towns come back as PERSON). A "name" made only
        # of known place names is a place — relabel so the location policy
        # decides. Lone field-label words ("NIC", "OTP", "Email") are not
        # names either.
        is_place = self.policy.is_place
        kept = []
        for r in results:
            if r.entity_type == "PERSON":
                span = text[r.start:r.end]
                if is_place(span):
                    r.entity_type = "LOCATION"
                elif span.strip(" .,;:'\"()").casefold() in rules.LABEL_WORDS:
                    continue
            kept.append(r)
        results = kept

        # A "person" whose final word is a role/honorific ("Hon. Attorney")
        # is a title fragment, not a name — drop it.
        results = [
            r for r in results
            if not (
                r.entity_type == "PERSON"
                and text[r.start:r.end].split()
                and text[r.start:r.end].split()[-1].strip(".,;:'\"()").lower()
                in rules.NAME_PROPAGATION_STOPWORDS
            )
        ]

        # Date policy. Dates are analytical fields, not direct identifiers,
        # so by default only birth-linked dates are masked (a DOB is a
        # classic re-identification quasi-identifier); every other date is
        # left for analysis. "all" masks every date, "none" masks none.
        # Digit-only runs (1919, 71829, a salary of 165624) are never
        # accepted as dates unless a birth context says so AND they have a
        # date-like length — NER routinely mislabels bare numbers, and
        # dropping the date label lets the real recognizer (e.g.
        # FINANCIAL_ACCOUNT) win instead.
        birth_re = rules.BIRTH_CONTEXT_RE
        ctx_birth = bool(context) and any(birth_re.search(w) for w in context)

        def keep_date(r):
            if self.dates == "none":
                return False
            snippet = text[r.start:r.end].strip()
            if snippet.isdigit():
                return ctx_birth and len(snippet) in (6, 8)
            if self.dates == "all":
                return True
            window = text[max(0, r.start - 60):min(len(text), r.end + 30)]
            return ctx_birth or bool(birth_re.search(window))

        results = [r for r in results
                   if r.entity_type != "DATE_TIME" or keep_date(r)]

        # Location policy. A city or district is an analysis dimension
        # shared by thousands of people; only a street-level address points
        # at a household. Default keeps bare place names.
        def keep_location(r):
            if self.locations == "none":
                return False
            if self.locations == "all":
                return True
            span = text[r.start:r.end]
            before = text[max(0, r.start - 40):r.start]
            return bool(self.policy.address_hint_re.search(span)
                        or self.policy.address_before_re.search(before))

        return [r for r in results
                if r.entity_type != "LOCATION" or keep_location(r)]

    def pseudonymize_text(self, text, entities=None, context=None):
        """
        Replace only the PII spans inside `text` with tokens, keeping the
        surrounding content intact. Returns (new_text, changed).

        context: extra words (e.g. a spreadsheet column header) fed to
        Presidio's context enhancer, which raises the score of ambiguous
        matches such as bare phone or account numbers.
        """
        results = self.analyze_text(text, entities=entities, context=context)
        self._last_findings = [
            {"entity": r.entity_type, "score": round(r.score, 2),
             "text": text[r.start:r.end]}
            for r in results
        ]

        # Ignore any span overlapping an existing token (makes re-runs
        # idempotent: tokens and their surroundings are never re-masked).
        token_spans = [m.span() for m in rules.TOKEN_RE.finditer(text)]
        results = [
            r for r in results
            if not any(r.start < end and r.end > start for start, end in token_spans)
        ]
        if not results:
            return text, False

        # One operator per detected entity type; the anonymizer resolves
        # overlapping matches before applying them.
        operators = {
            entity_type: OperatorConfig(
                "custom",
                {"lambda": lambda t, et=entity_type: self.generate_token(t, et)},
            )
            for entity_type in {r.entity_type for r in results}
        }
        outcome = self.anonymizer.anonymize(
            text=text, analyzer_results=results, operators=operators
        )
        return outcome.text, outcome.text != text

    def depseudonymize_text(self, text):
        """Restore original values for any vault tokens present in `text`."""
        return rules.TOKEN_RE.sub(lambda m: self.vault.get(m.group(0), m.group(0)), text)

    # ------------------------------------------------------------- vault
    # Plaintext JSON: store securely (see docs/TECHNICAL_DESIGN.md §5).
    def save_vault(self, path):
        payload = {"mappings": self.vault, "numeric_tokens": sorted(self.numeric_tokens),
                   "numeric_cells": sorted(self.numeric_cells)}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[Vault] {len(self.vault)} mappings written to: {path} (protect this file)")

    def load_vault(self, path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if "mappings" in data:
            self.vault.update(data["mappings"])
            self.numeric_tokens.update(data.get("numeric_tokens", []))
            self.numeric_cells.update(data.get("numeric_cells", []))
        else:  # legacy flat {token: value} format
            self.vault.update(data)
