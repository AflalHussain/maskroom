"""Locale loading: country-specific detection knowledge (identifier formats,
phone formats, honorifics, address vocabulary, place gazetteer) comes from a
YAML file in `maskroom/locales/` or any path, and is compiled into a
`Policy` the engine and file pipelines read instead of module constants."""
import os
import re
from dataclasses import dataclass, field

import yaml

from . import rules

LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
DEFAULT_LOCALE = os.environ.get("PII_LOCALE", "lk")


def available_locales():
    """{code: name} for every bundled locale file (template excluded)."""
    out = {}
    for fn in sorted(os.listdir(LOCALES_DIR)):
        if fn.endswith(".yaml") and not fn.startswith("_"):
            with open(os.path.join(LOCALES_DIR, fn), encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            out[data.get("code", fn[:-5])] = data.get("name", fn[:-5])
    return out


def load_locale_data(name_or_path):
    """Raw dict from a bundled code ("lk") or a YAML path. None -> generic."""
    if not name_or_path or name_or_path in ("none", "generic"):
        return {"code": "generic", "name": "Generic"}
    path = name_or_path
    if not os.path.isfile(path):
        path = os.path.join(LOCALES_DIR, f"{name_or_path}.yaml")
    if not os.path.isfile(path):
        raise ValueError(f"unknown locale {name_or_path!r}; bundled: "
                         f"{', '.join(available_locales())} or give a YAML path")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("code", os.path.splitext(os.path.basename(path))[0])
    data.setdefault("name", data["code"])
    return data


@dataclass
class Policy:
    """Compiled detection policy = generic rules + one locale."""
    code: str
    name: str
    phone_regions: tuple
    disabled_entities: set
    identifiers: list                 # raw locale identifier dicts (for recognizers)
    phone_patterns: list              # [(name, regex, score)]
    honorifics: list
    column_rules: list                # [(entity, compiled, deny)]
    profile_patterns: list            # [(entity, compiled fullmatch, validator)]
    places: set
    address_hint_re: re.Pattern
    address_before_re: re.Pattern
    keep_column_re: re.Pattern = rules.KEEP_COLUMN_RE
    null_markers: set = field(default_factory=lambda: rules.NULL_MARKERS)

    def is_place(self, span):
        s = span.casefold().strip(" .,;:'\"()")
        s = re.sub(r"\s*\d+$", "", s)  # "Colombo 03"
        if s in self.places:
            return True
        words = rules.PLACE_TOKEN_RE.findall(s)
        return bool(words) and all(w in self.places for w in words)


def _validator(name):
    if not name:
        return None
    try:
        return rules.VALIDATORS[name]
    except KeyError:
        raise ValueError(f"unknown validator {name!r}; known: {', '.join(rules.VALIDATORS)}")


def build_policy(name_or_path=DEFAULT_LOCALE):
    d = load_locale_data(name_or_path)
    identifiers = d.get("identifiers") or []

    # Column rules: locale identifiers go before the generic list so a
    # locale "passport"/"national id" header wins over the generic entity.
    column_rules = []
    for ident in identifiers:
        if ident.get("column_header"):
            column_rules.append((ident["entity"], re.compile(ident["column_header"], re.I), None))
    column_rules += rules.COLUMN_RULES

    profile = []
    for ident in identifiers:
        p = ident.get("profile")
        if p:
            profile.append((ident["entity"], re.compile(p["regex"]), _validator(p.get("validator"))))
    phone_regexes = [d["phone_profile_regex"]] if d.get("phone_profile_regex") else []
    phone_regexes.append(rules.INTL_PHONE_PROFILE)
    profile.append(("PHONE_NUMBER", re.compile("|".join(f"(?:{r})" for r in phone_regexes)), None))
    profile += rules.PROFILE_PATTERNS

    address_words = list(rules.ADDRESS_WORDS) + [w.lower() for w in d.get("address_words") or []]
    words_alt = "|".join(sorted(map(re.escape, set(address_words)), key=len, reverse=True))
    address_hint_re = re.compile(r"\d|\b(?:%s)\b" % words_alt, re.I)
    address_before_re = re.compile(
        r"(?:(?:p\.?o\.? ?box|pobox|no\.?|#|apt|flat|suite|unit)\s*\d+[a-z]?(?:[/-]\d+)?"
        r"|\b\d{1,5}[a-z]?(?:/\d+)?"
        r"|\b(?:%s))\s*,?\s*$" % words_alt, re.I)

    return Policy(
        code=d["code"], name=d["name"],
        phone_regions=tuple(d.get("phone_regions") or ("US", "GB")),
        disabled_entities=set(rules.DISABLED_ENTITIES) - set(d.get("enable_builtin") or []),
        identifiers=identifiers,
        phone_patterns=[(p["name"], p["regex"], float(p["score"])) for p in d.get("phone_patterns") or []],
        honorifics=[h.lower() for h in d.get("honorifics") or []],
        column_rules=column_rules,
        profile_patterns=profile,
        places={p.lower() for p in d.get("places") or []},
        address_hint_re=address_hint_re,
        address_before_re=address_before_re,
    )
