from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.main as main_module
from app.core.config import settings
from app.db.models import (
    Activity,
    AuditLog,
    Journey,
    JourneyMember,
    JourneyRoleCapability,
    PlanVersion,
    Place,
    Traveler,
    IdempotencyKey,
)
from app.db.session import SessionLocal
from app.execution.group_recovery_followthrough import (
    close_recovery_if_rejoined,
    followthrough_projection,
)
from app.traveler_principal import issue_traveler_token


client = TestClient(main_module.app)
TEST_SECRET = "REDACTED_FOR_PUBLIC_RELEASE"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_applied_recovery() -> dict[str, str]:
    now = _now()
    owner_id, companion_id = str(uuid4()), str(uuid4())
    journey_id, owner_member_id, companion_member_id = (str(uuid4()) for _ in range(3))
    plan_id, activity_id, place_id = (str(uuid4()) for _ in range(3))
    incident_key = f"r62-incident-{journey_id}"
    epoch_id = str(uuid4())
    proposal_id = str(uuid4())
    future_end = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
    snapshot = {
        "state": "RECOVERY_APPLIED_REJOIN_PENDING",
        "journey_id": journey_id,
        "incident_key": incident_key,
        "applied_proposal_id": proposal_id,
        "applied_plan_version_id": plan_id,
        "recovery_epoch_id": epoch_id,
        "authority_receipt_id": str(uuid4()),
        "trigger_event_id": None,
        "required_member_ids": [owner_member_id, companion_member_id],
        "member_roles_at_apply": {owner_member_id: "OWNER", companion_member_id: "ADMIN"},
        "rendezvous_activity_id": activity_id,
        "rendezvous_place_id": place_id,
        "rendezvous_title": "Shared stop",
        "applied_at": now,
        "evidence_level": "AUTHENTICATED_ACTIVITY_EXECUTION_TRUTH",
        "physical_presence_verified": False,
    }
    spend = {
        "kind": "COLLECTIVE_GROUP_RECOVERY_APPLY",
        "result": "APPLIED",
        "incident_key": incident_key,
        "selected_proposal_id": proposal_id,
        "after_plan_version_id": plan_id,
        "before_plan_version_id": str(uuid4()),
        "recovery_epoch_id": epoch_id,
        "followthrough": snapshot,
    }
    with SessionLocal() as session:
        session.add_all(
            [
                Traveler(id=owner_id, display_name="Owner", locale="en-US", timezone="UTC", created_at=now, updated_at=now),
                Traveler(id=companion_id, display_name="Companion", locale="en-US", timezone="UTC", created_at=now, updated_at=now),
            ]
        )
        session.flush()
        journey = Journey(
            id=journey_id,
            traveler_id=owner_id,
            title="R62 group rejoin",
            city_code="NARA",
            timezone="UTC",
            start_at=now,
            end_at=future_end,
            budget_currency="JPY",
            budget_amount_minor=10000,
            planning_mode="balanced",
            status="ACTIVE",
            active_plan_version_id=None,
            current_activity_id=None,
            created_at=now,
            updated_at=now,
        )
        session.add(journey)
        session.flush()
        session.add_all(
            [
                JourneyMember(id=owner_member_id, journey_id=journey_id, traveler_id=owner_id, role="OWNER", invited_by=owner_id, joined_at=now, preferences_json="{}", created_at=now, updated_at=now),
                JourneyMember(id=companion_member_id, journey_id=journey_id, traveler_id=companion_id, role="ADMIN", invited_by=owner_id, joined_at=now, preferences_json="{}", created_at=now, updated_at=now),
                Place(id=place_id, provider_key="r62-test", external_id=place_id, city_code="NARA", name="Shared stop", category="landmark", latitude=34.68, longitude=135.83, opening_hours_json="{}", tags_json="[]", metadata_json="{}", updated_at=now),
                PlanVersion(id=plan_id, journey_id=journey_id, version_no=2, planning_mode="balanced", status="ACTIVE", lifecycle="ACTIVE", source="REPLAN", parent_plan_version_id=None, total_cost_minor=0, currency="JPY", estimated_duration_min=30, utility_score=0.0, score_breakdown_json="{}", explanation_json="{}", mobility_evidence_json="{}", created_at=now),
            ]
        )
        session.flush()
        session.add(
            Activity(
                id=activity_id,
                plan_version_id=plan_id,
                sequence_no=1,
                activity_type="VISIT",
                title="Shared stop",
                place_id=place_id,
                city_service_id=None,
                start_at=now,
                end_at=future_end,
                travel_from_prev_min=0,
                estimated_cost_minor=0,
                currency="JPY",
                status="EN_ROUTE",
                locked=False,
                metadata_json="{}",
                created_at=now,
                updated_at=now,
            )
        )
        journey.active_plan_version_id = plan_id
        journey.current_activity_id = activity_id
        session.add(
            AuditLog(
                id=str(uuid4()),
                actor_type="AUTHENTICATED_TRAVELER",
                actor_id=owner_id,
                journey_id=journey_id,
                action="replan.accepted",
                entity_type="ReplanProposal",
                entity_id=proposal_id,
                before_json="{}",
                after_json=json.dumps({"group_recovery_spend": spend}, sort_keys=True),
                trace_id=str(uuid4()),
                created_at=now,
            )
        )
        session.commit()

    return {
        "journey_id": journey_id,
        "plan_id": plan_id,
        "activity_id": activity_id,
        "owner_id": owner_id,
        "companion_id": companion_id,
        "owner_member_id": owner_member_id,
        "companion_member_id": companion_member_id,
        "incident_key": incident_key,
    }


def _auth_headers(traveler_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_traveler_token(traveler_id)}"}


def _arrival_payload() -> dict[str, object]:
    return {"truth_kind": "OPERATOR_CONFIRMED_EXECUTION", "operator_confirmed": True}


def test_two_members_arrive_through_existing_execution_route_and_close_once():
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    ids = _seed_applied_recovery()
    try:
        endpoint = f"/api/v1/journeys/{ids['journey_id']}/activities/{ids['activity_id']}/arrive"
        forged = client.post(
            endpoint,
            headers=_auth_headers(ids["owner_id"]),
            json={**_arrival_payload(), "member_id": ids["companion_member_id"], "traveler_id": ids["companion_id"]},
        )
        assert forged.status_code == 422, forged.text

        first = client.post(endpoint, headers=_auth_headers(ids["owner_id"]), json=_arrival_payload())
        assert first.status_code == 200, first.text
        with SessionLocal() as session:
            journey = session.get(Journey, ids["journey_id"])
            assert journey is not None
            first_projection = followthrough_projection(
                session, journey, viewer_member_id=ids["owner_member_id"]
            )
            assert first_projection is not None
            assert first_projection["state"] == "REJOIN_IN_PROGRESS"
            assert first_projection["confirmed_count"] == 1
            assert first_projection["viewer_has_confirmed"] is True
            assert first_projection["plan_unchanged_by_closure"] is False
            assert session.scalar(
                select(AuditLog).where(
                    AuditLog.journey_id == ids["journey_id"],
                    AuditLog.action == "journey.group_recovery.rejoined",
                )
            ) is None

        begin = client.post(
            f"/api/v1/journeys/{ids['journey_id']}/activities/{ids['activity_id']}/begin",
            headers=_auth_headers(ids["owner_id"]),
            json=_arrival_payload(),
        )
        assert begin.status_code == 409, begin.text
        assert "GROUP_RECOVERY_WAITING_FOR_MEMBER_REJOIN" in begin.text
        early_complete = client.post(
            f"/api/v1/journeys/{ids['journey_id']}/activities/{ids['activity_id']}/complete",
            headers=_auth_headers(ids["owner_id"]),
            json=_arrival_payload(),
        )
        assert early_complete.status_code == 409, early_complete.text
        assert "GROUP_RECOVERY_WAITING_FOR_MEMBER_REJOIN" in early_complete.text

        second = client.post(
            endpoint, headers=_auth_headers(ids["companion_id"]), json=_arrival_payload()
        )
        assert second.status_code == 200, second.text
        replay = client.post(endpoint, headers=_auth_headers(ids["owner_id"]), json=_arrival_payload())
        assert replay.status_code == 200, replay.text

        # Once the reunion is durably closed, normal shared Journey execution
        # owns the cursor again and the same activity can begin.
        resumed_begin = client.post(
            f"/api/v1/journeys/{ids['journey_id']}/activities/{ids['activity_id']}/begin",
            headers=_auth_headers(ids["owner_id"]),
            json=_arrival_payload(),
        )
        assert resumed_begin.status_code == 200, resumed_begin.text

        with SessionLocal() as session:
            journey = session.get(Journey, ids["journey_id"])
            assert journey is not None
            final = followthrough_projection(session, journey, viewer_member_id=ids["owner_member_id"])
            assert final is not None
            assert final["state"] == "RECOVERY_CLOSED"
            assert final["confirmed_count"] == final["required_count"] == 2
            assert final["plan_unchanged_by_closure"] is True
            assert journey.active_plan_version_id == ids["plan_id"]
            assert session.scalar(
                select(AuditLog).where(
                    AuditLog.journey_id == ids["journey_id"],
                    AuditLog.action == "journey.group_recovery.rejoined",
                ).limit(2)
            ) is not None
            closures = session.scalars(
                select(AuditLog).where(
                    AuditLog.journey_id == ids["journey_id"],
                    AuditLog.action == "journey.group_recovery.rejoined",
                )
            ).all()
            assert len(closures) == 1
            progress_rows = session.scalars(
                select(AuditLog).where(
                    AuditLog.journey_id == ids["journey_id"],
                    AuditLog.action == "journey.group_recovery.member_arrival_bound",
                )
            ).all()
            assert len(progress_rows) == 2
            close_keys = session.scalars(
                select(IdempotencyKey).where(IdempotencyKey.operation == "group_recovery.close")
            ).all()
            assert len([row for row in close_keys if ids["journey_id"] in row.key or row.response_json and ids["incident_key"] in row.response_json]) == 1

        operator = client.get(
            f"/api/v1/journeys/{ids['journey_id']}/operator",
            headers=_auth_headers(ids["owner_id"]),
        )
        assert operator.status_code == 200, operator.text
        assert operator.json()["group_recovery"]["followthrough"]["state"] == "RECOVERY_CLOSED"
        assert operator.json()["group_recovery"]["foreground"] is False
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_get_eta_and_simulated_arrival_do_not_close_or_claim_group_reunion():
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    ids = _seed_applied_recovery()
    try:
        headers = _auth_headers(ids["owner_id"])
        operator = client.get(f"/api/v1/journeys/{ids['journey_id']}/operator", headers=headers)
        assert operator.status_code == 200, operator.text
        followthrough = operator.json()["group_recovery"]["followthrough"]
        assert followthrough["state"] == "RECOVERY_APPLIED_REJOIN_PENDING"
        assert followthrough["confirmed_count"] == 0
        assert followthrough["physical_presence_verified"] is False

        simulated = client.post(
            f"/api/v1/journeys/{ids['journey_id']}/activities/{ids['activity_id']}/arrive",
            headers=headers,
        )
        assert simulated.status_code == 409, simulated.text
        with SessionLocal() as session:
            journey = session.get(Journey, ids["journey_id"])
            activity = session.get(Activity, ids["activity_id"])
            assert journey is not None and activity is not None
            assert activity.status == "EN_ROUTE"
            projected = followthrough_projection(session, journey, viewer_member_id=ids["owner_member_id"])
            assert projected is not None
            assert projected["confirmed_count"] == 0
            assert projected["state"] != "RECOVERY_CLOSED"
            assert session.scalar(
                select(AuditLog).where(
                    AuditLog.journey_id == ids["journey_id"],
                    AuditLog.action == "journey.group_recovery.rejoined",
                )
            ) is None
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_role_without_execution_capability_cannot_report_arrival():
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    ids = _seed_applied_recovery()
    try:
        with SessionLocal() as session:
            session.execute(
                delete(JourneyRoleCapability).where(
                    JourneyRoleCapability.role == "ADMIN",
                    JourneyRoleCapability.capability == "JOURNEY_EXECUTION_UPDATE",
                )
            )
            session.commit()
        response = client.post(
            f"/api/v1/journeys/{ids['journey_id']}/activities/{ids['activity_id']}/arrive",
            headers=_auth_headers(ids["companion_id"]),
            json=_arrival_payload(),
        )
        assert response.status_code == 403, response.text
        with SessionLocal() as session:
            activity = session.get(Activity, ids["activity_id"])
            assert activity is not None
            assert activity.status == "EN_ROUTE"
            assert session.scalar(
                select(AuditLog).where(
                    AuditLog.journey_id == ids["journey_id"],
                    AuditLog.action == "journey.group_recovery.member_arrival_bound",
                )
            ) is None
    finally:
        with SessionLocal() as session:
            session.add(
                JourneyRoleCapability(
                    role="ADMIN",
                    capability="JOURNEY_EXECUTION_UPDATE",
                    scope_message_key="member.scope.admin",
                    created_at=_now(),
                )
            )
            session.commit()
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_durable_member_receipts_survive_shared_activity_history_loss():
    """Reunion progress must not depend on one concurrently mutable metadata blob."""
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    ids = _seed_applied_recovery()
    endpoint = (
        f"/api/v1/journeys/{ids['journey_id']}/activities/"
        f"{ids['activity_id']}/arrive"
    )
    try:
        first = client.post(
            endpoint,
            headers=_auth_headers(ids["owner_id"]),
            json=_arrival_payload(),
        )
        assert first.status_code == 200, first.text

        # Simulate the failure mode of concurrent shared-Activity metadata:
        # another writer preserved the Activity state but overwrote the JSON
        # execution history. The independent member receipt must remain enough
        # to remember the first traveller.
        with SessionLocal() as session:
            activity = session.get(Activity, ids["activity_id"])
            journey = session.get(Journey, ids["journey_id"])
            assert activity is not None and journey is not None
            activity.metadata_json = "{}"
            session.commit()

            projected = followthrough_projection(
                session,
                journey,
                viewer_member_id=ids["owner_member_id"],
            )
            assert projected is not None
            assert projected["confirmed_count"] == 1
            assert projected["viewer_has_confirmed"] is True
            assert (
                projected["arrival_progress_authority"]
                == "DURABLE_MEMBER_EXECUTION_RECEIPTS"
            )

        second = client.post(
            endpoint,
            headers=_auth_headers(ids["companion_id"]),
            json=_arrival_payload(),
        )
        assert second.status_code == 200, second.text
        with SessionLocal() as session:
            journey = session.get(Journey, ids["journey_id"])
            assert journey is not None
            final = followthrough_projection(
                session,
                journey,
                viewer_member_id=ids["owner_member_id"],
            )
            assert final is not None
            assert final["state"] == "RECOVERY_CLOSED"
            assert final["confirmed_count"] == final["required_count"] == 2
            closures = session.scalars(
                select(AuditLog).where(
                    AuditLog.journey_id == ids["journey_id"],
                    AuditLog.action == "journey.group_recovery.rejoined",
                )
            ).all()
            assert len(closures) == 1
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_one_device_verified_member_does_not_upgrade_whole_group_presence():
    ids = _seed_applied_recovery()
    now = _now()
    with SessionLocal() as session:
        accepted_json = session.scalar(
            select(AuditLog.after_json).where(
                AuditLog.journey_id == ids["journey_id"],
                AuditLog.action == "replan.accepted",
            )
        )
        assert accepted_json is not None
        recovery_epoch_id = json.loads(accepted_json)[
            "group_recovery_spend"
        ]["recovery_epoch_id"]
        activity = session.get(Activity, ids["activity_id"])
        assert activity is not None
        receipts = (
            (
                ids["owner_member_id"],
                ids["owner_id"],
                "DEVICE_LOCATION_VERIFIED_EXECUTION",
            ),
            (
                ids["companion_member_id"],
                ids["companion_id"],
                "OPERATOR_CONFIRMED_EXECUTION",
            ),
        )
        for member_id, traveler_id, kind in receipts:
            payload = {
                "journey_id": ids["journey_id"],
                "incident_key": ids["incident_key"],
                "applied_proposal_id": "fixture-proposal",
                "applied_plan_version_id": ids["plan_id"],
                "recovery_epoch_id": recovery_epoch_id,
                "rendezvous_activity_id": ids["activity_id"],
                "rendezvous_place_id": activity.place_id,
                "journey_member_id": member_id,
                "traveler_id": traveler_id,
                "role_at_confirmation": (
                    "OWNER"
                    if member_id == ids["owner_member_id"]
                    else "ADMIN"
                ),
                "confirmed_at": now,
                "execution_truth_kind": kind,
                "evidence_level": "AUTHENTICATED_ACTIVITY_EXECUTION_TRUTH",
                "physical_presence_verified": (
                    kind == "DEVICE_LOCATION_VERIFIED_EXECUTION"
                ),
            }
            session.add(
                IdempotencyKey(
                    key=f"r62-mixed-presence-{member_id}",
                    operation="group_recovery.execution_arrival",
                    request_hash=f"hash-{member_id}",
                    response_status=200,
                    response_json=json.dumps(payload, sort_keys=True),
                    created_at=now,
                    expires_at="9999-12-31T23:59:59+00:00",
                )
            )
        session.commit()

        journey = session.get(Journey, ids["journey_id"])
        assert journey is not None
        projected = followthrough_projection(
            session,
            journey,
            viewer_member_id=ids["owner_member_id"],
        )
        assert projected is not None
        assert projected["confirmed_count"] == projected["required_count"] == 2
        assert projected["physical_presence_verified_member_count"] == 1
        assert projected["physical_presence_verified"] is False
        owner = next(
            item
            for item in projected["members"]
            if item["member_id"] == ids["owner_member_id"]
        )
        companion = next(
            item
            for item in projected["members"]
            if item["member_id"] == ids["companion_member_id"]
        )
        assert owner["physical_presence_verified"] is True
        assert companion["physical_presence_verified"] is False

        closed = close_recovery_if_rejoined(session, journey)
        session.commit()
        assert closed is not None
        assert closed["state"] == "RECOVERY_CLOSED"
        closure = session.scalar(
            select(AuditLog).where(
                AuditLog.journey_id == ids["journey_id"],
                AuditLog.action == "journey.group_recovery.rejoined",
            )
        )
        assert closure is not None
        facts = json.loads(closure.after_json or "{}")
        assert facts["physical_presence_verified"] is False
        assert facts["physical_presence_verified_member_count"] == 1
