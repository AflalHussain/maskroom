"""Custom Presidio recognizers: financial account formats, OCR-tolerant
email, title/role-triggered and inverted all-caps person names, and Sri
Lankan identifiers (NIC, passport, phone formats)."""
import re

from presidio_analyzer import Pattern, PatternRecognizer
from presidio_analyzer.predefined_recognizers import PhoneRecognizer

from .rules import PHONE_CONTEXT


def financial_recognizers():
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
    titled_name = PatternRecognizer(
        supported_entity="PERSON",
        patterns=[Pattern(
            "role_titled_name",
            r"(?i:\b(?:mr|mrs|ms|miss|dr|hon|rev|prof|overseer|inspector|"
            r"officer|constable|sergeant|clerk|agent|foreman|headman|"
            r"mudaliyar|arachchi|manager|supervisor|engineer|surveyor)"
            r"\b\.?\s+)(?-i:[A-Z][\w'’-]{2,})",
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


def sri_lanka_recognizers():
    """Recognizers for Sri Lankan identifier formats."""
    nic = PatternRecognizer(
        supported_entity="LK_NIC",
        patterns=[
            # Old NIC: 9 digits + V/X suffix — distinctive on its own.
            Pattern("lk_nic_old", r"\b\d{9}[VvXx]\b", 0.8),
            # New NIC: 12 digits starting with the birth year. Ambiguous
            # with other long numbers, so it needs context to pass.
            Pattern("lk_nic_new", r"\b(?:19|20)\d{10}\b", 0.45),
        ],
        context=["nic", "national", "identity"],
    )
    passport = PatternRecognizer(
        supported_entity="LK_PASSPORT",
        # N/D/S prefix + 7 digits; too generic alone, requires context.
        patterns=[Pattern("lk_passport", r"\b[NDS]\d{7}\b", 0.3)],
        context=["passport", "travel"],
    )
    # Sri Lankan phone formats. A formatted MOBILE (07X / +94 7X) is a
    # personal identifier distinctive enough to mask without context;
    # landlines are often business numbers, so they stay below the
    # threshold until context (a header, 'call', 'tel'...) lifts them.
    lk_phone = PatternRecognizer(
        supported_entity="PHONE_NUMBER",
        patterns=[
            Pattern("lk_mobile",
                    r"(?<!\d)(?:\+94[- ]?|0)7\d[- ]?\d{3}[- ]?\d{4}(?!\d)", 0.65),
            Pattern("lk_landline",
                    r"(?<!\d)(?:\+94[- ]?|0)[1-9]\d[- ]?\d{3}[- ]?\d{4}(?!\d)", 0.45),
        ],
        context=PHONE_CONTEXT,
    )
    return [nic, passport, lk_phone]


def phone_recognizer():
    """Presidio's phonenumbers-backed recognizer with Sri Lanka included,
    so +94 / 0XX-XXXXXXX formats validate."""
    return PhoneRecognizer(supported_regions=("LK", "US", "GB", "IN"),
                           context=PHONE_CONTEXT)


def install(registry):
    """Prune irrelevant built-ins and add the custom recognizers."""
    from .rules import DISABLED_ENTITIES
    for rec in list(registry.recognizers):
        if set(rec.supported_entities) <= DISABLED_ENTITIES:
            registry.remove_recognizer(rec.name)
    for rec in financial_recognizers() + sri_lanka_recognizers():
        registry.add_recognizer(rec)
    registry.remove_recognizer("PhoneRecognizer")
    registry.add_recognizer(phone_recognizer())
