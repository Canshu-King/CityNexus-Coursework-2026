from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from demo_stage import demo_reset_headers
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.execution.member_place_invitation as member_place_invitation_module
import app.execution.member_side_quest as member_side_quest_module
import app.main as main_module
from app.core.config import settings
from app.db.models import (
    ActionApproval,
    Activity,
    AgentRun,
    AuditLog,
    Booking,
    Commitment,
    Event,
    GuardianPulse,
    Journey,
    JourneyIntent,
    JourneyMember,
    LocationObservation,
    MemberLiveMomentScoutReceipt,
    MemberPlaceInvitation,
    MemberPlaceInvitationProgress,
    MemberPlaceInvitationRecipient,
    MemberPlacePromotion,
    SharedStopCoPlanningGrant,
    MemberSideQuest,
    MemberSideQuestReturnReport,
    MobilityFact,
    Order,
    PaymentTransaction,
    Place,
    PlannedSplitMeetupHandshake,
    PlanVersion,
    ProviderCall,
    RealPlaceCandidate,
    RecoveryEpoch,
    ReplanProposal,
    RouteAcquisitionOperation,
    Traveler,
)
from app.db.session import SessionLocal
from app.execution.clock import execution_clock
from app.integrations.mobility import runtime as mobility_runtime
from app.traveler_principal import issue_traveler_token

client = TestClient(main_module.app)
TEST_SECRET = "r64-member-side-quest-test-secret-0123456789"


@pytest.fixture(autouse=True)
def _signed_traveler_tokens():
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    try:
        yield
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


@pytest.fixture(autouse=True)
def _restore_live_place_provider_settings():
    previous_enabled = settings.real_city_enabled
    previous_mode = settings.real_city_mode
    try:
        yield
    finally:
        object.__setattr__(settings, "real_city_enabled", previous_enabled)
        object.__setattr__(settings, "real_city_mode", previous_mode)


def _auth(traveler_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_traveler_token(traveler_id)}"}


def _find_promotion(payload: object, promotion_id: str) -> dict:
    if isinstance(payload, dict):
        if payload.get("promotion_id") == promotion_id:
            return payload
        for value in payload.values():
            try:
                return _find_promotion(value, promotion_id)
            except LookupError:
                continue
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            try:
                return _find_promotion(value, promotion_id)
            except LookupError:
                continue
    raise LookupError(f"promotion {promotion_id} was not present in the response")


def _active_shared_journey() -> tuple[str, str, str]:
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, reset.text
    journey_id = reset.json()["journey_id"]
    plans = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate",
        json={"candidate_count": 1},
    )
    assert plans.status_code == 201, plans.text
    plan_id = plans.json()[0]["id"]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select",
        headers=_auth(str(reset.json()["traveler_id"])),
    ).status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    now = datetime.now(timezone.utc).isoformat()
    other_id = str(uuid4())
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        owner_id = str(journey.traveler_id)
        session.add(Traveler(
            id=other_id,
            display_name="R64同行者",
            email=None,
            locale="zh-CN",
            timezone="UTC",
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        session.add(JourneyMember(
            id=str(uuid4()),
            journey_id=journey_id,
            traveler_id=other_id,
            role="COLLABORATOR",
            invited_by=owner_id,
            expires_at=None,
            joined_at=now,
            removed_at=None,
            preferences_json="{}",
            created_at=now,
            updated_at=now,
        ))
        session.commit()
    started = client.post(
        f"/api/v1/journeys/{journey_id}/operator/start",
        headers=_auth(owner_id),
        json={"interval_seconds": 3600},
    )
    assert started.status_code == 200, started.text
    return journey_id, owner_id, other_id


def _start_split(journey_id: str, traveler_id: str) -> dict:
    response = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        headers=_auth(traveler_id),
        json={
            "command": "I want to go alone for a while and meet at the next shared stop.",
            "requested_autonomy": "PLAN_ONLY",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()["planned_separation_result"]
    assert result["outcome"] == "PLANNED_SPLIT_CREATED"
    return result


def _private_place(journey_id: str, *excluded_place_ids: str | None) -> Place:
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        excluded = {str(value) for value in excluded_place_ids if value}
        excluded.update(
            str(value)
            for value in session.scalars(select(Activity.place_id).where(
                Activity.plan_version_id == journey.active_plan_version_id,
                Activity.place_id.is_not(None),
            )).all()
        )
        places = session.scalars(
            select(Place)
            .where(Place.city_code == journey.city_code)
            .order_by(Place.name, Place.id)
        ).all()
        place = next((item for item in places if item.id not in excluded), None)
        assert place is not None, "the demo city needs at least one known side destination"
        return place


def _count(model, *criteria) -> int:
    with SessionLocal() as session:
        return int(session.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)



def test_r66_dense_catalog_cap_preserves_nearest_even_if_older(monkeypatch):
    # Bounded shortlist must not be newest-records-first: older nearby venues
    # are valid discoveries when a dense city has many newer, farther rows.
    import app.execution.moment_scout as scout_module

    journey_id, _owner_id, _other_id = _active_shared_journey()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        nearby_id, farther_id = str(uuid4()), str(uuid4())
        common = {
            "city_code": journey.city_code,
            "category": "cafe",
            "opening_hours_json": "{}",
            "tags_json": "[]",
            "metadata_json": "{}",
            "coordinate_system": "WGS84",
        }
        session.add_all([
            Place(
                id=nearby_id,
                provider_key="r66-locality-regression",
                external_id=nearby_id,
                name="Older nearest venue",
                latitude=70.0,
                longitude=30.0,
                updated_at="2000-01-01T00:00:00+00:00",
                **common,
            ),
            Place(
                id=farther_id,
                provider_key="r66-locality-regression",
                external_id=farther_id,
                name="Newer but distant venue",
                latitude=70.04,
                longitude=30.0,
                updated_at="2100-01-01T00:00:00+00:00",
                **common,
            ),
        ])
        session.commit()
        monkeypatch.setattr(scout_module, "MAX_CITY_SHORTLIST_ROWS", 1)
        rows = scout_module._bounded_place_query(
            session,
            city_code=journey.city_code,
            latitude=70.0,
            longitude=30.0,
        )
        assert [row.id for row in rows] == [nearby_id]


def test_r66_moment_scout_is_private_read_only_and_revalidates_explicit_selection():
    journey_id, owner_id, other_id = _active_shared_journey()
    place = _private_place(journey_id)
    # Put one fixture-backed known Place at the consented sample so ranking has
    # a deterministic nearest result without asking an external provider.
    with SessionLocal() as session:
        place = session.get(Place, place.id)
        assert place is not None
        place.latitude = 35.123456
        place.longitude = 135.123456
        place.category = "cafe"
        place.provider_key = "r66-fixture"
        session.commit()

    sample = client.post(
        f"/api/v1/journeys/{journey_id}/location",
        headers=_auth(owner_id),
        json={
            "latitude": 35.123456,
            "longitude": 135.123456,
            "accuracy_m": 8,
            "captured_at": execution_clock.now().astimezone(timezone.utc).isoformat(),
        },
    )
    assert sample.status_code == 201, sample.text
    observation_id = sample.json()["id"]
    with SessionLocal() as session:
        other_member = session.scalar(
            select(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == other_id,
            )
        )
        assert other_member is not None
        foreign_observation_id = str(uuid4())
        session.add(LocationObservation(
            id=foreign_observation_id,
            journey_id=journey_id,
            traveler_id=other_id,
            journey_member_id=other_member.id,
            latitude=35.123456,
            longitude=135.123456,
            accuracy_m=8,
            captured_at=execution_clock.now().astimezone(timezone.utc).isoformat(),
            source="FOREGROUND_WEB",
        ))
        session.commit()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        active_plan_id = str(journey.active_plan_version_id)
        before = {
            "journey": (journey.active_plan_version_id, journey.current_activity_id),
            "plan_count": _count(PlanVersion, PlanVersion.journey_id == journey_id),
            "activity_count": _count(Activity, Activity.plan_version_id == active_plan_id),
            "intent_count": _count(JourneyIntent, JourneyIntent.journey_id == journey_id),
            "quest_count": _count(MemberSideQuest, MemberSideQuest.journey_id == journey_id),
        }

    scout = client.post(
        f"/api/v1/journeys/{journey_id}/moment-scout",
        headers=_auth(owner_id),
        json={
            "location_observation_id": observation_id,
            "budget_minutes": 30,
            "interest": "COFFEE_FOOD",
        },
    )
    assert scout.status_code == 200, scout.text
    result = scout.json()
    assert result["scout_state"] == "OPTIONS_AVAILABLE"
    assert 1 <= len(result["candidates"]) <= 3
    assert result["rendezvous"]["activity_id"]
    assert result["candidates"][0]["place_id"] == place.id
    assert result["candidates"][0]["straight_line_distance_m"] == 0
    assert result["candidates"][0]["place_provenance"] == "DEMO_FIXTURE"
    assert all(item["opening_state"] == "UNKNOWN" for item in result["candidates"])
    assert all(item["fit_authority"] == "UNVERIFIED" for item in result["candidates"])
    assert result["rendezvous"]["time_state"] == "AVAILABLE"
    assert isinstance(result["rendezvous"]["minutes_until"], int)
    assert "latitude" not in json.dumps(result) and "longitude" not in json.dumps(result)
    assert "ROUTE" in result["evidence_boundary"] and "GUARANTEE" in result["evidence_boundary"]

    with pytest.raises(HTTPException) as passed_rendezvous:
        main_module.select_moment_scout_candidate(
            current_scout={
                "scout_context": "f" * 64,
                "candidates": [{"place_id": place.id}],
                "rendezvous": {"time_state": "PAST"},
            },
            scout_context="f" * 64,
            place_id=place.id,
        )
    assert passed_rendezvous.value.status_code == 409
    assert passed_rendezvous.value.detail == "MOMENT_SCOUT_RENDEZVOUS_TIME_PASSED"

    foreign_observation = client.post(
        f"/api/v1/journeys/{journey_id}/moment-scout",
        headers=_auth(owner_id),
        json={"location_observation_id": foreign_observation_id},
    )
    assert foreign_observation.status_code == 403
    assert place.name not in foreign_observation.text

    unauthorized = client.post(
        f"/api/v1/journeys/{journey_id}/moment-scout",
        headers=_auth(other_id),
        json={"location_observation_id": observation_id},
    )
    assert unauthorized.status_code in {403, 409}
    assert place.name not in unauthorized.text

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        assert (journey.active_plan_version_id, journey.current_activity_id) == before["journey"]
        assert _count(PlanVersion, PlanVersion.journey_id == journey_id) == before["plan_count"]
        assert _count(Activity, Activity.plan_version_id == active_plan_id) == before["activity_count"]
        assert _count(JourneyIntent, JourneyIntent.journey_id == journey_id) == before["intent_count"]
        assert _count(MemberSideQuest, MemberSideQuest.journey_id == journey_id) == before["quest_count"]
        assert _count(
            AuditLog,
            AuditLog.journey_id == journey_id,
            AuditLog.action.like("journey.moment_scout.%"),
        ) == 0

    selection_path = f"/api/v1/journeys/{journey_id}/moment-scout/select"
    selection_data = {
        "location_observation_id": observation_id,
        "budget_minutes": 30,
        "interest": "COFFEE_FOOD",
        "place_id": place.id,
        "scout_context": "0" * 64,
    }
    stale = client.post(selection_path, headers=_auth(owner_id), json=selection_data)
    assert stale.status_code == 409
    assert "MOMENT_SCOUT_CONTEXT_STALE" in stale.text
    assert _count(MemberSideQuest, MemberSideQuest.journey_id == journey_id) == 0

    selection_data["scout_context"] = result["scout_context"]
    selected = client.post(selection_path, headers=_auth(owner_id), json=selection_data)
    assert selected.status_code == 201, selected.text
    assert selected.json()["member_side_quest"]["state"] == "READY"
    assert selected.json()["member_side_quest"]["destination"]["place_id"] == place.id
    assert selected.json()["member_side_quest"]["rendezvous"]["activity_id"] == result["rendezvous"]["activity_id"]
    assert selected.json()["group_presence"]["planned_separation"] is not None
    collaborator_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(other_id)
    )
    assert collaborator_view.status_code == 200, collaborator_view.text
    assert place.name not in collaborator_view.text
    with SessionLocal() as session:
        selected_quest = session.get(MemberSideQuest, selected.json()["member_side_quest"]["id"])
        assert selected_quest is not None
        selected_quest.state = "COMPLETED"
        session.commit()
    after_private_stop = client.post(
        f"/api/v1/journeys/{journey_id}/moment-scout",
        headers=_auth(owner_id),
        json={"location_observation_id": observation_id},
    )
    assert after_private_stop.status_code == 409
    assert "MOMENT_SCOUT_RETURN_TO_SHARED_GROUP_FIRST" in after_private_stop.text
    with SessionLocal() as session:
        member = session.scalar(
            select(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == other_id,
            )
        )
        assert member is not None
        member.removed_at = execution_clock.now().astimezone(timezone.utc).isoformat()
        session.commit()
    removed_member = client.post(
        f"/api/v1/journeys/{journey_id}/moment-scout",
        headers=_auth(other_id),
        json={"location_observation_id": observation_id},
    )
    assert removed_member.status_code in {403, 404}
    assert place.name not in removed_member.text


def test_private_lifecycle_is_member_bound_and_leaves_shared_execution_unchanged():
    journey_id, owner_id, other_id = _active_shared_journey()
    split = _start_split(journey_id, owner_id)
    place = _private_place(journey_id, split.get("rendezvous_place_id"))

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        plan_id = str(journey.active_plan_version_id)
        baseline = {
            "plans": len(session.scalars(select(PlanVersion).where(PlanVersion.journey_id == journey_id)).all()),
            "activities": len(session.scalars(select(Activity).where(Activity.plan_version_id == plan_id)).all()),
            "proposals": len(session.scalars(select(ReplanProposal).where(ReplanProposal.journey_id == journey_id)).all()),
            "approvals": len(session.scalars(select(ActionApproval).where(ActionApproval.journey_id == journey_id)).all()),
            "recovery_epochs": len(session.scalars(select(RecoveryEpoch).where(RecoveryEpoch.journey_id == journey_id)).all()),
            "events": len(session.scalars(select(Event).where(Event.journey_id == journey_id)).all()),
        }

    spoof = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(other_id),
        json={"place_id": place.id, "traveler_id": owner_id, "member_id": "owner"},
    )
    assert spoof.status_code == 422

    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": place.id},
    )
    assert created.status_code == 201, created.text
    owner_quest_id = created.json()["member_side_quest"]["id"]
    cannot_advance_owners_quest = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/start",
        headers=_auth(other_id),
    )
    assert cannot_advance_owners_quest.status_code == 404
    quest = created.json()["member_side_quest"]
    assert quest["state"] == "READY"
    assert quest["destination"] == {
        "place_id": place.id,
        "title": place.name,
        "authority": "EXACT_KNOWN_PLACE",
    }
    assert quest["rendezvous"]["activity_id"] == split["rendezvous_activity_id"]
    quest_id = quest["id"]

    started = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/start",
        headers=_auth(owner_id),
    )
    assert started.status_code == 200, started.text
    assert started.json()["member_side_quest"]["state"] == "EN_ROUTE"
    repeated_start = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/start",
        headers=_auth(owner_id),
    )
    assert repeated_start.status_code == 200, repeated_start.text
    assert _count(
        AuditLog,
        AuditLog.journey_id == journey_id,
        AuditLog.entity_type == "MemberSideQuest",
        AuditLog.action == "journey.member_side_quest.start",
    ) == 1

    arrived = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/arrive",
        headers=_auth(owner_id),
    )
    assert arrived.status_code == 200, arrived.text
    arrival_projection = arrived.json()["member_side_quest"]
    assert arrival_projection["state"] == "ARRIVED"
    assert arrival_projection["arrival_claim"] == "MEMBER_REPORTED_NOT_PHYSICAL_PRESENCE_PROOF"
    assert "physical_reunion_claimed" not in arrival_projection

    completed = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/complete",
        headers=_auth(owner_id),
    )
    assert completed.status_code == 200, completed.text
    final_projection = completed.json()
    assert final_projection["member_side_quest"]["state"] == "COMPLETED"
    assert final_projection["member_side_quest"]["return_state"] == "RETURN_UNKNOWN"
    assert final_projection["member_side_quest"]["shared_plan_changed"] is False
    assert final_projection["group_presence"]["planned_separation"] is not None
    assert "physical_reunion_claimed" not in final_projection["group_presence"]

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        assert str(journey.active_plan_version_id) == plan_id
        assert len(session.scalars(select(PlanVersion).where(PlanVersion.journey_id == journey_id)).all()) == baseline["plans"]
        assert len(session.scalars(select(Activity).where(Activity.plan_version_id == plan_id)).all()) == baseline["activities"]
        assert len(session.scalars(select(ReplanProposal).where(ReplanProposal.journey_id == journey_id)).all()) == baseline["proposals"]
        assert len(session.scalars(select(ActionApproval).where(ActionApproval.journey_id == journey_id)).all()) == baseline["approvals"]
        assert len(session.scalars(select(RecoveryEpoch).where(RecoveryEpoch.journey_id == journey_id)).all()) == baseline["recovery_epochs"]
        assert len(session.scalars(select(Event).where(Event.journey_id == journey_id)).all()) == baseline["events"]


def test_r65_head_back_is_idempotent_without_location_and_only_coarsely_shared():
    journey_id, owner_id, other_id = _active_shared_journey()
    split = _start_split(journey_id, owner_id)
    place = _private_place(journey_id, split.get("rendezvous_place_id"))
    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": place.id},
    )
    assert created.status_code == 201, created.text
    quest_id = created.json()["member_side_quest"]["id"]
    for action in ("start", "arrive", "complete"):
        advanced = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/{action}",
            headers=_auth(owner_id),
        )
        assert advanced.status_code == 200, advanced.text

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        plan_id = str(journey.active_plan_version_id)

    endpoint = f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/head-back"
    first = client.post(endpoint, headers=_auth(owner_id))
    assert first.status_code == 200, first.text
    report = first.json()["member_side_quest"]["return_report"]
    assert report["status"] == "RETURN_STARTED_REPORTED"
    assert report["physical_presence_verified"] is False

    repeated = client.post(endpoint, headers=_auth(owner_id))
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["member_side_quest"]["return_report"]["reported_at"] == report["reported_at"]
    assert _count(
        MemberSideQuestReturnReport,
        MemberSideQuestReturnReport.side_quest_id == quest_id,
    ) == 1
    assert _count(
        AuditLog,
        AuditLog.journey_id == journey_id,
        AuditLog.action == "journey.member_side_quest.return_started_reported",
    ) == 1

    with SessionLocal() as session:
        owner_member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == owner_id,
        ))
        assert owner_member is not None
        owner_member_id = owner_member.id
    collaborator = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(other_id)
    )
    assert collaborator.status_code == 200, collaborator.text
    collaborator_json = collaborator.json()
    owner_summary = next(
        item for item in collaborator_json["group_presence"]["members"]
        if item["member_id"] == owner_member_id
    )
    assert owner_summary["personal_stop"] == {
        "planned_personal_stop": True,
        "state": "RETURN_STARTED_REPORTED",
    }
    assert place.name not in json.dumps(collaborator_json, ensure_ascii=False)
    assert "physical_presence_verified" not in json.dumps(collaborator_json)

    # A later private stop in the same split must not inherit the previous stop's report.
    next_place = _private_place(journey_id, place.id, split.get("rendezvous_place_id"))
    replacement = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": next_place.id},
    )
    assert replacement.status_code == 201, replacement.text
    next_summary = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(other_id)
    ).json()
    next_owner_summary = next(
        item for item in next_summary["group_presence"]["members"]
        if item["member_id"] == owner_member_id
    )
    assert next_owner_summary["personal_stop"]["state"] == "PLANNED"
    assert next_place.name not in json.dumps(next_summary, ensure_ascii=False)

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None and str(journey.active_plan_version_id) == plan_id


def test_r65_private_navigation_and_fit_preview_reject_synthetic_routes_without_shared_records(monkeypatch):
    journey_id, owner_id, other_id = _active_shared_journey()
    split = _start_split(journey_id, owner_id)
    place = _private_place(journey_id, split.get("rendezvous_place_id"))
    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": place.id},
    )
    assert created.status_code == 201, created.text
    quest_id = created.json()["member_side_quest"]["id"]
    sample = client.post(
        f"/api/v1/journeys/{journey_id}/location",
        headers=_auth(owner_id),
        json={
            "latitude": 35.0,
            "longitude": 135.0,
            "accuracy_m": 8,
            "captured_at": execution_clock.now().astimezone(timezone.utc).isoformat(),
        },
    )
    assert sample.status_code == 201, sample.text
    observation_id = sample.json()["id"]
    now = execution_clock.now().astimezone(timezone.utc)
    with SessionLocal() as session:
        target = session.get(Activity, split["rendezvous_activity_id"])
        assert target is not None
        session.add(Commitment(
            id=str(uuid4()),
            journey_id=journey_id,
            activity_id=target.id,
            kind="APPOINTMENT",
            level="C4_Anchored",
            status="ACTIVE",
            monetary_value_minor=0,
            monetary_value_status="KNOWN",
            currency="JPY",
            external_ref="R65-PRIVATE-DETOUR-FIT-FIXTURE",
            policy_json=json.dumps({
                "scheduled_start_at": (now + timedelta(hours=3)).isoformat(),
                "safety_buffer_min": 15,
                "source": "R65_TEST_ONLY_CONTROLLED_COMMITMENT",
            }),
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        ))
        session.commit()

    captured_pairs = []

    def synthetic_capture(_session, _journey, pairs, **kwargs):
        pair = tuple(pairs[0])
        captured_pairs.append((pair, kwargs["operation_key"]))
        fact = type("SyntheticFact", (), {
            "provider_key": "mock-mobility",
            "route_provenance": "SYNTHETIC_FIXTURE",
            "fact_quality": "SYNTHETIC_FIXTURE",
            "minutes": 5,
            "distance_m": 100,
            "captured_at": execution_clock.now().astimezone(timezone.utc),
            "mode": kwargs.get("requested_mode") or "WALK",
            "route_geometry": [[135.0, 35.0], [135.1, 35.1]],
        })()
        return type("Capture", (), {
            "snapshot": type("Snapshot", (), {"facts": {pair: fact}})(),
        })()

    monkeypatch.setattr(mobility_runtime, "capture_runtime_mobility_snapshot", synthetic_capture)
    before_route_ops = _count(RouteAcquisitionOperation, RouteAcquisitionOperation.journey_id == journey_id)
    before_mobility_facts = _count(MobilityFact, MobilityFact.journey_id == journey_id)

    preview = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/fit-preview",
        headers=_auth(owner_id),
        json={"location_observation_id": observation_id, "dwell_minutes": 20},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["state"] == "UNKNOWN"
    assert preview.json()["reason_code"] == "QUALIFYING_LIVE_ROUTE_EVIDENCE_UNAVAILABLE"
    assert preview.json()["outbound_eta_minutes"] is None
    assert preview.json()["return_eta_minutes"] is None
    assert preview.json()["plan_unchanged"] is True

    navigation = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/navigate",
        headers=_auth(owner_id),
        json={"location_observation_id": observation_id},
    )
    assert navigation.status_code == 200, navigation.text
    assert navigation.json()["status"] == "UNKNOWN"
    assert navigation.json()["route"] is None
    assert navigation.json()["plan_unchanged"] is True
    assert len(captured_pairs) == 3  # outbound + hypothetical return + one navigation leg
    assert all(key.startswith("private-side-quest:") for _pair, key in captured_pairs)
    assert _count(RouteAcquisitionOperation, RouteAcquisitionOperation.journey_id == journey_id) == before_route_ops
    assert _count(MobilityFact, MobilityFact.journey_id == journey_id) == before_mobility_facts

    def live_capture(_session, _journey, pairs, **kwargs):
        pair = tuple(pairs[0])
        fact = type("LiveTestFact", (), {
            "provider_key": "REDACTED_FOR_PUBLIC_RELEASE",
            "route_provenance": "LIVE_PROVIDER_CAPTURE",
            "fact_quality": "LIVE_PROVIDER_CAPTURE",
            "minutes": 10 if pair[1] == place.id else 15,
            "distance_m": 900,
            "captured_at": execution_clock.now().astimezone(timezone.utc),
            "mode": kwargs.get("requested_mode") or "WALK",
            "route_geometry": [[135.0, 35.0], [135.1, 35.1]],
        })()
        return type("Capture", (), {
            "snapshot": type("Snapshot", (), {"facts": {pair: fact}})(),
        })()

    monkeypatch.setattr(mobility_runtime, "capture_runtime_mobility_snapshot", live_capture)
    before_guardian = _count(GuardianPulse, GuardianPulse.journey_id == journey_id)
    before_events = _count(Event, Event.journey_id == journey_id)
    grounded_preview = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/fit-preview",
        headers=_auth(owner_id),
        json={"location_observation_id": observation_id, "dwell_minutes": 20},
    )
    assert grounded_preview.status_code == 200, grounded_preview.text
    grounded = grounded_preview.json()
    assert grounded["state"] == "FITS_WITH_BUFFER"
    assert grounded["outbound_eta_minutes"] == 10
    assert grounded["return_eta_minutes"] == 15
    assert grounded["recommended_leave_by"] is not None
    assert grounded["safety_buffer_minutes"] == 15
    assert grounded["provenance"]["outbound"]["provenance"] == "LIVE_PROVIDER_CAPTURE"
    assert grounded["provenance"]["return"]["provenance"] == "LIVE_PROVIDER_CAPTURE"
    assert grounded["origin_basis"]["outbound"] == "AUTHENTICATED_FRESH_MEMBER_LOCATION"
    assert grounded["origin_basis"]["return"] == "HYPOTHETICAL_SIDE_PLACE"
    assert grounded["plan_unchanged"] is True
    assert _count(GuardianPulse, GuardianPulse.journey_id == journey_id) == before_guardian
    assert _count(Event, Event.journey_id == journey_id) == before_events

    # A mathematical leave-by is not useful guidance once it cannot be met.
    # Capture receipts here remain controlled test-only provider fixtures.
    with SessionLocal() as session:
        commitment = session.scalar(select(Commitment).where(
            Commitment.journey_id == journey_id,
            Commitment.external_ref == "R65-PRIVATE-DETOUR-FIT-FIXTURE",
        ))
        assert commitment is not None
        policy = json.loads(commitment.policy_json or "{}")
        policy["scheduled_start_at"] = (
            execution_clock.now().astimezone(timezone.utc) + timedelta(minutes=48)
        ).isoformat()
        commitment.policy_json = json.dumps(policy)
        session.commit()
    tight = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/fit-preview",
        headers=_auth(owner_id),
        json={"location_observation_id": observation_id, "dwell_minutes": 20},
    )
    assert tight.status_code == 200, tight.text
    assert tight.json()["state"] == "TIGHT_RETURN"
    assert tight.json()["recommended_leave_by"] is None

    with SessionLocal() as session:
        commitment = session.scalar(select(Commitment).where(
            Commitment.journey_id == journey_id,
            Commitment.external_ref == "R65-PRIVATE-DETOUR-FIT-FIXTURE",
        ))
        assert commitment is not None
        policy = json.loads(commitment.policy_json or "{}")
        policy["scheduled_start_at"] = (
            execution_clock.now().astimezone(timezone.utc) + timedelta(minutes=2)
        ).isoformat()
        commitment.policy_json = json.dumps(policy)
        session.commit()
    impossible = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/fit-preview",
        headers=_auth(owner_id),
        json={"location_observation_id": observation_id, "dwell_minutes": 20},
    )
    assert impossible.status_code == 200, impossible.text
    assert impossible.json()["state"] == "DOES_NOT_FIT"
    assert impossible.json()["recommended_leave_by"] is None
    assert impossible.json()["reason_code"] == "INSUFFICIENT_TIME_FOR_PRIVATE_DETOUR"

    other_sample = client.post(
        f"/api/v1/journeys/{journey_id}/location",
        headers=_auth(other_id),
        json={
            "latitude": 35.1,
            "longitude": 135.1,
            "accuracy_m": 8,
            "captured_at": execution_clock.now().astimezone(timezone.utc).isoformat(),
        },
    )
    assert other_sample.status_code == 201, other_sample.text
    foreign_observation = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/navigate",
        headers=_auth(owner_id),
        json={"location_observation_id": other_sample.json()["id"]},
    )
    assert foreign_observation.status_code == 403

    # A group member cannot use the owner's private quest or its captured location.
    forbidden = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/navigate",
        headers=_auth(other_id),
        json={"location_observation_id": observation_id},
    )
    assert forbidden.status_code == 404

    def cancel_during_capture(session, _journey, pairs, **_kwargs):
        current = session.get(MemberSideQuest, quest_id)
        assert current is not None
        current.state = "CANCELLED"
        session.flush()
        pair = tuple(pairs[0])
        return type("Capture", (), {
            "snapshot": type("Snapshot", (), {"facts": {pair: None}})(),
        })()

    monkeypatch.setattr(mobility_runtime, "capture_runtime_mobility_snapshot", cancel_during_capture)
    stale = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/navigate",
        headers=_auth(owner_id),
        json={"location_observation_id": observation_id},
    )
    assert stale.status_code == 409
    with SessionLocal() as session:
        current = session.get(MemberSideQuest, quest_id)
        assert current is not None and current.state == "READY"


def test_trusted_r63_return_risk_remains_the_only_recovery_handoff(monkeypatch):
    journey_id, owner_id, _other_id = _active_shared_journey()
    split = _start_split(journey_id, owner_id)
    rendezvous_id = split["rendezvous_activity_id"]
    private_place = _private_place(journey_id, split.get("rendezvous_place_id"))
    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": private_place.id},
    )
    assert created.status_code == 201, created.text
    quest_id = created.json()["member_side_quest"]["id"]

    now = execution_clock.now().astimezone(timezone.utc)
    sample = client.post(
        f"/api/v1/journeys/{journey_id}/location",
        headers=_auth(owner_id),
        json={
            "latitude": 35.0,
            "longitude": 135.0,
            "accuracy_m": 8,
            "captured_at": now.isoformat(),
        },
    )
    assert sample.status_code == 201, sample.text
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        target = session.get(Activity, rendezvous_id)
        observation = session.get(LocationObservation, sample.json()["id"])
        assert journey is not None and target is not None and observation is not None
        observation.source = "DEVICE_ATTESTED"
        session.add(Commitment(
            id=str(uuid4()),
            journey_id=journey_id,
            activity_id=rendezvous_id,
            kind="APPOINTMENT",
            level="C4_Anchored",
            status="ACTIVE",
            monetary_value_minor=0,
            monetary_value_status="KNOWN",
            currency="JPY",
            external_ref="R64-TRUSTED-RENDEZVOUS-RISK",
            policy_json=json.dumps({
                "scheduled_start_at": (now + timedelta(minutes=5)).isoformat(),
                "safety_buffer_min": 5,
                "source": "R64_DETERMINISTIC_REGRESSION_FIXTURE",
            }),
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        ))
        plan_id = str(journey.active_plan_version_id)
        session.commit()

    def fake_submit(session, journey, route_request):
        operation_id = str(uuid4())
        stamp = execution_clock.now().astimezone(timezone.utc).isoformat()
        session.add(RouteAcquisitionOperation(
            id=operation_id,
            journey_id=journey.id,
            fingerprint=str(uuid4()),
            origin_key=route_request.origin_key,
            target_key=route_request.target_key,
            target_activity_id=route_request.target_activity_id,
            requested_mode=route_request.requested_mode,
            provider_policy_version=route_request.provider_policy_version,
            route_truth_policy_version=route_request.route_truth_policy_version,
            status="COMPLETED",
            phase="COMPLETED",
            outcome="ROUTE_READY",
            request_json="{}",
            result_json=json.dumps({
                "duration_min": 30,
                "distance_m": 2400,
                "provider": "valhalla-http",
                "provenance": "LIVE_PROVIDER_CAPTURE",
            }),
            error_json="{}",
            queued_at=stamp,
            started_at=stamp,
            finished_at=stamp,
            updated_at=stamp,
        ))
        session.flush()
        return session.get(RouteAcquisitionOperation, operation_id), False

    monkeypatch.setattr(main_module, "submit_route_operation", fake_submit)
    monkeypatch.setattr(main_module, "route_operation_payload", lambda *_args, **_kwargs: {
        "status": "COMPLETED",
        "outcome": "ROUTE_READY",
        "provider": "valhalla-http",
        "provider_provenance": "LIVE_PROVIDER_CAPTURE",
        "result": {
            "duration_min": 30,
            "distance_m": 2400,
            "provider": "valhalla-http",
            "provenance": "LIVE_PROVIDER_CAPTURE",
        },
    })
    monkeypatch.setattr(main_module, "scan_guardian", lambda *_args, **_kwargs: {"status": "COMPLETED"})
    routed = client.post(
        f"/api/v1/journeys/{journey_id}/group-presence/route",
        headers=_auth(owner_id),
        json={"location_observation_id": sample.json()["id"], "requested_mode": "WALK"},
    )
    assert routed.status_code == 200, routed.text
    assert routed.json()["shared_commitment_risk"]["state"] == "COMMITMENT_AT_RISK"
    assert routed.json()["guardian_triggered"] is True

    before = client.get(f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id))
    assert before.status_code == 200, before.text
    before_json = before.json()
    assert before_json["group_recovery"]["state"] == "VERIFIED_MEMBER_RISK"
    assert before_json["group_presence"]["state"] == "PLANNED_SPLIT_RETURN_RISK"
    assert before_json["member_side_quest"]["return_state"] == "RETURN_RISK"
    recovery_epochs_before = _count(RecoveryEpoch, RecoveryEpoch.journey_id == journey_id)
    proposals_before = _count(ReplanProposal, ReplanProposal.journey_id == journey_id)

    for action in ("start", "arrive", "complete"):
        advanced = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/{action}",
            headers=_auth(owner_id),
        )
        assert advanced.status_code == 200, advanced.text
    after = client.get(f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id))
    assert after.status_code == 200, after.text
    after_json = after.json()
    assert after_json["group_recovery"]["state"] == "VERIFIED_MEMBER_RISK"
    assert after_json["group_recovery"]["can_request_planning"] is True
    assert after_json["member_side_quest"]["return_state"] == "RETURN_RISK"
    assert after_json["member_side_quest"]["state"] == "COMPLETED"
    assert after_json["member_side_quest"]["shared_plan_changed"] is False
    assert _count(RecoveryEpoch, RecoveryEpoch.journey_id == journey_id) == recovery_epochs_before
    assert _count(ReplanProposal, ReplanProposal.journey_id == journey_id) == proposals_before
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None and str(journey.active_plan_version_id) == plan_id


def test_other_member_sees_only_coarse_private_stop_state_and_can_own_an_independent_stop():
    journey_id, owner_id, other_id = _active_shared_journey()
    owner_split = _start_split(journey_id, owner_id)
    owner_place = _private_place(journey_id, owner_split.get("rendezvous_place_id"))
    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": owner_place.id},
    )
    assert created.status_code == 201, created.text

    with SessionLocal() as session:
        owner_member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == owner_id,
        ))
        assert owner_member is not None
        owner_member_id = owner_member.id
    collaborator_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(other_id)
    )
    assert collaborator_projection.status_code == 200, collaborator_projection.text
    other_json = collaborator_projection.json()
    assert other_json["member_side_quest"] is None
    owner_summary = next(
        item for item in other_json["group_presence"]["members"]
        if item["member_id"] == owner_member_id
    )
    assert owner_summary["personal_stop"] == {
        "planned_personal_stop": True,
        "state": "PLANNED",
    }
    assert owner_place.name not in json.dumps(other_json, ensure_ascii=False)

    # Only an authenticated start report advances the coarse member status.
    started = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{created.json()['member_side_quest']['id']}/start",
        headers=_auth(owner_id),
    )
    assert started.status_code == 200, started.text
    active_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(other_id)
    ).json()
    active_summary = next(
        item for item in active_projection["group_presence"]["members"]
        if item["member_id"] == owner_member_id
    )
    assert active_summary["personal_stop"]["state"] == "ACTIVE_SELF_REPORTED"
    assert owner_place.name not in json.dumps(active_projection, ensure_ascii=False)

    other_split = _start_split(journey_id, other_id)
    other_place = _private_place(
        journey_id,
        other_split.get("rendezvous_place_id"),
        owner_place.id,
    )
    assert other_place.id != owner_place.id
    other_created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(other_id),
        json={"place_id": other_place.id},
    )
    assert other_created.status_code == 201, other_created.text
    assert other_created.json()["member_side_quest"]["destination"]["title"] == other_place.name

    owner_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    )
    assert owner_projection.status_code == 200, owner_projection.text
    owner_json = owner_projection.json()
    assert owner_json["member_side_quest"]["destination"]["title"] == owner_place.name
    assert other_place.name not in json.dumps(owner_json, ensure_ascii=False)


def test_descriptive_destination_and_invalid_transitions_never_fabricate_a_place():
    journey_id, owner_id, _other_id = _active_shared_journey()
    _start_split(journey_id, owner_id)
    description = "the tiny blue dessert room behind the station"
    unresolved = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"destination_text": description},
    )
    assert unresolved.status_code == 201, unresolved.text
    quest = unresolved.json()["member_side_quest"]
    assert quest["state"] == "DESTINATION_UNRESOLVED"
    assert quest["destination"] == {
        "place_id": None,
        "title": description,
        "authority": "DESCRIPTIVE_ONLY",
    }
    quest_id = quest["id"]

    cannot_start = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/start",
        headers=_auth(owner_id),
    )
    assert cannot_start.status_code == 409
    cannot_complete = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/complete",
        headers=_auth(owner_id),
    )
    assert cannot_complete.status_code == 409

    exact_place = _private_place(journey_id)
    replaced = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": exact_place.id},
    )
    assert replaced.status_code == 201, replaced.text
    assert replaced.json()["member_side_quest"]["state"] == "READY"
    assert replaced.json()["member_side_quest"]["id"] == quest_id
    assert replaced.json()["member_side_quest"]["destination"]["place_id"] == exact_place.id


def test_r68_navigation_target_is_owner_only_exact_and_read_only(monkeypatch):
    import app.agent.autorun as autorun_module

    autorun_module.stop_autorun()
    monkeypatch.setattr(main_module, "start_autorun", lambda **_kwargs: {"enabled": False})
    journey_id, owner_id, other_id = _active_shared_journey()
    split = _start_split(journey_id, owner_id)
    private_place = _private_place(journey_id, split.get("rendezvous_place_id"))
    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": private_place.id},
    )
    assert created.status_code == 201, created.text
    quest_id = created.json()["member_side_quest"]["id"]
    endpoint = f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/navigation-targets"

    shared_models = (PlanVersion, Activity, ReplanProposal, GuardianPulse, AgentRun)

    def shared_snapshot() -> dict[str, tuple[tuple[object, ...], ...]]:
        with SessionLocal() as session:
            result: dict[str, tuple[tuple[object, ...], ...]] = {}
            for model in shared_models:
                if model is Activity:
                    rows = session.scalars(select(Activity).where(
                        Activity.plan_version_id.in_(
                            select(PlanVersion.id).where(PlanVersion.journey_id == journey_id)
                        )
                    )).all()
                else:
                    rows = session.scalars(
                        select(model).where(model.journey_id == journey_id)
                    ).all()
                result[model.__name__] = tuple(sorted(
                    [
                        tuple(getattr(row, column.name) for column in model.__table__.columns)
                        for row in rows
                    ],
                    key=repr,
                ))
            return result

    before_read = shared_snapshot()
    owner_target = client.get(endpoint, headers=_auth(owner_id))
    assert owner_target.status_code == 200, owner_target.text
    private_target = owner_target.json()["target"]
    assert private_target == {
        "kind": "PRIVATE_STOP",
        "place_id": private_place.id,
        "name": private_place.name,
        "latitude": private_place.latitude,
        "longitude": private_place.longitude,
        "coordinate_system": "WGS84",
        "provenance": {
            "provider_key": private_place.provider_key,
            "external_id": private_place.external_id,
            "source_coordinate_system": private_place.source_coordinate_system,
        },
        "navigation_available": True,
        "unavailable_reason": None,
    }
    serialized_target = json.dumps(owner_target.json(), ensure_ascii=False).casefold()
    assert "origin" not in serialized_target
    assert "location_observation" not in serialized_target
    assert shared_snapshot() == before_read

    foreign = client.get(endpoint, headers=_auth(other_id))
    assert foreign.status_code == 404
    collaborator_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(other_id)
    )
    assert collaborator_projection.status_code == 200
    assert private_place.name not in json.dumps(collaborator_projection.json(), ensure_ascii=False)

    for action in ("start", "arrive", "complete"):
        advanced = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/{action}",
            headers=_auth(owner_id),
        )
        assert advanced.status_code == 200, advanced.text
    return_target = client.get(endpoint, headers=_auth(owner_id))
    assert return_target.status_code == 200, return_target.text
    rendezvous = return_target.json()["target"]
    with SessionLocal() as session:
        rendezvous_activity = session.get(Activity, split["rendezvous_activity_id"])
        journey = session.get(Journey, journey_id)
        assert rendezvous_activity is not None and journey is not None
        rendezvous_place = session.get(Place, rendezvous_activity.place_id)
        assert rendezvous_place is not None
        assert str(journey.active_plan_version_id) == split["active_plan_version_id"]
    assert rendezvous["kind"] == "SHARED_RENDEZVOUS"
    assert rendezvous["place_id"] == rendezvous_place.id
    assert rendezvous["latitude"] == rendezvous_place.latitude
    assert rendezvous["longitude"] == rendezvous_place.longitude
    assert _count(MemberSideQuestReturnReport, MemberSideQuestReturnReport.journey_id == journey_id) == 0
    owner_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    ).json()
    assert owner_projection["member_side_quest"]["return_state"] == "RETURN_UNKNOWN"
    assert owner_projection["member_side_quest"]["return_report"] is None


def test_r68_navigation_rejects_descriptive_and_stale_targets():
    journey_id, owner_id, _other_id = _active_shared_journey()
    split = _start_split(journey_id, owner_id)
    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"destination_text": "the little blue room beside the station"},
    )
    assert created.status_code == 201, created.text
    quest_id = created.json()["member_side_quest"]["id"]
    endpoint = f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/navigation-targets"
    unresolved = client.get(endpoint, headers=_auth(owner_id))
    assert unresolved.status_code == 409

    place = _private_place(journey_id, split.get("rendezvous_place_id"))
    exact = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": place.id},
    )
    assert exact.status_code == 201, exact.text
    stale_endpoint = (
        f"/api/v1/journeys/{journey_id}/member-side-quests/"
        f"{exact.json()['member_side_quest']['id']}/navigation-targets"
    )
    assert client.get(stale_endpoint, headers=_auth(owner_id)).status_code == 200

    ended = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        headers=_auth(owner_id),
        json={"command": "I'm back with the group.", "requested_autonomy": "PLAN_ONLY"},
    )
    assert ended.status_code == 200, ended.text
    assert client.get(stale_endpoint, headers=_auth(owner_id)).status_code == 409

    replacement_split = _start_split(journey_id, owner_id)
    replacement_place = _private_place(journey_id, replacement_split.get("rendezvous_place_id"))
    replacement = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": replacement_place.id},
    )
    assert replacement.status_code == 201, replacement.text
    replacement_endpoint = (
        f"/api/v1/journeys/{journey_id}/member-side-quests/"
        f"{replacement.json()['member_side_quest']['id']}/navigation-targets"
    )
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == owner_id,
        ))
        assert journey is not None and member is not None
        active_plan_id = str(journey.active_plan_version_id)
        rendezvous_activity = session.get(Activity, replacement.json()["member_side_quest"]["rendezvous"]["activity_id"])
        assert rendezvous_activity is not None
        original_activity_status = rendezvous_activity.status
        rendezvous_activity.status = "COMPLETED"
        session.commit()
    assert client.get(replacement_endpoint, headers=_auth(owner_id)).status_code == 409
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        rendezvous_activity = session.get(Activity, replacement.json()["member_side_quest"]["rendezvous"]["activity_id"])
        assert journey is not None and rendezvous_activity is not None
        rendezvous_activity.status = original_activity_status
        journey.active_plan_version_id = None
        session.commit()
    assert client.get(replacement_endpoint, headers=_auth(owner_id)).status_code == 409
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == owner_id,
        ))
        assert journey is not None and member is not None
        journey.active_plan_version_id = active_plan_id
        member.removed_at = datetime.now(timezone.utc).isoformat()
        session.commit()
    assert client.get(replacement_endpoint, headers=_auth(owner_id)).status_code != 200


def test_r63_end_invalidates_the_side_quest_and_removed_members_cannot_advance():
    journey_id, owner_id, _other_id = _active_shared_journey()
    _start_split(journey_id, owner_id)
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        plan_id = str(journey.active_plan_version_id)
    place = _private_place(journey_id)
    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": place.id},
    )
    assert created.status_code == 201, created.text
    quest_id = created.json()["member_side_quest"]["id"]

    ended = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        headers=_auth(owner_id),
        json={"command": "I'm back with the group.", "requested_autonomy": "PLAN_ONLY"},
    )
    assert ended.status_code == 200, ended.text
    assert ended.json()["planned_separation_result"]["physical_reunion_claimed"] is False
    with SessionLocal() as session:
        quest = session.get(MemberSideQuest, quest_id)
        assert quest is not None and quest.state == "INVALIDATED"
    stale = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/start",
        headers=_auth(owner_id),
    )
    assert stale.status_code == 409

    _start_split(journey_id, owner_id)
    replacement = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": place.id},
    )
    assert replacement.status_code == 201, replacement.text
    replacement_quest_id = replacement.json()["member_side_quest"]["id"]

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        journey.active_plan_version_id = None
        session.commit()
    plan_changed = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{replacement_quest_id}/start",
        headers=_auth(owner_id),
    )
    assert plan_changed.status_code == 409
    with SessionLocal() as session:
        quest = session.get(MemberSideQuest, replacement_quest_id)
        assert quest is not None and quest.state == "INVALIDATED"

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        journey.active_plan_version_id = plan_id
        session.commit()
    after_plan_change = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": place.id},
    )
    assert after_plan_change.status_code == 201, after_plan_change.text
    next_quest_id = after_plan_change.json()["member_side_quest"]["id"]
    with SessionLocal() as session:
        member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == owner_id,
        ))
        assert member is not None
        member.removed_at = datetime.now(timezone.utc).isoformat()
        session.commit()
    removed = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{next_quest_id}/start",
        headers=_auth(owner_id),
    )
    assert removed.status_code == 403


def _r67_location(
    journey_id: str,
    traveler_id: str,
    *,
    latitude: float = 5.123456,
    longitude: float = 10.654321,
    accuracy: float = 8,
    age_seconds: int = 0,
) -> str:
    captured_at = (
        execution_clock.now().astimezone(timezone.utc) - timedelta(seconds=age_seconds)
    ).isoformat()
    response = client.post(
        f"/api/v1/journeys/{journey_id}/location",
        headers=_auth(traveler_id),
        json={
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_m": accuracy,
            "captured_at": captured_at,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _r67_candidate(
    *,
    osm_id: str = "123456789",
    name: str = "R67 controlled-provider museum",
    latitude: float = 5.123956,
    longitude: float = 10.654321,
):
    from app.integrations.discovery.real_city import DiscoveryCandidate

    return DiscoveryCandidate(
        provider_key="overpass",
        external_id=f"overpass:node/{osm_id}",
        name=name,
        category="museum",
        latitude=latitude,
        longitude=longitude,
        tags=("museum", "real_city"),
        opening_hours={"status": "UNKNOWN"},
        metadata={"osm_id": f"node/{osm_id}", "tags": {"name": name, "tourism": "museum"}},
        distance_m=None,
    )




@pytest.mark.parametrize(
    "access_metadata, expected_public",
    [
        ({"tags": {"name": "Public museum", "tourism": "museum", "access": "private"}}, False),
        ({"access": "yes", "tags": {"name": "Public museum", "tourism": "museum", "access": "no"}}, False),
        ({"osm_tags": {"access": "staff"}, "tags": {"tourism": "museum"}}, False),
        ({"tags": {"access": "customers", "tourism": "museum"}}, False),
        ({"tags": {"visibility": "restricted", "tourism": "museum"}}, False),
        ({"public": False, "tags": {"tourism": "museum"}}, False),
        ({"tags": {"access": "yes", "tourism": "museum"}}, True),
    ],
)
def test_r83_live_osm_scout_does_not_offer_known_restricted_access_places(
    access_metadata: dict, expected_public: bool,
):
    """OSM venue access restrictions cannot disappear before public catalog creation."""
    from dataclasses import replace
    from app.execution.live_moment_scout import _valid_live_candidate

    candidate = replace(_r67_candidate(), metadata=access_metadata)
    projected = _valid_live_candidate(
        candidate,
        interest="CULTURE",
        latitude=5.123456,
        longitude=10.654321,
        radius_m=1200,
    )
    assert (projected is not None) is expected_public


def _install_r67_provider(monkeypatch, *, candidates=None, attempts=None, error=None):
    import app.execution.live_moment_scout as live_module

    calls = []

    class ControlledOverpassProvider:
        def search_with_evidence(self, query, **kwargs):
            calls.append({"query": query, **kwargs})
            if error is not None:
                raise error
            return list(candidates or []), list(attempts or [])

        def close(self):
            return None

    monkeypatch.setattr(live_module, "OverpassPOIProvider", ControlledOverpassProvider)
    object.__setattr__(settings, "real_city_enabled", True)
    object.__setattr__(settings, "real_city_mode", "live")
    return calls


def _r67_live_request(
    journey_id: str,
    traveler_id: str,
    observation_id: str,
    *,
    consent: bool | None = True,
):
    payload = {
        "location_observation_id": observation_id,
        "interest": "CULTURE",
        "budget_minutes": 30,
        "radius_m": 1200,
    }
    if consent is not None:
        payload["consent_external_location"] = consent
    return client.post(
        f"/api/v1/journeys/{journey_id}/moment-scout/live",
        headers=_auth(traveler_id),
        json=payload,
    )


def test_r67_live_search_requires_consent_and_rejects_unusable_location_without_provider_call(monkeypatch):
    journey_id, owner_id, other_id = _active_shared_journey()
    observation_id = _r67_location(journey_id, owner_id)
    calls = _install_r67_provider(
        monkeypatch,
        candidates=[_r67_candidate()],
        attempts=[{
            "endpoint": "https://overpass.example/api/interpreter",
            "answered": True,
            "http_status": 200,
        }],
    )

    missing_consent = _r67_live_request(journey_id, owner_id, observation_id, consent=None)
    declined = _r67_live_request(journey_id, owner_id, observation_id, consent=False)
    assert missing_consent.status_code == 422
    assert declined.status_code == 422
    assert calls == []

    object.__setattr__(settings, "real_city_enabled", False)
    disabled = _r67_live_request(journey_id, owner_id, observation_id)
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["scout_state"] == "PROVIDER_DISABLED"
    assert calls == []
    object.__setattr__(settings, "real_city_enabled", True)

    foreign_observation_id = _r67_location(journey_id, other_id)
    foreign = _r67_live_request(journey_id, owner_id, foreign_observation_id)
    stale_observation_id = _r67_location(journey_id, owner_id, age_seconds=900)
    stale = _r67_live_request(journey_id, owner_id, stale_observation_id)
    imprecise_observation_id = _r67_location(
        journey_id, owner_id, accuracy=10_000
    )
    imprecise = _r67_live_request(journey_id, owner_id, imprecise_observation_id)
    assert foreign.status_code == stale.status_code == imprecise.status_code == 200
    assert all(
        response.json()["scout_state"] == "LOCATION_UNAVAILABLE"
        for response in (foreign, stale, imprecise)
    )
    assert calls == []


def test_r67_live_zero_and_provider_failure_are_distinct_and_unnamed_osm_is_not_actionable(monkeypatch):
    journey_id, owner_id, _other_id = _active_shared_journey()
    observation_id = _r67_location(journey_id, owner_id)
    failed_calls = _install_r67_provider(
        monkeypatch,
        error=httpx.ReadTimeout("request data must not be echoed"),
    )
    failed = _r67_live_request(journey_id, owner_id, observation_id)
    assert failed.status_code == 200, failed.text
    assert failed.json()["scout_state"] == "PROVIDER_UNAVAILABLE"
    assert "request data must not be echoed" not in failed.text
    assert len(failed_calls) == 1

    answered_attempt = [{
        "endpoint": "https://overpass.example/api/interpreter?token=secret",
        "answered": True,
        "http_status": 200,
        "elapsed_ms": 14,
        "elements": 1,
    }]
    zero_calls = _install_r67_provider(
        monkeypatch, candidates=[], attempts=answered_attempt
    )
    zero = _r67_live_request(journey_id, owner_id, observation_id)
    assert zero.json()["scout_state"] == "LIVE_ZERO_RESULTS"
    assert zero.json()["candidates"] == []
    assert "token=secret" not in zero.text
    assert len(zero_calls) == 1

    unnamed_calls = _install_r67_provider(
        monkeypatch,
        candidates=[_r67_candidate(name="OSM museum")],
        attempts=answered_attempt,
    )
    unnamed = _r67_live_request(journey_id, owner_id, observation_id)
    assert unnamed.json()["scout_state"] == "LIVE_ZERO_RESULTS"
    assert unnamed.json()["empty_reason"] == "NO_ACTIONABLE_POIS"
    assert unnamed.json()["candidates"] == []
    assert len(unnamed_calls) == 1



def test_r67_live_scout_excludes_shared_plan_stops_and_rendezvous(monkeypatch):
    journey_id, owner_id, _other_id = _active_shared_journey()
    observation_id = _r67_location(journey_id, owner_id)
    shared_external_id = "overpass:node/88990011"
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        activity = session.scalar(
            select(Activity).where(
                Activity.plan_version_id == str(journey.active_plan_version_id),
                Activity.place_id.is_not(None),
            ).order_by(Activity.sequence_no)
        )
        assert activity is not None
        shared_place = session.get(Place, activity.place_id)
        assert shared_place is not None
        shared_place.provider_key = "overpass"
        shared_place.external_id = shared_external_id
        shared_place.name = "R67 shared itinerary museum"
        shared_place.category = "museum"
        shared_place.latitude = 5.123956
        shared_place.longitude = 10.654321
        session.commit()

    candidates = [
        _r67_candidate(
            osm_id="88990011",
            name="R67 shared itinerary museum",
            latitude=5.123956,
            longitude=10.654321,
        ),
        _r67_candidate(
            osm_id="88990012",
            name="R67 genuinely new museum",
            latitude=5.124456,
            longitude=10.654321,
        ),
    ]
    _install_r67_provider(
        monkeypatch,
        candidates=candidates,
        attempts=[{"endpoint": "https://overpass.example/api/interpreter", "answered": True, "http_status": 200}],
    )
    response = _r67_live_request(journey_id, owner_id, observation_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scout_state"] == "LIVE_RESULTS"
    assert [item["name"] for item in body["candidates"]] == ["R67 genuinely new museum"]


def test_r67_live_scout_cannot_select_after_shared_rendezvous_has_passed(monkeypatch):
    journey_id, owner_id, _other_id = _active_shared_journey()
    observation_id = _r67_location(journey_id, owner_id)
    candidate = _r67_candidate(osm_id="88990013", name="R67 late-night museum")
    _install_r67_provider(
        monkeypatch,
        candidates=[candidate],
        attempts=[{"endpoint": "https://overpass.example/api/interpreter", "answered": True, "http_status": 200}],
    )
    response = _r67_live_request(journey_id, owner_id, observation_id)
    assert response.status_code == 200, response.text
    receipt_id = response.json()["receipt_id"]
    candidate_id = response.json()["candidates"][0]["candidate_id"]
    with SessionLocal() as session:
        receipt = session.get(MemberLiveMomentScoutReceipt, receipt_id)
        assert receipt is not None
        activity = session.get(Activity, receipt.rendezvous_activity_id)
        assert activity is not None
        activity.start_at = (
            execution_clock.now().astimezone(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        session.commit()
    selection = client.post(
        f"/api/v1/journeys/{journey_id}/moment-scout/live/select",
        headers=_auth(owner_id),
        json={
            "receipt_id": receipt_id,
            "candidate_id": candidate_id,
            "confirm_return_to_rendezvous": True,
        },
    )
    assert selection.status_code == 409, selection.text
    assert "LIVE_MOMENT_SCOUT_RENDEZVOUS_TIME_PASSED" in selection.text
    assert _count(MemberSideQuest, MemberSideQuest.journey_id == journey_id) == 0
    assert _count(Place, Place.provider_key == "overpass", Place.external_id == candidate.external_id) == 0


def test_r67_live_osm_selection_materializes_only_after_owner_confirmation_and_is_idempotent(monkeypatch):
    journey_id, owner_id, other_id = _active_shared_journey()
    observation_id = _r67_location(journey_id, owner_id)
    candidate = _r67_candidate(
        osm_id="987654321", name="R67 controlled-provider museum"
    )
    attempts = [{
        "endpoint": "https://overpass.example/api/interpreter?lat=private",
        "answered": True,
        "http_status": 200,
        "elapsed_ms": 17,
        "elements": 1,
    }]
    calls = _install_r67_provider(monkeypatch, candidates=[candidate], attempts=attempts)
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        active_plan_id = str(journey.active_plan_version_id)
    place_count_before = _count(
        Place,
        Place.provider_key == "overpass",
        Place.external_id == candidate.external_id,
    )
    plan_count_before = _count(PlanVersion, PlanVersion.journey_id == journey_id)
    intent_count_before = _count(JourneyIntent, JourneyIntent.journey_id == journey_id)
    quest_count_before = _count(MemberSideQuest, MemberSideQuest.journey_id == journey_id)
    event_count_before = _count(Event, Event.journey_id == journey_id)
    public_candidate_count_before = _count(
        RealPlaceCandidate, RealPlaceCandidate.journey_id == journey_id
    )
    provider_call_count_before = _count(ProviderCall)

    discovered = _r67_live_request(journey_id, owner_id, observation_id)
    assert discovered.status_code == 200, discovered.text
    result = discovered.json()
    assert result["scout_state"] == "LIVE_RESULTS"
    assert result["provider_provenance"] == "LIVE_OPENSTREETMAP_OVERPASS"
    assert result["attribution"] == "© OpenStreetMap contributors"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["name"] == candidate.name
    assert result["candidates"][0]["opening_state"] == "UNKNOWN"
    assert result["candidates"][0]["fit_authority"] == "UNVERIFIED"
    assert "5.123456" not in discovered.text and "10.654321" not in discovered.text
    assert "lat=private" not in discovered.text
    assert calls == [{
        "query": "museum",
        "latitude": 5.123,
        "longitude": 10.654,
        "radius_m": 1200,
        "limit": 12,
    }]
    assert _count(
        Place,
        Place.provider_key == "overpass",
        Place.external_id == candidate.external_id,
    ) == place_count_before
    assert _count(PlanVersion, PlanVersion.journey_id == journey_id) == plan_count_before
    assert _count(JourneyIntent, JourneyIntent.journey_id == journey_id) == intent_count_before
    assert _count(MemberSideQuest, MemberSideQuest.journey_id == journey_id) == quest_count_before
    assert _count(
        RealPlaceCandidate, RealPlaceCandidate.journey_id == journey_id
    ) == public_candidate_count_before
    assert _count(ProviderCall) == provider_call_count_before
    assert _count(Event, Event.journey_id == journey_id) == event_count_before

    receipt_id = result["receipt_id"]
    candidate_id = result["candidates"][0]["candidate_id"]
    foreign_select = client.post(
        f"/api/v1/journeys/{journey_id}/moment-scout/live/select",
        headers=_auth(other_id),
        json={
            "receipt_id": receipt_id,
            "candidate_id": candidate_id,
            "confirm_return_to_rendezvous": True,
        },
    )
    assert foreign_select.status_code == 404

    collaborator_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(other_id)
    )
    assert collaborator_projection.status_code == 200
    assert candidate.name not in collaborator_projection.text
    assert candidate.external_id not in collaborator_projection.text
    assert receipt_id not in collaborator_projection.text

    selected = client.post(
        f"/api/v1/journeys/{journey_id}/moment-scout/live/select",
        headers=_auth(owner_id),
        json={
            "receipt_id": receipt_id,
            "candidate_id": candidate_id,
            "confirm_return_to_rendezvous": True,
        },
    )
    assert selected.status_code == 201, selected.text
    assert selected.json()["member_side_quest"]["state"] == "READY"
    assert selected.json()["member_side_quest"]["destination"]["title"] == candidate.name
    collaborator_after = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(other_id)
    )
    assert candidate.name not in collaborator_after.text
    assert candidate.external_id not in collaborator_after.text

    with SessionLocal() as session:
        receipt = session.get(MemberLiveMomentScoutReceipt, receipt_id)
        assert receipt is not None
        assert receipt.status == "SELECTED"
        assert receipt.side_quest_id == selected.json()["member_side_quest"]["id"]
        assert "5.123456" not in receipt.provider_attempts_json
        assert "10.654321" not in receipt.provider_attempts_json
        current_journey = session.get(Journey, journey_id)
        assert current_journey is not None
        assert str(current_journey.active_plan_version_id) == active_plan_id
    assert _count(
        Place,
        Place.provider_key == "overpass",
        Place.external_id == candidate.external_id,
    ) == 1
    assert _count(PlanVersion, PlanVersion.journey_id == journey_id) == plan_count_before
    assert _count(JourneyIntent, JourneyIntent.journey_id == journey_id) == intent_count_before + 1
    assert _count(MemberSideQuest, MemberSideQuest.journey_id == journey_id) == quest_count_before + 1
    assert _count(
        RealPlaceCandidate, RealPlaceCandidate.journey_id == journey_id
    ) == public_candidate_count_before
    assert _count(ProviderCall) == provider_call_count_before
    assert _count(Event, Event.journey_id == journey_id) == event_count_before

    repeated = client.post(
        f"/api/v1/journeys/{journey_id}/moment-scout/live/select",
        headers=_auth(owner_id),
        json={
            "receipt_id": receipt_id,
            "candidate_id": candidate_id,
            "confirm_return_to_rendezvous": True,
        },
    )
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["member_side_quest"]["id"] == selected.json()["member_side_quest"]["id"]
    assert _count(
        Place,
        Place.provider_key == "overpass",
        Place.external_id == candidate.external_id,
    ) == 1
    assert _count(JourneyIntent, JourneyIntent.journey_id == journey_id) == intent_count_before + 1
    assert _count(MemberSideQuest, MemberSideQuest.journey_id == journey_id) == quest_count_before + 1


def test_r69_meetup_handshake_requires_peer_and_ends_only_the_owners_split():
    journey_id, owner_id, peer_id = _active_shared_journey()
    split = _start_split(journey_id, owner_id)
    private_place = _private_place(journey_id, split.get("rendezvous_place_id"))
    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": private_place.id},
    )
    assert created.status_code == 201, created.text
    quest_id = created.json()["member_side_quest"]["id"]
    for action in ("start", "arrive", "complete"):
        response = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/{action}",
            headers=_auth(owner_id),
        )
        assert response.status_code == 200, response.text

    plan_before = client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]["id"]
    report_path = f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/meetup-report"
    reported = client.post(report_path, headers=_auth(owner_id))
    assert reported.status_code == 200, reported.text
    handshake = reported.json()["group_presence"]["meetup_handshake"]["owner"]
    assert handshake["state"] == "PENDING"
    handshake_id = handshake["id"]
    assert handshake["physical_presence_verified"] is False
    repeated = client.post(report_path, headers=_auth(owner_id))
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["group_presence"]["meetup_handshake"]["owner"]["id"] == handshake_id
    assert _count(
        PlannedSplitMeetupHandshake,
        PlannedSplitMeetupHandshake.journey_id == journey_id,
    ) == 1
    first_handshake_id = handshake_id

    peer_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(peer_id)
    )
    assert peer_view.status_code == 200, peer_view.text
    assert private_place.name not in peer_view.text
    incoming = peer_view.json()["group_presence"]["meetup_handshake"]["incoming"]
    assert len(incoming) == 1
    assert incoming[0]["id"] == first_handshake_id

    replacement_place = _private_place(
        journey_id, split.get("rendezvous_place_id"), private_place.id
    )
    replacement = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": replacement_place.id},
    )
    assert replacement.status_code == 201, replacement.text
    replacement_quest_id = replacement.json()["member_side_quest"]["id"]
    stale_ack = client.post(
        f"/api/v1/journeys/{journey_id}/meetup-handshakes/{first_handshake_id}/acknowledge",
        headers=_auth(peer_id),
    )
    assert stale_ack.status_code == 409
    assert client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]["id"] == plan_before
    for action in ("start", "arrive", "complete"):
        response = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{replacement_quest_id}/{action}",
            headers=_auth(owner_id),
        )
        assert response.status_code == 200, response.text
    report_path = f"/api/v1/journeys/{journey_id}/member-side-quests/{replacement_quest_id}/meetup-report"
    renewed = client.post(report_path, headers=_auth(owner_id))
    assert renewed.status_code == 200, renewed.text
    declined_handshake_id = renewed.json()["group_presence"]["meetup_handshake"]["owner"]["id"]
    assert declined_handshake_id != first_handshake_id
    peer_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(peer_id)
    )
    assert peer_view.status_code == 200, peer_view.text
    incoming = peer_view.json()["group_presence"]["meetup_handshake"]["incoming"]
    assert len(incoming) == 1 and incoming[0]["id"] == declined_handshake_id

    not_yet = client.post(
        f"/api/v1/journeys/{journey_id}/meetup-handshakes/{declined_handshake_id}/not-yet",
        headers=_auth(peer_id),
    )
    assert not_yet.status_code == 200, not_yet.text
    declined_owner_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    )
    assert declined_owner_view.json()["group_presence"]["meetup_handshake"]["owner"]["state"] == "NOT_CONFIRMED"
    assert declined_owner_view.json()["group_presence"]["planned_separation"] is not None
    assert client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]["id"] == plan_before

    renewed = client.post(report_path, headers=_auth(owner_id))
    assert renewed.status_code == 200, renewed.text
    handshake_id = renewed.json()["group_presence"]["meetup_handshake"]["owner"]["id"]
    assert handshake_id != declined_handshake_id

    self_ack = client.post(
        f"/api/v1/journeys/{journey_id}/meetup-handshakes/{handshake_id}/acknowledge",
        headers=_auth(owner_id),
    )
    assert self_ack.status_code == 403
    acknowledged = client.post(
        f"/api/v1/journeys/{journey_id}/meetup-handshakes/{handshake_id}/acknowledge",
        headers=_auth(peer_id),
    )
    assert acknowledged.status_code == 200, acknowledged.text
    acked = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    )
    assert acked.status_code == 200, acked.text
    assert acked.json()["group_presence"]["meetup_handshake"]["owner"]["state"] == "PEER_ACKNOWLEDGED"
    assert acked.json()["group_presence"]["meetup_handshake"]["owner"]["physical_presence_verified"] is False
    assert acked.json()["group_presence"]["planned_separation"] is None
    assert client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]["id"] == plan_before
    with SessionLocal() as session:
        intent = session.get(JourneyIntent, split["intent_id"])
        assert intent is not None and intent.status == "CANCELLED"
        row = session.get(PlannedSplitMeetupHandshake, handshake_id)
        assert row is not None
        assert row.state == "PEER_ACKNOWLEDGED"
        assert row.responder_traveler_id == peer_id
        first_row = session.get(PlannedSplitMeetupHandshake, first_handshake_id)
        assert first_row is not None and first_row.state == "INVALIDATED"
        declined_row = session.get(PlannedSplitMeetupHandshake, declined_handshake_id)
        assert declined_row is not None and declined_row.state == "NOT_CONFIRMED"

    duplicate_ack = client.post(
        f"/api/v1/journeys/{journey_id}/meetup-handshakes/{handshake_id}/acknowledge",
        headers=_auth(peer_id),
    )
    assert duplicate_ack.status_code == 200, duplicate_ack.text
    assert _count(
        AuditLog,
        AuditLog.journey_id == journey_id,
        AuditLog.action == "journey.planned_split_meetup.peer_acknowledged",
    ) == 1


def test_r69_respects_r62_waiting_gate_and_does_not_fabricate_a_peer(monkeypatch):
    from app.execution import planned_split_meetup_handshake as handshake_module

    journey_id, owner_id, peer_id = _active_shared_journey()
    split = _start_split(journey_id, owner_id)
    private_place = _private_place(journey_id, split.get("rendezvous_place_id"))
    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": private_place.id},
    )
    assert created.status_code == 201, created.text
    quest_id = created.json()["member_side_quest"]["id"]
    for action in ("start", "arrive", "complete"):
        response = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/{action}",
            headers=_auth(owner_id),
        )
        assert response.status_code == 200, response.text

    monkeypatch.setattr(
        handshake_module,
        "active_group_recovery_spend",
        lambda *_args, **_kwargs: (object(), {}, {"state": "RECOVERY_APPLIED_REJOIN_PENDING"}),
    )
    monkeypatch.setattr(
        handshake_module,
        "followthrough_projection",
        lambda *_args, **_kwargs: {"state": "REJOIN_IN_PROGRESS"},
    )
    report_path = f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/meetup-report"
    blocked_by_r62 = client.post(report_path, headers=_auth(owner_id))
    assert blocked_by_r62.status_code == 409
    assert "R62_REJOIN_REQUIRED" in blocked_by_r62.text
    assert _count(
        PlannedSplitMeetupHandshake,
        PlannedSplitMeetupHandshake.journey_id == journey_id,
    ) == 0
    monkeypatch.undo()
    with SessionLocal() as session:
        peer = session.scalar(
            select(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == peer_id,
            )
        )
        assert peer is not None
        peer.removed_at = execution_clock.now().astimezone(timezone.utc).isoformat()
        session.commit()
    no_peer = client.post(report_path, headers=_auth(owner_id))
    assert no_peer.status_code == 409
    assert "MEETUP_HANDSHAKE_REQUIRES_CURRENT_PEER" in no_peer.text
    assert _count(
        PlannedSplitMeetupHandshake,
        PlannedSplitMeetupHandshake.journey_id == journey_id,
    ) == 0


def test_r69_one_peer_ack_in_three_person_journey_does_not_claim_group_together():
    journey_id, owner_id, other_peer_id = _active_shared_journey()
    third_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        session.add(Traveler(
            id=third_id,
            display_name="R69 Third Member",
            email=None,
            locale="en-US",
            timezone="UTC",
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        session.add(JourneyMember(
            id=str(uuid4()),
            journey_id=journey_id,
            traveler_id=third_id,
            role="COLLABORATOR",
            invited_by=owner_id,
            expires_at=None,
            joined_at=now,
            removed_at=None,
            preferences_json="{}",
            created_at=now,
            updated_at=now,
        ))
        session.commit()
    split = _start_split(journey_id, owner_id)
    private_place = _private_place(journey_id, split.get("rendezvous_place_id"))
    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": private_place.id},
    )
    assert created.status_code == 201, created.text
    quest_id = created.json()["member_side_quest"]["id"]
    for action in ("start", "arrive", "complete"):
        response = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/{action}",
            headers=_auth(owner_id),
        )
        assert response.status_code == 200, response.text
    plan_before = client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]["id"]
    report = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/meetup-report",
        headers=_auth(owner_id),
    )
    assert report.status_code == 200, report.text
    handshake_id = report.json()["group_presence"]["meetup_handshake"]["owner"]["id"]
    acknowledged = client.post(
        f"/api/v1/journeys/{journey_id}/meetup-handshakes/{handshake_id}/acknowledge",
        headers=_auth(third_id),
    )
    assert acknowledged.status_code == 200, acknowledged.text

    group_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(other_peer_id)
    )
    assert group_view.status_code == 200, group_view.text
    presence = group_view.json()["group_presence"]
    assert presence["planned_separation"] is None
    assert presence["state"] != "TOGETHER"
    resolved = presence["meetup_handshake"]["resolved"]
    assert len(resolved) == 1
    assert resolved[0]["viewer_acknowledged"] is False
    assert resolved[0]["physical_presence_verified"] is False
    assert resolved[0]["planned_split_state"] == "PLANNED_SPLIT_ENDED"
    acknowledger_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(third_id)
    )
    assert acknowledger_view.status_code == 200, acknowledger_view.text
    assert acknowledger_view.json()["group_presence"]["meetup_handshake"]["resolved"][0][
        "viewer_acknowledged"
    ] is True
    assert client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]["id"] == plan_before



def test_r69_new_side_trip_gets_a_new_meetup_action_not_old_success():
    journey_id, owner_id, peer_id = _active_shared_journey()
    first_split = _start_split(journey_id, owner_id)
    first_place = _private_place(journey_id, first_split.get("rendezvous_place_id"))
    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": first_place.id},
    )
    assert created.status_code == 201, created.text
    first_quest_id = created.json()["member_side_quest"]["id"]
    for action in ("start", "arrive", "complete"):
        reply = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{first_quest_id}/{action}",
            headers=_auth(owner_id),
        )
        assert reply.status_code == 200, reply.text
    first_report = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{first_quest_id}/meetup-report",
        headers=_auth(owner_id),
    )
    assert first_report.status_code == 200, first_report.text
    first_handshake_id = first_report.json()["group_presence"]["meetup_handshake"]["owner"]["id"]
    ack = client.post(
        f"/api/v1/journeys/{journey_id}/meetup-handshakes/{first_handshake_id}/acknowledge",
        headers=_auth(peer_id),
    )
    assert ack.status_code == 200, ack.text
    past_success = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    ).json()["group_presence"]["meetup_handshake"]["owner"]
    assert past_success["state"] == "PEER_ACKNOWLEDGED"

    second_split = _start_split(journey_id, owner_id)
    assert second_split["intent_id"] != first_split["intent_id"]
    during_new_split = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    ).json()["group_presence"]["meetup_handshake"]
    # The previous completed meetup must not be presented as current
    # while the member is away again and has not finished this side quest.
    assert during_new_split["owner"] is None

    second_place = _private_place(
        journey_id, second_split.get("rendezvous_place_id"), first_place.id
    )
    second = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": second_place.id},
    )
    assert second.status_code == 201, second.text
    second_quest_id = second.json()["member_side_quest"]["id"]
    for action in ("start", "arrive", "complete"):
        advanced = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{second_quest_id}/{action}",
            headers=_auth(owner_id),
        )
        assert advanced.status_code == 200, advanced.text
    current_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    ).json()["group_presence"]["meetup_handshake"]["owner"]
    assert current_view["state"] == "NOT_REPORTED"
    assert current_view["can_report"] is True

    new_report = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{second_quest_id}/meetup-report",
        headers=_auth(owner_id),
    )
    assert new_report.status_code == 200, new_report.text
    owner_meetup = new_report.json()["group_presence"]["meetup_handshake"]["owner"]
    assert owner_meetup["state"] == "PENDING"
    assert owner_meetup["id"] != first_handshake_id
    with SessionLocal() as session:
        first_row = session.get(PlannedSplitMeetupHandshake, first_handshake_id)
        assert first_row is not None and first_row.state == "PEER_ACKNOWLEDGED"


def test_r69_expired_request_cannot_be_acknowledged_and_can_be_resent():
    journey_id, owner_id, peer_id = _active_shared_journey()
    split = _start_split(journey_id, owner_id)
    private_place = _private_place(journey_id, split.get("rendezvous_place_id"))
    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": private_place.id},
    )
    assert created.status_code == 201, created.text
    quest_id = created.json()["member_side_quest"]["id"]
    for action in ("start", "arrive", "complete"):
        response = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/{action}",
            headers=_auth(owner_id),
        )
        assert response.status_code == 200, response.text
    report_path = f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/meetup-report"
    first = client.post(report_path, headers=_auth(owner_id))
    assert first.status_code == 200, first.text
    first_id = first.json()["group_presence"]["meetup_handshake"]["owner"]["id"]
    with SessionLocal() as session:
        row = session.get(PlannedSplitMeetupHandshake, first_id)
        assert row is not None
        row.expires_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        session.commit()

    owner_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    )
    assert owner_view.json()["group_presence"]["meetup_handshake"]["owner"]["state"] == "EXPIRED"
    stale_ack = client.post(
        f"/api/v1/journeys/{journey_id}/meetup-handshakes/{first_id}/acknowledge",
        headers=_auth(peer_id),
    )
    assert stale_ack.status_code == 409
    resent = client.post(report_path, headers=_auth(owner_id))
    assert resent.status_code == 200, resent.text
    resent_id = resent.json()["group_presence"]["meetup_handshake"]["owner"]["id"]
    assert resent_id != first_id
    assert resent.json()["group_presence"]["meetup_handshake"]["owner"]["state"] == "PENDING"
    with SessionLocal() as session:
        expired = session.get(PlannedSplitMeetupHandshake, first_id)
        assert expired is not None and expired.state == "EXPIRED"
        pending = session.get(PlannedSplitMeetupHandshake, resent_id)
        assert pending is not None and pending.state == "PENDING"


def _create_owner_invitation(journey_id: str, owner_id: str) -> tuple[str, str]:
    _start_split(journey_id, owner_id)
    place = _private_place(journey_id)
    created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(owner_id),
        json={"place_id": place.id},
    )
    assert created.status_code == 201, created.text
    quest = created.json()["member_side_quest"]
    assert quest["destination"]["authority"] == "EXACT_KNOWN_PLACE"
    return str(quest["id"]), str(place.id)


def _create_r71_invitation(
    journey_id: str,
    owner_id: str,
    *,
    rendezvous_lead_minutes: int = 90,
) -> tuple[str, str, str]:
    side_quest_id, place_id = _create_owner_invitation(journey_id, owner_id)
    with SessionLocal() as session:
        owner_quest = session.get(MemberSideQuest, side_quest_id)
        assert owner_quest is not None
        target = session.get(Activity, str(owner_quest.rendezvous_activity_id))
        assert target is not None
        target.start_at = (datetime.now(timezone.utc) + timedelta(minutes=rendezvous_lead_minutes)).isoformat()
        session.commit()
    preview = client.get(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{side_quest_id}/place-invitation-preview",
        headers=_auth(owner_id),
    )
    assert preview.status_code == 200, preview.text
    sent = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{side_quest_id}/place-invitations",
        headers=_auth(owner_id),
        json={
            "confirm_public_place_sharing": True,
            "audience_snapshot": preview.json()["audience_snapshot"],
        },
    )
    assert sent.status_code == 201, sent.text
    invitation_id = sent.json()["member_place_invitations"]["sent"][0]["invitation_id"]
    return str(invitation_id), side_quest_id, place_id


def _member_place_invitation_audience_token(
    journey_id: str, invitation_id: str, traveler_id: str
) -> str:
    projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(traveler_id)
    )
    assert projection.status_code == 200, projection.text
    boards = projection.json()["member_place_invitations"]
    invitation = next(
        row
        for group in ("sent", "joined")
        for row in boards[group]
        if row["invitation_id"] == invitation_id
    )
    token = invitation["own_progress"]["share_audience_token"]
    assert isinstance(token, str) and len(token) == 64
    return token


def _post_member_place_invitation_progress(
    journey_id: str,
    invitation_id: str,
    traveler_id: str,
    action: str,
    *,
    audience_token: str | None = None,
):
    payload = {"action": action}
    if action.startswith("SHARE_"):
        payload["share_audience_token"] = audience_token or _member_place_invitation_audience_token(
            journey_id, invitation_id, traveler_id
        )
    return client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/progress",
        headers=_auth(traveler_id),
        json=payload,
    )


def test_r70_place_invitation_is_member_scoped_and_accept_creates_own_stop():
    journey_id, owner_id, recipient_id = _active_shared_journey()
    now = datetime.now(timezone.utc).isoformat()
    third_id = str(uuid4())
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        session.add(Traveler(
            id=third_id,
            display_name="R70第三位同行者",
            email=None,
            locale="zh-CN",
            timezone="UTC",
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        session.add(JourneyMember(
            id=str(uuid4()),
            journey_id=journey_id,
            traveler_id=third_id,
            role="COLLABORATOR",
            invited_by=owner_id,
            expires_at=None,
            joined_at=now,
            removed_at=None,
            preferences_json="{}",
            created_at=now,
            updated_at=now,
        ))
        active_plan_before = str(journey.active_plan_version_id)
        session.commit()

    side_quest_id, place_id = _create_owner_invitation(journey_id, owner_id)
    rendezvous_commitment_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        owner_quest = session.get(MemberSideQuest, side_quest_id)
        assert owner_quest is not None
        session.add(Commitment(
            id=rendezvous_commitment_id,
            journey_id=journey_id,
            activity_id=owner_quest.rendezvous_activity_id,
            kind="APPOINTMENT",
            level="C3_Guaranteed",
            status="ACTIVE",
            monetary_value_minor=0,
            currency="CNY",
            external_ref="R70-PROTECTED-RENDEZVOUS",
            policy_json='{"owner":"R70 acceptance regression"}',
            created_at=now,
            updated_at=now,
        ))
        session.commit()
    before_invitation = client.get(
        f"/api/v1/journeys/{journey_id}/operator",
        headers=_auth(recipient_id),
    ).json()
    assert before_invitation["member_side_quest"] is None
    assert before_invitation["member_place_invitations"]["incoming"] == []
    with SessionLocal() as session:
        private_place = session.get(Place, place_id)
        assert private_place is not None
        private_place_name = private_place.name
    assert private_place_name not in json.dumps(before_invitation, ensure_ascii=False)
    preview = client.get(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{side_quest_id}/place-invitation-preview",
        headers=_auth(owner_id),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["recipient_count"] == 2
    assert preview.json()["place"]["place_id"] == place_id
    assert "device location" in preview.json()["disclosure"]

    late_id = str(uuid4())
    late_now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(Traveler(
            id=late_id,
            display_name="R70稍后加入者",
            email=None,
            locale="zh-CN",
            timezone="UTC",
            created_at=late_now,
            updated_at=late_now,
        ))
        session.flush()
        session.add(JourneyMember(
            id=str(uuid4()),
            journey_id=journey_id,
            traveler_id=late_id,
            role="COLLABORATOR",
            invited_by=owner_id,
            expires_at=None,
            joined_at=late_now,
            removed_at=None,
            preferences_json="{}",
            created_at=late_now,
            updated_at=late_now,
        ))
        session.commit()
    stale_audience = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{side_quest_id}/place-invitations",
        headers=_auth(owner_id),
        json={
            "confirm_public_place_sharing": True,
            "audience_snapshot": preview.json()["audience_snapshot"],
        },
    )
    assert stale_audience.status_code == 409
    assert stale_audience.json()["error"]["message"] == "PLACE_INVITATION_AUDIENCE_CHANGED"
    preview = client.get(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{side_quest_id}/place-invitation-preview",
        headers=_auth(owner_id),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["recipient_count"] == 3

    sent = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{side_quest_id}/place-invitations",
        headers=_auth(owner_id),
        json={
            "confirm_public_place_sharing": True,
            "audience_snapshot": preview.json()["audience_snapshot"],
        },
    )
    assert sent.status_code == 201, sent.text
    owner_projection = sent.json()["member_place_invitations"]
    invitation = owner_projection["sent"][0]
    invitation_id = invitation["invitation_id"]
    assert len(invitation["recipients"]) == 3

    # A member who joins after the owner's explicit preview is never silently
    # added to the invitation's fixed audience.
    post_send_id = str(uuid4())
    post_send_now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(Traveler(
            id=post_send_id,
            display_name="R70发送后加入者",
            email=None,
            locale="zh-CN",
            timezone="UTC",
            created_at=post_send_now,
            updated_at=post_send_now,
        ))
        session.flush()
        session.add(JourneyMember(
            id=str(uuid4()),
            journey_id=journey_id,
            traveler_id=post_send_id,
            role="COLLABORATOR",
            invited_by=owner_id,
            expires_at=None,
            joined_at=post_send_now,
            removed_at=None,
            preferences_json="{}",
            created_at=post_send_now,
            updated_at=post_send_now,
        ))
        session.commit()
    post_send_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(post_send_id)
    ).json()["member_place_invitations"]
    assert post_send_projection["incoming"] == []
    not_invited = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(post_send_id),
        json={"decision": "ACCEPT", "confirm_exact_public_place": True},
    )
    assert not_invited.status_code == 404

    for traveler_id in (recipient_id, third_id, late_id):
        projection = client.get(
            f"/api/v1/journeys/{journey_id}/operator",
            headers=_auth(traveler_id),
        ).json()["member_place_invitations"]
        assert [row["invitation_id"] for row in projection["incoming"]] == [invitation_id]
        assert "recipients" not in projection["incoming"][0]
        assert projection["incoming"][0]["place"]["place_id"] == place_id

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        before_side_effects = {
            "active_plan_version_id": str(journey.active_plan_version_id),
            "current_activity_id": journey.current_activity_id,
            "activities": session.scalar(select(func.count()).select_from(Activity)),
            "location_observations": session.scalar(select(func.count()).select_from(LocationObservation)),
            "agent_runs": session.scalar(select(func.count()).select_from(AgentRun)),
            "guardian_pulses": session.scalar(select(func.count()).select_from(GuardianPulse)),
        }
        protected_before = session.get(Commitment, rendezvous_commitment_id)
        assert protected_before is not None
        protected_snapshot = (
            protected_before.activity_id,
            protected_before.level,
            protected_before.status,
            protected_before.policy_json,
        )
    accepted = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(recipient_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "acknowledge_feasibility_unknown": True,
        },
    )
    assert accepted.status_code == 200, accepted.text
    own_quest = accepted.json()["member_side_quest"]
    assert own_quest["destination"]["place_id"] == place_id
    assert own_quest["state"] == "READY"
    assert accepted.json()["member_place_invitations"]["incoming"] == []
    assert accepted.json()["member_place_invitations"]["sent"] == []
    assert accepted.json()["group_presence"]["planned_separation"]["members"]
    navigation = client.get(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{own_quest['id']}/navigation-targets",
        headers=_auth(recipient_id),
    )
    assert navigation.status_code == 200, navigation.text
    assert navigation.json()["target"]["kind"] == "PRIVATE_STOP"
    assert navigation.json()["target"]["place_id"] == place_id
    assert navigation.json()["target"]["coordinate_system"] == "WGS84"
    fit = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{own_quest['id']}/fit-preview",
        headers=_auth(recipient_id),
        json={"location_observation_id": None, "dwell_minutes": 20, "requested_mode": "WALK"},
    )
    assert fit.status_code == 200, fit.text
    assert fit.json()["state"] == "UNKNOWN"
    assert fit.json()["reason_code"] == "CURRENT_LOCATION_UNAVAILABLE"
    assert fit.json()["origin_basis"]["outbound"] is None
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        assert str(journey.active_plan_version_id) == active_plan_before
        assert journey.current_activity_id == before_side_effects["current_activity_id"]
        assert session.scalar(select(func.count()).select_from(Activity)) == before_side_effects["activities"]
        assert session.scalar(select(func.count()).select_from(LocationObservation)) == before_side_effects["location_observations"]
        assert session.scalar(select(func.count()).select_from(AgentRun)) == before_side_effects["agent_runs"]
        assert session.scalar(select(func.count()).select_from(GuardianPulse)) == before_side_effects["guardian_pulses"]
        protected_after = session.get(Commitment, rendezvous_commitment_id)
        assert protected_after is not None
        assert (
            protected_after.activity_id,
            protected_after.level,
            protected_after.status,
            protected_after.policy_json,
        ) == protected_snapshot
        recipient_member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == recipient_id,
        ))
        assert recipient_member is not None
        recipient_quests = session.scalars(select(MemberSideQuest).where(
            MemberSideQuest.journey_id == journey_id,
            MemberSideQuest.member_id == recipient_member.id,
            MemberSideQuest.state.in_(("DESTINATION_UNRESOLVED", "READY", "EN_ROUTE", "ARRIVED")),
        )).all()
        assert len(recipient_quests) == 1
        assert recipient_quests[0].side_place_id == place_id
        owner_quest = session.get(MemberSideQuest, side_quest_id)
        assert owner_quest is not None and owner_quest.side_place_id == place_id
        response = session.scalar(select(MemberPlaceInvitationRecipient).where(
            MemberPlaceInvitationRecipient.invitation_id == invitation_id,
            MemberPlaceInvitationRecipient.recipient_traveler_id == recipient_id,
        ))
        assert response is not None and response.state == "ACCEPTED_OWN_STOP_READY"
        assert session.get(MemberPlaceInvitation, invitation_id).state == "OPEN"

    declined = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(third_id),
        json={"decision": "DECLINE"},
    )
    assert declined.status_code == 200, declined.text
    owner_after_response = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    ).json()["member_place_invitations"]["sent"][0]
    states = {recipient["member_id"]: recipient["state"] for recipient in owner_after_response["recipients"]}
    with SessionLocal() as session:
        recipient_member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == recipient_id,
        ))
        third_member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == third_id,
        ))
        late_member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == late_id,
        ))
        assert recipient_member and third_member and late_member
        assert states[str(recipient_member.id)] == "ACCEPTED_OWN_STOP_READY"
        assert states[str(third_member.id)] == "DECLINED"
        assert states[str(late_member.id)] == "INVITED"

    repeated = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(recipient_id),
        json={"decision": "ACCEPT", "confirm_exact_public_place": True},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["member_side_quest"]["id"] == own_quest["id"]
    withdrawn = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/withdraw",
        headers=_auth(owner_id),
    )
    assert withdrawn.status_code == 200, withdrawn.text
    after_withdraw = client.get(
        f"/api/v1/journeys/{journey_id}/operator",
        headers=_auth(recipient_id),
    ).json()
    assert after_withdraw["member_side_quest"]["id"] == own_quest["id"]
    rejected_after_withdraw = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(late_id),
        json={"decision": "ACCEPT", "confirm_exact_public_place": True},
    )
    assert rejected_after_withdraw.status_code == 409
    assert rejected_after_withdraw.json()["error"]["message"] == "PLACE_INVITATION_WITHDRAWN"
    late_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(late_id)
    ).json()
    assert late_projection["member_side_quest"] is None


def test_r72_invited_micro_group_progress_is_opt_in_member_bound_and_persistent():
    journey_id, owner_id, recipient_id = _active_shared_journey()
    third_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(Traveler(
            id=third_id,
            display_name="R72待加入同行者",
            email=None,
            locale="zh-CN",
            timezone="UTC",
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        session.add(JourneyMember(
            id=str(uuid4()),
            journey_id=journey_id,
            traveler_id=third_id,
            role="COLLABORATOR",
            invited_by=owner_id,
            expires_at=None,
            joined_at=now,
            removed_at=None,
            preferences_json="{}",
            created_at=now,
            updated_at=now,
        ))
        session.commit()

    invitation_id, owner_quest_id, place_id = _create_r71_invitation(journey_id, owner_id)
    accepted = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(recipient_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "acknowledge_feasibility_unknown": True,
        },
    )
    assert accepted.status_code == 200, accepted.text
    recipient_quest_id = accepted.json()["member_side_quest"]["id"]
    joined_participants = accepted.json()["member_place_invitations"]["joined"][0]["participants"]
    assert len(joined_participants) == 2
    assert [row["membership"] for row in joined_participants] == ["INVITER", "JOINED"]
    assert all(row["at_invited_place"] is None and row["heading_back"] is None for row in joined_participants)

    third_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(third_id)
    ).json()["member_place_invitations"]
    assert third_projection["joined"] == []
    assert third_projection["incoming"][0]["invitation_id"] == invitation_id
    progress_path = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/progress"
    pending_cannot_report = client.post(
        progress_path, headers=_auth(third_id), json={"action": "SHARE_ARRIVAL"}
    )
    assert pending_cannot_report.status_code == 404
    guessed_id_cannot_report = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{uuid4()}/progress",
        headers=_auth(recipient_id),
        json={"action": "SHARE_ARRIVAL"},
    )
    assert guessed_id_cannot_report.status_code == 404
    anonymous_cannot_report = client.post(progress_path, json={"action": "SHARE_ARRIVAL"})
    assert anonymous_cannot_report.status_code in {401, 403}

    b_cannot_report_before_r64_arrival = _post_member_place_invitation_progress(
        journey_id, invitation_id, recipient_id, "SHARE_ARRIVAL"
    )
    assert b_cannot_report_before_r64_arrival.status_code == 409
    a_cannot_report_before_r64_arrival = _post_member_place_invitation_progress(
        journey_id, invitation_id, owner_id, "SHARE_ARRIVAL"
    )
    assert a_cannot_report_before_r64_arrival.status_code == 409

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        shared_snapshot = (
            str(journey.active_plan_version_id),
            journey.current_activity_id,
            session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.journey_id == journey_id)),
            session.scalar(select(func.count()).select_from(GuardianPulse).where(GuardianPulse.journey_id == journey_id)),
            session.scalar(select(func.count()).select_from(ReplanProposal).where(ReplanProposal.journey_id == journey_id)),
            session.scalar(select(func.count()).select_from(PlannedSplitMeetupHandshake).where(PlannedSplitMeetupHandshake.journey_id == journey_id)),
            session.scalar(select(func.count()).select_from(Booking).where(Booking.journey_id == journey_id)),
            session.scalar(select(func.count()).select_from(Order).where(Order.journey_id == journey_id)),
            session.scalar(select(func.count()).select_from(PaymentTransaction).where(PaymentTransaction.journey_id == journey_id)),
        )
        invitation = session.get(MemberPlaceInvitation, invitation_id)
        assert invitation is not None
        invitation.expires_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        session.commit()

    for traveler_id, quest_id in ((owner_id, owner_quest_id), (recipient_id, recipient_quest_id)):
        started = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/start",
            headers=_auth(traveler_id),
        )
        assert started.status_code == 200, started.text
        arrived = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/arrive",
            headers=_auth(traveler_id),
        )
        assert arrived.status_code == 200, arrived.text
        assert arrived.json()["member_side_quest"]["arrival_claim"] == "MEMBER_REPORTED_NOT_PHYSICAL_PRESENCE_PROOF"

    # The parent invitation expired after B accepted. That stops new joins but does not
    # revoke the accepted member's still-current side quest or ability to share a new report.
    b_after_expiry = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(recipient_id)
    ).json()
    assert b_after_expiry["member_side_quest"]["id"] == recipient_quest_id
    assert b_after_expiry["member_place_invitations"]["joined"][0]["state"] == "EXPIRED"

    b_shared = _post_member_place_invitation_progress(
        journey_id, invitation_id, recipient_id, "SHARE_ARRIVAL"
    )
    assert b_shared.status_code == 200, b_shared.text
    b_reported_at = b_shared.json()["member_place_invitations"]["joined"][0]["participants"][1]["at_invited_place"]["reported_at"]
    b_shared_again = _post_member_place_invitation_progress(
        journey_id, invitation_id, recipient_id, "SHARE_ARRIVAL"
    )
    assert b_shared_again.status_code == 200, b_shared_again.text
    assert b_shared_again.json()["member_place_invitations"]["joined"][0]["participants"][1]["at_invited_place"]["reported_at"] == b_reported_at
    a_shared = _post_member_place_invitation_progress(
        journey_id, invitation_id, owner_id, "SHARE_ARRIVAL"
    )
    assert a_shared.status_code == 200, a_shared.text
    withdrawn_invitation = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/withdraw",
        headers=_auth(owner_id),
    )
    assert withdrawn_invitation.status_code == 200, withdrawn_invitation.text
    b_after_withdrawal = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(recipient_id)
    ).json()
    assert b_after_withdrawal["member_side_quest"]["id"] == recipient_quest_id
    assert b_after_withdrawal["member_place_invitations"]["joined"][0]["participants"][1]["at_invited_place"] is not None
    c_cannot_accept_after_withdrawal = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(third_id),
        json={"decision": "ACCEPT", "confirm_exact_public_place": True},
    )
    assert c_cannot_accept_after_withdrawal.status_code == 409
    assert c_cannot_accept_after_withdrawal.json()["error"]["message"] == "PLACE_INVITATION_WITHDRAWN"

    for traveler_id, quest_id in ((owner_id, owner_quest_id), (recipient_id, recipient_quest_id)):
        completed = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/complete",
            headers=_auth(traveler_id),
        )
        assert completed.status_code == 200, completed.text
        cannot_share_before_r65 = _post_member_place_invitation_progress(
            journey_id, invitation_id, traveler_id, "SHARE_HEADING_BACK"
        )
        assert cannot_share_before_r65.status_code == 409
        headed_back = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{quest_id}/head-back",
            headers=_auth(traveler_id),
        )
        assert headed_back.status_code == 200, headed_back.text

    b_shared_return = _post_member_place_invitation_progress(
        journey_id, invitation_id, recipient_id, "SHARE_HEADING_BACK"
    )
    assert b_shared_return.status_code == 200, b_shared_return.text
    a_shared_return = _post_member_place_invitation_progress(
        journey_id, invitation_id, owner_id, "SHARE_HEADING_BACK"
    )
    assert a_shared_return.status_code == 200, a_shared_return.text

    owner_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    ).json()["member_place_invitations"]["sent"][0]
    recipient_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(recipient_id)
    ).json()["member_place_invitations"]["joined"][0]
    assert len(owner_view["participants"]) == 2
    assert {row["membership"] for row in owner_view["participants"]} == {"INVITER", "JOINED"}
    assert all(row["at_invited_place"]["physical_presence_verified"] is False for row in owner_view["participants"])
    assert all(row["heading_back"]["physical_presence_verified"] is False for row in owner_view["participants"])
    assert len(recipient_view["participants"]) == 2
    assert place_id == recipient_view["place"]["place_id"]

    b_withdrew = client.post(
        progress_path, headers=_auth(recipient_id), json={"action": "WITHDRAW_ARRIVAL"}
    )
    assert b_withdrew.status_code == 200, b_withdrew.text
    owner_after_withdrawal = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    ).json()["member_place_invitations"]["sent"][0]
    b_owner_row = next(row for row in owner_after_withdrawal["participants"] if row["membership"] == "JOINED")
    assert b_owner_row["at_invited_place"] is None
    assert b_owner_row["heading_back"] is not None
    c_after_reports = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(third_id)
    ).json()["member_place_invitations"]
    assert c_after_reports["joined"] == []

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        assert (
            str(journey.active_plan_version_id),
            journey.current_activity_id,
            session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.journey_id == journey_id)),
            session.scalar(select(func.count()).select_from(GuardianPulse).where(GuardianPulse.journey_id == journey_id)),
            session.scalar(select(func.count()).select_from(ReplanProposal).where(ReplanProposal.journey_id == journey_id)),
            session.scalar(select(func.count()).select_from(PlannedSplitMeetupHandshake).where(PlannedSplitMeetupHandshake.journey_id == journey_id)),
            session.scalar(select(func.count()).select_from(Booking).where(Booking.journey_id == journey_id)),
            session.scalar(select(func.count()).select_from(Order).where(Order.journey_id == journey_id)),
            session.scalar(select(func.count()).select_from(PaymentTransaction).where(PaymentTransaction.journey_id == journey_id)),
        ) == shared_snapshot
        signals = session.scalars(
            select(MemberPlaceInvitationProgress).where(
                MemberPlaceInvitationProgress.invitation_id == invitation_id
            )
        ).all()
        assert len(signals) == 4
        assert len([row for row in signals if row.status == "ACTIVE"]) == 3
        b_arrival = next(
            row for row in signals
            if row.member_id == b_owner_row["member_id"] and row.signal_type == "AT_INVITED_PLACE_SELF_REPORTED"
        )
        assert b_arrival.status == "WITHDRAWN"
        assert b_arrival.consent_version == "R72_MICRO_GROUP_PROGRESS_V1"
        assert b_arrival.side_quest_id == recipient_quest_id
        assert b_arrival.place_id == place_id

    replacement_place = _private_place(journey_id, place_id)
    replacement_quest = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(recipient_id),
        json={"place_id": replacement_place.id},
    )
    assert replacement_quest.status_code == 201, replacement_quest.text
    assert replacement_quest.json()["member_side_quest"]["id"] != recipient_quest_id
    stale_quest_report = _post_member_place_invitation_progress(
        journey_id, invitation_id, recipient_id, "SHARE_ARRIVAL"
    )
    assert stale_quest_report.status_code == 409
    owner_keeps_historical_return = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    ).json()["member_place_invitations"]["sent"][0]
    b_historical_row = next(
        row for row in owner_keeps_historical_return["participants"] if row["membership"] == "JOINED"
    )
    assert b_historical_row["heading_back"] is not None

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        journey.active_plan_version_id = None
        session.commit()
    stale_plan_report = _post_member_place_invitation_progress(
        journey_id, invitation_id, owner_id, "SHARE_ARRIVAL"
    )
    assert stale_plan_report.status_code == 409
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        journey.active_plan_version_id = shared_snapshot[0]
        recipient_member = session.scalar(
            select(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == recipient_id,
            )
        )
        assert recipient_member is not None
        recipient_member.removed_at = datetime.now(timezone.utc).isoformat()
        session.commit()
    removed_member_report = client.post(
        progress_path, headers=_auth(recipient_id), json={"action": "SHARE_HEADING_BACK"}
    )
    assert removed_member_report.status_code in {403, 404}


def test_r70_stale_owner_stop_and_recipient_conflict_do_not_replace_existing_work():
    journey_id, owner_id, recipient_id = _active_shared_journey()
    owner_quest_id, _place_id = _create_owner_invitation(journey_id, owner_id)
    sent = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitations",
        headers=_auth(owner_id),
        json={
            "confirm_public_place_sharing": True,
            "audience_snapshot": client.get(
                f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitation-preview",
                headers=_auth(owner_id),
            ).json()["audience_snapshot"],
        },
    )
    assert sent.status_code == 201, sent.text
    invitation_id = sent.json()["member_place_invitations"]["sent"][0]["invitation_id"]

    _start_split(journey_id, recipient_id)
    recipient_place = _private_place(journey_id)
    recipient_created = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(recipient_id),
        json={"place_id": recipient_place.id},
    )
    assert recipient_created.status_code == 201, recipient_created.text
    original_quest_id = recipient_created.json()["member_side_quest"]["id"]
    blocked = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(recipient_id),
        json={"decision": "ACCEPT", "confirm_exact_public_place": True},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["message"] == "INVITATION_RECIPIENT_HAS_ACTIVE_SPLIT"
    with SessionLocal() as session:
        assert session.get(MemberSideQuest, original_quest_id).side_place_id == recipient_place.id
        response = session.scalar(select(MemberPlaceInvitationRecipient).where(
            MemberPlaceInvitationRecipient.invitation_id == invitation_id,
            MemberPlaceInvitationRecipient.recipient_traveler_id == recipient_id,
        ))
        assert response is not None and response.state == "INVITED"

    # Removing the owner's source quest invalidates any not-yet-accepted recipient.
    cancelled = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/cancel",
        headers=_auth(owner_id),
    )
    assert cancelled.status_code == 200, cancelled.text
    stale = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(recipient_id),
        json={"decision": "ACCEPT", "confirm_exact_public_place": True},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["message"] in {"PLACE_INVITATION_STALE", "PLACE_INVITATION_OWNER_SPLIT_STALE"}


def test_r70_in_place_disclosure_change_is_stale_and_keeps_confirmed_snapshot():
    journey_id, owner_id, recipient_id = _active_shared_journey()
    owner_quest_id, place_id = _create_owner_invitation(journey_id, owner_id)
    preview = client.get(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitation-preview",
        headers=_auth(owner_id),
    )
    assert preview.status_code == 200, preview.text
    sent = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitations",
        headers=_auth(owner_id),
        json={
            "confirm_public_place_sharing": True,
            "audience_snapshot": preview.json()["audience_snapshot"],
        },
    )
    assert sent.status_code == 201, sent.text
    invitation_id = sent.json()["member_place_invitations"]["sent"][0]["invitation_id"]
    invitation_snapshot = sent.json()["member_place_invitations"]["sent"][0]
    original_place = invitation_snapshot["place"]
    original_rendezvous = invitation_snapshot["rendezvous"]

    with SessionLocal() as session:
        place = session.get(Place, place_id)
        target = session.get(Activity, original_rendezvous["activity_id"])
        assert place is not None and target is not None
        place.latitude = float(place.latitude) + 0.001
        target.title = f"{target.title} (updated)"
        target.start_at = (datetime.fromisoformat(target.start_at) + timedelta(minutes=5)).isoformat()
        session.commit()

    incoming = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(recipient_id)
    ).json()["member_place_invitations"]["incoming"]
    assert len(incoming) == 1
    assert incoming[0]["state"] == "STALE"
    assert incoming[0]["place"] == original_place
    assert incoming[0]["rendezvous"] == original_rendezvous

    refreshed_preview = client.get(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitation-preview",
        headers=_auth(owner_id),
    )
    assert refreshed_preview.status_code == 200, refreshed_preview.text
    reissued = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitations",
        headers=_auth(owner_id),
        json={
            "confirm_public_place_sharing": True,
            "audience_snapshot": refreshed_preview.json()["audience_snapshot"],
        },
    )
    assert reissued.status_code == 201, reissued.text
    sent_rows = reissued.json()["member_place_invitations"]["sent"]
    fresh_invitation = next(row for row in sent_rows if row["invitation_id"] != invitation_id)
    assert fresh_invitation["state"] == "OPEN"
    assert fresh_invitation["place"]["latitude"] != original_place["latitude"]
    assert fresh_invitation["rendezvous"]["title"] != original_rendezvous["title"]
    assert fresh_invitation["rendezvous"]["scheduled_at"] != original_rendezvous["scheduled_at"]
    refreshed_incoming = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(recipient_id)
    ).json()["member_place_invitations"]["incoming"]
    incoming_by_id = {row["invitation_id"]: row for row in refreshed_incoming}
    assert incoming_by_id[invitation_id]["state"] == "STALE"
    assert incoming_by_id[fresh_invitation["invitation_id"]]["state"] == "INVITED"
    assert incoming_by_id[invitation_id]["place"] == original_place
    assert incoming_by_id[fresh_invitation["invitation_id"]]["place"] == fresh_invitation["place"]

    rejected = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(recipient_id),
        json={"decision": "ACCEPT", "confirm_exact_public_place": True},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["message"] == "PLACE_INVITATION_STALE"
    with SessionLocal() as session:
        recipient_member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == recipient_id,
        ))
        assert recipient_member is not None
        assert session.scalar(select(JourneyIntent).where(
            JourneyIntent.journey_id == journey_id,
            JourneyIntent.created_by_member_id == recipient_member.id,
            JourneyIntent.intent_kind == "PLANNED_SEPARATION",
            JourneyIntent.status == "ACTIVE",
        )) is None
        assert session.scalar(select(MemberSideQuest).where(
            MemberSideQuest.journey_id == journey_id,
            MemberSideQuest.member_id == recipient_member.id,
        )) is None


@pytest.mark.parametrize(
    ("invalidation", "expected_message", "expected_state"),
    [
        ("expired", "PLACE_INVITATION_EXPIRED", "EXPIRED"),
        ("withdrawn", "PLACE_INVITATION_WITHDRAWN", "WITHDRAWN"),
    ],
)
def test_r70_decline_rejects_expired_or_withdrawn_invitation(
    invalidation, expected_message, expected_state
):
    journey_id, owner_id, recipient_id = _active_shared_journey()
    owner_quest_id, _place_id = _create_owner_invitation(journey_id, owner_id)
    preview = client.get(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitation-preview",
        headers=_auth(owner_id),
    )
    assert preview.status_code == 200, preview.text
    sent = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitations",
        headers=_auth(owner_id),
        json={
            "confirm_public_place_sharing": True,
            "audience_snapshot": preview.json()["audience_snapshot"],
        },
    )
    assert sent.status_code == 201, sent.text
    invitation_id = sent.json()["member_place_invitations"]["sent"][0]["invitation_id"]

    if invalidation == "expired":
        with SessionLocal() as session:
            invitation = session.get(MemberPlaceInvitation, invitation_id)
            assert invitation is not None
            invitation.expires_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            session.commit()
    else:
        withdrawn = client.post(
            f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/withdraw",
            headers=_auth(owner_id),
        )
        assert withdrawn.status_code == 200, withdrawn.text

    declined = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(recipient_id),
        json={"decision": "DECLINE"},
    )
    assert declined.status_code == 409
    assert declined.json()["error"]["message"] == expected_message

    with SessionLocal() as session:
        invitation = session.get(MemberPlaceInvitation, invitation_id)
        recipient_member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == recipient_id,
        ))
        assert invitation is not None and invitation.state == expected_state
        assert recipient_member is not None
        response = session.scalar(select(MemberPlaceInvitationRecipient).where(
            MemberPlaceInvitationRecipient.invitation_id == invitation_id,
            MemberPlaceInvitationRecipient.recipient_member_id == recipient_member.id,
        ))
        assert response is not None and response.state == "INVITED"

    owner_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    ).json()["member_place_invitations"]["sent"][0]
    assert owner_projection["state"] == expected_state
    assert owner_projection["recipients"][0]["state"] == expected_state


@pytest.mark.parametrize(
    ("invalidation", "expected_message"),
    [
        ("expired", "PLACE_INVITATION_EXPIRED"),
        ("active_plan_drift", "PLACE_INVITATION_STALE"),
        ("rendezvous_closed", "PLACE_INVITATION_STALE"),
        ("addressed_member_removed", "PLACE_INVITATION_STALE"),
        ("invalid_place_coordinates", "PLACE_INVITATION_STALE"),
    ],
)
def test_r70_lifecycle_context_changes_reject_acceptance(invalidation, expected_message):
    journey_id, owner_id, recipient_id = _active_shared_journey()
    owner_quest_id, place_id = _create_owner_invitation(journey_id, owner_id)
    removed_member_id = None
    if invalidation == "addressed_member_removed":
        third_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            assert journey is not None
            session.add(Traveler(
                id=third_id,
                display_name="R70离队同行者",
                email=None,
                locale="zh-CN",
                timezone="UTC",
                created_at=now,
                updated_at=now,
            ))
            session.flush()
            member = JourneyMember(
                id=str(uuid4()),
                journey_id=journey_id,
                traveler_id=third_id,
                role="COLLABORATOR",
                invited_by=owner_id,
                expires_at=None,
                joined_at=now,
                removed_at=None,
                preferences_json="{}",
                created_at=now,
                updated_at=now,
            )
            session.add(member)
            session.commit()
            removed_member_id = str(member.id)

    preview = client.get(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitation-preview",
        headers=_auth(owner_id),
    )
    assert preview.status_code == 200, preview.text
    sent = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitations",
        headers=_auth(owner_id),
        json={
            "confirm_public_place_sharing": True,
            "audience_snapshot": preview.json()["audience_snapshot"],
        },
    )
    assert sent.status_code == 201, sent.text
    invitation_id = sent.json()["member_place_invitations"]["sent"][0]["invitation_id"]

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        invitation = session.get(MemberPlaceInvitation, invitation_id)
        assert journey is not None and invitation is not None
        if invalidation == "expired":
            invitation.expires_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        elif invalidation == "active_plan_drift":
            old_plan = session.get(PlanVersion, str(journey.active_plan_version_id))
            assert old_plan is not None
            old_plan.status = "SUPERSEDED"
            session.flush()
            now = datetime.now(timezone.utc).isoformat()
            drifted_plan = PlanVersion(
                id=str(uuid4()),
                journey_id=journey_id,
                version_no=int(old_plan.version_no) + 1,
                planning_mode=old_plan.planning_mode,
                status="ACTIVE",
                lifecycle="ACTIVE",
                source="REPLAN",
                parent_plan_version_id=old_plan.id,
                total_cost_minor=0,
                currency=old_plan.currency,
                estimated_duration_min=0,
                utility_score=0.0,
                score_breakdown_json="{}",
                explanation_json="{}",
                mobility_evidence_json="{}",
                created_at=now,
            )
            session.add(drifted_plan)
            session.flush()
            journey.active_plan_version_id = drifted_plan.id
        elif invalidation == "rendezvous_closed":
            target = session.get(Activity, str(invitation.rendezvous_activity_id))
            assert target is not None
            target.status = "CANCELLED"
        elif invalidation == "addressed_member_removed":
            member = session.get(JourneyMember, str(removed_member_id))
            assert member is not None
            member.removed_at = datetime.now(timezone.utc).isoformat()
        elif invalidation == "invalid_place_coordinates":
            place = session.get(Place, place_id)
            assert place is not None
            place.coordinate_system = "GCJ-02"
        session.commit()

    rejected = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(recipient_id),
        json={"decision": "ACCEPT", "confirm_exact_public_place": True},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["message"] == expected_message
    with SessionLocal() as session:
        recipient_member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == recipient_id,
        ))
        assert recipient_member is not None
        assert session.scalar(select(JourneyIntent).where(
            JourneyIntent.journey_id == journey_id,
            JourneyIntent.created_by_member_id == recipient_member.id,
            JourneyIntent.intent_kind == "PLANNED_SEPARATION",
            JourneyIntent.status == "ACTIVE",
        )) is None
        assert session.scalar(select(MemberSideQuest).where(
            MemberSideQuest.journey_id == journey_id,
            MemberSideQuest.member_id == recipient_member.id,
        )) is None
    if invalidation == "addressed_member_removed":
        refreshed_preview = client.get(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitation-preview",
            headers=_auth(owner_id),
        )
        assert refreshed_preview.status_code == 200, refreshed_preview.text
        assert refreshed_preview.json()["recipient_count"] == 1
        reissued = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitations",
            headers=_auth(owner_id),
            json={
                "confirm_public_place_sharing": True,
                "audience_snapshot": refreshed_preview.json()["audience_snapshot"],
            },
        )
        assert reissued.status_code == 201, reissued.text
        reissued_invitation = reissued.json()["member_place_invitations"]["sent"][0]
        assert reissued_invitation["invitation_id"] != invitation_id
        assert len(reissued_invitation["recipients"]) == 1


def test_r71_missing_origin_is_unknown_and_preview_is_recipient_scoped(monkeypatch):
    journey_id, owner_id, recipient_id = _active_shared_journey()
    invitation_id, owner_quest_id, _place_id = _create_r71_invitation(journey_id, owner_id)
    c_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(Traveler(
            id=c_id,
            display_name="R71未受邀同行者C",
            email=None,
            locale="zh-CN",
            timezone="UTC",
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        session.add(JourneyMember(
            id=str(uuid4()),
            journey_id=journey_id,
            traveler_id=c_id,
            role="COLLABORATOR",
            invited_by=owner_id,
            expires_at=None,
            joined_at=now,
            removed_at=None,
            preferences_json="{}",
            created_at=now,
            updated_at=now,
        ))
        journey = session.get(Journey, journey_id)
        assert journey is not None
        active_plan_id = str(journey.active_plan_version_id)
        session.commit()

    route_calls: list[tuple[str, str]] = []

    def route_capture(_session=None, *, origin_key, target_key, **_kwargs):
        route_calls.append((str(origin_key), str(target_key)))
        raise AssertionError("missing-location preview must not request a route")

    monkeypatch.setattr(member_side_quest_module, "_capture_private_route_leg", route_capture)
    recipient_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(recipient_id)
    ).json()
    assert recipient_projection["member_place_invitations"]["incoming"][0]["invitation_id"] == invitation_id
    assert route_calls == []  # Passive projection never acquires route evidence.

    c_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(c_id)
    ).json()
    assert (c_projection.get("member_place_invitations") or {}).get("incoming", []) == []
    c_preview = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/feasibility-preview",
        headers=_auth(c_id),
        json={"consent_route_provider": True, "location_observation_id": None},
    )
    assert c_preview.status_code == 404

    path = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    missing_ack = client.post(
        f"{path}/respond",
        headers=_auth(recipient_id),
        json={"decision": "ACCEPT", "confirm_exact_public_place": True},
    )
    assert missing_ack.status_code == 422
    preview = client.post(
        f"{path}/feasibility-preview",
        headers=_auth(recipient_id),
        json={"consent_route_provider": True, "location_observation_id": None},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["state"] == "UNKNOWN"
    assert body["reason_code"] == "CURRENT_LOCATION_UNAVAILABLE"
    assert body["travel_duration_min"] is None
    assert body["travel_duration_interval_min"] is None
    assert body["evidence"]["captured_at"] is None
    assert body["plan_unchanged"] is True
    assert body["physical_arrival_proof"] is False
    assert owner_quest_id not in json.dumps(body)
    assert "latitude" not in json.dumps(body) and "longitude" not in json.dumps(body)
    assert route_calls == []

    token = body["feasibility_token"]
    tampered_token = token[:-1] + ("A" if token[-1] != "A" else "B")
    tampered = client.post(
        f"{path}/respond",
        headers=_auth(recipient_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "feasibility_token": tampered_token,
            "acknowledge_feasibility_unknown": True,
        },
    )
    assert tampered.status_code == 409
    assert tampered.json()["error"]["message"] == "PLACE_INVITATION_FEASIBILITY_TOKEN_INVALID"

    no_ack = client.post(
        f"{path}/respond",
        headers=_auth(recipient_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "feasibility_token": token,
        },
    )
    assert no_ack.status_code == 409
    assert no_ack.json()["error"]["message"] == "PLACE_INVITATION_UNKNOWN_ACKNOWLEDGEMENT_REQUIRED"
    accepted = client.post(
        f"{path}/respond",
        headers=_auth(recipient_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "feasibility_token": token,
            "acknowledge_feasibility_unknown": True,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["member_side_quest"]["destination"]["place_id"] == _place_id
    assert client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(c_id)
    ).json()["member_place_invitations"]["incoming"] == []
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None and str(journey.active_plan_version_id) == active_plan_id


@pytest.mark.parametrize(
    ("outbound_minutes", "return_minutes", "lead_minutes", "protected", "expected_state"),
    [
        (5, 5, 90, False, "CAN_MAKE_IT"),
        (10, 55, 60, False, "AT_RISK"),
        (10, 55, 60, True, "BLOCKED"),
    ],
)
def test_r71_reuses_captured_route_and_protected_commitment_evaluators(
    monkeypatch, outbound_minutes, return_minutes, lead_minutes, protected, expected_state
):
    journey_id, owner_id, recipient_id = _active_shared_journey()
    invitation_id, _owner_quest_id, _place_id = _create_r71_invitation(
        journey_id, owner_id, rendezvous_lead_minutes=lead_minutes
    )
    observation_response = client.post(
        f"/api/v1/journeys/{journey_id}/location",
        headers=_auth(recipient_id),
        json={
            "latitude": 39.1234,
            "longitude": 117.2345,
            "accuracy_m": 8,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert observation_response.status_code == 201, observation_response.text
    observation_id = observation_response.json()["id"]
    route_calls: list[tuple[str, str]] = []
    test_capture_at = datetime.now(timezone.utc).isoformat()

    def captured_test_route(_session, *, origin_key, target_key, requested_mode, **_kwargs):
        route_calls.append((str(origin_key), str(target_key)))
        duration = outbound_minutes if len(route_calls) == 1 else return_minutes
        return {
            "duration_min": duration,
            "distance_m": 1200,
            "provider": "test-captured-route-fixture",
            "provenance": "LIVE_PROVIDER_CAPTURE",
            "captured_at": test_capture_at,
            "age_seconds": 0,
            "mode": requested_mode,
            "geometry": [[39.0, 117.0]],
        }

    monkeypatch.setattr(member_side_quest_module, "_capture_private_route_leg", captured_test_route)
    if protected:
        with SessionLocal() as session:
            invitation = session.get(MemberPlaceInvitation, invitation_id)
            assert invitation is not None
            target = session.get(Activity, str(invitation.rendezvous_activity_id))
            assert target is not None
            session.add(Commitment(
                id=str(uuid4()),
                journey_id=journey_id,
                activity_id=target.id,
                kind="APPOINTMENT",
                level="C3_Guaranteed",
                status="ACTIVE",
                monetary_value_minor=0,
                currency="CNY",
                external_ref="R71-protected-rendezvous-fixture",
                policy_json=json.dumps({"scheduled_start_at": target.start_at, "safety_buffer_min": 5}),
                created_at=test_capture_at,
                updated_at=test_capture_at,
            ))
            session.commit()

    preview = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/feasibility-preview",
        headers=_auth(recipient_id),
        json={
            "consent_route_provider": True,
            "location_observation_id": observation_id,
            "dwell_minutes": 20,
            "requested_mode": "WALK",
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["state"] == expected_state
    assert body["travel_duration_min"] == outbound_minutes + 20 + return_minutes
    assert body["evidence"]["source_type"] == "LIVE_PROVIDER_CAPTURE"
    assert body["evidence"]["provider"] == "test-captured-route-fixture"
    assert body["physical_arrival_proof"] is False
    assert body["plan_unchanged"] is True
    assert len(route_calls) == 2
    # The signed, client-held token has no coordinates or route geometry.
    assert "39.1234" not in body["feasibility_token"]
    assert "117.2345" not in body["feasibility_token"]
    assert "geometry" not in body["feasibility_token"]

    response_path = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond"
    decision_payload = {
        "decision": "ACCEPT",
        "confirm_exact_public_place": True,
        "feasibility_token": body["feasibility_token"],
    }
    if expected_state == "CAN_MAKE_IT":
        accepted = client.post(response_path, headers=_auth(recipient_id), json=decision_payload)
        assert accepted.status_code == 200, accepted.text
    elif expected_state == "AT_RISK":
        missing_risk_ack = client.post(response_path, headers=_auth(recipient_id), json=decision_payload)
        assert missing_risk_ack.status_code == 409
        assert missing_risk_ack.json()["error"]["message"] == "PLACE_INVITATION_RISK_ACKNOWLEDGEMENT_REQUIRED"
        decision_payload["acknowledge_feasibility_risk"] = True
        accepted = client.post(response_path, headers=_auth(recipient_id), json=decision_payload)
        assert accepted.status_code == 200, accepted.text
    else:
        decision_payload["acknowledge_feasibility_risk"] = True
        blocked = client.post(response_path, headers=_auth(recipient_id), json=decision_payload)
        assert blocked.status_code == 409
        assert blocked.json()["error"]["message"] == "PLACE_INVITATION_FEASIBILITY_BLOCKED"
        with SessionLocal() as session:
            member = session.scalar(select(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == recipient_id,
            ))
            assert member is not None
            assert session.scalar(select(MemberSideQuest).where(
                MemberSideQuest.journey_id == journey_id,
                MemberSideQuest.member_id == member.id,
            )) is None


def test_r71_feasibility_token_is_invalidated_by_rendezvous_change():
    journey_id, owner_id, recipient_id = _active_shared_journey()
    invitation_id, _owner_quest_id, _place_id = _create_r71_invitation(journey_id, owner_id)
    path = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    preview = client.post(
        f"{path}/feasibility-preview",
        headers=_auth(recipient_id),
        json={"consent_route_provider": True, "location_observation_id": None},
    )
    assert preview.status_code == 200, preview.text
    with SessionLocal() as session:
        invitation = session.get(MemberPlaceInvitation, invitation_id)
        assert invitation is not None
        target = session.get(Activity, str(invitation.rendezvous_activity_id))
        assert target is not None
        target.start_at = (datetime.now(timezone.utc) + timedelta(minutes=150)).isoformat()
        session.commit()
    rejected = client.post(
        f"{path}/respond",
        headers=_auth(recipient_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "feasibility_token": preview.json()["feasibility_token"],
            "acknowledge_feasibility_unknown": True,
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["message"] == "PLACE_INVITATION_STALE"
    with SessionLocal() as session:
        member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == recipient_id,
        ))
        assert member is not None
        assert session.scalar(select(MemberSideQuest).where(
            MemberSideQuest.journey_id == journey_id,
            MemberSideQuest.member_id == member.id,
        )) is None


def test_r71_stale_captured_routes_remain_unknown(monkeypatch):
    journey_id, owner_id, recipient_id = _active_shared_journey()
    invitation_id, _owner_quest_id, _place_id = _create_r71_invitation(journey_id, owner_id)
    observation_response = client.post(
        f"/api/v1/journeys/{journey_id}/location",
        headers=_auth(recipient_id),
        json={
            "latitude": 39.1234,
            "longitude": 117.2345,
            "accuracy_m": 8,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert observation_response.status_code == 201, observation_response.text
    stale_capture_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    route_calls: list[tuple[str, str]] = []

    def stale_captured_route(_session, *, origin_key, target_key, requested_mode, **_kwargs):
        route_calls.append((str(origin_key), str(target_key)))
        return {
            "duration_min": 5,
            "distance_m": 1200,
            "provider": "test-captured-route-fixture",
            "provenance": "LIVE_PROVIDER_CAPTURE",
            "captured_at": stale_capture_at,
            "age_seconds": 600,
            "mode": requested_mode,
            "geometry": [[39.0, 117.0]],
        }

    monkeypatch.setattr(member_side_quest_module, "_capture_private_route_leg", stale_captured_route)
    preview = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/feasibility-preview",
        headers=_auth(recipient_id),
        json={
            "consent_route_provider": True,
            "location_observation_id": observation_response.json()["id"],
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["state"] == "UNKNOWN"
    assert body["travel_duration_min"] is None
    assert body["outbound_duration_min"] is None
    assert body["return_duration_min"] is None
    assert body["evidence"]["captured_at"] is None
    assert route_calls and len(route_calls) == 2


def test_r71_preview_token_expires_and_withdrawal_invalidates_acceptance(monkeypatch):
    journey_id, owner_id, recipient_id = _active_shared_journey()
    invitation_id, _owner_quest_id, _place_id = _create_r71_invitation(journey_id, owner_id)
    path = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    preview = client.post(
        f"{path}/feasibility-preview",
        headers=_auth(recipient_id),
        json={"consent_route_provider": True, "location_observation_id": None},
    )
    assert preview.status_code == 200, preview.text
    token = preview.json()["feasibility_token"]

    real_now = datetime.now(timezone.utc)
    with monkeypatch.context() as expired_clock:
        expired_clock.setattr(
            member_place_invitation_module,
            "_now",
            lambda value=None: real_now + timedelta(minutes=3) if value is None else value,
        )
        expired = client.post(
            f"{path}/respond",
            headers=_auth(recipient_id),
            json={
                "decision": "ACCEPT",
                "confirm_exact_public_place": True,
                "feasibility_token": token,
                "acknowledge_feasibility_unknown": True,
            },
        )
    assert expired.status_code == 409
    assert expired.json()["error"]["message"] == "PLACE_INVITATION_FEASIBILITY_PREVIEW_EXPIRED"

    withdrawn = client.post(f"{path}/withdraw", headers=_auth(owner_id))
    assert withdrawn.status_code == 200, withdrawn.text
    after_withdrawal = client.post(
        f"{path}/respond",
        headers=_auth(recipient_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "feasibility_token": token,
            "acknowledge_feasibility_unknown": True,
        },
    )
    assert after_withdrawal.status_code == 409
    assert after_withdrawal.json()["error"]["message"] == "PLACE_INVITATION_WITHDRAWN"


def test_r71_acceptance_cas_blocks_fresh_duplicate_and_recovers_after_lease():
    journey_id, owner_id, recipient_id = _active_shared_journey()
    invitation_id, _owner_quest_id, _place_id = _create_r71_invitation(journey_id, owner_id)
    with SessionLocal() as session:
        response = session.scalar(select(MemberPlaceInvitationRecipient).where(
            MemberPlaceInvitationRecipient.invitation_id == invitation_id,
            MemberPlaceInvitationRecipient.recipient_traveler_id == recipient_id,
        ))
        assert response is not None
        response.state = "ACCEPTANCE_NEEDS_COMPLETION"
        response.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()
    path = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond"
    concurrent_retry = client.post(
        path,
        headers=_auth(recipient_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "acknowledge_feasibility_unknown": True,
        },
    )
    assert concurrent_retry.status_code == 409
    assert concurrent_retry.json()["error"]["message"] == "PLACE_INVITATION_ACCEPTANCE_IN_PROGRESS"
    with SessionLocal() as session:
        response = session.scalar(select(MemberPlaceInvitationRecipient).where(
            MemberPlaceInvitationRecipient.invitation_id == invitation_id,
            MemberPlaceInvitationRecipient.recipient_traveler_id == recipient_id,
        ))
        assert response is not None
        response.updated_at = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        session.commit()
    recovered = client.post(
        path,
        headers=_auth(recipient_id),
        json={"decision": "ACCEPT", "confirm_exact_public_place": True},
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["member_side_quest"]["destination"]["authority"] == "EXACT_KNOWN_PLACE"


def test_r72_joined_member_can_share_after_inviter_finishes_own_split():
    """An accepted B owns a separate trip even when A's original split has ended."""
    journey_id, owner_id, recipient_id = _active_shared_journey()
    invitation_id, owner_quest_id, _place_id = _create_r71_invitation(journey_id, owner_id)
    path = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    accepted = client.post(
        f"{path}/respond",
        headers=_auth(recipient_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "acknowledge_feasibility_unknown": True,
        },
    )
    assert accepted.status_code == 200, accepted.text
    b_quest_id = accepted.json()["member_side_quest"]["id"]
    for action in ("start", "arrive"):
        response = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{b_quest_id}/{action}",
            headers=_auth(recipient_id),
        )
        assert response.status_code == 200, response.text

    # Arrange the same domain end state as A's own R63 end, without ending B's
    # separate split. This is a narrow regression fixture, not physical reunion.
    with SessionLocal() as session:
        invitation = session.get(MemberPlaceInvitation, invitation_id)
        assert invitation is not None
        owner_intent = session.get(JourneyIntent, invitation.owner_split_intent_id)
        owner_quest = session.get(MemberSideQuest, owner_quest_id)
        assert owner_intent is not None and owner_quest is not None
        owner_intent.status = "CANCELLED"
        owner_quest.state = "INVALIDATED"
        session.commit()

    b_shared = _post_member_place_invitation_progress(
        journey_id, invitation_id, recipient_id, "SHARE_ARRIVAL"
    )
    assert b_shared.status_code == 200, b_shared.text
    b_view = b_shared.json()["member_place_invitations"]["joined"][0]
    assert b_view["state"] == "STALE"  # No new invitations, accepted trips survive.
    assert any(
        participant["membership"] == "JOINED"
        and participant["at_invited_place"] is not None
        for participant in b_view["participants"]
    )
    a_cannot_report_old_trip = _post_member_place_invitation_progress(
        journey_id, invitation_id, owner_id, "SHARE_ARRIVAL"
    )
    assert a_cannot_report_old_trip.status_code == 409


def test_r72_late_joiner_does_not_inherit_prior_opt_in_status():
    """An opt-in report is not automatically disclosed to future joiners."""
    journey_id, owner_id, recipient_id = _active_shared_journey()
    third_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(Traveler(
            id=third_id, display_name="R72后来加入的同行者",
            email=None, locale="zh-CN", timezone="UTC",
            created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(JourneyMember(
            id=str(uuid4()), journey_id=journey_id, traveler_id=third_id,
            role="COLLABORATOR", invited_by=owner_id,
            expires_at=None, joined_at=now, removed_at=None,
            preferences_json="{}", created_at=now, updated_at=now,
        ))
        session.commit()
    invitation_id, _owner_quest_id, _place_id = _create_r71_invitation(journey_id, owner_id)
    path = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"

    def accept(traveler_id: str) -> dict:
        response = client.post(
            f"{path}/respond",
            headers=_auth(traveler_id),
            json={
                "decision": "ACCEPT",
                "confirm_exact_public_place": True,
                "acknowledge_feasibility_unknown": True,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    b_accepted = accept(recipient_id)
    b_quest = b_accepted["member_side_quest"]["id"]
    for action in ("start", "arrive"):
        result = client.post(
            f"/api/v1/journeys/{journey_id}/member-side-quests/{b_quest}/{action}",
            headers=_auth(recipient_id),
        )
        assert result.status_code == 200, result.text
    first_report = _post_member_place_invitation_progress(
        journey_id, invitation_id, recipient_id, "SHARE_ARRIVAL"
    )
    assert first_report.status_code == 200, first_report.text
    b_member_id = next(
        item["member_id"] for item in first_report.json()["member_place_invitations"]["joined"][0]["participants"]
        if item["membership"] == "JOINED"
    )
    owner_before = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    ).json()["member_place_invitations"]["sent"][0]["participants"]
    assert next(item for item in owner_before if item["member_id"] == b_member_id)["at_invited_place"] is not None
    b_withdrew_before_reconfirm = client.post(
        f"{path}/progress", headers=_auth(recipient_id),
        json={"action": "WITHDRAW_ARRIVAL"},
    )
    assert b_withdrew_before_reconfirm.status_code == 200
    # This is the immutable audience snapshot held by B's open confirmation.
    b_consent_token_before_c_joins = _member_place_invitation_audience_token(
        journey_id, invitation_id, recipient_id
    )

    c_accepted = accept(third_id)
    c_board = c_accepted["member_place_invitations"]["joined"][0]["participants"]
    b_for_late_c = next(item for item in c_board if item["member_id"] == b_member_id)
    assert b_for_late_c["at_invited_place"] is None

    # An old confirmation cannot disclose to a member who joined after the
    # dialog opened. B must review the refreshed audience and explicitly agree.
    stale_consent = _post_member_place_invitation_progress(
        journey_id,
        invitation_id,
        recipient_id,
        "SHARE_ARRIVAL",
        audience_token=b_consent_token_before_c_joins,
    )
    assert stale_consent.status_code == 409, stale_consent.text
    assert stale_consent.json()["error"]["message"] == "PLACE_INVITATION_PROGRESS_AUDIENCE_CHANGED"
    c_still_sees_nothing = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(third_id)
    ).json()["member_place_invitations"]["joined"][0]["participants"]
    assert next(item for item in c_still_sees_nothing if item["member_id"] == b_member_id)["at_invited_place"] is None

    refreshed_consent_token = _member_place_invitation_audience_token(
        journey_id, invitation_id, recipient_id
    )
    reshared = _post_member_place_invitation_progress(
        journey_id,
        invitation_id,
        recipient_id,
        "SHARE_ARRIVAL",
        audience_token=refreshed_consent_token,
    )
    assert reshared.status_code == 200, reshared.text
    c_after = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(third_id)
    ).json()["member_place_invitations"]["joined"][0]["participants"]
    assert next(item for item in c_after if item["member_id"] == b_member_id)["at_invited_place"] is not None


def test_r73_shared_stop_without_grounded_route_evidence_stays_blocked(monkeypatch):
    journey_id, owner_id, recipient_id = _active_shared_journey()
    invitation_id, _side_quest_id, _place_id = _create_r71_invitation(journey_id, owner_id)
    accepted_invitation = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(recipient_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "acknowledge_feasibility_unknown": True,
        },
    )
    assert accepted_invitation.status_code == 200, accepted_invitation.text
    with SessionLocal() as session:
        recipient_member_id = str(session.scalar(
            select(JourneyMember.id).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == recipient_id,
            )
        ))
    invitation_url = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    offered = client.post(
        invitation_url + "/co-planning-grants",
        headers={**_auth(owner_id), "Idempotency-Key": "REDACTED_FOR_PUBLIC_RELEASE"},
        json={"grantee_member_id": recipient_member_id},
    )
    assert offered.status_code == 201, offered.text
    grant = next(
        item for item in offered.json()["member_place_invitations"]["co_planning_grants"]
        if item["grantee_member_id"] == recipient_member_id and item["state"] == "OFFERED"
    )
    accepted = client.post(
        invitation_url + f"/co-planning-grants/{grant['grant_id']}/respond",
        headers=_auth(recipient_id),
        json={"decision": "ACCEPT"},
    )
    assert accepted.status_code == 200, accepted.text
    monkeypatch.setattr(main_module, "_ensure_opportunity_window_event", lambda _session, _journey: None)
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        original_plan_id = str(journey.active_plan_version_id)
        original_cursor_id = str(journey.current_activity_id or "")
        original_proposal_count = int(
            session.scalar(select(func.count()).select_from(ReplanProposal).where(
                ReplanProposal.journey_id == journey_id,
            )) or 0
        )

    headers = {
        **_auth(owner_id),
        "Idempotency-Key": "REDACTED_FOR_PUBLIC_RELEASE",
    }
    response = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/promotions",
        headers=headers,
        json={"dwell_minutes": 20},
    )
    assert response.status_code == 201, response.text
    projection = response.json()["member_place_invitations"]
    assert len(projection["promotions"]) == 1
    promotion_view = projection["promotions"][0]
    assert promotion_view["state"] == "BLOCKED"
    assert promotion_view["state_detail"]["feasibility"] == "FEASIBILITY_UNKNOWN"
    assert promotion_view["state_detail"]["next_action"] == "NEEDS_ROUTE_EVIDENCE"
    assert promotion_view["proposal_id"] is None
    with SessionLocal() as session:
        assert int(
            session.scalar(select(func.count()).select_from(ReplanProposal).where(
                ReplanProposal.journey_id == journey_id,
            )) or 0
        ) == original_proposal_count

    # A generic replan accept URL cannot bypass the promotion's explicit
    # unanimous-consent/apply boundary, even if a caller presents a proposal
    # row carrying the R73 marker.
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        promotion_row = session.scalar(
            select(MemberPlacePromotion).where(MemberPlacePromotion.journey_id == journey_id)
        )
        assert promotion_row is not None
        base_plan = session.get(PlanVersion, str(journey.active_plan_version_id))
        assert base_plan is not None
        candidate_plan_id = str(uuid4())
        session.add(PlanVersion(
            id=candidate_plan_id,
            journey_id=journey_id,
            version_no=int(base_plan.version_no) + 1,
            planning_mode=str(base_plan.planning_mode),
            status="CANDIDATE",
            lifecycle="ACTIVE",
            trigger_contract_json="{}",
            source="REPLAN",
            parent_plan_version_id=str(base_plan.id),
            total_cost_minor=int(base_plan.total_cost_minor),
            currency=str(base_plan.currency),
            estimated_duration_min=int(base_plan.estimated_duration_min),
            utility_score=base_plan.utility_score,
            score_breakdown_json=base_plan.score_breakdown_json,
            explanation_json=base_plan.explanation_json,
            mobility_snapshot_id=base_plan.mobility_snapshot_id,
            mobility_evidence_json=base_plan.mobility_evidence_json,
            created_at=datetime.now(timezone.utc).isoformat(),
        ))
        proposal_id = str(uuid4())
        session.add(ReplanProposal(
            id=proposal_id,
            journey_id=journey_id,
            trigger_event_id=None,
            base_plan_version_id=str(base_plan.id),
            proposed_plan_version_id=candidate_plan_id,
            status="PRESENTED",
            hard_constraints_passed=True,
            locked_commitments_preserved=True,
            utility_score=0.0,
            score_breakdown_json="{}",
            diff_json=json.dumps({"r73_shared_stop": {"promotion_id": promotion_row.id}}),
            explanation_json="{}",
            created_at=datetime.now(timezone.utc).isoformat(),
        ))
        session.commit()
    bypass = client.post(
        f"/api/v1/replans/{proposal_id}/accept",
        headers={**_auth(owner_id), "Idempotency-Key": "r73-generic-bypass-01"},
    )
    assert bypass.status_code == 409, bypass.text
    assert "R73_SHARED_PLACE_PROMOTION_AUTHORITY_REQUIRED" in bypass.text

    replay = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/promotions",
        headers=headers,
        json={"dwell_minutes": 20},
    )
    assert replay.status_code == 201, replay.text
    assert len(replay.json()["member_place_invitations"]["promotions"]) == 1
    conflict = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/promotions",
        headers=headers,
        json={"dwell_minutes": 30},
    )
    assert conflict.status_code == 409, conflict.text

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        persisted = session.scalars(
            select(MemberPlacePromotion).where(MemberPlacePromotion.journey_id == journey_id)
        ).all()
        assert len(persisted) == 1
        assert journey.active_plan_version_id == original_plan_id
        assert str(journey.current_activity_id or "") == original_cursor_id
        assert int(
            session.scalar(select(func.count()).select_from(ReplanProposal).where(
                ReplanProposal.journey_id == journey_id,
            )) or 0
        ) == original_proposal_count + 1  # explicit generic-accept bypass fixture above


def test_r73_shared_stop_reuses_collective_authority_and_applies_one_real_successor(monkeypatch):
    from app.integrations.discovery.real_city import DiscoveryCandidate
    from app.integrations.mobility.snapshot_capture import MobilitySnapshotCaptureResult
    from app.replanning.provider_snapshots import EXPLICIT_FROZEN_FIXTURE, MobilitySnapshot

    journey_id, owner_id, companion_id = _active_shared_journey()
    # A remains OWNER; B and C stay ordinary signed COLLABORATORs throughout.

    # Give the controlled replay a real, bounded insertion window before the
    # next shared stop. These are test schedule facts, not provider evidence.
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None and journey.active_plan_version_id
        rows = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.sequence_no)
        ).all()
        current = next((row for row in rows if row.id == journey.current_activity_id), None)
        assert current is not None
        rendezvous = next(
            row for row in rows
            if row.sequence_no > current.sequence_no
            and row.status not in {"COMPLETED", "SKIPPED", "CANCELLED"}
        )
        now = execution_clock.now().astimezone(timezone.utc)
        current.start_at = (now - timedelta(minutes=60)).isoformat()
        current.end_at = (now - timedelta(minutes=15)).isoformat()
        current.status = "IN_PROGRESS"
        original_duration = max(
            30,
            int((datetime.fromisoformat(rendezvous.end_at) - datetime.fromisoformat(rendezvous.start_at)).total_seconds() // 60),
        )
        anchor_start = now + timedelta(minutes=180)
        rendezvous.start_at = anchor_start.isoformat()
        rendezvous.end_at = (anchor_start + timedelta(minutes=original_duration)).isoformat()
        cursor = datetime.fromisoformat(rendezvous.end_at)
        for row in rows:
            if row.sequence_no <= rendezvous.sequence_no:
                continue
            duration = max(
                30,
                int((datetime.fromisoformat(row.end_at) - datetime.fromisoformat(row.start_at)).total_seconds() // 60),
            )
            start = cursor + timedelta(minutes=20)
            row.start_at = start.isoformat()
            row.end_at = (start + timedelta(minutes=duration)).isoformat()
            cursor = datetime.fromisoformat(row.end_at)
        for activity in rows:
            if not activity.place_id:
                continue
            place = session.get(Place, str(activity.place_id))
            if place is None:
                continue
            place.opening_hours_json = json.dumps({
                "status": "NORMALIZED_VALID",
                "open": "00:00",
                "close": "24:00",
                "intervals": [{"days": ["*"], "open": "00:00", "close": "24:00"}],
                "fixture_class": "TEST_REPLAY",
            })
            metadata = json.loads(place.metadata_json or "{}")
            metadata["opening_hours_fixture"] = {
                "fixture_class": "TEST_REPLAY",
                "source": "R73_CONTROLLED_ACTIVE_PLAN_FIXTURE",
            }
            place.metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        journey.start_at = current.start_at
        session.commit()

    location_id = _r67_location(journey_id, owner_id)
    candidate = DiscoveryCandidate(
        provider_key="overpass",
        external_id="overpass:node/987654322",
        name="R73 controlled OSM public garden",
        category="museum",
        latitude=5.123956,
        longitude=10.654321,
        tags=("museum", "real_city"),
        opening_hours={"status": "NORMALIZED_VALID", "open": "00:00", "close": "24:00"},
        metadata={
            "osm_id": "node/987654322",
            "tags": {
                "name": "R73 controlled OSM public garden",
                "tourism": "museum",
                "fee": "no",
                "opening_hours": "24/7",
            },
        },
        distance_m=None,
    )
    _install_r67_provider(
        monkeypatch,
        candidates=[candidate],
        attempts=[{"endpoint": "https://overpass.example/api/interpreter", "answered": True, "http_status": 200}],
    )
    discovered = _r67_live_request(journey_id, owner_id, location_id)
    assert discovered.status_code == 200, discovered.text
    assert discovered.json()["scout_state"] == "LIVE_RESULTS"
    candidate_id = discovered.json()["candidates"][0]["candidate_id"]
    selected = client.post(
        f"/api/v1/journeys/{journey_id}/moment-scout/live/select",
        headers=_auth(owner_id),
        json={
            "receipt_id": discovered.json()["receipt_id"],
            "candidate_id": candidate_id,
            "confirm_return_to_rendezvous": True,
        },
    )
    assert selected.status_code == 201, selected.text
    owner_quest_id = selected.json()["member_side_quest"]["id"]
    place_id = selected.json()["member_side_quest"]["destination"]["place_id"]
    with SessionLocal() as session:
        place = session.get(Place, place_id)
        assert place is not None
        metadata = json.loads(place.metadata_json or "{}")
        metadata["tags"] = {**metadata.get("tags", {}), "fee": "no", "opening_hours": "24/7"}
        metadata["cost_evidence"] = {
            "status": "KNOWN",
            "amount_minor": 0,
            "currency": None,
            "currency_agnostic": True,
            "evidence_basis": "OSM_FEE_NO",
            "source": "TEST_REPLAY_CONTROLLED_OVERPASS_FIXTURE",
        }
        place.opening_hours_json = json.dumps({
            "status": "NORMALIZED_VALID",
            "open": "00:00",
            "close": "24:00",
            "intervals": [{"days": ["*"], "open": "00:00", "close": "24:00"}],
        })
        place.metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        session.commit()

    preview = client.get(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitation-preview",
        headers=_auth(owner_id),
    )
    assert preview.status_code == 200, preview.text
    sent = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{owner_quest_id}/place-invitations",
        headers=_auth(owner_id),
        json={
            "confirm_public_place_sharing": True,
            "audience_snapshot": preview.json()["audience_snapshot"],
        },
    )
    assert sent.status_code == 201, sent.text
    invitation_id = sent.json()["member_place_invitations"]["sent"][0]["invitation_id"]
    feasibility = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/feasibility-preview",
        headers=_auth(companion_id),
        json={
            "consent_route_provider": True,
            "location_observation_id": None,
            "dwell_minutes": 30,
            "requested_mode": "WALK",
        },
    )
    assert feasibility.status_code == 200, feasibility.text
    assert feasibility.json()["state"] == "UNKNOWN"
    accepted = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(companion_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "feasibility_token": feasibility.json()["feasibility_token"],
            "acknowledge_feasibility_unknown": True,
        },
    )
    assert accepted.status_code == 200, accepted.text

    # C is a current Journey member but was not addressed by or accepted into
    # the frozen R70 micro-group. The R73 collective round must still include C.
    third_id = str(uuid4())
    stamp = execution_clock.now().astimezone(timezone.utc).isoformat()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        session.add(Traveler(
            id=third_id,
            display_name="R73 C outside the invitation group",
            email=None,
            locale="en-US",
            timezone="UTC",
            created_at=stamp,
            updated_at=stamp,
        ))
        session.flush()
        session.add(JourneyMember(
            id=str(uuid4()),
            journey_id=journey_id,
            traveler_id=third_id,
            role="COLLABORATOR",
            invited_by=owner_id,
            expires_at=None,
            joined_at=stamp,
            removed_at=None,
            preferences_json="{}",
            created_at=stamp,
            updated_at=stamp,
        ))
        session.flush()
        anchor = session.get(Activity, str(journey.current_activity_id))
        invitation = session.get(MemberPlaceInvitation, invitation_id)
        assert invitation is not None
        anchor = session.get(Activity, str(invitation.rendezvous_activity_id))
        assert anchor is not None
        anchor_place = session.get(Place, str(anchor.place_id)) if anchor.place_id else None
        semantic_key = (
            json.loads(anchor.metadata_json or "{}").get("semantic_key")
            or anchor.place_id
            or anchor.city_service_id
        )
        session.add(Commitment(
            id=str(uuid4()),
            journey_id=journey_id,
            activity_id=anchor.id,
            kind="APPOINTMENT",
            level="C4_Anchored",
            status="ACTIVE",
            monetary_value_minor=0,
            currency=str(journey.budget_currency or "CNY"),
            external_ref="R73_CONTROLLED_REPLAY_RENDEZVOUS",
            policy_json=json.dumps({
                "semantic_key": str(semantic_key or ""),
                "scheduled_start_at": anchor.start_at,
                "evidence_class": "TEST_REPLAY",
            }),
            created_at=stamp,
            updated_at=stamp,
        ))
        active_plan_before = str(journey.active_plan_version_id)
        session.commit()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        authority_members = main_module.active_journey_members_with_capabilities(
            session, journey
        )["members"]
        assert sum(bool(item["can_authorize_mutation"]) for item in authority_members) == 1, json.dumps(
            [{"member_id": item["member_id"], "role": item["role"], "capabilities": item["capabilities"]}
             for item in authority_members],
            ensure_ascii=False,
            sort_keys=True,
        )
    c_invitation_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(third_id)
    )
    assert c_invitation_view.status_code == 200, c_invitation_view.text
    assert c_invitation_view.json()["member_place_invitations"]["incoming"] == []

    # The original invitation only named the initial companion and was created
    # before C joined. Its signed source owner must widen this disclosure.
    group_share_url = (
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/group-share"
    )
    group_share_preview = client.get(group_share_url + "/preview", headers=_auth(owner_id))
    assert group_share_preview.status_code == 200, group_share_preview.text
    group_share = group_share_preview.json()
    assert {item["display_name"] for item in group_share["audience"]} == {
        "R64同行者",
        "R73 C outside the invitation group",
    }
    group_share_confirmed = client.post(
        group_share_url + "/confirm",
        headers=_auth(owner_id),
        json={
            "confirm_full_journey_place_sharing": True,
            "audience_snapshot": group_share["audience_snapshot"],
            "plan_snapshot": group_share["plan_snapshot"],
            "source_snapshot": group_share["source_snapshot"],
        },
    )
    assert group_share_confirmed.status_code == 200, group_share_confirmed.text

    def create_test_replay_event(session, journey):
        from app.db.models import Event as EventModel

        current = session.get(Activity, str(journey.current_activity_id))
        invitation = session.get(MemberPlaceInvitation, invitation_id)
        assert current is not None and invitation is not None
        anchor = session.get(Activity, str(invitation.rendezvous_activity_id))
        assert anchor is not None
        current_end = datetime.fromisoformat(current.end_at)
        anchor_start = datetime.fromisoformat(anchor.start_at)
        departure = anchor_start - timedelta(minutes=15)
        available = int((departure - current_end).total_seconds() // 60)
        assert available >= 30
        event_id = str(uuid4())
        now_text = execution_clock.now().astimezone(timezone.utc).isoformat()
        payload = {
            "signal_kind": "OPPORTUNITY_WINDOW",
            "opens_at": current.end_at,
            "must_depart_by": departure.isoformat(),
            "available_minutes": available,
            "minimum_actionable_window_min": 30,
            "current_activity_id": current.id,
            "current_execution_activity_id": current.id,
            "current_execution_location": current.place_id or current.city_service_id,
            "next_activity_id": anchor.id,
            "next_activity_start": anchor.start_at,
            "next_commitment_start": anchor.start_at,
            "next_commitment_semantic_key": anchor.place_id or anchor.city_service_id,
            "safety_buffer_min": 15,
            "active_plan_version_id": journey.active_plan_version_id,
            "twin_snapshot_at": now_text,
            "mobility_snapshot_id": "R73-TEST-REPLAY-EXPLICIT-FIXTURE",
            "mobility_provider": "EXPLICIT_FROZEN_FIXTURE",
            "fixture_class": "TEST_REPLAY",
        }
        event = EventModel(
            id=event_id,
            journey_id=journey.id,
            provider_key="citynexus-twin-opportunity",
            provenance="SYSTEM_DERIVED",
            event_type="OPPORTUNITY_WINDOW",
            severity="INFO",
            status="DETECTED",
            affected_activity_id=None,
            occurred_at=current.end_at,
            detected_at=now_text,
            resolved_at=None,
            payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            created_at=now_text,
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        return event

    def capture_test_replay(_session, _journey, pairs, *, operation_key, captured_at=None, **_kwargs):
        facts = {tuple(pair): 6 for pair in pairs}
        snapshot = MobilitySnapshot.from_minutes(
            source_provider_key="R73-TEST-REPLAY-EXPLICIT-FIXTURE",
            minutes=facts,
            captured_at=captured_at,
            operation_key=operation_key,
            fact_quality=EXPLICIT_FROZEN_FIXTURE,
            provider_capabilities={"fixture_class": "TEST_REPLAY"},
        )
        return MobilitySnapshotCaptureResult(
            snapshot=snapshot,
            failed_primary_pairs=(),
            fallback_pairs=(),
        )

    monkeypatch.setattr(main_module, "_ensure_opportunity_window_event", create_test_replay_event)
    monkeypatch.setattr(main_module, "capture_runtime_mobility_snapshot", capture_test_replay)

    invitation_url = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    promotion_url = invitation_url + "/promotions"
    scoped_travelers = {companion_id, third_id}

    def offer_and_accept_scoped_grants(round_key: str) -> dict[str, str]:
        with SessionLocal() as session:
            rows = session.scalars(
                select(JourneyMember).where(
                    JourneyMember.journey_id == journey_id,
                    JourneyMember.traveler_id.in_(scoped_travelers),
                )
            ).all()
            member_by_traveler = {str(row.traveler_id): str(row.id) for row in rows}
        assert set(member_by_traveler) == scoped_travelers
        grant_ids: dict[str, str] = {}
        for traveler_id in sorted(scoped_travelers):
            offered = client.post(
                invitation_url + "/co-planning-grants",
                headers={**_auth(owner_id), "Idempotency-Key": f"{round_key}-{traveler_id[:12]}"},
                json={"grantee_member_id": member_by_traveler[traveler_id]},
            )
            assert offered.status_code == 201, offered.text
            owner_projection = offered.json()["member_place_invitations"]
            matching_grants = [
                item for item in owner_projection["co_planning_grants"]
                if item["grantee_member_id"] == member_by_traveler[traveler_id]
            ]
            grant_view = next(
                (item for item in matching_grants if item["state"] in {"OFFERED", "ACCEPTED"}),
                None,
            )
            assert grant_view is not None, f"no current grant for {traveler_id}: {matching_grants}"
            assert grant_view["invitation_id"] == invitation_id
            grant_ids[traveler_id] = grant_view["grant_id"]
            if grant_view["state"] == "OFFERED":
                accept_url = invitation_url + f"/co-planning-grants/{grant_view['grant_id']}/respond"
                if round_key == "r74-decline-round" and traveler_id == companion_id:
                    barrier = Barrier(2)

                    def accept_in_parallel():
                        barrier.wait(timeout=10)
                        return client.post(
                            accept_url,
                            headers=_auth(traveler_id),
                            json={"decision": "ACCEPT"},
                        )

                    with ThreadPoolExecutor(max_workers=2) as pool:
                        parallel_results = [
                            pool.submit(accept_in_parallel),
                            pool.submit(accept_in_parallel),
                        ]
                        accept_responses = [result.result(timeout=15) for result in parallel_results]
                    assert all(response.status_code == 200 for response in accept_responses), [
                        response.text for response in accept_responses
                    ]
                    with SessionLocal() as session:
                        accept_audit_count = session.scalar(
                            select(func.count()).select_from(AuditLog).where(
                                AuditLog.action == "journey.shared_stop_coplanning.accept",
                                AuditLog.entity_id == str(grant_view["grant_id"]),
                            )
                        )
                    assert int(accept_audit_count or 0) == 1
                    accepted_grant = accept_responses[-1]
                else:
                    accepted_grant = client.post(
                        accept_url,
                        headers=_auth(traveler_id),
                        json={"decision": "ACCEPT"},
                    )
                    assert accepted_grant.status_code == 200, accepted_grant.text
                assert any(
                    item["grant_id"] == grant_view["grant_id"] and item["state"] == "ACCEPTED"
                    for item in accepted_grant.json()["member_place_invitations"]["co_planning_grants"]
                )
        return grant_ids

    blocked_before_opt_in = client.post(
        promotion_url,
        headers={**_auth(owner_id), "Idempotency-Key": "r74-blocked-before-opt-in"},
        json={"dwell_minutes": 30},
    )
    assert blocked_before_opt_in.status_code == 409, blocked_before_opt_in.text
    assert "SCOPED_COPLANNING_ACCEPTANCE_REQUIRED" in blocked_before_opt_in.text
    first_round_grants = offer_and_accept_scoped_grants("r74-decline-round")

    # A stale timestamp and an explicit revocation each remove only that
    # collaborator's consent. The owner must obtain fresh acceptance before
    # proposing the shared stop.
    with SessionLocal() as session:
        expired_grant = session.get(SharedStopCoPlanningGrant, first_round_grants[third_id])
        assert expired_grant is not None
        expired_grant.expires_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        session.commit()
    expired_consent = client.post(
        promotion_url,
        headers={**_auth(owner_id), "Idempotency-Key": "r74-expired-consent"},
        json={"dwell_minutes": 30},
    )
    assert expired_consent.status_code == 409, expired_consent.text
    assert "SCOPED_COPLANNING_ACCEPTANCE_REQUIRED" in expired_consent.text

    renewed_after_expiry = offer_and_accept_scoped_grants("r74-renew-expired-consent")
    revoked = client.post(
        invitation_url + f"/co-planning-grants/{renewed_after_expiry[companion_id]}/revoke",
        headers=_auth(owner_id),
    )
    assert revoked.status_code == 200, revoked.text
    revoked_consent = client.post(
        promotion_url,
        headers={**_auth(owner_id), "Idempotency-Key": "REDACTED_FOR_PUBLIC_RELEASE"},
        json={"dwell_minutes": 30},
    )
    assert revoked_consent.status_code == 409, revoked_consent.text
    assert "SCOPED_COPLANNING_ACCEPTANCE_REQUIRED" in revoked_consent.text
    offer_and_accept_scoped_grants("r74-renew-revoked-consent")

    # Exercise the explicit decline boundary first: it ends this round without
    # changing the active shared plan, then a fresh proposal requires new votes.
    declined_create = client.post(
        promotion_url,
        headers={**_auth(owner_id), "Idempotency-Key": "r73-controlled-decline-create"},
        json={"dwell_minutes": 30},
    )
    assert declined_create.status_code == 201, declined_create.text
    declined_promotions = declined_create.json()["member_place_invitations"]["promotions"]
    assert any(item["state"] == "WAITING_FOR_MEMBERS" for item in declined_promotions), json.dumps(
        declined_promotions, ensure_ascii=False, sort_keys=True
    )
    declined_row = next(item for item in declined_promotions if item["state"] == "WAITING_FOR_MEMBERS")
    assert declined_row["place"]["place_id"] == place_id
    decline = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-promotions/{declined_row['promotion_id']}/respond",
        headers=_auth(companion_id),
        json={"clarification_id": declined_row["clarification_id"], "decision": "DECLINE"},
    )
    assert decline.status_code == 200, decline.text
    assert _find_promotion(decline.json(), declined_row["promotion_id"])["state"] == "DECLINED"
    assert client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]["id"] == active_plan_before

    # The declined round consumed its grants; a new exact decision needs fresh
    # consent from both ordinary collaborators.
    offer_and_accept_scoped_grants("r74-success-round")

    proposed = client.post(
        promotion_url,
        headers={**_auth(owner_id), "Idempotency-Key": "r73-controlled-success-create"},
        json={"dwell_minutes": 30},
    )
    assert proposed.status_code == 201, proposed.text
    waiting = next(
        item for item in proposed.json()["member_place_invitations"]["promotions"]
        if item["state"] == "WAITING_FOR_MEMBERS"
    )
    assert waiting["place"]["place_id"] == place_id
    assert waiting["state_detail"]["feasibility"] == "GROUNDED_FEASIBLE"
    # Members must see bounded, proposal-derived consequences before voting,
    # with truthful TEST_REPLAY provenance rather than a live routing claim.
    tradeoff = waiting["tradeoff"]
    assert tradeoff is not None
    assert tradeoff["added_count"] == 1
    assert tradeoff["removed_count"] == 0
    assert tradeoff["protected_commitments_preserved"] is True
    assert tradeoff["hard_constraints_passed"] is True
    assert "R73-TEST-REPLAY" in tradeoff["route_provider"]
    assert "EXPLICIT_FROZEN_FIXTURE" in tradeoff["route_fact_qualities"]
    assert isinstance(tradeoff["end_time_delta_min"], int)
    assert len(waiting["responses"]) == 3
    assert {item["decision"] for item in waiting["responses"]} == {"WAITING"}
    c_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(third_id)
    )
    assert c_projection.status_code == 200, c_projection.text
    c_offer = _find_promotion(c_projection.json(), waiting["promotion_id"])
    assert c_offer["viewer_can_decide"] is True
    assert c_offer["viewer_scoped_authority_expires_at"]
    assert c_offer["tradeoff"] == waiting["tradeoff"]

    with SessionLocal() as session:
        collaborator_member_id = str(session.scalar(
            select(JourneyMember.id).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == companion_id,
            )
        ))
    generic_select = client.post(
        f"/api/v1/journeys/{journey_id}/plans/{active_plan_before}/select",
        headers=_auth(companion_id),
    )
    assert generic_select.status_code == 403, generic_select.text
    generic_accept = client.post(
        f"/api/v1/replans/{waiting['proposal_id']}/accept",
        headers={**_auth(companion_id), "Idempotency-Key": "r74-scoped-generic-accept"},
    )
    assert generic_accept.status_code == 403, generic_accept.text
    collaborator_withdraw = client.post(
        f"/api/v1/journeys/{journey_id}/members/{collaborator_member_id}/withdraw",
        headers=_auth(companion_id),
    )
    assert collaborator_withdraw.status_code == 403, collaborator_withdraw.text
    wrong_journey_grant_use = client.post(
        f"/api/v1/journeys/{uuid4()}/member-place-invitations/{invitation_id}/co-planning-grants/{uuid4()}/respond",
        headers=_auth(companion_id),
        json={"decision": "ACCEPT"},
    )
    assert wrong_journey_grant_use.status_code == 404, wrong_journey_grant_use.text

    response_url = f"/api/v1/journeys/{journey_id}/member-place-promotions/{waiting['promotion_id']}/respond"
    for traveler_id in (owner_id, companion_id):
        answer = client.post(
            response_url,
            headers=_auth(traveler_id),
            json={"clarification_id": waiting["clarification_id"], "decision": "APPROVE"},
        )
        assert answer.status_code == 200, answer.text
    after_ab = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    )
    still_waiting = _find_promotion(after_ab.json(), waiting["promotion_id"])
    assert still_waiting["state"] == "WAITING_FOR_MEMBERS"
    assert sum(item["decision"] == "APPROVE" for item in still_waiting["responses"]) == 2
    assert client.get(f"/api/v1/journeys/{journey_id}/experience").json()["active_plan"]["id"] == active_plan_before
    premature_apply = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-promotions/{waiting['promotion_id']}/apply",
        headers={**_auth(owner_id), "Idempotency-Key": "r74-premature-apply"},
    )
    assert premature_apply.status_code == 409, premature_apply.text

    final_answer = client.post(
        response_url,
        headers=_auth(third_id),
        json={"clarification_id": waiting["clarification_id"], "decision": "APPROVE"},
    )
    assert final_answer.status_code == 200, final_answer.text
    authorized = _find_promotion(final_answer.json(), waiting["promotion_id"])
    assert authorized["state"] == "AUTHORIZED"
    assert authorized["viewer_can_apply"] is False
    owner_authorized = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    )
    assert _find_promotion(owner_authorized.json(), waiting["promotion_id"])["viewer_can_apply"] is True
    apply = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-promotions/{waiting['promotion_id']}/apply",
        headers={**_auth(owner_id), "Idempotency-Key": "r73-controlled-success-apply"},
    )
    assert apply.status_code == 200, apply.text
    # Applying the unanimous decision advances the active plan, which makes
    # the one-plan disclosure receipt stale. The projection therefore stops
    # listing the old invitation/promotion; verify the durable application row.
    with SessionLocal() as session:
        applied = session.get(MemberPlacePromotion, waiting["promotion_id"])
        assert applied is not None and applied.state == "APPLIED"
        applied_plan_version_id = str(applied.applied_plan_version_id)
    assert applied_plan_version_id != active_plan_before
    normal_experience = client.get(f"/api/v1/journeys/{journey_id}/experience")
    assert normal_experience.status_code == 200, normal_experience.text
    active_after = normal_experience.json()["active_plan"]
    assert active_after["id"] == applied_plan_version_id
    assert sum(item.get("place_id") == place_id for item in active_after["activities"]) == 1
    post_apply_scoped_replay = client.post(
        response_url,
        headers=_auth(companion_id),
        json={"clarification_id": waiting["clarification_id"], "decision": "APPROVE"},
    )
    assert post_apply_scoped_replay.status_code == 409, post_apply_scoped_replay.text

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        plan = session.get(PlanVersion, str(active_after["id"]))
        assert plan is not None and plan.status == "ACTIVE"
        old_plan = session.get(PlanVersion, active_plan_before)
        assert old_plan is not None and old_plan.status == "SUPERSEDED"
        assert session.scalar(
            select(func.count()).select_from(PlanVersion).where(
                PlanVersion.journey_id == journey_id,
                PlanVersion.status == "ACTIVE",
            )
        ) == 1
        inserted = session.scalars(
            select(Activity).where(
                Activity.plan_version_id == plan.id,
                Activity.place_id == place_id,
            )
        ).all()
        assert len(inserted) == 1 and inserted[0].activity_type.lower() == "visit"
        protected_commitment = session.scalar(
            select(Commitment).where(
                Commitment.journey_id == journey_id,
                Commitment.external_ref == "R73_CONTROLLED_REPLAY_RENDEZVOUS",
                Commitment.status == "ACTIVE",
            )
        )
        assert protected_commitment is not None
        assert session.get(Activity, protected_commitment.activity_id).plan_version_id == plan.id
        recipient_c = session.scalar(
            select(MemberPlaceInvitationRecipient).where(
                MemberPlaceInvitationRecipient.invitation_id == invitation_id,
                MemberPlaceInvitationRecipient.recipient_traveler_id == third_id,
            )
        )
        assert recipient_c is None
        promotion = session.get(MemberPlacePromotion, waiting["promotion_id"])
        assert promotion is not None and promotion.state == "APPLIED"
        proposal = session.get(ReplanProposal, str(promotion.proposal_id))
        assert proposal is not None and proposal.status == "ACCEPTED"
        evidence = json.loads(plan.mobility_evidence_json or "{}")
        assert evidence.get("source_provider") == "R73-TEST-REPLAY-EXPLICIT-FIXTURE"
        assert {item.get("fact_quality") for item in evidence.get("facts", [])} == {EXPLICIT_FROZEN_FIXTURE}


def test_r73_read_only_collaborator_sees_no_fake_promote_authority():
    """R73 must never offer a UI write that existing JourneyRoleCapability forbids."""
    journey_id, owner_id, collaborator_id = _active_shared_journey()
    invitation_id, _owner_quest_id, _place_id = _create_r71_invitation(journey_id, owner_id)
    invitation_url = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    accepted = client.post(
        f"{invitation_url}/respond",
        headers=_auth(collaborator_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "acknowledge_feasibility_unknown": True,
        },
    )
    assert accepted.status_code == 200, accepted.text
    owner_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    ).json()["member_place_invitations"]["sent"][0]
    joined_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(collaborator_id)
    ).json()["member_place_invitations"]["joined"][0]
    assert owner_view["viewer_can_propose"] is True
    assert joined_view["viewer_can_propose"] is False

    unauthorized = client.post(
        f"{invitation_url}/promotions",
        headers={
            **_auth(collaborator_id),
            "Idempotency-Key": "r73-collaborator-has-no-journey-update",
        },
        json={"dwell_minutes": 20},
    )
    assert unauthorized.status_code == 403
    assert "JOURNEY_UPDATE" in unauthorized.text
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(MemberPlacePromotion).where(
                MemberPlacePromotion.journey_id == journey_id
            )
        ) == 0


def _add_r75_member(
    journey_id: str,
    invited_by: str,
    name: str,
    *,
    role: str = "COLLABORATOR",
) -> tuple[str, str]:
    traveler_id = str(uuid4())
    member_id = str(uuid4())
    stamp = execution_clock.now().astimezone(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(Traveler(
            id=traveler_id,
            display_name=name,
            email=None,
            locale="en-US",
            timezone="Asia/Shanghai",
            created_at=stamp,
            updated_at=stamp,
        ))
        session.flush()
        session.add(JourneyMember(
            id=member_id,
            journey_id=journey_id,
            traveler_id=traveler_id,
            role=role,
            invited_by=invited_by,
            expires_at=None,
            joined_at=stamp,
            removed_at=None,
            preferences_json="{}",
            created_at=stamp,
            updated_at=stamp,
        ))
        session.commit()
    return traveler_id, member_id


def _accept_r75_source_invitation(journey_id: str, invitation_id: str, traveler_id: str) -> None:
    accepted = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/respond",
        headers=_auth(traveler_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "acknowledge_feasibility_unknown": True,
        },
    )
    assert accepted.status_code == 200, accepted.text


def test_r75_source_consent_gates_grants_projection_and_withdrawal():
    from app.db.models import MemberPlaceGroupShareConsent

    journey_id, sponsor_id, source_id = _active_shared_journey()
    invitation_id, _quest_id, place_id = _create_r71_invitation(journey_id, source_id)
    _accept_r75_source_invitation(journey_id, invitation_id, sponsor_id)
    late_id, late_member_id = _add_r75_member(
        journey_id, sponsor_id, "R75 late Journey companion C"
    )
    invitation_url = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"

    with SessionLocal() as session:
        place = session.get(Place, place_id)
        invitation = session.get(MemberPlaceInvitation, invitation_id)
        source_member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == source_id,
        ))
        assert place is not None and invitation is not None and source_member is not None
        place_name = str(place.name)
        source_quest_id = str(invitation.owner_side_quest_id)
        assert source_member.role == "COLLABORATOR"
        assert session.scalar(select(func.count()).select_from(main_module.JourneyRoleCapability).where(
            main_module.JourneyRoleCapability.role == source_member.role,
            main_module.JourneyRoleCapability.capability == "JOURNEY_UPDATE",
        )) == 0

    # A signed manager who accepted the invitation still cannot widen B's
    # audience before B's own disclosure confirmation.
    preconsent_grant = client.post(
        invitation_url + "/co-planning-grants",
        headers={**_auth(sponsor_id), "Idempotency-Key": "REDACTED_FOR_PUBLIC_RELEASE"},
        json={"grantee_member_id": late_member_id},
    )
    assert preconsent_grant.status_code == 409
    assert preconsent_grant.json()["error"]["message"] == "AUDIENCE_CHANGED"
    preconsent_promotion = client.post(
        invitation_url + "/promotions",
        headers={**_auth(sponsor_id), "Idempotency-Key": "r75-before-consent-promotion"},
        json={"dwell_minutes": 20},
    )
    assert preconsent_promotion.status_code == 409
    assert preconsent_promotion.json()["error"]["message"] == "AUDIENCE_CHANGED"

    before = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(late_id)
    )
    assert before.status_code == 200, before.text
    assert place_name not in before.text
    assert source_quest_id not in before.text
    assert before.json()["member_place_invitations"]["co_planning_grants"] == []
    assert before.json()["member_place_invitations"]["promotions"] == []

    preview_response = client.get(
        invitation_url + "/group-share/preview", headers=_auth(source_id)
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["place"]["place_id"] == place_id
    assert {item["display_name"] for item in preview["audience"]} == {
        "Alex Chen",
        "R75 late Journey companion C",
    }
    assert "device location" in preview["disclosure"]

    forged_by_sponsor = client.post(
        invitation_url + "/group-share/confirm",
        headers=_auth(sponsor_id),
        json={
            "confirm_full_journey_place_sharing": True,
            "audience_snapshot": preview["audience_snapshot"],
            "plan_snapshot": preview["plan_snapshot"],
            "source_snapshot": preview["source_snapshot"],
        },
    )
    assert forged_by_sponsor.status_code == 403
    forged_by_c = client.post(
        invitation_url + "/group-share/confirm",
        headers=_auth(late_id),
        json={
            "confirm_full_journey_place_sharing": True,
            "audience_snapshot": preview["audience_snapshot"],
            "plan_snapshot": preview["plan_snapshot"],
            "source_snapshot": preview["source_snapshot"],
        },
    )
    assert forged_by_c.status_code == 403

    consented = client.post(
        invitation_url + "/group-share/confirm",
        headers=_auth(source_id),
        json={
            "confirm_full_journey_place_sharing": True,
            "audience_snapshot": preview["audience_snapshot"],
            "plan_snapshot": preview["plan_snapshot"],
            "source_snapshot": preview["source_snapshot"],
        },
    )
    assert consented.status_code == 200, consented.text
    retried_consent = client.post(
        invitation_url + "/group-share/confirm",
        headers=_auth(source_id),
        json={
            "confirm_full_journey_place_sharing": True,
            "audience_snapshot": preview["audience_snapshot"],
            "plan_snapshot": preview["plan_snapshot"],
            "source_snapshot": preview["source_snapshot"],
        },
    )
    assert retried_consent.status_code == 200, retried_consent.text
    with SessionLocal() as session:
        receipt = session.scalar(select(MemberPlaceGroupShareConsent).where(
            MemberPlaceGroupShareConsent.journey_id == journey_id,
            MemberPlaceGroupShareConsent.invitation_id == invitation_id,
        ))
        assert receipt is not None and receipt.status == "ACTIVE"
        assert receipt.owner_member_id != receipt.owner_traveler_id
        assert receipt.place_id == place_id
        assert receipt.audience_snapshot_hash == preview["audience_snapshot"]
        assert receipt.base_plan_version_id == session.get(Journey, journey_id).active_plan_version_id
        assert session.scalar(select(func.count()).select_from(MemberPlaceGroupShareConsent).where(
            MemberPlaceGroupShareConsent.journey_id == journey_id,
            MemberPlaceGroupShareConsent.invitation_id == invitation_id,
            MemberPlaceGroupShareConsent.status == "ACTIVE",
        )) == 1

    offered = client.post(
        invitation_url + "/co-planning-grants",
        headers={**_auth(sponsor_id), "Idempotency-Key": "REDACTED_FOR_PUBLIC_RELEASE"},
        json={"grantee_member_id": late_member_id},
    )
    assert offered.status_code == 201, offered.text
    repeated_offer = client.post(
        invitation_url + "/co-planning-grants",
        headers={**_auth(sponsor_id), "Idempotency-Key": "REDACTED_FOR_PUBLIC_RELEASE-retry"},
        json={"grantee_member_id": late_member_id},
    )
    assert repeated_offer.status_code == 201, repeated_offer.text
    with SessionLocal() as session:
        original_grant = session.scalar(select(SharedStopCoPlanningGrant).where(
            SharedStopCoPlanningGrant.journey_id == journey_id,
            SharedStopCoPlanningGrant.invitation_id == invitation_id,
            SharedStopCoPlanningGrant.grantee_member_id == late_member_id,
            SharedStopCoPlanningGrant.active_slot.is_(True),
        ))
        assert original_grant is not None
        from sqlalchemy.exc import IntegrityError

        duplicate_active_grant = SharedStopCoPlanningGrant(
            id=str(uuid4()),
            journey_id=original_grant.journey_id,
            invitation_id=original_grant.invitation_id,
            base_plan_version_id=original_grant.base_plan_version_id,
            grantee_member_id=original_grant.grantee_member_id,
            grantee_traveler_id=original_grant.grantee_traveler_id,
            grantor_member_id=original_grant.grantor_member_id,
            grantor_traveler_id=original_grant.grantor_traveler_id,
            promotion_id=None,
            active_slot=True,
            purpose=original_grant.purpose,
            status="OFFERED",
            request_key="r75-duplicate-active-slot-" + str(uuid4()),
            request_hash="r75-duplicate-active-slot",
            expires_at=original_grant.expires_at,
            accepted_at=None,
            declined_at=None,
            revoked_at=None,
            decision_epoch=None,
            created_at=original_grant.created_at,
            updated_at=original_grant.updated_at,
        )
        session.add(duplicate_active_grant)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    source_observation_id = str(uuid4())
    with SessionLocal() as session:
        source_member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == source_id,
        ))
        assert source_member is not None
        session.add(LocationObservation(
            id=source_observation_id,
            journey_id=journey_id,
            traveler_id=source_id,
            journey_member_id=source_member.id,
            latitude=35.0,
            longitude=135.0,
            accuracy_m=8,
            captured_at=execution_clock.now().astimezone(timezone.utc).isoformat(),
            source="FOREGROUND_WEB",
        ))
        session.commit()
    after_consent = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(late_id)
    )
    assert after_consent.status_code == 200, after_consent.text
    assert place_name in after_consent.text
    assert source_quest_id not in after_consent.text
    assert source_observation_id not in after_consent.text
    grant_view = after_consent.json()["member_place_invitations"]["co_planning_grants"]
    assert len(grant_view) == 1 and grant_view[0]["place"]["place_id"] == place_id
    source_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(source_id)
    )
    assert source_view.status_code == 200, source_view.text
    assert source_observation_id in source_view.text

    withdrawn = client.post(
        invitation_url + "/group-share/withdraw", headers=_auth(source_id)
    )
    assert withdrawn.status_code == 200, withdrawn.text
    after_withdrawal = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(late_id)
    )
    assert after_withdrawal.status_code == 200, after_withdrawal.text
    assert place_name not in after_withdrawal.text
    assert source_quest_id not in after_withdrawal.text
    assert after_withdrawal.json()["member_place_invitations"]["co_planning_grants"] == []
    rejected_after_withdrawal = client.post(
        invitation_url + "/promotions",
        headers={**_auth(sponsor_id), "Idempotency-Key": "r75-after-withdrawal-promotion"},
        json={"dwell_minutes": 20},
    )
    assert rejected_after_withdrawal.status_code == 409
    assert rejected_after_withdrawal.json()["error"]["message"] == "INVITER_CONSENT_WITHDRAWN"
    with SessionLocal() as session:
        receipt = session.scalar(select(MemberPlaceGroupShareConsent).where(
            MemberPlaceGroupShareConsent.journey_id == journey_id,
            MemberPlaceGroupShareConsent.invitation_id == invitation_id,
        ))
        assert receipt is not None and receipt.status == "WITHDRAWN" and receipt.withdrawn_at


def test_r75_group_share_confirm_rejects_audience_plan_and_source_drift():
    from app.db.models import MemberPlaceGroupShareConsent

    journey_id, sponsor_id, source_id = _active_shared_journey()
    invitation_id, _quest_id, _place_id = _create_r71_invitation(journey_id, source_id)
    _accept_r75_source_invitation(journey_id, invitation_id, sponsor_id)
    invitation_url = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    first_preview = client.get(invitation_url + "/group-share/preview", headers=_auth(source_id))
    assert first_preview.status_code == 200, first_preview.text
    _add_r75_member(journey_id, sponsor_id, "R75 preview-time audience change")
    stale_audience = client.post(
        invitation_url + "/group-share/confirm",
        headers=_auth(source_id),
        json={
            "confirm_full_journey_place_sharing": True,
            "audience_snapshot": first_preview.json()["audience_snapshot"],
            "plan_snapshot": first_preview.json()["plan_snapshot"],
            "source_snapshot": first_preview.json()["source_snapshot"],
        },
    )
    assert stale_audience.status_code == 409
    assert stale_audience.json()["error"]["message"] == "AUDIENCE_CHANGED"
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(MemberPlaceGroupShareConsent).where(
            MemberPlaceGroupShareConsent.invitation_id == invitation_id,
        )) == 0

    preview = client.get(invitation_url + "/group-share/preview", headers=_auth(source_id))
    assert preview.status_code == 200, preview.text
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None and journey.active_plan_version_id
        next_activity = session.scalar(
            select(Activity)
            .where(
                Activity.plan_version_id == journey.active_plan_version_id,
                Activity.id != journey.current_activity_id,
            )
            .order_by(Activity.sequence_no)
        )
        assert next_activity is not None
        journey.current_activity_id = next_activity.id
        session.commit()
    stale_plan = client.post(
        invitation_url + "/group-share/confirm",
        headers=_auth(source_id),
        json={
            "confirm_full_journey_place_sharing": True,
            "audience_snapshot": preview.json()["audience_snapshot"],
            "plan_snapshot": preview.json()["plan_snapshot"],
            "source_snapshot": preview.json()["source_snapshot"],
        },
    )
    assert stale_plan.status_code == 409
    assert stale_plan.json()["error"]["message"] == "PLAN_CHANGED"


def test_r75_group_share_confirm_rejects_member_leave_after_preview():
    from app.db.models import MemberPlaceGroupShareConsent

    journey_id, sponsor_id, source_id = _active_shared_journey()
    invitation_id, _quest_id, _place_id = _create_r71_invitation(journey_id, source_id)
    _accept_r75_source_invitation(journey_id, invitation_id, sponsor_id)
    departing_id, _departing_member_id = _add_r75_member(
        journey_id, sponsor_id, "R75 departing audience member"
    )
    invitation_url = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    preview = client.get(invitation_url + "/group-share/preview", headers=_auth(source_id))
    assert preview.status_code == 200, preview.text
    with SessionLocal() as session:
        departing = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == departing_id,
        ))
        assert departing is not None
        departing.removed_at = execution_clock.now().astimezone(timezone.utc).isoformat()
        session.commit()
    rejected = client.post(
        invitation_url + "/group-share/confirm",
        headers=_auth(source_id),
        json={
            "confirm_full_journey_place_sharing": True,
            "audience_snapshot": preview.json()["audience_snapshot"],
            "plan_snapshot": preview.json()["plan_snapshot"],
            "source_snapshot": preview.json()["source_snapshot"],
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["message"] == "AUDIENCE_CHANGED"
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(MemberPlaceGroupShareConsent).where(
            MemberPlaceGroupShareConsent.invitation_id == invitation_id,
        )) == 0


def test_r75_source_split_change_invalidates_preview_confirmation():
    from app.db.models import MemberPlaceGroupShareConsent

    journey_id, sponsor_id, source_id = _active_shared_journey()
    invitation_id, _quest_id, _place_id = _create_r71_invitation(journey_id, source_id)
    _accept_r75_source_invitation(journey_id, invitation_id, sponsor_id)
    invitation_url = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    preview = client.get(invitation_url + "/group-share/preview", headers=_auth(source_id))
    assert preview.status_code == 200, preview.text
    with SessionLocal() as session:
        invitation = session.get(MemberPlaceInvitation, invitation_id)
        assert invitation is not None
        split = session.get(JourneyIntent, str(invitation.owner_split_intent_id))
        assert split is not None and split.status == "ACTIVE"
        split.status = "WITHDRAWN"
        split.updated_at = execution_clock.now().astimezone(timezone.utc).isoformat()
        session.commit()
    stale_source = client.post(
        invitation_url + "/group-share/confirm",
        headers=_auth(source_id),
        json={
            "confirm_full_journey_place_sharing": True,
            "audience_snapshot": preview.json()["audience_snapshot"],
            "plan_snapshot": preview.json()["plan_snapshot"],
            "source_snapshot": preview.json()["source_snapshot"],
        },
    )
    assert stale_source.status_code == 409
    assert stale_source.json()["error"]["message"] == "INVITER_SPLIT_STALE"
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(MemberPlaceGroupShareConsent).where(
            MemberPlaceGroupShareConsent.invitation_id == invitation_id,
        )) == 0


def test_r75_withdrawal_before_sponsor_offer_keeps_uninvited_member_blind():
    from app.db.models import MemberPlaceGroupShareConsent

    journey_id, sponsor_id, source_id = _active_shared_journey()
    invitation_id, source_quest_id, place_id = _create_r71_invitation(journey_id, source_id)
    _accept_r75_source_invitation(journey_id, invitation_id, sponsor_id)
    late_id, late_member_id = _add_r75_member(
        journey_id, sponsor_id, "R75 not-yet-disclosed companion C"
    )
    invitation_url = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    preview = client.get(invitation_url + "/group-share/preview", headers=_auth(source_id))
    assert preview.status_code == 200, preview.text
    confirmed = client.post(
        invitation_url + "/group-share/confirm",
        headers=_auth(source_id),
        json={
            "confirm_full_journey_place_sharing": True,
            "audience_snapshot": preview.json()["audience_snapshot"],
            "plan_snapshot": preview.json()["plan_snapshot"],
            "source_snapshot": preview.json()["source_snapshot"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    withdrawn = client.post(invitation_url + "/group-share/withdraw", headers=_auth(source_id))
    assert withdrawn.status_code == 200, withdrawn.text

    grant = client.post(
        invitation_url + "/co-planning-grants",
        headers={**_auth(sponsor_id), "Idempotency-Key": "REDACTED_FOR_PUBLIC_RELEASE"},
        json={"grantee_member_id": late_member_id},
    )
    assert grant.status_code == 409
    assert grant.json()["error"]["message"] == "INVITER_CONSENT_WITHDRAWN"
    promotion = client.post(
        invitation_url + "/promotions",
        headers={**_auth(sponsor_id), "Idempotency-Key": "r75-withdrawn-before-offer-promotion"},
        json={"dwell_minutes": 20},
    )
    assert promotion.status_code == 409
    assert promotion.json()["error"]["message"] == "INVITER_CONSENT_WITHDRAWN"
    c_projection = client.get(f"/api/v1/journeys/{journey_id}/operator", headers=_auth(late_id))
    assert c_projection.status_code == 200, c_projection.text
    assert c_projection.json()["member_place_invitations"]["co_planning_grants"] == []
    assert c_projection.json()["member_place_invitations"]["promotions"] == []
    assert c_projection.json()["member_side_quest"] is None
    assert str(place_id) not in c_projection.text
    assert source_quest_id not in c_projection.text
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(SharedStopCoPlanningGrant).where(
            SharedStopCoPlanningGrant.invitation_id == invitation_id,
        )) == 0
        assert session.scalar(select(func.count()).select_from(MemberPlacePromotion).where(
            MemberPlacePromotion.invitation_id == invitation_id,
        )) == 0
        receipt = session.scalar(select(MemberPlaceGroupShareConsent).where(
            MemberPlaceGroupShareConsent.invitation_id == invitation_id,
        ))
        assert receipt is not None and receipt.status == "WITHDRAWN"


def test_r75_uninvited_admin_cannot_sponsor_or_learn_source_place():
    journey_id, sponsor_id, source_id = _active_shared_journey()
    invitation_id, source_quest_id, place_id = _create_r71_invitation(journey_id, source_id)
    _accept_r75_source_invitation(journey_id, invitation_id, sponsor_id)
    _late_id, late_member_id = _add_r75_member(
        journey_id, sponsor_id, "R75 grant target C"
    )
    admin_id, _admin_member_id = _add_r75_member(
        journey_id, sponsor_id, "R75 uninvolved manager D", role="ADMIN"
    )
    invitation_url = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    offer = client.post(
        invitation_url + "/co-planning-grants",
        headers={**_auth(admin_id), "Idempotency-Key": "r75-uninvited-admin-grant"},
        json={"grantee_member_id": late_member_id},
    )
    assert offer.status_code == 403
    assert "JOURNEY_UPDATE" in offer.text
    admin_projection = client.get(f"/api/v1/journeys/{journey_id}/operator", headers=_auth(admin_id))
    c_projection = client.get(f"/api/v1/journeys/{journey_id}/operator", headers=_auth(_late_id))
    assert admin_projection.status_code == c_projection.status_code == 200
    for projection in (admin_projection, c_projection):
        assert str(place_id) not in projection.text
        assert source_quest_id not in projection.text
        assert projection.json()["member_place_invitations"]["co_planning_grants"] == []
        assert projection.json()["member_place_invitations"]["promotions"] == []


def test_r75_expired_group_share_receipt_cannot_offer_or_project_a_place():
    from app.db.models import MemberPlaceGroupShareConsent

    journey_id, sponsor_id, source_id = _active_shared_journey()
    invitation_id, _quest_id, place_id = _create_r71_invitation(journey_id, source_id)
    _accept_r75_source_invitation(journey_id, invitation_id, sponsor_id)
    late_id, late_member_id = _add_r75_member(
        journey_id, sponsor_id, "R75 expiry companion C"
    )
    invitation_url = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    preview = client.get(invitation_url + "/group-share/preview", headers=_auth(source_id))
    assert preview.status_code == 200, preview.text
    confirmed = client.post(
        invitation_url + "/group-share/confirm",
        headers=_auth(source_id),
        json={
            "confirm_full_journey_place_sharing": True,
            "audience_snapshot": preview.json()["audience_snapshot"],
            "plan_snapshot": preview.json()["plan_snapshot"],
            "source_snapshot": preview.json()["source_snapshot"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    with SessionLocal() as session:
        receipt = session.scalar(select(MemberPlaceGroupShareConsent).where(
            MemberPlaceGroupShareConsent.journey_id == journey_id,
            MemberPlaceGroupShareConsent.invitation_id == invitation_id,
        ))
        place = session.get(Place, place_id)
        assert receipt is not None and place is not None
        receipt.expires_at = (execution_clock.now().astimezone(timezone.utc) - timedelta(seconds=1)).isoformat()
        place_name = str(place.name)
        session.commit()
    offer = client.post(
        invitation_url + "/co-planning-grants",
        headers={**_auth(sponsor_id), "Idempotency-Key": "r75-expired-share-grant"},
        json={"grantee_member_id": late_member_id},
    )
    assert offer.status_code == 409
    assert offer.json()["error"]["message"] == "INVITER_CONSENT_EXPIRED"
    projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(late_id)
    )
    assert projection.status_code == 200, projection.text
    assert place_name not in projection.text
    assert projection.json()["member_place_invitations"]["co_planning_grants"] == []
