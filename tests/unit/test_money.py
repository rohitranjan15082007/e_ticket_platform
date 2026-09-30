"""Money and currency primitives must reject coercion and unsupported values."""

from decimal import Decimal

import pytest

from app.core.money import format_paise, require_inr_currency, require_paise
from app.exceptions import ValidationError


@pytest.mark.parametrize("value", [1.0, Decimal("1.00"), True, "100"])
def test_fractional_or_coerced_money_types_are_rejected(value: object) -> None:
    with pytest.raises(ValidationError, match="INVALID_MONEY_TYPE"):
        require_paise(value)


def test_paise_is_validated_and_formatted_with_integer_arithmetic() -> None:
    assert require_paise(12_345) == 12_345
    assert require_paise(0, allow_zero=True) == 0
    assert format_paise(12_345) == "Rs123.45"
    assert format_paise(123_456_789) == "Rs1,234,567.89"


@pytest.mark.parametrize("value", [-1, 0])
def test_non_positive_paise_is_rejected_for_mutations(value: int) -> None:
    with pytest.raises(ValidationError, match="INVALID_MONEY_AMOUNT"):
        require_paise(value)


@pytest.mark.parametrize("value", ["USD", "IN", "INRR", "12R", 356, None])
def test_only_inr_is_accepted_for_version_one(value: object) -> None:
    with pytest.raises(ValidationError):
        require_inr_currency(value)


def test_inr_currency_normalizes_case_without_coercing_non_strings() -> None:
    assert require_inr_currency("inr") == "INR"
