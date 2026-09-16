from datetime import date, timedelta

import pytest

from app import validators as v
from app.models import Sex


@pytest.mark.parametrize("raw,expected", [
    ("(512) 555-0199", "5125550199"),
    ("+1 512 555 0199", "5125550199"),
    ("512.555.0199", "5125550199"),
    ("15125550199", "5125550199"),
])
def test_phone_normalization(raw, expected):
    assert v.normalize_us_phone(raw) == expected


@pytest.mark.parametrize("raw", ["555", "512555019", "123-456-7890", "512-555-01999", "call me"])
def test_phone_rejects_invalid(raw):
    with pytest.raises(ValueError):
        v.normalize_us_phone(raw)


def test_dob_rejects_future_and_bad_dates():
    tomorrow = date.today() + timedelta(days=2)
    with pytest.raises(ValueError, match="future"):
        v.normalize_date_of_birth(tomorrow.strftime("%m/%d/%Y"))
    with pytest.raises(ValueError):
        v.normalize_date_of_birth("02/30/1990")
    with pytest.raises(ValueError):
        v.normalize_date_of_birth("01/01/1850")
    assert v.normalize_date_of_birth("1988-03-15") == date(1988, 3, 15)


@pytest.mark.parametrize("raw,ok", [
    ("O'Brien", True), ("Mary-Jane", True), ("José", True), ("De La Cruz", True),
    ("J0hn", False), ("", False), ("A" * 51, False), ("Robert; DROP TABLE", False),
])
def test_names(raw, ok):
    if ok:
        assert v.normalize_person_name(raw)
    else:
        with pytest.raises(ValueError):
            v.normalize_person_name(raw)


def test_state_zip_sex_member_id():
    assert v.normalize_state("texas") == "TX"
    assert v.normalize_state("ny") == "NY"
    with pytest.raises(ValueError):
        v.normalize_state("XX")
    assert v.normalize_zip("787011234") == "78701-1234"
    with pytest.raises(ValueError):
        v.normalize_zip("7870")
    assert v.normalize_sex("prefer not to say") is Sex.DECLINE
    assert v.normalize_member_id("xyz 123-456") == "XYZ123456"
    assert v.normalize_email("Jane.Doe@Example.com") == "jane.doe@example.com"
    with pytest.raises(ValueError):
        v.normalize_email("jane at example")
