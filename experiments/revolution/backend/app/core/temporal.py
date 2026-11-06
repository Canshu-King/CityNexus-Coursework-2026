"""Canonical Journey day/night semantics shared by planning and stay pricing."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class JourneyTemporalShape:
    """Calendar dates and overnight boundaries touched by a Journey."""

    day_count: int
    night_count: int


# Named rather than inlined in the signature so an entry point can state the same window it
# enforces (ORD-0033: the create endpoint now rejects before it persists, and the rejection
# has to carry the limit the traveller can act on).  These are product rules, not tuning
# knobs - `V13_JOURNEY_DAYS_OUT_OF_BOUNDS` is raised against them, and a gate pins the
# values so shrinking them to make a test pass cannot be silent.
MIN_JOURNEY_DAYS = 1
MAX_JOURNEY_DAYS = 7

# The window judgement is reported, never implied: a read projection has to keep rendering a
# Journey whose stored window the doors would reject today (rows created before ORD-0033),
# and "out of window" and "inverted anchors" must not collapse into a plausible number.
WINDOW_IN = "IN_WINDOW"
WINDOW_OUT = "OUT_OF_SUPPORTED_WINDOW"
WINDOW_INVALID = "INVALID_ANCHOR_ORDER"
# A naive instant and an offset-aware one cannot be ordered at all (`TypeError`, not
# `ValueError`).  That is a different statement from "the departure is before the arrival",
# and a read projection has to name it separately instead of answering 500.
WINDOW_UNCOMPARABLE = "ANCHORS_NOT_DIRECTLY_COMPARABLE"


def calendar_day_night_shape(
    arrival_at: datetime, departure_at: datetime
) -> JourneyTemporalShape:
    """The pure calendar count, carrying no window judgement.

    Split out of `derive_journey_temporal_shape` so a *read* projection can show the same
    number the pricing path used even for a Journey whose stored window the doors would
    reject.  One formula, two judgements: a second implementation of "how many days is this
    trip" is exactly the defect ORD-0063 filed against the frontend.
    """

    night_count = max(0, (departure_at.date() - arrival_at.date()).days)
    return JourneyTemporalShape(day_count=max(1, night_count + 1), night_count=night_count)


def derive_journey_temporal_shape(
    arrival_at: datetime,
    departure_at: datetime,
    *,
    min_day_count: int = MIN_JOURNEY_DAYS,
    max_day_count: int = MAX_JOURNEY_DAYS,
) -> JourneyTemporalShape:
    """Derive one calendar-date shape for every Journey consumer.

    A same-date Journey touches one calendar day and crosses no overnight
    boundary.  A departure on a later calendar date touches that many date
    boundaries plus the arrival date, so Oct 1 -> Oct 4 is 4 days / 3 nights.
    """

    if departure_at <= arrival_at:
        raise ValueError("V13_INVALID_ANCHOR_WINDOW")

    shape = calendar_day_night_shape(arrival_at, departure_at)
    if shape.day_count < min_day_count or shape.day_count > max_day_count:
        raise ValueError(f"V13_JOURNEY_DAYS_OUT_OF_BOUNDS:{shape.day_count}")
    return shape


# The reference daily allowance a traveller reads on the Today screen is their own declared
# budget divided by the *authoritative* trip shape, so the arithmetic lives here and not in
# a screen (ORD-0063).  The floor that calls an allowance "tight" is a Chinese price level
# expressed in CNY minor units, so it travels with its own currency: comparing it against a
# JPY or SGD budget - as the frontend did - rates a Tokyo trip by Shanghai prices
# (ORD-0064 found the same class of bug in the hardcoded ¥ sign).
BUDGET_DAILY_FLOOR_MINOR = 15_000
BUDGET_DAILY_FLOOR_CURRENCY = "CNY"

UNAVAILABLE_NO_BUDGET = "BUDGET_NOT_DECLARED"
UNAVAILABLE_SHAPE_UNKNOWN = "TRIP_SHAPE_UNKNOWN"
FLOOR_NOT_COMPARABLE = "FLOOR_DENOMINATED_IN_CNY"


@dataclass(frozen=True)
class BudgetReference:
    """A declared budget divided by the authoritative trip shape.

    When the number cannot be stated honestly, `available` is False and every amount is
    None, so a screen has to render "—" rather than invent a figure.  A None day count is
    never coerced to 1: that coercion is how a broken window reads as a one-day trip.
    """

    available: bool
    amount_minor: int | None
    currency: str | None
    party_size: int | None
    day_count: int | None
    per_day_minor: int | None
    per_person_per_day_minor: int | None
    floor_minor: int | None
    floor_currency: str
    floor_comparable: bool
    reason: str | None

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def _divide_round_half_up(numerator: int, denominator: int) -> int:
    """Integer division that reads the way a traveller rounds.

    Python's built-in `round` is half-to-even (`round(0.5) == 0`), so an allowance landing
    exactly on a half unit would print differently from every neighbouring amount for no
    reason the traveller could follow.
    """

    return (numerator * 2 + denominator) // (2 * denominator)


def derive_budget_reference(
    *,
    day_count: int | None,
    budget_amount_minor: int | None,
    budget_currency: str | None,
    party_size: int | None,
) -> BudgetReference:
    """State the traveller's own reference allowance, or say why it cannot be stated.

    Divides per day first, then per person, because that is the order a budget is read in:
    a trip's daily cost, then the share each traveller gets from it.
    """

    currency = str(budget_currency or "").strip().upper() or None
    amount = budget_amount_minor if isinstance(budget_amount_minor, int) else None
    if day_count is None:
        return BudgetReference(
            available=False, amount_minor=amount, currency=currency, party_size=party_size,
            day_count=None, per_day_minor=None, per_person_per_day_minor=None,
            floor_minor=None, floor_currency=BUDGET_DAILY_FLOOR_CURRENCY,
            floor_comparable=False, reason=UNAVAILABLE_SHAPE_UNKNOWN,
        )
    if amount is None or amount <= 0:
        return BudgetReference(
            available=False, amount_minor=amount, currency=currency,
            party_size=max(1, party_size or 1), day_count=day_count,
            per_day_minor=None, per_person_per_day_minor=None,
            floor_minor=BUDGET_DAILY_FLOOR_MINOR, floor_currency=BUDGET_DAILY_FLOOR_CURRENCY,
            floor_comparable=False, reason=UNAVAILABLE_NO_BUDGET,
        )
    party = max(1, party_size or 1)
    per_day = _divide_round_half_up(amount, day_count)
    per_person_per_day = _divide_round_half_up(per_day, party)
    return BudgetReference(
        available=True, amount_minor=amount, currency=currency, party_size=party,
        day_count=day_count, per_day_minor=per_day,
        per_person_per_day_minor=per_person_per_day,
        floor_minor=BUDGET_DAILY_FLOOR_MINOR, floor_currency=BUDGET_DAILY_FLOOR_CURRENCY,
        # The floor is only a verdict when the budget is measured in the same money.
        floor_comparable=currency == BUDGET_DAILY_FLOOR_CURRENCY,
        reason=None if currency == BUDGET_DAILY_FLOOR_CURRENCY else FLOOR_NOT_COMPARABLE,
    )
