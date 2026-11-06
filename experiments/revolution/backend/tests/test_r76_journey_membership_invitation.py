from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.core.config import settings
from app.db.models import (
    Activity,
    AuditLog,
    Journey,
    JourneyMission,
    JourneyMember,
    JourneyMembershipInvitation,
    MemberPlaceInvitation,
    MemberPlacePromotion,
    Place,
    SharedStopCoPlanningGrant,
)
from app.db.session import SessionLocal
from app.execution.group_presence import rendezvous_target
from app.execution.group_recovery_followthrough import followthrough_projection
from app.traveler_principal import issue_traveler_token
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)
TEST_SECRET = "r76-membership-invitation-test-secret-0123456789"


@pytest.fixture(autouse=True)
def _signed_traveler_tokens():
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    try:
        yield
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def _auth(traveler_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_traveler_token(traveler_id)}"}


def _traveler(name: str) -> str:
    response = client.post(
        "/api/v1/travelers",
        json={"display_name": name, "email": None, "locale": "en-US", "timezone": "UTC"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _active_journey() -> tuple[str, str]:
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, reset.text
    journey_id = str(reset.json()["journey_id"])
    owner_id = str(reset.json()["traveler_id"])
    plans = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert plans.status_code == 201, plans.text
    plan_id = str(plans.json()[0]["id"])
    selected = client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select", headers=_auth(owner_id)
    )
    assert selected.status_code == 200, selected.text
    started = client.post(f"/api/v1/journeys/{journey_id}/start", headers=_auth(owner_id))
    assert started.status_code == 200, started.text
    return journey_id, owner_id


def _offer(journey_id: str, owner_id: str, target_id: str, key: str | None = None):
    return client.post(
        f"/api/v1/journeys/{journey_id}/membership-invitations",
        headers={**_auth(owner_id), "Idempotency-Key": key or str(uuid4())},
        json={"target_traveler_id": target_id},
    )


def _accept_membership_invitation(journey_id: str, owner_id: str, target_id: str, key: str):
    offered = _offer(journey_id, owner_id, target_id, key)
    assert offered.status_code == 201, offered.text
    accepted = client.post(
        f"/api/v1/journey-membership-invitations/{offered.json()['invitation_id']}/respond",
        headers=_auth(target_id),
        json={"decision": "ACCEPT"},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted


def _member_place_invitation_row(journey_id: str, invitation_id: str):
    projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(str(_owner_for(journey_id)))
    )
    assert projection.status_code == 200, projection.text
    boards = projection.json()["member_place_invitations"]
    return next(
        row
        for group in ("sent", "joined")
        for row in boards[group]
        if row["invitation_id"] == invitation_id
    )


def _owner_for(journey_id: str) -> str:
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        return str(journey.traveler_id)


def test_recipient_owned_invitation_accepts_once_and_keeps_preaccept_view_bounded():
    journey_id, owner_id = _active_journey()
    target_id = _traveler("R76 invited companion")
    stranger_id = _traveler("R76 unrelated traveler")
    offered = _offer(journey_id, owner_id, target_id, "r76-accept-once")
    assert offered.status_code == 201, offered.text
    invitation_id = str(offered.json()["invitation_id"])
    assert offered.json()["proposed_role"] == "COLLABORATOR"
    replayed_offer = _offer(journey_id, owner_id, target_id, "r76-accept-once")
    assert replayed_offer.status_code == 201
    assert replayed_offer.json()["invitation_id"] == invitation_id
    mismatched_key = _offer(journey_id, owner_id, stranger_id, "r76-accept-once")
    assert mismatched_key.status_code == 409

    inbox = client.get(
        "/api/v1/travelers/me/journey-membership-invitations", headers=_auth(target_id)
    )
    assert inbox.status_code == 200, inbox.text
    assert len(inbox.json()) == 1
    preview = inbox.json()[0]
    assert preview["inviter_display_name"]
    assert preview["journey_title"]
    assert preview["city_code"]
    assert preview["proposed_role"] == "COLLABORATOR"
    assert not {
        "journey_id", "active_plan_version_id", "member_ids", "member_roster", "route", "location"
    } & set(preview)

    target_operator_before = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(target_id)
    )
    assert target_operator_before.status_code == 403
    stranger_inbox = client.get(
        "/api/v1/travelers/me/journey-membership-invitations", headers=_auth(stranger_id)
    )
    assert stranger_inbox.status_code == 200 and stranger_inbox.json() == []
    forged_response = client.post(
        f"/api/v1/journey-membership-invitations/{invitation_id}/respond",
        headers=_auth(stranger_id),
        json={"decision": "ACCEPT"},
    )
    assert forged_response.status_code == 404

    bypass = client.post(
        f"/api/v1/journeys/{journey_id}/members",
        headers=_auth(owner_id),
        json={"traveler_id": target_id, "role": "COLLABORATOR", "invited_by": owner_id},
    )
    assert bypass.status_code == 409 and "RECIPIENT_ACCEPTANCE_REQUIRED" in bypass.text
    duplicate_pending = _offer(journey_id, owner_id, target_id, "r76-duplicate-pending")
    assert duplicate_pending.status_code == 409
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == target_id,
            )
        ) == 0

    accepted = client.post(
        f"/api/v1/journey-membership-invitations/{invitation_id}/respond",
        headers=_auth(target_id),
        json={"decision": "ACCEPT"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == "ACCEPTED"
    assert accepted.json()["membership"]["role"] == "COLLABORATOR"
    assert accepted.json()["membership"]["joined_at"]
    duplicate_accept = client.post(
        f"/api/v1/journey-membership-invitations/{invitation_id}/respond",
        headers=_auth(target_id),
        json={"decision": "ACCEPT"},
    )
    assert duplicate_accept.status_code == 409
    target_operator_after = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(target_id)
    )
    assert target_operator_after.status_code == 200, target_operator_after.text
    # A newcomer can use the current shared public Place as a map pin;
    # no member's private device observation is exposed as that destination.
    rendezvous = target_operator_after.json()["group_presence"]["rendezvous"]
    if rendezvous is not None:
        assert rendezvous["basis"] == "ACTIVE_PLAN_EXACT_PLACE"
        with SessionLocal() as pin_session:
            pinned_place = pin_session.get(Place, rendezvous["place_id"])
            assert pinned_place is not None
            assert rendezvous["place_name"] == pinned_place.name
            if (
                str(pinned_place.coordinate_system).upper() == "WGS84"
                and -90 <= float(pinned_place.latitude) <= 90
                and -180 <= float(pinned_place.longitude) <= 180
            ):
                assert rendezvous["coordinate_system"] == "WGS84"
                assert rendezvous["latitude"] == float(pinned_place.latitude)
                assert rendezvous["longitude"] == float(pinned_place.longitude)
            else:
                assert rendezvous["latitude"] is None
                assert rendezvous["longitude"] is None
                assert rendezvous["coordinate_system"] is None
    with SessionLocal() as session:
        invitation = session.get(JourneyMembershipInvitation, invitation_id)
        members = session.scalars(
            select(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == target_id,
            )
        ).all()
        assert invitation is not None and invitation.state == "ACCEPTED"
        assert len(members) == 1 and invitation.joined_member_id == members[0].id
        assert members[0].invited_by == owner_id


def test_decline_revoke_expiry_and_changed_journey_base_never_create_membership():
    journey_id, owner_id = _active_journey()
    declined_id = _traveler("R76 decline companion")
    declined_offer = _offer(journey_id, owner_id, declined_id, "r76-decline")
    assert declined_offer.status_code == 201, declined_offer.text
    declined_invitation_id = declined_offer.json()["invitation_id"]
    declined = client.post(
        f"/api/v1/journey-membership-invitations/{declined_invitation_id}/respond",
        headers=_auth(declined_id),
        json={"decision": "DECLINE"},
    )
    assert declined.status_code == 200 and declined.json()["state"] == "DECLINED"

    revoked_id = _traveler("R76 revoked companion")
    revoked_offer = _offer(journey_id, owner_id, revoked_id, "r76-revoke")
    assert revoked_offer.status_code == 201, revoked_offer.text
    revoked_invitation_id = revoked_offer.json()["invitation_id"]
    revoked = client.post(
        f"/api/v1/journeys/{journey_id}/membership-invitations/{revoked_invitation_id}/revoke",
        headers=_auth(owner_id),
    )
    assert revoked.status_code == 200 and revoked.json()["state"] == "REVOKED"
    revoked_accept = client.post(
        f"/api/v1/journey-membership-invitations/{revoked_invitation_id}/respond",
        headers=_auth(revoked_id),
        json={"decision": "ACCEPT"},
    )
    assert revoked_accept.status_code == 409

    stale_id = _traveler("R76 stale-base companion")
    stale_offer = _offer(journey_id, owner_id, stale_id, "r76-stale-cursor")
    assert stale_offer.status_code == 201, stale_offer.text
    stale_invitation_id = stale_offer.json()["invitation_id"]
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        next_activity_id = session.scalar(
            select(Activity.id)
            .where(Activity.plan_version_id == str(journey.active_plan_version_id))
            .order_by(Activity.sequence_no)
            .limit(1)
        )
        assert next_activity_id is not None
        journey.current_activity_id = (
            None if journey.current_activity_id else str(next_activity_id)
        )
        session.commit()
    stale_accept = client.post(
        f"/api/v1/journey-membership-invitations/{stale_invitation_id}/respond",
        headers=_auth(stale_id),
        json={"decision": "ACCEPT"},
    )
    assert stale_accept.status_code == 409 and "JOURNEY_BASE_CHANGED" in stale_accept.text
    with SessionLocal() as session:
        stale_invitation = session.get(JourneyMembershipInvitation, stale_invitation_id)
        assert stale_invitation is not None and stale_invitation.state == "EXPIRED"
        for traveler_id in (declined_id, revoked_id, stale_id):
            assert session.scalar(
                select(func.count()).select_from(JourneyMember).where(
                    JourneyMember.journey_id == journey_id,
                    JourneyMember.traveler_id == traveler_id,
                )
            ) == 0


def test_expired_offer_and_removed_inviter_cannot_add_a_member():
    journey_id, owner_id = _active_journey()
    expired_target_id = _traveler("R76 expired companion")
    expired_offer = _offer(journey_id, owner_id, expired_target_id, "r76-expired")
    assert expired_offer.status_code == 201, expired_offer.text
    expired_invitation_id = str(expired_offer.json()["invitation_id"])
    with SessionLocal() as session:
        invitation = session.get(JourneyMembershipInvitation, expired_invitation_id)
        assert invitation is not None
        invitation.expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        session.commit()
    expired_inbox = client.get(
        "/api/v1/travelers/me/journey-membership-invitations", headers=_auth(expired_target_id)
    )
    assert expired_inbox.status_code == 200 and expired_inbox.json() == []
    with SessionLocal() as session:
        invitation = session.get(JourneyMembershipInvitation, expired_invitation_id)
        assert invitation is not None and invitation.state == "EXPIRED"

    removed_target_id = _traveler("R76 inviter-removed companion")
    removed_offer = _offer(journey_id, owner_id, removed_target_id, "r76-removed-inviter")
    assert removed_offer.status_code == 201, removed_offer.text
    with SessionLocal() as session:
        owner_member = session.scalar(
            select(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == owner_id,
            )
        )
        assert owner_member is not None
        owner_member.role = "COLLABORATOR"
        owner_member.removed_at = datetime.now(timezone.utc).isoformat()
        session.commit()
    rejected = client.post(
        f"/api/v1/journey-membership-invitations/{removed_offer.json()['invitation_id']}/respond",
        headers=_auth(removed_target_id),
        json={"decision": "ACCEPT"},
    )
    assert rejected.status_code == 409 and "INVITER_NO_LONGER_AUTHORIZED" in rejected.text
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == expired_target_id,
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == removed_target_id,
            )
        ) == 0


def test_parallel_accept_creates_one_membership_and_one_accept_audit():
    journey_id, owner_id = _active_journey()
    target_id = _traveler("R76 parallel accepter")
    offered = _offer(journey_id, owner_id, target_id, "r76-parallel-accept")
    assert offered.status_code == 201, offered.text
    invitation_id = str(offered.json()["invitation_id"])
    barrier = Barrier(2)

    def accept():
        barrier.wait(timeout=5)
        return client.post(
            f"/api/v1/journey-membership-invitations/{invitation_id}/respond",
            headers=_auth(target_id),
            json={"decision": "ACCEPT"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: accept(), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == target_id,
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.journey_id == journey_id,
                AuditLog.action == "journey.membership_invitation.accepted",
                AuditLog.entity_id == invitation_id,
            )
        ) == 1


def test_new_member_invalidates_existing_group_place_share_until_source_reconsents():
    journey_id, owner_id = _active_journey()
    target_id = _traveler("R76 existing source explorer")
    _accept_membership_invitation(journey_id, owner_id, target_id, "r76-source-member")

    operator_started = client.post(
        f"/api/v1/journeys/{journey_id}/operator/start",
        headers=_auth(owner_id),
        json={"interval_seconds": 3600},
    )
    assert operator_started.status_code == 200, operator_started.text
    split = client.post(
        f"/api/v1/journeys/{journey_id}/agent/commands",
        headers=_auth(target_id),
        json={
            "command": "I want to go alone for a while and meet at the next shared stop.",
            "requested_autonomy": "PLAN_ONLY",
        },
    )
    assert split.status_code == 200, split.text
    assert split.json()["planned_separation_result"]["outcome"] == "PLANNED_SPLIT_CREATED"

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        planned_place_ids = set(session.scalars(
            select(Activity.place_id).where(
                Activity.plan_version_id == str(journey.active_plan_version_id),
                Activity.place_id.is_not(None),
            )
        ).all())
        place = session.scalars(
            select(Place).where(Place.city_code == journey.city_code).order_by(Place.name, Place.id)
        ).first()
        if place is None or str(place.id) in planned_place_ids:
            place = session.scalars(
                select(Place)
                .where(Place.city_code == journey.city_code, Place.id.not_in(planned_place_ids))
                .order_by(Place.name, Place.id)
            ).first()
        assert place is not None, "the active demo city needs an unplanned public Place"
        place_id = str(place.id)
        place_name = str(place.name)

    side_quest = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests",
        headers=_auth(target_id),
        json={"place_id": place_id},
    )
    assert side_quest.status_code == 201, side_quest.text
    side_quest_id = str(side_quest.json()["member_side_quest"]["id"])
    preview = client.get(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{side_quest_id}/place-invitation-preview",
        headers=_auth(target_id),
    )
    assert preview.status_code == 200, preview.text
    sent = client.post(
        f"/api/v1/journeys/{journey_id}/member-side-quests/{side_quest_id}/place-invitations",
        headers=_auth(target_id),
        json={
            "confirm_public_place_sharing": True,
            "audience_snapshot": preview.json()["audience_snapshot"],
        },
    )
    assert sent.status_code == 201, sent.text
    invitation_id = str(sent.json()["member_place_invitations"]["sent"][0]["invitation_id"])
    invitation_url = f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}"
    accepted_place = client.post(
        invitation_url + "/respond",
        headers=_auth(owner_id),
        json={
            "decision": "ACCEPT",
            "confirm_exact_public_place": True,
            "acknowledge_feasibility_unknown": True,
        },
    )
    assert accepted_place.status_code == 200, accepted_place.text
    group_preview = client.get(invitation_url + "/group-share/preview", headers=_auth(target_id))
    assert group_preview.status_code == 200, group_preview.text
    confirmed = client.post(
        invitation_url + "/group-share/confirm",
        headers=_auth(target_id),
        json={
            "confirm_full_journey_place_sharing": True,
            "audience_snapshot": group_preview.json()["audience_snapshot"],
            "plan_snapshot": group_preview.json()["plan_snapshot"],
            "source_snapshot": group_preview.json()["source_snapshot"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    before_join = _member_place_invitation_row(journey_id, invitation_id)
    assert before_join["group_share_status"] in {
        "SHARED_TO_GROUP",
        "SHARED_BY_ORIGINAL_INVITATION",
    }

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        source = session.get(MemberPlaceInvitation, invitation_id)
        assert journey is not None and source is not None
        base_plan_id = str(journey.active_plan_version_id)
        active_members = session.scalars(
            select(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.removed_at.is_(None),
            )
        ).all()
        member_by_traveler = {str(item.traveler_id): str(item.id) for item in active_members}
        assert set(member_by_traveler) == {owner_id, target_id}
        owner_member_id = member_by_traveler[owner_id]
        source_member_id = member_by_traveler[target_id]
        old_required_member_ids = sorted(member_by_traveler.values())
        current_activity_id = str(journey.current_activity_id)
        rendezvous_activity_id = str(source.rendezvous_activity_id)

    grant_offer = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/co-planning-grants",
        headers={**_auth(owner_id), "Idempotency-Key": "REDACTED_FOR_PUBLIC_RELEASE"},
        json={"grantee_member_id": source_member_id},
    )
    assert grant_offer.status_code == 201, grant_offer.text
    with SessionLocal() as session:
        old_grant = session.scalar(
            select(SharedStopCoPlanningGrant).where(
                SharedStopCoPlanningGrant.journey_id == journey_id,
                SharedStopCoPlanningGrant.invitation_id == invitation_id,
                SharedStopCoPlanningGrant.grantee_member_id == source_member_id,
            )
        )
        assert old_grant is not None and old_grant.status == "OFFERED"
        old_grant_id = str(old_grant.id)
    grant_accept = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-invitations/{invitation_id}/co-planning-grants/{old_grant_id}/respond",
        headers=_auth(target_id),
        json={"decision": "ACCEPT"},
    )
    assert grant_accept.status_code == 200, grant_accept.text

    # Preserve a real, durable R73 decision envelope with A+B approvals so the
    # membership accept below proves that old votes and R74 authority are
    # invalidated together, before either can be replayed or applied.
    stale_promotion_id = str(uuid4())
    stale_mission_id = str(uuid4())
    stale_proposal_id = str(uuid4())
    stale_clarification_id = f"r76-old-member-set-{uuid4()}"
    old_responses = {
        owner_member_id: {"decision": "APPROVE", "proposal_id": stale_proposal_id},
        source_member_id: {"decision": "APPROVE", "proposal_id": stale_proposal_id},
    }
    stamp = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(JourneyMission(
            id=stale_mission_id,
            journey_id=journey_id,
            traveler_id=owner_id,
            user_text="R76 old-member-set collective decision fixture",
            status="WAITING_FOR_MEMBERS",
            created_at=stamp,
            clarification_json=json.dumps({
                "kind": "COLLECTIVE_TRADEOFF",
                "status": "WAITING_FOR_MEMBERS",
                "clarification_id": stale_clarification_id,
                "base_plan_version_id": base_plan_id,
                "required_member_ids": old_required_member_ids,
                "decision_relevant_member_ids": old_required_member_ids,
                "candidate_options": [{"proposal_id": stale_proposal_id}],
                "responses": old_responses,
            }, sort_keys=True),
            resolved_intent_json=json.dumps({
                "r73_shared_stop": {
                    "promotion_id": stale_promotion_id,
                    "invitation_id": invitation_id,
                    "proposal_id": stale_proposal_id,
                    "active_plan_version_id": base_plan_id,
                }
            }, sort_keys=True),
        ))
        session.flush()
        session.add(MemberPlacePromotion(
            id=stale_promotion_id,
            journey_id=journey_id,
            invitation_id=invitation_id,
            actor_member_id=owner_member_id,
            actor_traveler_id=owner_id,
            active_plan_version_id=base_plan_id,
            place_id=place_id,
            rendezvous_activity_id=rendezvous_activity_id,
            current_activity_id=current_activity_id,
            proposal_id=None,
            mission_id=stale_mission_id,
            request_key="r76-prejoin-promotion",
            request_hash="r76-prejoin-promotion-hash",
            dwell_minutes=20,
            state="WAITING_FOR_MEMBERS",
            state_detail_json="{}",
            decision_epoch="r76-old-member-set-epoch",
            created_at=stamp,
            updated_at=stamp,
        ))
        session.commit()

    newcomer_id = _traveler("R76 newly accepted companion")
    _accept_membership_invitation(journey_id, owner_id, newcomer_id, "r76-newcomer")

    with SessionLocal() as session:
        stale_round = session.get(JourneyMission, stale_mission_id)
        stale_promotion = session.get(MemberPlacePromotion, stale_promotion_id)
        invalidated_grant = session.get(SharedStopCoPlanningGrant, old_grant_id)
        journey = session.get(Journey, journey_id)
        assert stale_round is not None and stale_promotion is not None
        assert invalidated_grant is not None and journey is not None
        assert json.loads(stale_round.clarification_json)["status"] == "STALE"
        assert stale_promotion.state == "STALE"
        assert json.loads(stale_promotion.state_detail_json)["code"] == "STALE_COLLECTIVE_MEMBERSHIP"
        assert len(json.loads(stale_round.clarification_json)["responses"]) == 2
        assert invalidated_grant.status == "REVOKED"
        assert invalidated_grant.active_slot is False
        assert str(journey.active_plan_version_id) == base_plan_id

    replayed_old_vote = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-promotions/{stale_promotion_id}/respond",
        headers=_auth(owner_id),
        json={"clarification_id": stale_clarification_id, "decision": "APPROVE"},
    )
    assert replayed_old_vote.status_code == 409, replayed_old_vote.text
    assert "not waiting for members" in replayed_old_vote.text
    collaborator_old_vote = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-promotions/{stale_promotion_id}/respond",
        headers=_auth(target_id),
        json={"clarification_id": stale_clarification_id, "decision": "APPROVE"},
    )
    assert collaborator_old_vote.status_code == 409, collaborator_old_vote.text
    stale_apply = client.post(
        f"/api/v1/journeys/{journey_id}/member-place-promotions/{stale_promotion_id}/apply",
        headers={**_auth(owner_id), "Idempotency-Key": "r76-stale-approval-apply"},
    )
    assert stale_apply.status_code == 409, stale_apply.text

    after_join = _member_place_invitation_row(journey_id, invitation_id)
    assert after_join["group_share_status"] == "AUDIENCE_CHANGED"
    newcomer_projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(newcomer_id)
    )
    assert newcomer_projection.status_code == 200, newcomer_projection.text
    assert place_id not in newcomer_projection.text
    assert place_name not in newcomer_projection.text
    assert side_quest_id not in newcomer_projection.text

    blocked_promotion = client.post(
        invitation_url + "/promotions",
        headers={**_auth(owner_id), "Idempotency-Key": "r76-stale-audience-promotion"},
        json={"dwell_minutes": 20},
    )
    assert blocked_promotion.status_code == 409
    assert "AUDIENCE_CHANGED" in blocked_promotion.text
    refreshed_preview = client.get(invitation_url + "/group-share/preview", headers=_auth(target_id))
    assert refreshed_preview.status_code == 200, refreshed_preview.text
    assert newcomer_id in {person["display_name"] for person in refreshed_preview.json()["audience"]} or any(
        person["display_name"] == "R76 newly accepted companion"
        for person in refreshed_preview.json()["audience"]
    )


def test_r77_route_result_is_bound_to_fresh_member_location_and_current_public_stop(monkeypatch):
    journey_id, owner_id = _active_journey()
    peer_id = _traveler("R77 route companion")
    _accept_membership_invitation(journey_id, owner_id, peer_id, "r77-route-peer")
    projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(peer_id)
    )
    assert projection.status_code == 200, projection.text
    presence = projection.json()["group_presence"]
    target = presence["rendezvous"]
    assert presence["can_guide"] is True
    assert presence["active_plan_version_id"]
    assert target["coordinate_system"] == "WGS84"

    origin = {"latitude": 12.345678, "longitude": 23.456789}
    observed = client.post(
        f"/api/v1/journeys/{journey_id}/location",
        headers=_auth(peer_id),
        json={**origin, "accuracy_m": 6, "captured_at": datetime.now(timezone.utc).isoformat()},
    )
    assert observed.status_code == 201, observed.text
    observation_id = observed.json()["id"]
    captured = {}

    def fake_submit(_session, _journey, route_request):
        captured["request"] = route_request
        return SimpleNamespace(id="r77-captured-route-operation"), False

    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(main_module, "submit_route_operation", fake_submit)
    monkeypatch.setattr(main_module, "route_operation_payload", lambda *_args, **_kwargs: {
        "status": "COMPLETED",
        "outcome": "ROUTE_READY",
        "finished_at": now,
        "result": {
            "duration_min": 17,
            "distance_m": 1400,
            "provider": "r77-frozen-route-capture",
            "provenance": "CAPTURED_REPLAY",
        },
    })
    monkeypatch.setattr(
        main_module, "_persist_trusted_member_rendezvous_risk_event", lambda *_args, **_kwargs: None
    )

    routed = client.post(
        f"/api/v1/journeys/{journey_id}/group-presence/route",
        headers=_auth(peer_id),
        json={"location_observation_id": observation_id, "requested_mode": "WALK"},
    )
    assert routed.status_code == 200, routed.text
    body = routed.json()
    route_request = captured["request"]
    assert route_request.origin_key == f"location-observation:{observation_id}"
    assert route_request.origin_evidence["traveler_id"] == peer_id
    assert route_request.origin_evidence["journey_member_id"]
    assert route_request.target_activity_id == target["activity_id"]
    assert route_request.target_key == target["place_id"]
    assert body["plan_version_id"] == presence["active_plan_version_id"]
    assert body["current_activity_id"] == presence["current_activity_id"]
    assert body["target"]["place_id"] == target["place_id"]
    assert body["target"]["latitude"] == target["latitude"]
    assert body["target"]["longitude"] == target["longitude"]
    assert body["route"]["provenance"] == "CAPTURED_REPLAY"
    assert body["route"]["captured_at"] == now
    assert body["plan_unchanged"] is True
    assert body["target_unchanged"] is True
    assert body["guardian_triggered"] is False
    assert body["plan_unchanged"] is True
    assert observation_id not in routed.text
    assert str(origin["latitude"]) not in routed.text
    assert str(origin["longitude"]) not in routed.text

    owner_view = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(owner_id)
    )
    assert owner_view.status_code == 200, owner_view.text
    projected = json.dumps(owner_view.json()["group_presence"], sort_keys=True)
    assert observation_id not in projected
    assert str(origin["latitude"]) not in projected
    assert str(origin["longitude"]) not in projected


def test_r77_route_fails_closed_when_public_destination_crs_is_unknown(monkeypatch):
    journey_id, owner_id = _active_journey()
    peer_id = _traveler("R77 unknown CRS companion")
    _accept_membership_invitation(journey_id, owner_id, peer_id, "r77-unknown-crs-peer")
    projection = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(peer_id)
    )
    assert projection.status_code == 200, projection.text
    target_id = projection.json()["group_presence"]["rendezvous"]["place_id"]
    with SessionLocal() as session:
        place = session.get(Place, target_id)
        assert place is not None
        place.coordinate_system = "EPSG:3857"
        session.commit()

    called = False

    def should_not_route(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("unknown-CRS destination must not reach route acquisition")

    monkeypatch.setattr(main_module, "submit_route_operation", should_not_route)
    response = client.post(
        f"/api/v1/journeys/{journey_id}/group-presence/route",
        headers=_auth(peer_id),
        json={"location_observation_id": str(uuid4()), "requested_mode": "WALK"},
    )
    assert response.status_code == 409, response.text
    assert "GROUP_RENDEZVOUS_TARGET_COORDINATES_UNAVAILABLE" in response.text
    assert called is False


def test_r77_join_during_active_recovery_keeps_original_required_members_and_requests_reconfirmation():
    journey_id, owner_id = _active_journey()
    original_peer_id = _traveler("R77 recovery original peer")
    _accept_membership_invitation(journey_id, owner_id, original_peer_id, "r77-recovery-peer")

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None and journey.active_plan_version_id
        original_members = session.scalars(
            select(JourneyMember).where(JourneyMember.journey_id == journey_id)
        ).all()
        target_pair = rendezvous_target(session, journey)
        assert target_pair is not None
        activity, place = target_pair
        plan_id = str(journey.active_plan_version_id)
        stamp = datetime.now(timezone.utc).isoformat()
        snapshot = {
            "state": "RECOVERY_APPLIED_REJOIN_PENDING",
            "incident_key": "r77-recovery-in-flight",
            "applied_proposal_id": "r77-recovery-proposal",
            "applied_plan_version_id": plan_id,
            "recovery_epoch_id": "r77-recovery-epoch",
            "authority_receipt_id": "r77-recovery-authority",
            "trigger_event_id": None,
            "required_member_ids": sorted(str(member.id) for member in original_members),
            "member_roles_at_apply": {str(member.id): str(member.role) for member in original_members},
            "rendezvous_activity_id": str(activity.id),
            "rendezvous_place_id": str(place.id),
            "rendezvous_title": str(activity.title),
            "applied_at": stamp,
            "evidence_level": "AUTHENTICATED_ACTIVITY_EXECUTION_TRUTH",
            "physical_presence_verified": False,
        }
        session.add(AuditLog(
            id=str(uuid4()),
            actor_type="SYSTEM",
            actor_id="r77-followthrough-fixture",
            journey_id=journey_id,
            action="replan.accepted",
            entity_type="ReplanProposal",
            entity_id="r77-recovery-proposal",
            before_json=None,
            after_json=json.dumps({"group_recovery_spend": {
                "kind": "COLLECTIVE_GROUP_RECOVERY_APPLY",
                "result": "APPLIED",
                "after_plan_version_id": plan_id,
                "followthrough": snapshot,
            }}, sort_keys=True),
            trace_id=str(uuid4()),
            created_at=stamp,
        ))
        session.commit()
        before_projection = followthrough_projection(
            session, journey, viewer_member_id=str(original_members[0].id)
        )
        assert before_projection is not None
        assert before_projection["state"] == "RECOVERY_APPLIED_REJOIN_PENDING"

    newcomer_id = _traveler("R77 newcomer during recovery")
    _accept_membership_invitation(journey_id, owner_id, newcomer_id, "r77-join-during-recovery")

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        newcomer = session.scalar(
            select(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == newcomer_id,
            )
        )
        assert newcomer is not None
        after = followthrough_projection(
            session, journey, viewer_member_id=str(newcomer.id)
        )
        assert after is not None
        assert after["state"] == "REJOIN_CONFIRMATION_REQUIRED"
        assert after["membership_reconfirmation_required"] is True
        assert after["required_count"] == len(original_members)
        assert after["viewer"]["status"] == "NOT_IN_REQUIRED_SET"
        assert after["viewer_can_confirm"] is False
        assert str(newcomer.id) not in {item["member_id"] for item in after["members"]}
        assert after["new_member_ids"] == [str(newcomer.id)]
        assert str(journey.active_plan_version_id) == plan_id
        assert after["state"] != "RECOVERY_CLOSED"
