"""Pure validation + normalization functions for patient fields.

This is the single source of truth for field rules. It is used by:
  * the Pydantic request schemas (REST API), and
  * the voice agent's tools (field-by-field validation during the call).

Every function takes raw input, returns a normalized value, or raises
ValueError with a short, caller-friendly message. Messages are written so the
voice agent can paraphrase them directly ("that phone number needs 10 digits").
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from email_validator import EmailNotValidError, validate_email

from app.config import get_settings
from app.models import Sex

MIN_DOB = date(1900, 1, 1)

US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    # Territories with USPS codes
    "AS": "American Samoa", "GU": "Guam", "MP": "Northern Mariana Islands",
    "PR": "Puerto Rico", "VI": "U.S. Virgin Islands",
}
_STATE_NAME_TO_CODE = {name.lower().replace(".", ""): code for code, name in US_STATES.items()}
_STATE_NAME_TO_CODE.update({"washington dc": "DC", "washington d c": "DC", "virgin islands": "VI"})

_SEX_ALIASES: dict[str, Sex] = {
    "male": Sex.MALE, "m": Sex.MALE, "man": Sex.MALE, "masculino": Sex.MALE, "hombre": Sex.MALE,
    "female": Sex.FEMALE, "f": Sex.FEMALE, "woman": Sex.FEMALE, "femenino": Sex.FEMALE, "mujer": Sex.FEMALE,
    "other": Sex.OTHER, "otro": Sex.OTHER, "non-binary": Sex.OTHER, "nonbinary": Sex.OTHER, "intersex": Sex.OTHER,
    "decline to answer": Sex.DECLINE, "decline": Sex.DECLINE, "prefer not to say": Sex.DECLINE,
    "prefer not to answer": Sex.DECLINE, "declined": Sex.DECLINE, "no answer": Sex.DECLINE,
}

# Letters (any script, so "José" and "Zoë" work) separated by single spaces, hyphens or apostrophes.
_NAME_RE = re.compile(r"^[^\W\d_]+(?:[ '\-][^\W\d_]+)*$", re.UNICODE)
_FULL_NAME_RE = re.compile(r"^[^\W\d_]+\.?(?:[ '\-][^\W\d_]+\.?)*$", re.UNICODE)
_CITY_RE = re.compile(r"^[^\W\d_]+(?:[ '\-.]{1,2}[^\W\d_]+)*\.?$", re.UNICODE)
_PHONE_ALLOWED_RE = re.compile(r"^[\d\s()+.\-]+$")
_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
_MEMBER_ID_RE = re.compile(r"^[A-Z0-9]{3,30}$")
_LANGUAGE_RE = re.compile(r"^[^\W\d_]+(?:[ \-][^\W\d_]+)*$", re.UNICODE)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def clean_text(value: Any) -> str:
    """Basic sanitization: must be a string, strip control chars, collapse whitespace."""
    if value is None:
        raise ValueError("is required.")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise ValueError("must be text.")
    value = unicodedata.normalize("NFC", value)
    value = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C" or ch in " \t")
    value = value.replace("’", "'").replace("‘", "'")  # curly apostrophes from STT
    return re.sub(r"\s+", " ", value).strip()


def _require_length(value: str, label: str, min_len: int, max_len: int) -> str:
    if len(value) < min_len:
        raise ValueError(f"{label} is required." if min_len == 1 else f"{label} must be at least {min_len} characters.")
    if len(value) > max_len:
        raise ValueError(f"{label} must be {max_len} characters or fewer.")
    return value


def today_in(tz_name: str = "America/New_York") -> date:
    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:  # pragma: no cover - bad tz config
        return date.today()


# ---------------------------------------------------------------------------
# field normalizers
# ---------------------------------------------------------------------------

def normalize_person_name(value: Any, label: str = "Name") -> str:
    value = _require_length(clean_text(value), label, 1, 50)
    if not _NAME_RE.match(value):
        raise ValueError(f"{label} can only contain letters, hyphens, apostrophes and spaces.")
    return value


def normalize_date_of_birth(value: Any, today: date | None = None) -> date:
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        text = clean_text(value)
        parsed = None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
            try:
                parsed = datetime.strptime(text, fmt).date()
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError("Date of birth must be a real date in MM/DD/YYYY format.")
    today = today or today_in(get_settings().clinic_timezone)
    if parsed > today:
        raise ValueError("Date of birth can't be in the future.")
    if parsed < MIN_DOB:
        raise ValueError("Date of birth must be after January 1, 1900.")
    return parsed


def normalize_sex(value: Any) -> Sex:
    if isinstance(value, Sex):
        return value
    text = clean_text(value).lower()
    if text in _SEX_ALIASES:
        return _SEX_ALIASES[text]
    raise ValueError("Sex must be one of: Male, Female, Other, or Decline to Answer.")


def normalize_us_phone(value: Any, label: str = "Phone number") -> str:
    text = clean_text(value)
    if not text:
        raise ValueError(f"{label} is required.")
    if not _PHONE_ALLOWED_RE.match(text):
        raise ValueError(f"{label} can only contain digits.")
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError(f"{label} must be a 10-digit U.S. number (I heard {len(digits)} digits).")
    if digits[0] in "01":
        raise ValueError(f"{label} has an invalid area code; U.S. area codes can't start with 0 or 1.")
    return digits


def normalize_email(value: Any) -> str:
    text = clean_text(value).replace(" ", "")
    try:
        return validate_email(text, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        raise ValueError("Email address doesn't look valid (expected something like name@example.com).") from None


def normalize_address_line(value: Any, label: str = "Street address", max_len: int = 200) -> str:
    value = _require_length(clean_text(value), label, 1, max_len)
    if not re.search(r"[A-Za-z0-9]", value):
        raise ValueError(f"{label} must contain letters or numbers.")
    if re.search(r"[<>{}\[\]\\;]", value):
        raise ValueError(f"{label} contains characters that aren't allowed.")
    return value


def normalize_city(value: Any) -> str:
    value = _require_length(clean_text(value), "City", 1, 100)
    if not _CITY_RE.match(value):
        raise ValueError("City can only contain letters, spaces, hyphens, periods and apostrophes.")
    return value


def normalize_state(value: Any) -> str:
    text = clean_text(value)
    code = text.upper().replace(".", "")
    if code in US_STATES:
        return code
    by_name = _STATE_NAME_TO_CODE.get(text.lower().replace(".", ""))
    if by_name:
        return by_name
    raise ValueError("State must be a valid U.S. state, like TX or California.")


def normalize_zip(value: Any) -> str:
    text = clean_text(value).replace(" ", "")
    if re.fullmatch(r"\d{9}", text):
        text = f"{text[:5]}-{text[5:]}"
    if not _ZIP_RE.match(text):
        raise ValueError("ZIP code must be 5 digits, or ZIP+4 like 12345-6789.")
    return text


def normalize_insurance_provider(value: Any) -> str:
    value = _require_length(clean_text(value), "Insurance provider", 1, 100)
    if re.search(r"[<>{}\[\]\\;]", value):
        raise ValueError("Insurance provider contains characters that aren't allowed.")
    return value


def normalize_member_id(value: Any) -> str:
    text = re.sub(r"[\s\-]", "", clean_text(value)).upper()
    if not _MEMBER_ID_RE.match(text):
        raise ValueError("Insurance member ID must be 3 to 30 letters and numbers.")
    return text


def normalize_language(value: Any) -> str:
    text = _require_length(clean_text(value), "Preferred language", 1, 50)
    aliases = {"espanol": "Spanish", "español": "Spanish", "castellano": "Spanish", "ingles": "English", "inglés": "English"}
    text = aliases.get(text.lower(), text)
    if not _LANGUAGE_RE.match(text):
        raise ValueError("Preferred language should be a language name, like English or Spanish.")
    return text.title()


def normalize_contact_name(value: Any) -> str:
    value = _require_length(clean_text(value), "Emergency contact name", 1, 100)
    if not _FULL_NAME_RE.match(value):
        raise ValueError("Emergency contact name can only contain letters, spaces, hyphens and apostrophes.")
    return value


# Field name -> normalizer. Drives both the API schemas and the voice tools.
FIELD_NORMALIZERS = {
    "first_name": lambda v: normalize_person_name(v, "First name"),
    "last_name": lambda v: normalize_person_name(v, "Last name"),
    "date_of_birth": normalize_date_of_birth,
    "sex": normalize_sex,
    "phone_number": lambda v: normalize_us_phone(v, "Phone number"),
    "email": normalize_email,
    "address_line_1": lambda v: normalize_address_line(v, "Street address", 200),
    "address_line_2": lambda v: normalize_address_line(v, "Apartment or unit", 100),
    "city": normalize_city,
    "state": normalize_state,
    "zip_code": normalize_zip,
    "insurance_provider": normalize_insurance_provider,
    "insurance_member_id": normalize_member_id,
    "preferred_language": normalize_language,
    "emergency_contact_name": normalize_contact_name,
    "emergency_contact_phone": lambda v: normalize_us_phone(v, "Emergency contact phone"),
}

REQUIRED_FIELDS = (
    "first_name", "last_name", "date_of_birth", "sex", "phone_number",
    "address_line_1", "city", "state", "zip_code",
)
OPTIONAL_FIELDS = tuple(f for f in FIELD_NORMALIZERS if f not in REQUIRED_FIELDS)


def field_label(field: str) -> str:
    return field.replace("_", " ").capitalize()


def normalize_field(field: str, value: Any) -> Any:
    """Run the normalizer for `field`, making sure every error message names the field."""
    if field not in FIELD_NORMALIZERS:
        raise ValueError(f"Unknown field '{field}'.")
    try:
        return FIELD_NORMALIZERS[field](value)
    except ValueError as exc:
        message = str(exc)
        if message[:1].islower():  # generic helper messages like "must be text."
            message = f"{field_label(field)} {message}"
        raise ValueError(message) from None
