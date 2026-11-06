from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.core.config import settings
from app.db.models import (
    ActionApproval,
    Activity,
    AutopilotEnrollment,
    Commitment,
    Event,
    IdempotencyKey,
    Journey,
    JourneyIntent,
    JourneyMember,
    LocationObservation,
    PlanVersion,
    ReplanProposal,
    RouteAcquisitionOperation,
    Traveler,
)
from app.db.session import SessionLocal
from app.execution.clock import execution_clock
from app.traveler_principal import issue_traveler_token
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)
TEST_SECRET = "r63-planned-separation-test-secret-0123456789"


def test_negated_side_trip_language_does_not_start_a_planned_separation():
    assert main_module._planned_separation_command_action(
        "I don't want a side trip; I want to stay with the group."
    ) is None
    assert main_module._planned_separation_command_action("我不想单独行动，继续和大家一起") is None
    assert main_module._planned_separation_command_action(
        "I'm going to explore alone and meet at the next shared stop."
    ) == "START"


def _auth(traveler_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_traveler_token(traveler_id)}"}


def _active_shared_journey() -> tuple[str, str, str]:
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, reset.text
    journey_id = reset.json()["journey_id"]
    plans = client.post(f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1})
    assert plans.status_code == 201, plans.text
    plan_id = plans.json()[0]["id"]
    assert client.post(f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select").status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    now = datetime.now(timezone.utc).isoformat()
    second_id = str(uuid4())
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        owner_id = journey.traveler_id
        session.add(Traveler(
            id=second_id, display_name="R63同行者", email=None, locale="zh-CN",
            timezone="UTC", created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(JourneyMember(
            id=str(uuid4()), journey_id=journey_id, traveler_id=second_id,
            role="COLLABORATOR", invited_by=owner_id, expires_at=None,
            joined_at=now, removed_at=None, preferences_json="{}",
            created_at=now, updated_at=now,
        ))
        session.commit()
    owner_headers = _auth(owner_id)
    started = client.post(
        f"/api/v1/journeys/{journey_id}/operator/start",
        headers=owner_headers,
        json={"interval_seconds": 3600},
    )
    assert started.status_code == 200, started.text
    return journey_id, owner_id, second_id


def test_signed_member_can_create_only_their_own_plan_bound_side_trip(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    try:
        journey_id, owner_id, other_id = _active_shared_journey()
        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            assert journey is not None
            active_plan_before = str(journey.active_plan_version_id)
            activity_count_before = len(session.scalars(
                select(Activity).where(Activity.plan_version_id == active_plan_before)
            ).all())
            plan_count_before = len(session.scalars(
                select(PlanVersion).where(PlanVersion.journey_id == journey_id)
            ).all())
            proposal_count_before = len(session.scalars(
                select(ReplanProposal).where(ReplanProposal.journey_id == journey_id)
            ).all())

        command = "I'm going to browse the shops alone for a while; I'll meet everyone at the next shared stop."
        created = client.post(
            f"/api/v1/journeys/{journey_id}/agent/commands",
            headers=_auth(owner_id),
            json={"command": command, "requested_autonomy": "PLAN_ONLY"},
        )
        assert created.status_code == 200, created.text
        result = created.json()
        assert result["planned_separation_result"]["outcome"] == "PLANNED_SPLIT_CREATED"
        assert result["planned_separation_result"]["physical_reunion_claimed"] is False
        assert result["group_presence"]["state"] == "PLANNED_SPLIT_RETURN_UNKNOWN"
        assert result["group_presence"]["planned_separation"]["shared_plan_changed"] is False
        assert result["group_presence"]["planned_separation"]["rendezvous"]["activity_id"]
        assert result["group_recovery"]["can_request_planning"] is False

        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            assert journey is not None
            assert str(journey.active_plan_version_id) == active_plan_before
            assert len(session.scalars(
                select(Activity).where(Activity.plan_version_id == active_plan_before)
            ).all()) == activity_count_before
            assert len(session.scalars(
                select(PlanVersion).where(PlanVersion.journey_id == journey_id)
            ).all()) == plan_count_before
            assert len(session.scalars(
                select(ReplanProposal).where(ReplanProposal.journey_id == journey_id)
            ).all()) == proposal_count_before
            intents = session.scalars(
                select(JourneyIntent).where(
                    JourneyIntent.journey_id == journey_id,
                    JourneyIntent.intent_kind == "PLANNED_SEPARATION",
                )
            ).all()
            assert len(intents) == 1
            assert intents[0].created_by_traveler_id == owner_id
            assert intents[0].created_by_member_id != other_id
            assert intents[0].scope_kind == "UNTIL_RENDEZVOUS"
            assert intents[0].target_activity_ids_json != "[]"
            assert len(session.scalars(select(ActionApproval).where(ActionApproval.journey_id == journey_id)).all()) == 0
            assert len(session.scalars(select(Event).where(Event.journey_id == journey_id)).all()) == 0
            assert session.scalar(select(IdempotencyKey.key).where(
                IdempotencyKey.operation == "group_recovery.execution_arrival",
            )) is None

        unauthenticated = client.post(
            f"/api/v1/journeys/{journey_id}/agent/commands",
            json={"command": command, "requested_autonomy": "PLAN_ONLY"},
        )
        assert unauthenticated.status_code == 401

        # AgentCommandRequest forbids client-selected identities; the signed
        # second member can create only their own intent through the same route.
        spoofed = client.post(
            f"/api/v1/journeys/{journey_id}/agent/commands",
            headers=_auth(other_id),
            json={"command": command, "requested_autonomy": "PLAN_ONLY", "member_id": "owner"},
        )
        assert spoofed.status_code == 422
        other_created = client.post(
            f"/api/v1/journeys/{journey_id}/agent/commands",
            headers=_auth(other_id),
            json={"command": "I want to go by myself for a while and meet at the next shared stop.", "requested_autonomy": "PLAN_ONLY"},
        )
        assert other_created.status_code == 200, other_created.text
        with SessionLocal() as session:
            intents = session.scalars(
                select(JourneyIntent).where(
                    JourneyIntent.journey_id == journey_id,
                    JourneyIntent.intent_kind == "PLANNED_SEPARATION",
                    JourneyIntent.status == "ACTIVE",
                )
            ).all()
            assert {str(item.created_by_traveler_id) for item in intents} == {owner_id, other_id}
            assert len({str(item.created_by_member_id) for item in intents}) == 2
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_ending_member_intent_does_not_claim_reunion_or_cancel_another_member_intent(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    try:
        journey_id, owner_id, other_id = _active_shared_journey()
        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            assert journey is not None
            active_plan_before = str(journey.active_plan_version_id)
        command = "I want to explore alone for a while; we will meet at the next shared stop."
        for traveler_id, text in ((owner_id, command), (other_id, command.replace("explore", "browse"))):
            response = client.post(
                f"/api/v1/journeys/{journey_id}/agent/commands",
                headers=_auth(traveler_id),
                json={"command": text, "requested_autonomy": "PLAN_ONLY"},
            )
            assert response.status_code == 200, response.text
        ended = client.post(
            f"/api/v1/journeys/{journey_id}/agent/commands",
            headers=_auth(owner_id),
            json={"command": "I'm back with the group.", "requested_autonomy": "PLAN_ONLY"},
        )
        assert ended.status_code == 200, ended.text
        assert ended.json()["planned_separation_result"]["outcome"] == "PLANNED_SPLIT_ENDED"
        assert ended.json()["planned_separation_result"]["physical_reunion_claimed"] is False
        with SessionLocal() as session:
            intents = session.scalars(
                select(JourneyIntent).where(
                    JourneyIntent.journey_id == journey_id,
                    JourneyIntent.intent_kind == "PLANNED_SEPARATION",
                )
            ).all()
            owner = next(item for item in intents if item.created_by_traveler_id == owner_id)
            other = next(item for item in intents if item.created_by_traveler_id == other_id)
            assert owner.status == "CANCELLED"
            assert other.status == "ACTIVE"
            journey = session.get(Journey, journey_id)
            assert journey is not None
            assert str(journey.active_plan_version_id) == active_plan_before
        assert ended.json()["group_presence"]["state"] != "TOGETHER"
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_replacing_one_members_split_does_not_supersede_another_members_intent(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    try:
        journey_id, owner_id, other_id = _active_shared_journey()
        commands = (
            (owner_id, "I want to go alone and meet at Tech Art Center."),
            (other_id, "I want to browse by myself and meet at Riverside Museum."),
            (owner_id, "I want to split off alone and meet at Central Food Hall."),
        )
        for traveler_id, command in commands:
            response = client.post(
                f"/api/v1/journeys/{journey_id}/agent/commands",
                headers=_auth(traveler_id),
                json={"command": command, "requested_autonomy": "PLAN_ONLY"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["planned_separation_result"]["outcome"] == "PLANNED_SPLIT_CREATED"
        with SessionLocal() as session:
            intents = session.scalars(select(JourneyIntent).where(
                JourneyIntent.journey_id == journey_id,
                JourneyIntent.intent_kind == "PLANNED_SEPARATION",
            )).all()
            owner_intents = [item for item in intents if item.created_by_traveler_id == owner_id]
            other_intents = [item for item in intents if item.created_by_traveler_id == other_id]
            assert len(owner_intents) == 2
            assert sorted(item.status for item in owner_intents) == ["ACTIVE", "SUPERSEDED"]
            assert len(other_intents) == 1 and other_intents[0].status == "ACTIVE"
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_changed_parent_or_completed_rendezvous_stops_claiming_current_split(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    try:
        journey_id, owner_id, _other_id = _active_shared_journey()
        created = client.post(
            f"/api/v1/journeys/{journey_id}/agent/commands",
            headers=_auth(owner_id),
            json={"command": "I want to go alone and meet at Tech Art Center.", "requested_autonomy": "PLAN_ONLY"},
        )
        assert created.status_code == 200, created.text
        intent_id = created.json()["planned_separation_result"]["intent_id"]
        with SessionLocal() as session:
            intent = session.get(JourneyIntent, intent_id)
            assert intent is not None
            envelope = json.loads(intent.semantic_envelope_json)
            envelope["r63_planned_separation"]["active_plan_version_id"] = "old-plan-version"
            intent.semantic_envelope_json = json.dumps(envelope)
            session.commit()

        stale = client.get(f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id))
        assert stale.status_code == 200, stale.text
        assert stale.json()["group_presence"]["planned_separation"] is None
        assert stale.json()["group_presence"]["state"] != "TOGETHER"
        assert not any(
            item.get("intent_kind") == "PLANNED_SEPARATION"
            for item in stale.json().get("intents", [])
        )

        with SessionLocal() as session:
            intent = session.get(JourneyIntent, intent_id)
            assert intent is not None
            envelope = json.loads(intent.semantic_envelope_json)
            envelope["r63_planned_separation"]["active_plan_version_id"] = str(
                session.get(Journey, journey_id).active_plan_version_id
            )
            intent.semantic_envelope_json = json.dumps(envelope)
            target = session.get(Activity, created.json()["planned_separation_result"]["rendezvous_activity_id"])
            assert target is not None
            target.status = "COMPLETED"
            session.commit()

        from app.agent.journey_intent import expire_journey_intents

        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            intent = session.get(JourneyIntent, intent_id)
            assert journey is not None and intent is not None
            assert expire_journey_intents(
                session, journey, now=execution_clock.now().astimezone(timezone.utc)
            ) == 1
            session.commit()
            assert intent.status == "EXPIRED"

        expired = client.get(f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id))
        assert expired.status_code == 200, expired.text
        assert expired.json()["group_presence"]["planned_separation"] is None
        assert expired.json()["group_presence"]["state"] != "TOGETHER"
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_planned_route_uses_member_rendezvous_and_trusted_live_risk_hands_off_to_guardian(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    try:
        journey_id, owner_id, _other_id = _active_shared_journey()
        created = client.post(
            f"/api/v1/journeys/{journey_id}/agent/commands",
            headers=_auth(owner_id),
            json={
                "command": "I want to go alone and meet at Tech Art Center.",
                "requested_autonomy": "PLAN_ONLY",
            },
        )
        assert created.status_code == 200, created.text
        target_activity_id = created.json()["planned_separation_result"]["rendezvous_activity_id"]

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
            target = session.get(Activity, target_activity_id)
            observation = session.get(LocationObservation, sample.json()["id"])
            assert journey is not None and target is not None and observation is not None
            observation.source = "DEVICE_ATTESTED"
            session.add(Commitment(
                id=str(uuid4()),
                journey_id=journey_id,
                activity_id=target_activity_id,
                kind="APPOINTMENT",
                level="C4_Anchored",
                status="ACTIVE",
                monetary_value_minor=0,
                monetary_value_status="KNOWN",
                currency="JPY",
                external_ref="R63-TRUSTED-RENDEZVOUS-RISK",
                policy_json=json.dumps({
                    "scheduled_start_at": (now + timedelta(minutes=5)).isoformat(),
                    "safety_buffer_min": 5,
                    "source": "R63_DETERMINISTIC_REGRESSION_FIXTURE",
                }),
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            ))
            session.commit()
            active_plan_before = str(journey.active_plan_version_id)

        route_requests = []
        guardian_calls = []
        route_provenance = {"value": "CAPTURED_REPLAY"}

        def fake_submit(session, journey, route_request):
            route_requests.append(route_request)
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
                    "provenance": route_provenance["value"],
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
        def route_payload(*_args, **_kwargs):
            return {
                "status": "COMPLETED",
                "outcome": "ROUTE_READY",
                "provider": "valhalla-http",
                "provider_provenance": route_provenance["value"],
                "result": {
                    "duration_min": 30,
                    "distance_m": 2400,
                    "provider": "valhalla-http",
                    "provenance": route_provenance["value"],
                },
            }

        monkeypatch.setattr(main_module, "route_operation_payload", route_payload)
        monkeypatch.setattr(
            main_module,
            "scan_guardian",
            lambda *_args, **_kwargs: guardian_calls.append("called") or {"status": "COMPLETED"},
        )
        replayed = client.post(
            f"/api/v1/journeys/{journey_id}/group-presence/route",
            headers=_auth(owner_id),
            json={"location_observation_id": sample.json()["id"], "requested_mode": "WALK"},
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["shared_commitment_risk"]["state"] == "COMMITMENT_AT_RISK"
        assert replayed.json()["route"]["provenance"] == "CAPTURED_REPLAY"
        assert replayed.json()["member_risk_event_id"] is None
        assert replayed.json()["guardian_triggered"] is False
        assert guardian_calls == []
        with SessionLocal() as session:
            assert session.scalar(select(Event.id).where(
                Event.journey_id == journey_id,
                Event.provider_key == "journey-group-presence",
            )) is None

        route_provenance["value"] = "LIVE_PROVIDER_CAPTURE"
        routed = client.post(
            f"/api/v1/journeys/{journey_id}/group-presence/route",
            headers=_auth(owner_id),
            json={"location_observation_id": sample.json()["id"], "requested_mode": "WALK"},
        )
        assert routed.status_code == 200, routed.text
        body = routed.json()
        assert body["target"]["activity_id"] == target_activity_id
        assert route_requests[0].target_activity_id == target_activity_id
        assert body["shared_commitment_risk"]["state"] == "COMMITMENT_AT_RISK"
        assert body["guardian_triggered"] is True
        assert guardian_calls == ["called"]
        assert body["plan_unchanged"] is True

        repeated_risk = client.post(
            f"/api/v1/journeys/{journey_id}/group-presence/route",
            headers=_auth(owner_id),
            json={"location_observation_id": sample.json()["id"], "requested_mode": "WALK"},
        )
        assert repeated_risk.status_code == 200, repeated_risk.text
        assert repeated_risk.json()["guardian_triggered"] is False
        assert repeated_risk.json()["guardian_deduplicated"] is True
        assert guardian_calls == ["called"]

        projection = client.get(
            f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
        )
        assert projection.status_code == 200, projection.text
        assert projection.json()["group_presence"]["state"] == "PLANNED_SPLIT_RETURN_RISK"
        assert projection.json()["group_recovery"]["state"] == "VERIFIED_MEMBER_RISK"
        assert projection.json()["group_recovery"]["can_request_planning"] is True
        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            assert journey is not None
            assert str(journey.active_plan_version_id) == active_plan_before
            assert len(session.scalars(select(Event).where(
                Event.journey_id == journey_id,
                Event.provider_key == "journey-group-presence",
            )).all()) == 1
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_advisory_planned_route_does_not_request_group_recovery(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    try:
        journey_id, owner_id, _other_id = _active_shared_journey()
        created = client.post(
            f"/api/v1/journeys/{journey_id}/agent/commands",
            headers=_auth(owner_id),
            json={
                "command": "I want to go alone and meet at Tech Art Center.",
                "requested_autonomy": "PLAN_ONLY",
            },
        )
        assert created.status_code == 200, created.text
        target_id = created.json()["planned_separation_result"]["rendezvous_activity_id"]
        now = execution_clock.now().astimezone(timezone.utc)
        sample = client.post(
            f"/api/v1/journeys/{journey_id}/location",
            headers=_auth(owner_id),
            json={"latitude": 35.0, "longitude": 135.0, "accuracy_m": 8, "captured_at": now.isoformat()},
        )
        assert sample.status_code == 201, sample.text

        def fake_submit(session, journey, route_request):
            stamp = execution_clock.now().astimezone(timezone.utc).isoformat()
            operation = RouteAcquisitionOperation(
                id=str(uuid4()),
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
                    "provider": "valhalla-http",
                    "provenance": "LIVE_PROVIDER_CAPTURE",
                }),
                error_json="{}",
                queued_at=stamp,
                started_at=stamp,
                finished_at=stamp,
                updated_at=stamp,
            )
            session.add(operation)
            session.flush()
            return operation, False

        monkeypatch.setattr(main_module, "submit_route_operation", fake_submit)
        monkeypatch.setattr(main_module, "route_operation_payload", lambda *_args, **_kwargs: {
            "status": "COMPLETED",
            "outcome": "ROUTE_READY",
            "provider": "valhalla-http",
            "provider_provenance": "LIVE_PROVIDER_CAPTURE",
            "result": {
                "duration_min": 30,
                "provider": "valhalla-http",
                "provenance": "LIVE_PROVIDER_CAPTURE",
            },
        })
        response = client.post(
            f"/api/v1/journeys/{journey_id}/group-presence/route",
            headers=_auth(owner_id),
            json={"location_observation_id": sample.json()["id"], "requested_mode": "WALK"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["target"]["activity_id"] == target_id
        # Browser location remains advisory even when the route provider responds.
        assert response.json()["authority"] == "ADVISORY_FOREGROUND_LOCATION"
        projection = client.get(
            f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
        )
        assert projection.status_code == 200, projection.text
        assert projection.json()["group_presence"]["state"] == "PLANNED_SPLIT_RETURN_UNKNOWN"
        assert projection.json()["group_recovery"]["can_request_planning"] is False
        assert projection.json()["group_recovery"]["state"] != "VERIFIED_MEMBER_RISK"
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_grounded_safe_return_projects_on_track_without_recovery_or_mutation(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    try:
        journey_id, owner_id, _other_id = _active_shared_journey()
        created = client.post(
            f"/api/v1/journeys/{journey_id}/agent/commands",
            headers=_auth(owner_id),
            json={"command": "I want to browse alone and meet at the next shared stop.", "requested_autonomy": "PLAN_ONLY"},
        )
        assert created.status_code == 200, created.text
        target_id = created.json()["planned_separation_result"]["rendezvous_activity_id"]
        now = execution_clock.now().astimezone(timezone.utc)
        observation_id = str(uuid4())
        route_id = str(uuid4())
        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            target = session.get(Activity, target_id)
            member = session.scalar(select(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == owner_id,
            ))
            assert journey is not None and target is not None and member is not None
            plan_before = str(journey.active_plan_version_id)
            session.add(Commitment(
                id=str(uuid4()), journey_id=journey_id, activity_id=target_id,
                kind="APPOINTMENT", level="C4_Anchored", status="ACTIVE",
                monetary_value_minor=0, monetary_value_status="KNOWN", currency="JPY",
                external_ref="R63-SAFE-RENDEZVOUS",
                policy_json=json.dumps({
                    "scheduled_start_at": (now + timedelta(minutes=120)).isoformat(),
                    "safety_buffer_min": 10,
                }),
                created_at=now.isoformat(), updated_at=now.isoformat(),
            ))
            session.add(LocationObservation(
                id=observation_id, journey_id=journey_id, traveler_id=owner_id,
                journey_member_id=member.id, latitude=35.0, longitude=135.0,
                accuracy_m=8, captured_at=now.isoformat(), source="DEVICE_ATTESTED",
            ))
            session.flush()
            session.add(RouteAcquisitionOperation(
                id=route_id, journey_id=journey_id, fingerprint=str(uuid4()),
                origin_key=f"location-observation:{observation_id}",
                target_key=str(target.place_id), target_activity_id=target.id,
                requested_mode="WALK", provider_policy_version="R63_TEST_PROVIDER_POLICY",
                route_truth_policy_version="R63_TEST_TRUTH_POLICY", status="COMPLETED",
                phase="COMPLETED", outcome="ROUTE_READY", request_json="{}",
                result_json=json.dumps({
                    "duration_min": 30,
                    "provider": "valhalla-http",
                    "provenance": "LIVE_PROVIDER_CAPTURE",
                }), error_json="{}", queued_at=now.isoformat(),
                started_at=now.isoformat(), finished_at=now.isoformat(), updated_at=now.isoformat(),
            ))
            session.commit()

        response = client.get(
            f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["group_presence"]["state"] == "PLANNED_SPLIT_ON_TRACK"
        owner = next(item for item in body["group_presence"]["members"] if item["is_viewer"])
        assert owner["return_state"] == "ON_TRACK"
        assert owner["route_eta_state"] == "LIVE_PROVIDER_CAPTURE"
        assert body["group_recovery"]["can_request_planning"] is False
        assert body["group_recovery"]["state"] == "NO_SHARED_RISK"
        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            assert journey is not None
            assert str(journey.active_plan_version_id) == plan_before
            assert session.scalar(select(Event.id).where(
                Event.journey_id == journey_id,
                Event.provider_key == "journey-group-presence",
            )) is None
            assert session.scalar(select(IdempotencyKey.key).where(
                IdempotencyKey.operation == "group_recovery.execution_arrival",
            )) is None
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
