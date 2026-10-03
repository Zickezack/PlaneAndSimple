import pytest

from flighttracker.services.settings import SettingValidationError, validate


def test_validate_rejects_value_below_minimum():
    with pytest.raises(SettingValidationError):
        validate("worker_tick_seconds", "0")


def test_validate_accepts_value_in_range():
    assert validate("worker_tick_seconds", "45") == 45


def test_validate_rejects_value_above_maximum():
    with pytest.raises(SettingValidationError):
        validate("country_default_airports", "21")


def test_validate_rejects_non_numeric_input():
    with pytest.raises(SettingValidationError):
        validate("scraper_request_delay_seconds", "soon")


def test_validate_rejects_unknown_provider():
    with pytest.raises(SettingValidationError):
        validate("flight_provider", "not-a-provider")


def test_validate_accepts_known_provider():
    assert validate("flight_provider", "mock") == "mock"


def test_validate_rejects_unknown_key():
    with pytest.raises(SettingValidationError):
        validate("not_a_real_setting", "x")


def test_validate_plain_string_field_is_passed_through():
    assert validate("wikidata_contact", "me@example.com") == "me@example.com"


def test_validate_global_currency():
    assert validate("default_currency", "CHF") == "CHF"
    with pytest.raises(SettingValidationError):
        validate("default_currency", "chf")


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "61"])
def test_validate_rejects_unusable_request_delay(raw):
    # `time.sleep(inf)` raises OverflowError, which would fail every poll job.
    with pytest.raises(SettingValidationError):
        validate("scraper_request_delay_seconds", raw)
