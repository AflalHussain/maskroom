"""Admin-editable policy overlay: exact-term deny lists, never-mask allow
lists, and custom regex rules layered on top of the locale policy. Lives in
the database (maskroom.store.policy) and is edited through the admin API or
`maskroom-admin policy import`; YAML is the interchange format for review and
version control. Never a model or network dependency, so it imports cleanly.

The overlay affects free-text and per-cell detection (anything that flows
through FinancialPrivacyEngine.analyze_text). Whole-column header shortcuts in
the tabular path are a separate policy layer and are not touched here.
"""
import hashlib
import json
import os
import re

import yaml

from presidio_analyzer import Pattern, PatternRecognizer

DEFAULT_ENTITY = "CUSTOM_TERM"
EMPTY = {"version": 1, "deny_terms": [], "allow_terms": [], "regex_rules": []}

_ENTITY_RE = re.compile(r"[A-Z][A-Z0-9_]{1,39}")
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")
# Nested quantifiers are the classic catastrophic-backtracking shape
# ((a+)+, (.*)*, (x+)* ...). Reject them rather than run untrusted regex.
_REDOS_RE = re.compile(r"\([^)]*[+*][^)]*\)[+*?]")
MAX_REGEX_LEN = 300
MIN_DENY_LEN = 3  # a 1-2 char term (a, of, id) would mask half the document


# --------------------------------------------------------------- validation
class PolicyError(ValueError):
    """A human-readable reason the submitted overlay was rejected."""


def _clean_str(v, what):
    if not isinstance(v, str):
        raise PolicyError(f"{what} must be text")
    return v.strip()


def _check_entity(v):
    v = _clean_str(v, "entity").upper()
    if not _ENTITY_RE.fullmatch(v):
        raise PolicyError(
            f"entity {v!r} must be 2-40 chars, UPPER_SNAKE_CASE (e.g. CUSTOM_TERM, EMPLOYEE_ID)")
    return v


def _compile_regex(pattern):
    """Compile a user regex, rejecting oversized or ReDoS-prone patterns."""
    pattern = _clean_str(pattern, "regex")
    if not pattern:
        raise PolicyError("regex must not be empty")
    if len(pattern) > MAX_REGEX_LEN:
        raise PolicyError(f"regex too long (max {MAX_REGEX_LEN} chars)")
    if _REDOS_RE.search(pattern):
        raise PolicyError(
            f"regex {pattern!r} has nested quantifiers that can hang the server "
            "(catastrophic backtracking); rewrite without a quantifier inside a "
            "quantified group")
    try:
        return re.compile(pattern)
    except re.error as e:
        raise PolicyError(f"invalid regex {pattern!r}: {e}")


def validate(data):
    """Return a normalized overlay dict, or raise PolicyError. Pure — does no I/O."""
    if data is None:
        return dict(EMPTY, deny_terms=[], allow_terms=[], regex_rules=[])
    if not isinstance(data, dict):
        raise PolicyError("policy must be a mapping")

    deny = []
    seen_terms = set()
    for i, row in enumerate(data.get("deny_terms") or []):
        if isinstance(row, str):
            row = {"term": row}
        if not isinstance(row, dict):
            raise PolicyError(f"deny_terms[{i}] must be a term or {{term, entity}}")
        term = _clean_str(row.get("term", ""), f"deny_terms[{i}].term")
        if len(term) < MIN_DENY_LEN:
            raise PolicyError(
                f"deny term {term!r} is too short (min {MIN_DENY_LEN} chars) — it would "
                "match far too much; use a regex rule for short structured ids instead")
        key = term.casefold()
        if key in seen_terms:
            continue  # de-dupe silently
        seen_terms.add(key)
        entity = _check_entity(row.get("entity") or DEFAULT_ENTITY)
        deny.append({"term": term, "entity": entity})

    allow = []
    seen_allow = set()
    for i, row in enumerate(data.get("allow_terms") or []):
        term = _clean_str(row, f"allow_terms[{i}]")
        if not term:
            continue
        if term.casefold() not in seen_allow:
            seen_allow.add(term.casefold())
            allow.append(term)

    regex_rules = []
    seen_names = set()
    for i, row in enumerate(data.get("regex_rules") or []):
        if not isinstance(row, dict):
            raise PolicyError(f"regex_rules[{i}] must be a mapping")
        name = _clean_str(row.get("name", ""), f"regex_rules[{i}].name")
        if not _NAME_RE.fullmatch(name):
            raise PolicyError(
                f"regex rule name {name!r} must be 1-40 chars, letters/digits/underscore")
        if name in seen_names:
            raise PolicyError(f"duplicate regex rule name {name!r}")
        seen_names.add(name)
        _compile_regex(row.get("regex", ""))  # validate; store the source string
        entity = _check_entity(row.get("entity") or DEFAULT_ENTITY)
        try:
            score = float(row.get("score", 0.85))
        except (TypeError, ValueError):
            raise PolicyError(f"regex rule {name!r}: score must be a number")
        if not 0 < score <= 1:
            raise PolicyError(f"regex rule {name!r}: score must be > 0 and <= 1")
        context = [_clean_str(c, "context word").lower()
                   for c in (row.get("context") or []) if _clean_str(c, "context word")]
        regex_rules.append({"name": name, "entity": entity, "regex": row["regex"].strip(),
                            "score": score, "context": context})

    return {"version": 1, "deny_terms": deny, "allow_terms": allow, "regex_rules": regex_rules}


# ------------------------------------------------------------------ storage
def _store():
    from .store import PolicyStore, connect
    return PolicyStore(connect())


def load(path=None):
    """The normalized overlay. With `path`: read that YAML file (the empty
    overlay if it is absent). Without: the database. A library caller with no
    database configured (a bare CLI run) gets the empty overlay and no
    database is created as a side effect."""
    if path is not None:
        if not os.path.isfile(path):
            return validate(None)
        with open(path, encoding="utf-8") as f:
            return validate(yaml.safe_load(f))
    from .store import db as _db
    if not _db.is_configured():
        return validate(None)
    return _store().load()


def save(data, path=None, user_id=None):
    """Validate then persist. With `path`: write YAML (atomic). Without: the
    database, recording a revision. Returns the normalized dict written."""
    norm = validate(data)
    if path is not None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(norm, f, sort_keys=False, allow_unicode=True)
        os.replace(tmp, path)
        return norm
    return _store().save(norm, user_id=user_id)[0]


# --------------------------------------------------------------- engine glue
def fingerprint(data):
    """Stable short hash of a normalized overlay, for engine cache keys."""
    blob = json.dumps(validate(data), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def allow_set(data):
    """Casefolded set of never-mask terms."""
    return {t.casefold() for t in validate(data).get("allow_terms", [])}


def recognizers(data):
    """PatternRecognizers for the overlay's deny terms and regex rules."""
    norm = validate(data)
    recs = []

    # Group deny terms by entity so each entity is one deny_list recognizer.
    by_entity = {}
    for row in norm["deny_terms"]:
        by_entity.setdefault(row["entity"], []).append(row["term"])
    for entity, terms in by_entity.items():
        recs.append(PatternRecognizer(
            supported_entity=entity, name=f"overlay_deny_{entity.lower()}",
            deny_list=terms))

    for row in norm["regex_rules"]:
        recs.append(PatternRecognizer(
            supported_entity=row["entity"], name=f"overlay_re_{row['name']}",
            patterns=[Pattern(row["name"], row["regex"], row["score"])],
            context=row["context"]))
    return recs
