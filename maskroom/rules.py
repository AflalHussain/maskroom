"""Static detection policy: entity lists, column-header rules, value-profile
patterns and their validators. No model or I/O dependencies — safe to import
anywhere and easy to review as a policy document."""
import re

# Matches tokens produced by the engine, e.g. TOK_US_SSN_8B584CCF
TOKEN_RE = re.compile(r"TOK_[A-Z0-9_]+_[0-9A-F]{8,}")

# Bare numbers carry no linguistic signal, so NER entities (PERSON,
# DATE_TIME, LOCATION, ...) are meaningless for numeric cells. Only
# pattern/checksum-validated identifier types apply.
NUMERIC_CELL_ENTITIES = [
    "CREDIT_CARD", "US_BANK_NUMBER", "US_SSN", "PHONE_NUMBER",
    "IBAN_CODE", "US_ITIN", "FINANCIAL_ACCOUNT",
]
# Shortest digit count any NUMERIC_CELL_ENTITIES pattern can match.
MIN_NUMERIC_DIGITS = 8

# Column rules: when a header cell matches, every cell below it in that
# column is masked wholesale — no per-cell detection, full recall.
# Order matters (first match wins): specific identifier types before
# the broad name/address rules. Each rule is (entity, match, deny).
COLUMN_RULES = [
    ("EMAIL_ADDRESS", re.compile(r"e-?mail", re.I), None),
    ("IP_ADDRESS", re.compile(r"\bip\b.{0,4}addr", re.I), None),
    ("PHONE_NUMBER",
     re.compile(r"phone|mobile|landline|telephone|fixed line|\bcell\b"
                r"|\bfax\b|contact (no|number)", re.I), None),
    ("LK_NIC", re.compile(r"\bnic\b|national id", re.I), None),
    ("US_SSN", re.compile(r"\bssn\b|social security|tax id", re.I), None),
    ("LK_PASSPORT", re.compile(r"passport", re.I), None),
    ("FINANCIAL_ACCOUNT",
     re.compile(r"account\s*(no|number|#)|\biban\b|routing", re.I), None),
    ("DATE_TIME", re.compile(r"date of birth|\bdob\b|birth ?date", re.I), None),
    ("PERSON", re.compile(r"name\b|surname", re.I),
     re.compile(r"department|company|bank|branch|product|project|item"
                r"|sheet|file|host|business|org|test|scheme|drug|road"
                r"|street|city|country|holiday|brand|model", re.I)),
    ("LOCATION",
     re.compile(r"address|residence|home ?town|p\.?o\.? ?box|pobox", re.I), None),
]

# Presidio ships many country-specific recognizers whose checksums fire
# on arbitrary numbers (a 10-digit order id passes the UK NHS check ~10%
# of the time; US driver-licence patterns match almost anything). None
# are relevant to this deployment, so they are removed at startup.
DISABLED_ENTITIES = {
    "UK_NHS", "UK_NINO", "AU_ABN", "AU_ACN", "AU_TFN", "AU_MEDICARE",
    "SG_NRIC_FIN", "SG_UEN", "IN_PAN", "IN_AADHAAR", "IN_VEHICLE_REGISTRATION",
    "IN_VOTER", "IN_PASSPORT", "ES_NIF", "ES_NIE", "IT_FISCAL_CODE",
    "IT_DRIVER_LICENSE", "IT_VAT_CODE", "IT_PASSPORT", "IT_IDENTITY_CARD",
    "PL_PESEL", "FI_PERSONAL_IDENTITY_CODE", "US_DRIVER_LICENSE",
    "MEDICAL_LICENSE", "KR_RRN", "TH_TNIN",
}

# Values that mean "no data" — never worth tokenizing in a rule column.
NULL_MARKERS = {"", "-", "--", "n/a", "na", "none", "null", "nil"}

# Words that signal a phone number nearby. Presidio's default list lacks
# landline vocabulary and messaging cues (OTP/SMS imply a mobile).
# NOTE: Presidio matches context words as SUBSTRINGS of the context
# tokens ("ring" would match "string", "line" matches "online"), so
# only words too long/specific to hide inside other words belong here.
PHONE_CONTEXT = ["phone", "number", "telephone", "cellphone", "mobile",
                 "call", "landline", "fixed", "dial", "contact", "fax",
                 "hotline", "otp", "sms", "whatsapp", "tel"]

# Words never worth redacting on their own when propagating name parts:
# honorifics and legal/role vocabulary NER sometimes folds into a PERSON.
NAME_PROPAGATION_STOPWORDS = {
    "hon", "mr", "mrs", "ms", "dr", "prof", "rev", "attorney", "general",
    "justice", "court", "judge", "counsel", "learned", "president",
    "secretary", "chairman", "director", "officer", "petitioner",
    "respondent", "appellant", "accused", "complainant",
}

BIRTH_CONTEXT_RE = re.compile(r"\b(dob|birth|born|birthday)\b", re.IGNORECASE)

# A LOCATION span is a street-level address (identifies a household) when it
# carries a house/box number or a street-type word; a bare city, district or
# country name is an aggregation dimension, not an identifier.
ADDRESS_HINT_RE = re.compile(
    r"\d|\b(?:road|rd|street|st|lane|ln|mawatha|mw|avenue|ave|place|pl|drive|dr"
    r"|terrace|gardens?|court|crescent|close|boulevard|blvd|square|estate|watta"
    r"|pedesa|veediya|p\.?o\.? ?box|pobox|apt|apartment|flat|floor|suite|unit"
    r"|no\.?)\b", re.IGNORECASE)
# ...or when the text just before the place name is a house/box number or
# street word ("PO Box 14370 Salem", "45 Galle Road, Colombo") — NER often
# labels only the place-name part of an address.
ADDRESS_BEFORE_RE = re.compile(
    r"(?:(?:p\.?o\.? ?box|pobox|no\.?|#|apt|flat|suite|unit)\s*\d+[a-z]?(?:[/-]\d+)?"
    r"|\b\d{1,5}[a-z]?(?:/\d+)?"
    r"|\b(?:road|rd|street|st|lane|mawatha|avenue|ave|terrace|drive|place|gardens?))"
    r"\s*,?\s*$", re.IGNORECASE)


# ---------------------------------------------------------------- validators
def valid_lk_nic(v):
    """NIC embeds day-of-year (001-366, +500 for women) after the year."""
    digits = v[:-1] if v[-1] in "VvXx" else v
    day = int(digits[2:5]) if len(digits) == 9 else int(digits[4:7])
    return 1 <= day <= 366 or 501 <= day <= 866


def valid_luhn(v):
    ds = [int(c) for c in v if c.isdigit()]
    if not 13 <= len(ds) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(ds)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def valid_ssn(v):
    area, group, serial = v.split("-")
    return (area not in ("000", "666") and not area.startswith("9")
            and group != "00" and serial != "0000")


# Value-profile rules: if >= PROFILE_MIN_RATIO of a column's values match
# ONE of these high-precision patterns (with validator), the column is an
# identifier column no matter what its header says — or whether it has
# one. Covers cryptic headers ("C3"), non-English headers, and headers
# too deep to be found. Only formats distinctive enough to be safe.
# Each entry is (entity, fullmatch pattern, validator or None).
PROFILE_MIN_RATIO = 0.9
PROFILE_MIN_SAMPLES = 4
PROFILE_PATTERNS = [
    ("LK_NIC", re.compile(r"(?:\d{9}[VvXx]|(?:19|20)\d{10})"), valid_lk_nic),
    ("EMAIL_ADDRESS", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]{2,})+"), None),
    ("PHONE_NUMBER",
     re.compile(r"(?:\+94[- ]?|0)[1-9]\d[- ]?\d{3}[- ]?\d{4}|\+\d{1,3}[- ]?\d{2,4}[- ]?\d{3}[- ]?\d{3,4}"),
     None),
    ("CREDIT_CARD", re.compile(r"\d[\d -]{11,21}\d"), valid_luhn),
    ("US_SSN", re.compile(r"\d{3}-\d{2}-\d{4}"), valid_ssn),
    ("LK_PASSPORT", re.compile(r"[NDS]\d{7}"), None),
]
