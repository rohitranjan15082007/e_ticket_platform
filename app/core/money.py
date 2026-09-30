"""Integer-paise validation and display helpers.

Money never crosses this boundary as float, Decimal, or a formatted rupee string.
"""

from decimal import Decimal
from typing import TypeAlias

from app.exceptions import ValidationError

Paise: TypeAlias = int


def require_paise(value: object, *, field: str = "amount", allow_zero: bool = False) -> Paise:
    """Return a validated integer-paise amount or raise a domain validation error."""

    if isinstance(value, bool) or not isinstance(value, int):
        received = "Decimal" if isinstance(value, Decimal) else type(value).__name__
        raise ValidationError(
            "INVALID_MONEY_TYPE", f"{field} must be an integer paise value, not {received}"
        )
    minimum = 0 if allow_zero else 1
    if value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValidationError("INVALID_MONEY_AMOUNT", f"{field} must be {qualifier} integer paise")
    return value


def require_currency(value: object) -> str:
    """Validate a three-letter ISO-style currency code without accepting coercions."""

    if not isinstance(value, str):
        raise ValidationError("INVALID_CURRENCY", "currency must be a three-letter string")
    currency = value.upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValidationError("INVALID_CURRENCY", "currency must be a three-letter code")
    return currency


def require_inr_currency(value: object) -> str:
    """Validate the single currency supported by the Version 1 platform."""

    currency = require_currency(value)
    if currency != "INR":
        raise ValidationError("UNSUPPORTED_CURRENCY", "Only INR is supported in Version 1")
    return currency


def format_paise(value: object, *, currency_symbol: str = "Rs") -> str:
    """Format paise for display using only integer arithmetic."""

    paise = require_paise(value, allow_zero=True)
    whole, remainder = divmod(paise, 100)
    return f"{currency_symbol}{whole:,}.{remainder:02d}"
