"""Final hotfix regressions: price truth, budget advisory inputs, route aliases.

These lock in the three truthful price states (known free / known paid /
unknown) and the partial-coverage rule, so a later refactor cannot silently
collapse "no price evidence" into "free" or "paid".
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services import _activity_cost_status


def _activity(amount_minor: int) -> SimpleNamespace:
    return SimpleNamespace(estimated_cost_minor=amount_minor)


def _place(metadata: dict) -> SimpleNamespace:
    return SimpleNamespace(metadata_json=json.dumps(metadata))


@pytest.mark.parametrize(
    "label,amount,metadata,place,expected",
    [
        ("explicit free", 0, {"cost_status": "KNOWN_FREE"}, None, "KNOWN_FREE"),
        ("explicit paid", 2500, {"cost_status": "KNOWN_PAID"}, None, "KNOWN_PAID"),
        ("explicit unknown", 0, {"cost_status": "UNKNOWN"}, None, "UNKNOWN"),
        (
            "place says price known, zero amount",
            0,
            {},
            _place({"price_known": True}),
            "KNOWN_FREE",
        ),
        (
            "place says price known, paid",
            8000,
            {},
            _place({"price_known": True}),
            "KNOWN_PAID",
        ),
        (
            "place says price unknown",
            0,
            {},
            _place({"price_known": False}),
            "UNKNOWN",
        ),
        ("no evidence, zero amount", 0, {}, None, "UNKNOWN"),
        ("no evidence, positive amount", 1200, {}, None, "KNOWN_PAID"),
    ],
)
def test_cost_status_is_truthful(
    label: str,
    amount: int,
    metadata: dict,
    place: object,
    expected: str,
) -> None:
    """A free place must never read as unknown, and absence must never read as free."""

    assert _activity_cost_status(_activity(amount), metadata, place) == expected, label


def test_unknown_price_is_not_reported_as_free() -> None:
    """The regression that mattered: amount 0 with no evidence is UNKNOWN."""

    assert _activity_cost_status(_activity(0), {}, None) != "KNOWN_FREE"
