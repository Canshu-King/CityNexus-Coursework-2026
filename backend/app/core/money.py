"""Shared minor-unit rules for Journey money values."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


ZERO_DECIMAL_CURRENCIES = frozenset({"JPY", "KRW", "VND", "IDR"})


def currency_exponent(currency: str) -> int:
    return 0 if str(currency).upper() in ZERO_DECIMAL_CURRENCIES else 2


def currency_minor_factor(currency: str) -> int:
    return 10 ** currency_exponent(currency)


def major_to_minor(amount: int | float | Decimal, currency: str) -> int:
    scaled = Decimal(str(amount)) * currency_minor_factor(currency)
    return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def minor_to_major(amount_minor: int, currency: str) -> Decimal:
    return Decimal(int(amount_minor)) / currency_minor_factor(currency)
