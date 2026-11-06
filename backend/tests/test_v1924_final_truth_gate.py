"""V19.2.4 final truth closure regression tests.

Covers the closure acceptance matrix:

* F01/F02/F03  shared feasibility gate rejects impossible C4 timelines
* F04          a valid ARRIVAL control still passes
* R01/R02/R03  recovery projection follows durable facts
* R04          only replan-validated candidates are advertised
* D02/D03      ORM quarantine helper agrees with the schema and is idempotent
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v192_harness as H  # noqa: E402

from app.replanning.feasibility import (  # noqa: E402
    FeasibilityActivity,
    FeasibilityContext,
    evaluate_feasibility,
)
from app.replanning.provider_snapshots import MobilitySnapshot  # noqa: E402

TZ = timezone.utc
DAY = datetime(2026, 9, 16, tzinfo=TZ)
OPEN_HOURS = json.dumps({"status": "NORMALIZED", "open": "00:00", "close": "23:59"})
ARRIVAL_KEY = "v12:J:arrival-anchor"
DEPARTURE_KEY = "v12:J:departure-anchor"


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


def _lock(place_id: str, when: datetime, boundary: str, role: str) -> dict:
    return {
        "kind": "HARD",
        "category": "commitment",
        "rule_code": "LOCKED_COMMITMENT",
        "active": True,
        "params_json": json.dumps(
            {
                "place_id": place_id,
                "start_at": when.isoformat(),
                "time_boundary": boundary,
                "anchor_role": role,
            }
        ),
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
        source_provider_key="v1924-gate-test",
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


# --------------------------------------------------------------------------
# F01 — the SHARED gate rejects a persisted plan with pre-arrival activity
# --------------------------------------------------------------------------
def test_v1924_shared_gate_rejects_pre_arrival_activity() -> None:
    result = evaluate_feasibility(
        _context([_lock(ARRIVAL_KEY, _at(12, 30), "START", "ARRIVAL")], ["p", ARRIVAL_KEY]),
        [_act("p", _at(9), _at(10, 15)), _act(ARRIVAL_KEY, _at(12, 30), _at(12, 50))],
    )
    assert result.passed is False, "the shared gate must reject a pre-arrival activity"
    assert any(
        failure.startswith("C4_ACTIVITY_BEFORE_ARRIVAL") for failure in result.failures
    ), result.failures


# --------------------------------------------------------------------------
# F02 — the SHARED gate rejects a persisted plan with post-departure activity
# --------------------------------------------------------------------------
def test_v1924_shared_gate_rejects_post_departure_activity() -> None:
    result = evaluate_feasibility(
        _context(
            [_lock(DEPARTURE_KEY, _at(15, 30), "END", "DEPARTURE")],
            [DEPARTURE_KEY, "p"],
        ),
        [_act(DEPARTURE_KEY, _at(15, 10), _at(15, 30)), _act("p", _at(17), _at(18, 30))],
    )
    assert result.passed is False, "the shared gate must reject a post-departure activity"
    assert any(
        failure.startswith("C4_ACTIVITY_AFTER_DEPARTURE") for failure in result.failures
    ), result.failures


# --------------------------------------------------------------------------
# F03 — ARRIVAL after DEPARTURE is a precise conflict at the shared gate
# --------------------------------------------------------------------------
def test_v1924_shared_gate_rejects_arrival_after_departure() -> None:
    result = evaluate_feasibility(
        _context(
            [
                _lock(ARRIVAL_KEY, _at(17), "START", "ARRIVAL"),
                _lock(DEPARTURE_KEY, _at(10), "END", "DEPARTURE"),
            ],
            [ARRIVAL_KEY, DEPARTURE_KEY],
        ),
        [_act(DEPARTURE_KEY, _at(9, 40), _at(10)), _act(ARRIVAL_KEY, _at(17), _at(17, 20))],
    )
    assert result.passed is False
    assert "C4_ANCHOR_ARRIVAL_AFTER_DEPARTURE" in result.failures, result.failures


# --------------------------------------------------------------------------
# F04 — a valid ARRIVAL control still passes
# --------------------------------------------------------------------------
def test_v1924_shared_gate_accepts_valid_arrival_control() -> None:
    result = evaluate_feasibility(
        _context([_lock(ARRIVAL_KEY, _at(12, 30), "START", "ARRIVAL")], [ARRIVAL_KEY, "p"]),
        [_act(ARRIVAL_KEY, _at(12, 30), _at(12, 50)), _act("p", _at(13, 20), _at(14, 20))],
    )
    assert result.passed is True, result.failures
    assert list(result.failures) == []


# --------------------------------------------------------------------------
# F05 — an anchor that legitimately defines the journey edge is not "outside"
# --------------------------------------------------------------------------
def test_v1924_shared_gate_allows_edge_defining_departure() -> None:
    """A booked 08:00 departure on a 09:00 draft Journey is an authoritative
    edge, not an out-of-window violation.  (Semantic port of the starter patch:
    the window widening is retained for anchors only.)"""

    departure = _at(8)
    journey_start = _at(9)
    journey_end = _at(19)
    snapshot = MobilitySnapshot.from_minutes(
        source_provider_key="v1924-edge-test",
        minutes={(DEPARTURE_KEY, "p"): 0, ("p", DEPARTURE_KEY): 0},
        captured_at="2026-09-15T00:00:00+00:00",
    )
    context = FeasibilityContext(
        journey_id="J",
        city_code="CITY-DELTA",
        journey_start=journey_start,
        journey_end=journey_end,
        budget_amount_minor=1_000_000,
        budget_currency="EUR",
        constraints=tuple([_lock(DEPARTURE_KEY, departure, "END", "DEPARTURE")]),
        places={pid: _place(pid) for pid in (DEPARTURE_KEY, "p")},
        services={},
        mobility_provider=snapshot,
        plan_currency="EUR",
    )
    result = evaluate_feasibility(
        context,
        [_act(DEPARTURE_KEY, departure - timedelta(minutes=20), departure)],
    )
    assert not any(
        failure.startswith("C4_ANCHOR_OUTSIDE_JOURNEY") for failure in result.failures
    ), result.failures
    assert result.passed is True, result.failures


# --------------------------------------------------------------------------
# R01/R02 — an open Event never projects RESOLVED, and flags agree
# --------------------------------------------------------------------------
def _active_with_hard_recovery(ref: str = "V1924-R") -> str:
    journey_id = H.DEMO_JOURNEY_ID
    H.reset_demo()
    assert H.generate_plans()[0] == 201
    plans = H.list_plans()
    assert H.select_plan(journey_id, plans[0]["id"]).status_code == 200
    assert H.start_journey(journey_id).status_code == 200
    confirmed = H.confirm_journey_capabilities(
        [H.capability("STEP_FREE_REQUIRED", "HARD")]
    )
    assert confirmed.status_code == 200, confirmed.text
    return journey_id


def test_v1924_open_event_never_projects_resolved() -> None:
    from app.db.models import Event, Journey
    from app.planning.hard_recovery import recovery_flags, recovery_state
    from sqlalchemy import select

    journey_id = _active_with_hard_recovery("V1924-R01")
    with H._session() as session:
        journey = session.get(Journey, journey_id)
        # A caller may pass is_feasible=True; it must not resolve durable truth.
        projected = recovery_state(session, journey, is_feasible=True)
        flags = recovery_flags(session, journey)
        event = session.scalar(
            select(Event).where(
                Event.journey_id == journey_id,
                Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
            )
        )
        assert event is not None and event.status == "DETECTED"
        assert projected["state"] != "RESOLVED", projected
        assert flags["recovery_required"] is True
        # projection and durable truth agree
        assert projected["state"] == flags["recovery_state"]


# --------------------------------------------------------------------------
# R03 — GET /experience is a projection, not a durable transition
# --------------------------------------------------------------------------
def test_v1924_experience_get_does_not_resolve_recovery() -> None:
    from app.db.models import Event
    from sqlalchemy import select

    journey_id = _active_with_hard_recovery("V1924-R03")
    for _ in range(3):
        response = H.client().get(f"/api/v1/journeys/{journey_id}/experience")
        assert response.status_code == 200
        body = response.json()
        assert body["recovery"] is not None
        assert body["recovery"]["state"] != "RESOLVED"
        assert body["experience_flags"]["recovery_required"] is True
    with H._session() as session:
        event = session.scalar(
            select(Event).where(
                Event.journey_id == journey_id,
                Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
            )
        )
        assert event is not None, "a read must not durably resolve the Event"
        assert event.status == "DETECTED"


# --------------------------------------------------------------------------
# R04 — only replan-validated candidates are advertised
# --------------------------------------------------------------------------
def test_v1924_unvalidated_candidates_are_not_advertised() -> None:
    journey_id = _active_with_hard_recovery("V1924-R04")
    plans = H.list_plans(journey_id)
    unvalidated = [plan for plan in plans if plan["status"] == "CANDIDATE"]
    assert unvalidated, "the fixture should contain arbitrary candidate rows"

    recovery = H.experience(journey_id)["recovery"]
    assert recovery["replan_attempted"] is False
    assert recovery["candidate_plan_count"] == 0, (
        "an arbitrary CANDIDATE row is not proof of feasibility"
    )
    assert recovery["candidate_plan_ids"] == []


def test_v1924_replan_validated_candidates_are_advertised() -> None:
    journey_id = _active_with_hard_recovery("V1924-R04B")
    # A real replan attempt is what makes candidates advertisable.
    assert H.generate_plans()[0] == 422, "no feasible alternative for this HARD need"
    recovery = H.experience(journey_id)["recovery"]
    assert recovery["replan_attempted"] is True
    assert recovery["no_alternative"] is True
    assert recovery["state"] == "RECOVERY_REQUIRED_NO_ALTERNATIVE"
    assert recovery["candidate_plan_count"] == 0


# --------------------------------------------------------------------------
# R02b — a genuinely feasible replacement still resolves transactionally
# --------------------------------------------------------------------------
def test_v1924_feasible_replacement_still_resolves_recovery() -> None:
    journey_id = _active_with_hard_recovery("V1924-R02B")
    H.put_journey_capabilities([])
    assert H.confirm_journey_capabilities([]).status_code == 200
    assert H.generate_plans()[0] == 201
    original = H.journey(journey_id)["active_plan_version_id"]
    replacement = next(
        plan for plan in H.list_plans(journey_id) if plan["id"] != original
    )
    assert H.select_plan(journey_id, replacement["id"]).status_code == 200
    experience = H.experience(journey_id)
    assert experience["recovery"] is None
    assert experience["experience_flags"]["recovery_required"] is False
    assert experience["experience_flags"]["needs_review"] is False
    # reload must not resurrect it
    assert H.experience(journey_id)["recovery"] is None


# --------------------------------------------------------------------------
# D02/D03 — ORM quarantine helper agrees with the schema and is idempotent
# --------------------------------------------------------------------------
def _seed_raw_duplicates(journey_id: str, reference: str, count: int = 3) -> None:
    """Insert duplicate confirmation refs the way a legacy database could hold them."""

    from app.db.session import engine

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP INDEX IF EXISTS uq_transport_reservations_journey_confirmation"
        )
        for index in range(count):
            connection.exec_driver_sql(
                "INSERT INTO transport_reservations (id, journey_id, reservation_type, "
                "provider_key, confirmation_ref, origin_json, destination_json, "
                "scheduled_departure_at, scheduled_arrival_at, status, source_provenance, "
                "captured_at) VALUES (?, ?, 'RAIL', 'p', ?, '{}', '{}', "
                "'2026-10-10T08:00:00+08:00', '2026-10-10T12:30:00+08:00', 'CONFIRMED', "
                "'USER', ?)",
                (
                    f"{reference}-{index}",
                    journey_id,
                    reference,
                    f"2026-01-0{index + 1}T00:00:00+00:00",
                ),
            )


def test_v1924_orm_quarantine_helper_matches_schema_and_is_idempotent() -> None:
    from app.db.models import Event
    from app.operations.reservation_identity import (
        detect_duplicate_confirmation_identities,
        quarantine_duplicate_confirmation_identities,
    )
    from sqlalchemy import select

    H.reset_demo()
    journey_id = H.DEMO_JOURNEY_ID
    reference = "V1924-ORM-DUP"
    _seed_raw_duplicates(journey_id, reference)

    with H._session() as session:
        reports = detect_duplicate_confirmation_identities(session, journey_id)
        assert any(report.confirmation_ref == reference for report in reports)
        repaired = quarantine_duplicate_confirmation_identities(session, journey_id)
        session.commit()
        assert any(report.confirmation_ref == reference for report in repaired)

    with H._session() as session:
        # D02: the helper wrote a schema-valid event (no CHECK violation)
        events = session.scalars(
            select(Event).where(
                Event.payload_json.like("%RESERVATION_IDENTITY_DUPLICATE%")
            )
        ).all()
        assert events, "the repair must be audited"
        assert all(event.event_type == "USER_CHANGE" for event in events)
        payload = json.loads(events[0].payload_json)
        assert payload.get("kind") == "RESERVATION_IDENTITY_DUPLICATE"

        # D03: quarantined losers are excluded from future detection
        assert detect_duplicate_confirmation_identities(session, journey_id) == []
        assert quarantine_duplicate_confirmation_identities(session, journey_id) == []
        session.commit()

    with H._session() as session:
        again = session.scalars(
            select(Event).where(
                Event.payload_json.like("%RESERVATION_IDENTITY_DUPLICATE%")
            )
        ).all()
        assert len(again) == len(events), "a second run must not repeat the repair"


def test_v1924_reservation_confirm_flow_survives_legacy_duplicates() -> None:
    """The helper runs inside the confirmation flow and must not raise."""

    from app.operations.reservation_identity import (
        quarantine_duplicate_confirmation_identities,
    )

    H.reset_demo()
    journey_id = H.DEMO_JOURNEY_ID
    _seed_raw_duplicates(journey_id, "V1924-FLOW-DUP")
    # Must not raise an IntegrityError from the closed event_type vocabulary.
    with H._session() as session:
        quarantine_duplicate_confirmation_identities(session, journey_id)
        session.commit()
