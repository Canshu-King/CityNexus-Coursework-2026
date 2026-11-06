from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.core.config import settings
from app.db.models import ActionApproval, Journey, JourneyMember, JourneyMission, Traveler
from app.db.session import SessionLocal
from app.traveler_principal import issue_traveler_token


client = TestClient(main_module.app)
_TEST_SECRET = "r28-authenticated-member-test-secret-0123456789abcdef"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_identity_fixture() -> dict[str, str]:
    owner_id = str(uuid4())
    admin_id = str(uuid4())
    outsider_id = str(uuid4())
    journey_id = str(uuid4())
    owner_member_id = str(uuid4())
    admin_member_id = str(uuid4())
    mission_id = str(uuid4())
    approval_id = str(uuid4())
    clarification_id = str(uuid4())
    now = _now()
    with SessionLocal() as session:
        for traveler_id, name in (
            (owner_id, "R28 owner"),
            (admin_id, "R28 admin"),
            (outsider_id, "R28 outsider"),
        ):
            session.add(
                Traveler(
                    id=traveler_id,
                    display_name=name,
                    email=None,
                    locale="en-US",
                    timezone="UTC",
                    created_at=now,
                    updated_at=now,
                )
            )
        session.flush()
        session.add(
            Journey(
                id=journey_id,
                traveler_id=owner_id,
                title="R28 authenticated collective",
                city_code="TYO",
                timezone="UTC",
                start_at=now,
                end_at=(datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
                budget_currency="JPY",
                budget_amount_minor=10000,
                planning_mode="balanced",
                status="ACTIVE",
                active_plan_version_id=None,
                current_activity_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add_all(
            [
                JourneyMember(
                    id=owner_member_id,
                    journey_id=journey_id,
                    traveler_id=owner_id,
                    role="OWNER",
                    invited_by=owner_id,
                    expires_at=None,
                    joined_at=now,
                    removed_at=None,
                    preferences_json="{}",
                    created_at=now,
                    updated_at=now,
                ),
                JourneyMember(
                    id=admin_member_id,
                    journey_id=journey_id,
                    traveler_id=admin_id,
                    role="ADMIN",
                    invited_by=owner_id,
                    expires_at=None,
                    joined_at=now,
                    removed_at=None,
                    preferences_json="{}",
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.flush()
        session.add(
            JourneyMission(
                id=mission_id,
                journey_id=journey_id,
                traveler_id=owner_id,
                trigger_event_id=None,
                user_text="Choose the shared option.",
                requested_autonomy="EXECUTE_WITHIN_BOUNDS",
                status="WAITING_FOR_MEMBERS",
                clarification_json=json.dumps(
                    {
                        "kind": "COLLECTIVE_TRADEOFF",
                        "status": "WAITING_FOR_MEMBERS",
                        "clarification_id": clarification_id,
                        "required_member_ids": [owner_member_id, admin_member_id],
                        "candidate_options": [],
                        "responses": {},
                    },
                    sort_keys=True,
                ),
                created_at=now,
                started_at=now,
            )
        )
        session.add(
            ActionApproval(
                id=approval_id,
                traveler_id=owner_id,
                journey_id=journey_id,
                action="ACCEPT_REPLAN",
                target_type="ReplanProposal",
                target_id=str(uuid4()),
                request_fingerprint="r28-auth-principal-test",
                status="PENDING",
                required_decision="REQUIRE_CONFIRMATION",
                created_at=now,
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            )
        )
        session.commit()
    return {
        "owner_id": owner_id,
        "admin_id": admin_id,
        "outsider_id": outsider_id,
        "journey_id": journey_id,
        "owner_member_id": owner_member_id,
        "admin_member_id": admin_member_id,
        "mission_id": mission_id,
        "approval_id": approval_id,
        "clarification_id": clarification_id,
    }


def _set_secret(value: str | None) -> str | None:
    previous = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", value)
    return previous


def test_r28_collective_response_requires_authenticated_traveler() -> None:
    previous = _set_secret(_TEST_SECRET)
    try:
        seeded = _seed_identity_fixture()
        response = client.post(
            f"/api/v1/journeys/{seeded['journey_id']}/missions/{seeded['mission_id']}/"
            f"collective-clarification/{seeded['owner_member_id']}",
            json={
                "clarification_id": seeded["clarification_id"],
                "proposal_id": str(uuid4()),
            },
        )
        assert response.status_code == 401, response.text
    finally:
        _set_secret(previous)


def test_r28_owner_token_cannot_answer_admin_member_path() -> None:
    previous = _set_secret(_TEST_SECRET)
    try:
        seeded = _seed_identity_fixture()
        owner_token = issue_traveler_token(seeded["owner_id"])
        response = client.post(
            f"/api/v1/journeys/{seeded['journey_id']}/missions/{seeded['mission_id']}/"
            f"collective-clarification/{seeded['admin_member_id']}",
            headers=_auth(owner_token),
            json={
                "clarification_id": seeded["clarification_id"],
                "proposal_id": str(uuid4()),
            },
        )
        assert response.status_code == 403, response.text
    finally:
        _set_secret(previous)


def test_r28_authenticated_outsider_cannot_answer_collective_decision() -> None:
    previous = _set_secret(_TEST_SECRET)
    try:
        seeded = _seed_identity_fixture()
        outsider_token = issue_traveler_token(seeded["outsider_id"])
        response = client.post(
            f"/api/v1/journeys/{seeded['journey_id']}/missions/{seeded['mission_id']}/"
            f"collective-clarification/{seeded['owner_member_id']}",
            headers=_auth(outsider_token),
            json={
                "clarification_id": seeded["clarification_id"],
                "proposal_id": str(uuid4()),
            },
        )
        assert response.status_code == 403, response.text
    finally:
        _set_secret(previous)


def test_r28_withdrawn_member_token_is_rejected() -> None:
    previous = _set_secret(_TEST_SECRET)
    try:
        seeded = _seed_identity_fixture()
        with SessionLocal() as session:
            member = session.get(JourneyMember, seeded["admin_member_id"])
            assert member is not None
            member.removed_at = _now()
            member.updated_at = member.removed_at
            session.commit()
        admin_token = issue_traveler_token(seeded["admin_id"])
        response = client.post(
            f"/api/v1/journeys/{seeded['journey_id']}/missions/{seeded['mission_id']}/"
            f"collective-clarification/{seeded['admin_member_id']}",
            headers=_auth(admin_token),
            json={
                "clarification_id": seeded["clarification_id"],
                "proposal_id": str(uuid4()),
            },
        )
        assert response.status_code == 403, response.text
    finally:
        _set_secret(previous)


def test_r28_approval_requires_owner_principal_and_journey_update_capability() -> None:
    previous = _set_secret(_TEST_SECRET)
    try:
        seeded = _seed_identity_fixture()
        route = (
            f"/api/v1/journeys/{seeded['journey_id']}/agent/approvals/"
            f"{seeded['approval_id']}/approve"
        )

        unauthenticated = client.post(
            route, json={"approval_method": "USER_CONFIRMATION"}
        )
        assert unauthenticated.status_code == 401, unauthenticated.text

        admin_token = issue_traveler_token(seeded["admin_id"])
        admin_attempt = client.post(
            route,
            headers=_auth(admin_token),
            json={"approval_method": "USER_CONFIRMATION"},
        )
        assert admin_attempt.status_code == 403, admin_attempt.text

        owner_token = issue_traveler_token(seeded["owner_id"])
        owner_approval = client.post(
            route,
            headers=_auth(owner_token),
            json={"approval_method": "USER_CONFIRMATION"},
        )
        assert owner_approval.status_code == 200, owner_approval.text
        assert owner_approval.json()["status"] == "APPROVED"
        assert owner_approval.json()["approved_by"] == seeded["owner_id"]
    finally:
        _set_secret(previous)


@pytest.mark.parametrize("member_key", ["owner_member_id", "admin_member_id"])
def test_r28_expired_members_cannot_answer_or_approve(member_key) -> None:
    previous = _set_secret(_TEST_SECRET)
    try:
        seeded = _seed_identity_fixture()
        traveler_key = "owner_id" if member_key == "owner_member_id" else "admin_id"
        token = issue_traveler_token(seeded[traveler_key])
        with SessionLocal() as session:
            member = session.get(JourneyMember, seeded[member_key])
            member.expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            session.commit()
        answer = client.post(
            f"/api/v1/journeys/{seeded['journey_id']}/missions/{seeded['mission_id']}/"
            f"collective-clarification/{seeded[member_key]}",
            headers=_auth(token),
            json={"clarification_id": seeded["clarification_id"], "proposal_id": str(uuid4())},
        )
        approval = client.post(
            f"/api/v1/journeys/{seeded['journey_id']}/agent/approvals/{seeded['approval_id']}/approve",
            headers=_auth(token), json={"approval_method": "USER_CONFIRMATION"},
        )
        assert answer.status_code == 403, answer.text
        assert approval.status_code == 403, approval.text
    finally:
        _set_secret(previous)


def test_r28_admin_cannot_answer_owner_and_body_cannot_override_actor() -> None:
    previous = _set_secret(_TEST_SECRET)
    try:
        seeded = _seed_identity_fixture()
        token = issue_traveler_token(seeded["admin_id"])
        root = f"/api/v1/journeys/{seeded['journey_id']}"
        answer_body = {"clarification_id": seeded["clarification_id"], "proposal_id": str(uuid4())}
        impersonation = client.post(
            f"{root}/missions/{seeded['mission_id']}/collective-clarification/{seeded['owner_member_id']}",
            headers=_auth(token), json=answer_body,
        )
        assert impersonation.status_code == 403, impersonation.text
        forged = {"member_id": seeded["owner_member_id"], "traveler_id": seeded["owner_id"]}
        forged_answer = client.post(
            f"{root}/missions/{seeded['mission_id']}/collective-clarification/{seeded['admin_member_id']}",
            headers=_auth(token), json={**answer_body, **forged},
        )
        forged_approval = client.post(
            f"{root}/agent/approvals/{seeded['approval_id']}/approve",
            headers=_auth(token), json={"approval_method": "USER_CONFIRMATION", **forged},
        )
        assert forged_answer.status_code == 422, forged_answer.text
        assert forged_approval.status_code == 422, forged_approval.text
        with SessionLocal() as session:
            assert session.get(ActionApproval, seeded["approval_id"]).status == "PENDING"
            assert json.loads(session.get(JourneyMission, seeded["mission_id"]).clarification_json)["responses"] == {}
    finally:
        _set_secret(previous)


@pytest.mark.parametrize("kind", ["tampered", "expired"])
def test_r28_invalid_signed_credentials_rejected(kind) -> None:
    previous = _set_secret(_TEST_SECRET)
    try:
        seeded = _seed_identity_fixture()
        if kind == "expired":
            token = issue_traveler_token(seeded["owner_id"], now=1)
        else:
            token = issue_traveler_token(seeded["owner_id"])
            body, signature = token.split(".")
            token = body + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
        response = client.post(
            f"/api/v1/journeys/{seeded['journey_id']}/agent/approvals/{seeded['approval_id']}/approve",
            headers=_auth(token), json={"approval_method": "USER_CONFIRMATION"},
        )
        assert response.status_code == 401, response.text
    finally:
        _set_secret(previous)


def test_r28_pending_read_model_is_identity_bound_and_member_visible() -> None:
    previous = _set_secret(_TEST_SECRET)
    try:
        seeded = _seed_identity_fixture()
        route = f"/api/v1/journeys/{seeded['journey_id']}/missions/pending"

        unauthenticated = client.get(route)
        assert unauthenticated.status_code == 401, unauthenticated.text

        owner_token = issue_traveler_token(seeded["owner_id"])
        response = client.get(route, headers=_auth(owner_token))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["viewer"]["traveler_id"] == seeded["owner_id"]
        assert body["viewer"]["member_id"] == seeded["owner_member_id"]
        assert body["viewer"]["role"] == "OWNER"

        collective = next(
            item["collective"]
            for item in body["items"]
            if item["mission"]["id"] == seeded["mission_id"]
        )
        assert collective["required_count"] == 2
        assert collective["response_count"] == 0
        assert collective["viewer_member_id"] == seeded["owner_member_id"]
        assert collective["viewer_has_responded"] is False
        assert len(collective["required_members"]) == 2
        assert sum(1 for item in collective["required_members"] if item["is_viewer"]) == 1
        assert all(item["display_name"] for item in collective["required_members"])
    finally:
        _set_secret(previous)


def test_r28_legacy_pending_projection_does_not_leak_member_names() -> None:
    previous = _set_secret(None)
    try:
        seeded = _seed_identity_fixture()
        response = client.get(
            f"/api/v1/journeys/{seeded['journey_id']}/missions/pending"
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["viewer"] is None
        collective = next(
            item["collective"]
            for item in body["items"]
            if item["mission"]["id"] == seeded["mission_id"]
        )
        assert all(item["display_name"] is None for item in collective["required_members"])
    finally:
        _set_secret(previous)
