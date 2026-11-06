from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.core.config import settings
from app.db.models import Activity, Journey, JourneyMember, Place
from app.db.session import SessionLocal
from app.traveler_principal import issue_traveler_token
from app.services import plan_authority_blocker, SYNTHETIC_LIVE_SELECTION_BLOCKED
from test_v12_authoritative_trip_execution import _journey_with_inputs, _seal_v12_test_plan


client = TestClient(main_module.app)
_TEST_SECRET = "REDACTED_FOR_PUBLIC_RELEASE"


def test_sealed_fixture_authority_only_uses_selected_content() -> None:
    def place(key: str, sealed: bool) -> Place:
        return Place(
            id=key, provider_key="overpass", external_id=f"overpass:node/{key}",
            city_code="TEST", name=key, category="museum", latitude=1, longitude=2,
            opening_hours_json='{"status":"OFFICIAL_WEB_VERIFIED","open":"09:00","close":"17:00"}',
            metadata_json=json.dumps({
                "authority_envelope": "SEALED_DEMO_FIXTURE" if sealed else None,
                "cost_evidence": {"status": "VERIFIED_PROVIDER", "amount_minor": 0},
            }), tags_json="[]", updated_at=_now(),
        )

    rows = {"a": place("a", True), "b": place("b", True), "unused": place("unused", False)}
    plan = SimpleNamespace(explanation_json="{}", mobility_evidence_json=json.dumps({
        "facts": [{"origin_key": "a", "destination_key": "b", "route_provenance": "SYNTHETIC_FIXTURE"}],
        "runtime_label": "SEALED_REPLAY_OR_SYNTHETIC_FIXTURE_EXPLICIT",
        "provider_evidence": {"selected_path": "SYNTHETIC_FIXTURE"},
    }))
    assert plan_authority_blocker(plan, rows, used_place_ids=["a", "b"]) is None
    rows["b"] = place("b", False)
    assert plan_authority_blocker(plan, rows, used_place_ids=["a", "b"]) == SYNTHETIC_LIVE_SELECTION_BLOCKED


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_secret(value: str | None) -> str | None:
    previous = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", value)
    return previous


def _start_authoritative_journey() -> tuple[str, str, str]:
    # The V12 fixture setup itself predates signed-member enforcement. Keep
    # that setup outside the R28 auth assertion, then restore strict auth for
    # the endpoint under test.
    previous = _set_secret(None)
    try:
        return _start_authoritative_journey_fixture()
    finally:
        _set_secret(previous)


def _start_authoritative_journey_fixture() -> tuple[str, str, str]:
    journey_id, place_ids, commitment_id = _journey_with_inputs()
    composed = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={
            "place_ids": place_ids,
            "candidate_count": 1,
            "reservation_id": commitment_id,
        },
    )
    assert composed.status_code == 201, composed.text
    plan = composed.json()["top_k"][0]
    _seal_v12_test_plan(plan)
    selected = client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
    )
    assert selected.status_code == 200, selected.text
    started = client.post(f"/api/v1/journeys/{journey_id}/start")
    assert started.status_code == 200, started.text
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None and journey.current_activity_id
        activity_id = str(journey.current_activity_id)
        traveler_id = str(journey.traveler_id)
    return journey_id, activity_id, traveler_id


def _ensure_owner_member(journey_id: str, traveler_id: str) -> str:
    with SessionLocal() as session:
        existing = session.scalar(
            select(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == traveler_id,
            )
        )
        if existing is not None:
            return existing.id
        now = _now()
        member = JourneyMember(
            id=str(uuid4()),
            journey_id=journey_id,
            traveler_id=traveler_id,
            role="OWNER",
            invited_by=traveler_id,
            expires_at=None,
            joined_at=now,
            removed_at=None,
            preferences_json="{}",
            created_at=now,
            updated_at=now,
        )
        session.add(member)
        session.commit()
        return member.id


def test_r28_plain_http_execution_is_durably_simulation_labeled() -> None:
    previous = _set_secret(None)
    try:
        journey_id, activity_id, _ = _start_authoritative_journey()
        departed = client.post(
            f"/api/v1/journeys/{journey_id}/activities/{activity_id}/depart"
        )
        assert departed.status_code == 200, departed.text

        with SessionLocal() as session:
            activity = session.get(Activity, activity_id)
            metadata = json.loads(activity.metadata_json)
            truth = metadata["execution_truth"]
            assert truth["kind"] == "SIMULATED_STATE_TRANSITION"
            assert truth["transition"] == "EN_ROUTE"
            assert truth["evidence"]["physical_presence_claimed"] is False
            assert truth["actor_type"] == "SIMULATION"

        twin = client.get(f"/api/v1/journeys/{journey_id}/twin")
        assert twin.status_code == 200, twin.text
        execution = twin.json()["context"]["execution"]
        assert execution["execution_truth"]["kind"] == "SIMULATED_STATE_TRANSITION"
        assert execution["now"]["execution_truth"]["kind"] == "SIMULATED_STATE_TRANSITION"
    finally:
        _set_secret(previous)


def test_r28_operator_confirmed_execution_requires_signed_member_and_explicit_confirmation() -> None:
    previous = _set_secret(_TEST_SECRET)
    try:
        # The V12 fixture resets the DB while strict auth is configured. Its
        # setup endpoints are outside the R28 member-auth boundary; the
        # execution transition below is the identity assertion under test.
        journey_id, activity_id, traveler_id = _start_authoritative_journey()
        member_id = _ensure_owner_member(journey_id, traveler_id)
        token = issue_traveler_token(traveler_id)
        route = f"/api/v1/journeys/{journey_id}/activities/{activity_id}/depart"

        missing_confirmation = client.post(
            route,
            headers={"Authorization": f"Bearer {token}"},
            json={"truth_kind": "OPERATOR_CONFIRMED_EXECUTION"},
        )
        assert missing_confirmation.status_code == 422, missing_confirmation.text

        unauthenticated = client.post(
            route,
            json={
                "truth_kind": "OPERATOR_CONFIRMED_EXECUTION",
                "operator_confirmed": True,
            },
        )
        assert unauthenticated.status_code == 401, unauthenticated.text

        confirmed = client.post(
            route,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "truth_kind": "OPERATOR_CONFIRMED_EXECUTION",
                "operator_confirmed": True,
            },
        )
        assert confirmed.status_code == 200, confirmed.text

        with SessionLocal() as session:
            activity = session.get(Activity, activity_id)
            truth = json.loads(activity.metadata_json)["execution_truth"]
            assert truth["kind"] == "OPERATOR_CONFIRMED_EXECUTION"
            assert truth["actor_id"] == traveler_id
            assert truth["evidence"]["member_id"] == member_id
            assert truth["evidence"]["operator_confirmed"] is True
            assert truth["evidence"]["physical_presence_claimed"] is False
    finally:
        _set_secret(previous)


def test_r28_foreground_web_location_cannot_claim_device_verified_execution() -> None:
    previous = _set_secret(_TEST_SECRET)
    try:
        journey_id, activity_id, traveler_id = _start_authoritative_journey()
        member_id = _ensure_owner_member(journey_id, traveler_id)
        token = issue_traveler_token(traveler_id)
        headers = {"Authorization": f"Bearer {token}"}

        with SessionLocal() as session:
            activity = session.get(Activity, activity_id)
            assert activity is not None
            place = session.get(Place, activity.place_id) if activity.place_id else None
            latitude = float(place.latitude) if place is not None else 39.14
            longitude = float(place.longitude) if place is not None else 117.18

        location = client.post(
            f"/api/v1/journeys/{journey_id}/location",
            headers=headers,
            json={
                "latitude": latitude,
                "longitude": longitude,
                "accuracy_m": 20.0,
                "captured_at": _now(),
            },
        )
        assert location.status_code == 201, location.text
        assert location.json()["principal_bound"] is True
        assert location.json()["source"] == "FOREGROUND_WEB"

        # Ordinary browser JSON cannot choose a trusted persisted source.
        forged_source = client.post(
            f"/api/v1/journeys/{journey_id}/location",
            headers=headers,
            json={
                "latitude": latitude,
                "longitude": longitude,
                "accuracy_m": 20.0,
                "captured_at": _now(),
                "source": "DEVICE_ATTESTED",
            },
        )
        assert forged_source.status_code == 422, forged_source.text

        from app.execution.presence import presence_projection

        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            presence = presence_projection(
                session, journey, viewer_member_id=member_id
            )
        assert presence["state"] == "NEAR_TARGET"
        assert presence["authority"] == "ADVISORY_FOREGROUND"
        assert presence["arrival_transition_allowed"] is False
        assert presence["distance_to_target_m"] <= 150.0

        verified = client.post(
            f"/api/v1/journeys/{journey_id}/activities/{activity_id}/depart",
            headers=headers,
            json={
                "truth_kind": "DEVICE_LOCATION_VERIFIED_EXECUTION",
                "location_observation_id": location.json()["id"],
            },
        )
        assert verified.status_code == 409, verified.text
        assert verified.json()["error"]["message"] == "LOCATION_OBSERVATION_NOT_DEVICE_VERIFIED"

        with SessionLocal() as session:
            activity = session.get(Activity, activity_id)
            assert activity.status == "ACTIVE"
            metadata = json.loads(activity.metadata_json or "{}")
            assert "execution_truth" not in metadata
    finally:
        _set_secret(previous)
