"""V19.2.5 C4 interior-boundary truth regression tests.

The shared deterministic feasibility gate must independently reject interior
C4 anchors that escape the structural ARRIVAL/DEPARTURE boundaries, because it
is the defence-in-depth path for imported, persisted, edited and stale plans.

ARRIVAL and DEPARTURE are structural edge markers and do not constrain
themselves; every other LOCKED_COMMITMENT activity - MID_TRIP as well as a
role-less booking/order anchor - must lie inside
[ARRIVAL completion, DEPARTURE start].
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.replanning.feasibility import (  # noqa: E402
    FeasibilityActivity,
    FeasibilityContext,
    evaluate_feasibility,
)
from app.replanning.provider_snapshots import MobilitySnapshot  # noqa: E402

TZ = timezone.utc
DAY = datetime(2026, 9, 16, tzinfo=TZ)
OPEN_HOURS = json.dumps({"status": "NORMALIZED", "open": "00:00", "close": "23:59"})


def _at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


def _place(place_id: str) -> dict:
    return {
        "id": place_id,
        "city_code": "CITY-DELTA",
        "status": "available",
        "metadata_json": json.dumps({"available": True}),
        "opening_hours_json": OPEN_HOURS,
    }


def _lock(place_id: str, when: datetime, boundary: str, role: str | None) -> dict:
    params: dict = {
        "place_id": place_id,
        "start_at": when.isoformat(),
        "time_boundary": boundary,
    }
    # A role-less lock models a booking/order C4 anchor: it promotes a concrete
    # Activity and carries no reservation role.
    if role is not None:
        params["anchor_role"] = role
    return {
        "kind": "HARD",
        "category": "commitment",
        "rule_code": "LOCKED_COMMITMENT",
        "active": True,
        "params_json": json.dumps(params),
    }


def _act(place_id: str, start: datetime, end: datetime) -> FeasibilityActivity:
    return FeasibilityActivity(
        key=place_id,
        place_id=place_id,
        start_at=start,
        end_at=end,
        cost_minor=0,
        currency="EUR",
        metadata={"semantic_key": place_id},
    )


def _context(constraints: list[dict], ids: list[str]) -> FeasibilityContext:
    snapshot = MobilitySnapshot.from_minutes(
        source_provider_key="v1925-gate-test",
        minutes={(a, b): 0 for a in ids for b in ids if a != b},
        captured_at="2026-09-15T00:00:00+00:00",
    )
    return FeasibilityContext(
        journey_id="J",
        city_code="CITY-DELTA",
        journey_start=_at(9),
        journey_end=_at(19),
        budget_amount_minor=1_000_000,
        budget_currency="EUR",
        constraints=tuple(constraints),
        places={pid: _place(pid) for pid in ids},
        services={},
        mobility_provider=snapshot,
        plan_currency="EUR",
    )


ARRIVAL = "arrival"
MID = "mid"
DEPARTURE = "departure"
BOOKING = "booking"


# --------------------------------------------------------------------------
# 1) MID_TRIP before ARRIVAL -> reject
# --------------------------------------------------------------------------
def test_v1925_mid_trip_before_arrival_is_rejected() -> None:
    result = evaluate_feasibility(
        _context(
            [
                _lock(ARRIVAL, _at(12, 30), "START", "ARRIVAL"),
                _lock(MID, _at(10), "START", "MID_TRIP"),
            ],
            [ARRIVAL, MID],
        ),
        [_act(MID, _at(10), _at(11)), _act(ARRIVAL, _at(12, 30), _at(12, 50))],
    )
    assert result.passed is False, "a MID_TRIP anchor before ARRIVAL must be rejected"
    assert any(
        failure.startswith("C4_ACTIVITY_BEFORE_ARRIVAL") for failure in result.failures
    ), result.failures


# --------------------------------------------------------------------------
# 2) MID_TRIP after DEPARTURE -> reject
# --------------------------------------------------------------------------
def test_v1925_mid_trip_after_departure_is_rejected() -> None:
    result = evaluate_feasibility(
        _context(
            [
                _lock(DEPARTURE, _at(15, 30), "END", "DEPARTURE"),
                _lock(MID, _at(17), "START", "MID_TRIP"),
            ],
            [DEPARTURE, MID],
        ),
        [_act(DEPARTURE, _at(15, 10), _at(15, 30)), _act(MID, _at(17), _at(18))],
    )
    assert result.passed is False, "a MID_TRIP anchor after DEPARTURE must be rejected"
    assert any(
        failure.startswith("C4_ACTIVITY_AFTER_DEPARTURE") for failure in result.failures
    ), result.failures


# --------------------------------------------------------------------------
# 3) role-less booking/order C4 before ARRIVAL -> reject
# --------------------------------------------------------------------------
def test_v1925_roleless_lock_before_arrival_is_rejected() -> None:
    result = evaluate_feasibility(
        _context(
            [
                _lock(ARRIVAL, _at(12, 30), "START", "ARRIVAL"),
                _lock(BOOKING, _at(10), "START", None),
            ],
            [ARRIVAL, BOOKING],
        ),
        [_act(BOOKING, _at(10), _at(11)), _act(ARRIVAL, _at(12, 30), _at(12, 50))],
    )
    assert result.passed is False, (
        "a role-less interior C4 anchor must not escape the ARRIVAL boundary"
    )
    assert any(
        failure.startswith("C4_ACTIVITY_BEFORE_ARRIVAL") for failure in result.failures
    ), result.failures


# --------------------------------------------------------------------------
# 4) valid MID_TRIP inside the boundaries -> pass
# --------------------------------------------------------------------------
def test_v1925_mid_trip_inside_boundaries_is_accepted() -> None:
    result = evaluate_feasibility(
        _context(
            [
                _lock(ARRIVAL, _at(10), "START", "ARRIVAL"),
                _lock(MID, _at(13), "START", "MID_TRIP"),
                _lock(DEPARTURE, _at(17), "END", "DEPARTURE"),
            ],
            [ARRIVAL, MID, DEPARTURE],
        ),
        [
            _act(ARRIVAL, _at(10), _at(10, 20)),
            _act(MID, _at(13), _at(14)),
            _act(DEPARTURE, _at(16, 40), _at(17)),
        ],
    )
    assert result.passed is True, result.failures
    assert list(result.failures) == []


# --------------------------------------------------------------------------
# 5) valid role-less interior lock inside the boundaries -> pass
# --------------------------------------------------------------------------
def test_v1925_roleless_lock_inside_boundaries_is_accepted() -> None:
    result = evaluate_feasibility(
        _context(
            [
                _lock(ARRIVAL, _at(10), "START", "ARRIVAL"),
                _lock(BOOKING, _at(13), "START", None),
                _lock(DEPARTURE, _at(17), "END", "DEPARTURE"),
            ],
            [ARRIVAL, BOOKING, DEPARTURE],
        ),
        [
            _act(ARRIVAL, _at(10), _at(10, 20)),
            _act(BOOKING, _at(13), _at(14)),
            _act(DEPARTURE, _at(16, 40), _at(17)),
        ],
    )
    assert result.passed is True, result.failures
    assert list(result.failures) == []


# --------------------------------------------------------------------------
# 6) structural edge anchors still do not constrain themselves
# --------------------------------------------------------------------------
def test_v1925_structural_edges_do_not_reject_themselves() -> None:
    """F05 of V19.2.4 must keep working: a booked 08:00 departure on a 09:00
    draft Journey is an authoritative edge, not an out-of-window violation."""

    from datetime import timedelta

    departure_at = _at(8)
    snapshot = MobilitySnapshot.from_minutes(
        source_provider_key="v1925-edge-test",
        minutes={(DEPARTURE, BOOKING): 0, (BOOKING, DEPARTURE): 0},
        captured_at="2026-09-15T00:00:00+00:00",
    )
    context = FeasibilityContext(
        journey_id="J",
        city_code="CITY-DELTA",
        journey_start=_at(9),
        journey_end=_at(19),
        budget_amount_minor=1_000_000,
        budget_currency="EUR",
        constraints=tuple([_lock(DEPARTURE, departure_at, "END", "DEPARTURE")]),
        places={pid: _place(pid) for pid in (DEPARTURE, BOOKING)},
        services={},
        mobility_provider=snapshot,
        plan_currency="EUR",
    )
    result = evaluate_feasibility(
        context,
        [_act(DEPARTURE, departure_at - timedelta(minutes=20), departure_at)],
    )
    assert not any(
        failure.startswith("C4_ANCHOR_OUTSIDE_JOURNEY") for failure in result.failures
    ), result.failures
    assert result.passed is True, result.failures


# --------------------------------------------------------------------------
# 7) an interior lock exactly on the boundary is inside, not outside
# --------------------------------------------------------------------------
def test_v1925_interior_lock_on_the_boundary_is_inside() -> None:
    """Boundary semantics are inclusive: an interior anchor starting exactly
    when ARRIVAL completes, and ending exactly when DEPARTURE begins, is valid."""

    from datetime import timedelta

    arrival_start = _at(10)
    arrival_end = arrival_start + timedelta(minutes=20)   # 10:20
    mid_start = arrival_end                                # exactly on the boundary
    mid_end = _at(11)                                      # 11:00
    departure_scheduled = _at(11, 20)                      # anchor spans 11:00-11:20
    departure_start = departure_scheduled - timedelta(minutes=20)  # 11:00
    ids = (ARRIVAL, MID, DEPARTURE)
    snapshot = MobilitySnapshot.from_minutes(
        source_provider_key="REDACTED_FOR_PUBLIC_RELEASE",
        minutes={(a, b): 0 for a in ids for b in ids if a != b},
        captured_at="2026-09-15T00:00:00+00:00",
    )
    context = FeasibilityContext(
        journey_id="J",
        city_code="CITY-DELTA",
        journey_start=_at(9),
        journey_end=_at(19),
        budget_amount_minor=1_000_000,
        budget_currency="EUR",
        constraints=tuple(
            [
                _lock(ARRIVAL, arrival_start, "START", "ARRIVAL"),
                _lock(MID, mid_start, "START", "MID_TRIP"),
                _lock(DEPARTURE, departure_scheduled, "END", "DEPARTURE"),
            ]
        ),
        places={pid: _place(pid) for pid in ids},
        services={},
        mobility_provider=snapshot,
        plan_currency="EUR",
    )
    result = evaluate_feasibility(
        context,
        [
            _act(ARRIVAL, arrival_start, arrival_end),
            # starts exactly when ARRIVAL completes ...
            _act(MID, mid_start, mid_end),
            # ... and ends exactly when DEPARTURE begins.
            _act(DEPARTURE, departure_start, departure_scheduled),
        ],
    )
    assert result.passed is True, result.failures
    assert mid_start == arrival_end and mid_end == departure_start
