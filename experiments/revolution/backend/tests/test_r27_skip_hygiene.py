from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import app.main as main_module
from app.db.models import (
    Activity,
    Journey,
    JourneyOutcomeEvent,
    PlanVersion,
    Place,
    Traveler,
)
from app.db.session import SessionLocal
from app.execution.traveler_twin import _event_signal
from app.main import _skip_current_activity
from app.replanning.provider_snapshots import (
    EXPLICIT_FROZEN_FIXTURE,
    LIVE_PROVIDER_CAPTURE,
    MobilitySnapshot,
)
from app.integrations.mobility.snapshot_capture import MobilitySnapshotCaptureResult


def _snapshot(pair: tuple[str, str], *, quality: str) -> MobilitySnapshot:
    snapshot = MobilitySnapshot.from_minutes(
        source_provider_key="r27-test-provider",
        minutes={pair: 12},
        fact_quality=quality,
        operation_key="r27-test",
    )
    fact = replace(snapshot.facts[pair], route_provenance=quality)
    return replace(snapshot, facts={pair: fact})


def _fixture(
    *,
    current_status: str,
    current_key: str = "r27-current",
    next_key: str = "r27-next",
    frozen_pair: tuple[str, str] | None = None,
) -> tuple[object, Journey, Activity, Activity, Activity]:
    session = SessionLocal()
    traveler = session.scalar(select(Traveler))
    stamp = "2026-10-04T08:00:00+00:00"
    token = uuid4().hex
    previous_key = f"r27-previous-{token}"
    current_key = f"{current_key}-{token}"
    next_key = f"{next_key}-{token}"
    if traveler is None:
        traveler = Traveler(
            id=str(uuid4()),
            display_name="R27 traveler",
            email=None,
            locale="en-US",
            timezone="UTC",
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(traveler)
        session.flush()
    journey_id = str(uuid4())
    plan_id = str(uuid4())
    previous_id = str(uuid4())
    current_id = str(uuid4())
    next_id = str(uuid4())

    journey = Journey(
        id=journey_id,
        traveler_id=traveler.id,
        title="R27 skip hygiene",
        city_code="R27",
        timezone="UTC",
        start_at=stamp,
        end_at="2026-10-04T20:00:00+00:00",
        budget_currency="CNY",
        budget_amount_minor=10000,
        status="ACTIVE",
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(journey)
    session.flush()

    plan = PlanVersion(
        id=plan_id,
        journey_id=journey_id,
        version_no=1,
        planning_mode="balanced",
        status="ACTIVE",
        source="INITIAL",
        total_cost_minor=0,
        currency="CNY",
        estimated_duration_min=120,
        mobility_evidence_json="{}",
        created_at=stamp,
    )
    session.add(plan)
    session.flush()
    journey.active_plan_version_id = plan_id

    previous = Activity(
        id=previous_id,
        plan_version_id=plan_id,
        sequence_no=1,
        activity_type="visit",
        title="Previous",
        place_id=previous_key,
        start_at=stamp,
        end_at="2026-10-04T09:00:00+00:00",
        travel_from_prev_min=10,
        estimated_cost_minor=0,
        currency="CNY",
        status="COMPLETED",
        locked=False,
        metadata_json=json.dumps({"semantic_key": previous_key}),
        created_at=stamp,
        updated_at=stamp,
    )
    current = Activity(
        id=current_id,
        plan_version_id=plan_id,
        sequence_no=2,
        activity_type="visit",
        title="Current",
        place_id=current_key,
        start_at="2026-10-04T09:15:00+00:00",
        end_at="2026-10-04T10:00:00+00:00",
        travel_from_prev_min=15,
        estimated_cost_minor=0,
        currency="CNY",
        status=current_status,
        locked=False,
        metadata_json=json.dumps({"semantic_key": current_key}),
        created_at=stamp,
        updated_at=stamp,
    )
    next_activity = Activity(
        id=next_id,
        plan_version_id=plan_id,
        sequence_no=3,
        activity_type="visit",
        title="Next",
        place_id=next_key if next_key.startswith("r27-") else None,
        city_service_id=None,
        start_at="2026-10-04T10:15:00+00:00",
        end_at="2026-10-04T11:00:00+00:00",
        travel_from_prev_min=15,
        estimated_cost_minor=0,
        currency="CNY",
        status="NOT_STARTED",
        locked=False,
        metadata_json=json.dumps({"semantic_key": next_key}),
        created_at=stamp,
        updated_at=stamp,
    )
    session.add_all(
        [
            Place(
                id=previous.place_id,
                provider_key="r27",
                external_id=previous.place_id,
                city_code="R27",
                name="Previous",
                category="park",
                latitude=39.1,
                longitude=117.1,
                opening_hours_json="{}",
                tags_json="[]",
                metadata_json="{}",
                updated_at=stamp,
            ),
            Place(
                id=current.place_id,
                provider_key="r27",
                external_id=current.place_id,
                city_code="R27",
                name="Current",
                category="park",
                latitude=39.2,
                longitude=117.2,
                opening_hours_json="{}",
                tags_json="[]",
                metadata_json="{}",
                updated_at=stamp,
            ),
            Place(
                id=next_key,
                provider_key="r27",
                external_id=next_key,
                city_code="R27",
                name="Next",
                category="park",
                latitude=39.3,
                longitude=117.3,
                opening_hours_json="{}",
                tags_json="[]",
                metadata_json="{}",
                updated_at=stamp,
            ),
        ]
    )
    session.flush()
    session.add_all([previous, current, next_activity])
    if frozen_pair is not None:
        actual_pair = (current_key, next_key)
        plan.mobility_evidence_json = json.dumps(
            _snapshot(actual_pair, quality=EXPLICIT_FROZEN_FIXTURE).to_evidence()
        )
    journey.current_activity_id = current_id
    session.commit()
    session.refresh(journey)
    session.refresh(current)
    session.refresh(next_activity)
    return session, journey, previous, current, next_activity


def _quiet_skip_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "_twin_snapshot", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main_module, "refresh_offline_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_module, "record_audit", lambda *_args, **_kwargs: None)


@pytest.mark.parametrize("current_status", ["ACTIVE", "NOT_STARTED"])
def test_r27_pre_departure_skip_uses_completed_routing_origin_and_live_override(
    monkeypatch: pytest.MonkeyPatch,
    current_status: str,
) -> None:
    _quiet_skip_side_effects(monkeypatch)
    session, journey, previous, current, next_activity = _fixture(current_status=current_status)
    pair = (
        main_module._activity_routing_key(previous),
        main_module._activity_routing_key(next_activity),
    )
    live = _snapshot(pair, quality=LIVE_PROVIDER_CAPTURE)
    monkeypatch.setattr(
        main_module,
        "capture_runtime_mobility_snapshot",
        lambda *_args, **_kwargs: MobilitySnapshotCaptureResult(live, (), ()),
    )

    try:
        _, receipt = _skip_current_activity(
            session,
            journey,
            current,
            actor_type="USER",
            reason="USER_REQUESTED_SKIP",
            mission_id="mission-r27",
            clarification_id="clarification-r27",
        )
        assert receipt["actual_edge"] == {
            "origin_key": pair[0],
            "destination_key": pair[1],
            "fact_quality": LIVE_PROVIDER_CAPTURE,
            "route_provenance": LIVE_PROVIDER_CAPTURE,
        }
        refreshed_next = session.get(Activity, next_activity.id)
        assert refreshed_next is not None
        override = json.loads(refreshed_next.metadata_json)["execution_mobility_override"]
        assert override["actual_edge"]["origin_key"] == pair[0]
        event = session.scalar(
            select(JourneyOutcomeEvent)
            .where(JourneyOutcomeEvent.activity_id == current.id)
            .order_by(JourneyOutcomeEvent.created_at.desc())
        )
        assert event is not None
        payload = json.loads(event.payload_json)
        assert payload["decision_scope"] == "CLARIFIED_HORIZON_CHOICE"
        assert payload["preference_learning_eligible"] is False
        assert "forced_by_disruption" not in payload
        assert _event_signal(session, event) is None
    finally:
        session.close()


@pytest.mark.parametrize("current_status", ["ARRIVED", "IN_PROGRESS"])
def test_r27_post_departure_skip_keeps_current_origin_and_frozen_edge(
    monkeypatch: pytest.MonkeyPatch, current_status: str
) -> None:
    _quiet_skip_side_effects(monkeypatch)
    pair = ("r27-current", "r27-next")
    session, journey, _previous, current, next_activity = _fixture(
        current_status=current_status, frozen_pair=pair
    )
    monkeypatch.setattr(
        main_module,
        "capture_runtime_mobility_snapshot",
        lambda *_args, **_kwargs: pytest.fail("unchanged post-departure edge must not recapture"),
    )

    try:
        _twin, receipt = _skip_current_activity(
            session,
            journey,
            current,
            actor_type="USER",
            reason="USER_REQUESTED_SKIP",
        )
        assert receipt["actual_edge"] is None
        assert session.get(Activity, next_activity.id).status == "ACTIVE"
    finally:
        session.close()


def test_r27_en_route_skip_fails_closed_without_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_skip_side_effects(monkeypatch)
    session, journey, _previous, current, _next_activity = _fixture(current_status="EN_ROUTE")
    try:
        with pytest.raises(HTTPException, match="REVALIDATION_REQUIRED"):
            _skip_current_activity(
                session,
                journey,
                current,
                actor_type="USER",
                reason="USER_REQUESTED_SKIP",
            )
        assert current.status == "EN_ROUTE"
        assert session.scalar(
            select(JourneyOutcomeEvent).where(JourneyOutcomeEvent.activity_id == current.id)
        ) is None
    finally:
        session.close()


def test_r27_clarified_skip_is_pre_departure_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_skip_side_effects(monkeypatch)
    session, journey, _previous, current, _next_activity = _fixture(
        current_status="ARRIVED", frozen_pair=("r27-current", "r27-next")
    )
    try:
        with pytest.raises(HTTPException, match="CLARIFIED_SKIP_ONLY_PRE_DEPART"):
            _skip_current_activity(
                session,
                journey,
                current,
                actor_type="AI_AGENT",
                reason="CLARIFIED_DEFER_FOR_LATER_OPTION",
                mission_id="mission-r27",
                clarification_id="clarification-r27",
            )
        assert current.status == "ARRIVED"
    finally:
        session.close()


def test_r27_missing_post_skip_routing_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _quiet_skip_side_effects(monkeypatch)
    session, journey, _previous, current, next_activity = _fixture(current_status="ACTIVE")
    next_activity.place_id = None
    next_activity.metadata_json = "{}"
    session.commit()
    monkeypatch.setattr(
        main_module,
        "capture_runtime_mobility_snapshot",
        lambda *_args, **_kwargs: pytest.fail("missing identity must fail before capture"),
    )
    try:
        with pytest.raises(HTTPException, match="REVALIDATION_REQUIRED"):
            _skip_current_activity(
                session,
                journey,
                current,
                actor_type="USER",
                reason="USER_REQUESTED_SKIP",
            )
        assert current.status == "ACTIVE"
    finally:
        session.close()

