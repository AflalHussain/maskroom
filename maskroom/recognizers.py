"""Presidio recognizers: generic ones (financial accounts, OCR-tolerant
email, title-triggered and inverted all-caps names) plus the ones built
from the active locale (identifiers, phone formats, phone regions)."""
import re

from presidio_analyzer import Pattern, PatternRecognizer
from presidio_analyzer.predefined_recognizers import PhoneRecognizer

from .rules import PHONE_CONTEXT

# Honorifics and role nouns common to English-language records; a locale
# adds its own (e.g. mudaliyar, shri).
GENERIC_HONORIFICS = [
    "mr", "mrs", "ms", "miss", "dr", "hon", "rev", "prof", "officer", "clerk",
    "agent", "foreman", "manager", "supervisor", "engineer", "surveyor",
]


def generic_recognizers(honorifics=()):
    """Account numbers, loose email, and name patterns statistical NER misses."""
    account = PatternRecognizer(
        supported_entity="FINANCIAL_ACCOUNT",
        patterns=[
            # Structured ids like CHQ-4491-002: distinctive shape, high base score.
            Pattern("structured_account_pattern", r"\b[A-Z]{2,4}-\d{3,6}-\d{2,4}\b", 0.65),
            # Bare long digit runs are ambiguous (order ids, timestamps, ...).
            # Low base score: only masked when nearby context words boost it
            # past min_score via Presidio's context enhancer.
            Pattern("bare_account_number_pattern", r"\b\d{9,18}\b", 0.3),
            # Short labelled refs like EPF/ETF numbers (A/12345, E-88123).
            # Base score below threshold; only masked when column/sentence
            # context (epf, fund, account, ...) boosts it.
            Pattern("labelled_ref_pattern", r"\b[A-Z]{1,2}[/-]\d{4,6}\b", 0.3),
        ],
        context=["account", "acct", "iban", "routing", "bsb", "swift",
                 "ledger", "policy", "member", "customer",
                 "epf", "etf", "provident", "fund"],
    )

    # OCR-tolerant email fallback: the built-in recognizer validates the
    # TLD, so an OCR misread like ".Ik" for ".lk" makes it reject the
    # whole address. For masking, over-matching beats leaking.
    loose_email = PatternRecognizer(
        supported_entity="EMAIL_ADDRESS",
        patterns=[Pattern("email_loose", r"\b[\w.+-]+@[\w-]+(?:\.[\w-]{2,})+\b", 0.7)],
    )

    # A capitalized word right after an honorific or role noun is a
    # strong person signal even when NER does not know the name
    # ("overseer Jayasuriya", "Mr. Silva"). Catches names statistical
    # NER misses, which hits non-Western names hardest.
    words = sorted({*GENERIC_HONORIFICS, *honorifics}, key=len, reverse=True)
    titled_name = PatternRecognizer(
        supported_entity="PERSON",
        patterns=[Pattern(
            "role_titled_name",
            r"(?i:\b(?:" + "|".join(map(re.escape, words)) + r")\b\.?\s+)(?-i:[A-Z][\w'’-]{2,})",
            0.6)],
    )

    # Inverted all-caps names ("JAYAWARDENA, SANDUNI D") — the payroll /
    # HR export format that defeats statistical NER. Requires the comma
    # and an optional trailing initial, so ordinary caps headings don't
    # match.
    inverted_caps_name = PatternRecognizer(
        supported_entity="PERSON",
        patterns=[Pattern(
            "inverted_caps_name",
            r"\b[A-Z][A-Z'’-]{3,}(?: [A-Z][A-Z'’-]{2,})?, [A-Z][A-Z'’-]{2,}"
            r"(?: [A-Z][A-Z'’-]{2,})?(?: [A-Z]\.?)?(?=[\s,.;:)]|$)(?!, [A-Z])",
            0.6)],
        # Presidio compiles patterns with IGNORECASE by default, which
        # would turn this caps-only heuristic into "any Word, Word".
        global_regex_flags=re.DOTALL | re.MULTILINE,
    )
    return [account, loose_email, titled_name, inverted_caps_name]


def locale_recognizers(policy):
    """Identifier and phone-format recognizers declared by the locale."""
    recs = []
    for ident in policy.identifiers:
        recs.append(PatternRecognizer(
            supported_entity=ident["entity"],
            patterns=[Pattern(p["name"], p["regex"], float(p["score"]))
                      for p in ident.get("patterns") or []],
            context=ident.get("context") or [],
        ))
    if policy.phone_patterns:
        # Local phone formats. Mobile numbers are personal identifiers
        # distinctive enough to mask without context; landlines are often
        # business numbers and stay below the threshold until context (a
        # header, 'call', 'tel'...) lifts them — the locale sets the scores.
        recs.append(PatternRecognizer(
            supported_entity="PHONE_NUMBER",
            patterns=[Pattern(n, r, s) for n, r, s in policy.phone_patterns],
            context=PHONE_CONTEXT,
        ))
    return recs


def install(registry, policy):
    """Prune irrelevant built-ins, add generic + locale recognizers, and
    re-register Presidio's phone recognizer with the locale's regions."""
    for rec in list(registry.recognizers):
        if set(rec.supported_entities) <= policy.disabled_entities:
            registry.remove_recognizer(rec.name)
    for rec in generic_recognizers(policy.honorifics) + locale_recognizers(policy):
        registry.add_recognizer(rec)
    registry.remove_recognizer("PhoneRecognizer")
    registry.add_recognizer(PhoneRecognizer(supported_regions=policy.phone_regions,
                                            context=PHONE_CONTEXT))
