from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import app.main as main_module
from app.core.config import settings
from app.db.models import (
    AgentRun,
    AuditLog,
    Booking,
    Event,
    GuardianPulse,
    GuardianRun,
    Journey,
    JourneyOutcomeEvent,
    JourneyRendezvousBuddyHandoff,
    LocationObservation,
    PaymentTransaction,
    PlanVersion,
    ProviderCall,
    RouteAcquisitionOperation,
)
from app.db.session import SessionLocal
from app.execution.rendezvous_buddy_handoff import _is_public_place
from app.traveler_principal import issue_traveler_token
from demo_stage import demo_reset_headers

client = TestClient(main_module.app)
TEST_SECRET = "REDACTED_FOR_PUBLIC_RELEASE"


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
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select", headers=_auth(owner_id)
    ).status_code == 200
    assert client.post(
        f"/api/v1/journeys/{journey_id}/start", headers=_auth(owner_id)
    ).status_code == 200
    return journey_id, owner_id


def _join(journey_id: str, owner_id: str, traveler_id: str) -> None:
    offered = client.post(
        f"/api/v1/journeys/{journey_id}/membership-invitations",
        headers={**_auth(owner_id), "Idempotency-Key": str(uuid4())},
        json={"target_traveler_id": traveler_id},
    )
    assert offered.status_code == 201, offered.text
    accepted = client.post(
        f"/api/v1/journey-membership-invitations/{offered.json()['invitation_id']}/respond",
        headers=_auth(traveler_id),
        json={"decision": "ACCEPT"},
    )
    assert accepted.status_code == 200, accepted.text


def _presence(journey_id: str, traveler_id: str) -> dict:
    response = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(traveler_id)
    )
    assert response.status_code == 200, response.text
    return response.json()["group_presence"]


def _help_request(journey_id: str, owner_id: str, requester_id: str) -> tuple[str, dict]:
    presence = _presence(journey_id, requester_id)
    audience_json = json.dumps(
        sorted(member["member_id"] for member in presence["members"]), separators=(",", ":")
    )
    assert presence["coordination_audience_sha256"] == hashlib.sha256(
        audience_json.encode("utf-8")
    ).hexdigest()
    target = presence["rendezvous"]
    request = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-member-signals",
        headers={**_auth(requester_id), "Idempotency-Key": "r79-help-" + str(uuid4())},
        json={
            "signal_kind": "NEEDS_COORDINATION",
            "expected_active_plan_version_id": presence["active_plan_version_id"],
            "expected_current_activity_id": presence["current_activity_id"],
            "expected_rendezvous_activity_id": target["activity_id"],
            "expected_public_place_id": target["place_id"],
            "expected_audience_sha256": presence["coordination_audience_sha256"],
        },
    )
    assert request.status_code == 201, request.text
    signal_id = str(request.json()["signal_id"])
    pre_ack = client.get(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/candidates?signal_id={signal_id}",
        headers=_auth(owner_id),
    )
    assert pre_ack.status_code == 409
    assert "ACKNOWLEDGEMENT_REQUIRED" in pre_ack.text
    ack = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-member-signals/{signal_id}/acknowledge",
        headers=_auth(owner_id),
    )
    assert ack.status_code == 200, ack.text
    return signal_id, presence


def _handoff_payload(signal_id: str, candidates: dict, selected_place_id: str) -> dict:
    return {
        "help_signal_id": signal_id,
        "expected_active_plan_version_id": candidates["active_plan_version_id"],
        "expected_current_activity_id": candidates["current_activity_id"],
        "expected_rendezvous_activity_id": candidates["rendezvous_activity_id"],
        "expected_original_public_place_id": candidates["original_public_place_id"],
        "expected_audience_sha256": candidates["audience_sha256"],
        "handoff_place_id": selected_place_id,
    }


def _counts() -> dict[str, int]:
    models = (
        PlanVersion,
        LocationObservation,
        RouteAcquisitionOperation,
        ProviderCall,
        AgentRun,
        GuardianRun,
        GuardianPulse,
        Event,
        JourneyOutcomeEvent,
        Booking,
        PaymentTransaction,
    )
    with SessionLocal() as session:
        return {
            model.__tablename__: int(session.scalar(select(func.count()).select_from(model)) or 0)
            for model in models
        }


def test_r79_signed_pair_accepts_private_public_handoff_without_changing_journey():
    journey_id, helper_id = _active_journey()
    bystander_id = _traveler("R79 bystander")
    _join(journey_id, helper_id, bystander_id)
    requester_id = _traveler("R79 help requester")
    _join(journey_id, helper_id, requester_id)
    signal_id, before = _help_request(journey_id, helper_id, requester_id)
    counts_before = _counts()

    candidates_response = client.get(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/candidates?signal_id={signal_id}",
        headers=_auth(helper_id),
    )
    assert candidates_response.status_code == 200, candidates_response.text
    candidates = candidates_response.json()
    assert candidates["help_signal_id"] == signal_id
    assert 1 <= len(candidates["items"]) <= 3
    assert candidates["items"][0]["is_original_shared_stop"] is True
    assert all(item["distance_basis"] == "STRAIGHT_LINE_NOT_ETA" for item in candidates["items"])
    assert all(item["distance_from_original_m"] <= 1500 for item in candidates["items"])
    assert all(item["route_feasibility_state"] == "UNKNOWN" for item in candidates["items"])
    assert all(item["accessibility_state"] == "UNKNOWN" for item in candidates["items"])
    assert all(item["opening_hours_state"] == "UNKNOWN" for item in candidates["items"])
    assert all(item["evidence_grade"] == "DEMO_FIXTURE_NOT_LIVE_VERIFIED" for item in candidates["items"])
    selected = candidates["items"][-1]
    assert "not live-verified" in selected["source_label"].lower()

    unauthorized_candidates = client.get(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/candidates?signal_id={signal_id}",
        headers=_auth(bystander_id),
    )
    assert unauthorized_candidates.status_code == 403
    assert "MUST_BE_ACKNOWLEDGER" in unauthorized_candidates.text
    payload = _handoff_payload(signal_id, candidates, selected["place_id"])
    forged_offer = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs",
        headers={**_auth(bystander_id), "Idempotency-Key": "r79-forged-" + str(uuid4())},
        json=payload,
    )
    assert forged_offer.status_code == 403
    assert "MUST_BE_ACKNOWLEDGER" in forged_offer.text

    handoff_path = f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs"
    idempotency_key = "r79-offer-" + str(uuid4())
    offer_payload = {**_auth(helper_id), "Idempotency-Key": idempotency_key}
    first = client.post(handoff_path, headers=offer_payload, json=payload)
    replay = client.post(handoff_path, headers=offer_payload, json=payload)
    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    offered = first.json()
    handoff_id = str(offered["handoff_id"])
    assert replay.json()["handoff_id"] == handoff_id
    assert offered["status"] == "OFFERED"
    assert offered["viewer_role"] == "HELPER"
    assert offered["handoff_place"] == selected
    assert offered["shared_plan_changed"] is False
    assert offered["route_eta_state"] == "UNKNOWN"
    assert offered["evidence_level"] == "ONE_SIDED_PUBLIC_HANDOFF_OFFER_NOT_ACCEPTED"

    requester_current = client.get(f"{handoff_path}/current", headers=_auth(requester_id))
    assert requester_current.status_code == 200, requester_current.text
    assert len(requester_current.json()["items"]) == 1
    requester_offer = requester_current.json()["items"][0]
    assert requester_offer["viewer_role"] == "REQUESTER"
    assert requester_offer["handoff_place"] == selected
    bystander_current = client.get(f"{handoff_path}/current", headers=_auth(bystander_id))
    assert bystander_current.status_code == 200
    assert bystander_current.json() == {"items": []}
    operator = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(bystander_id)
    )
    assert operator.status_code == 200
    assert handoff_id not in json.dumps(operator.json(), sort_keys=True)

    accepted = client.post(
        f"{handoff_path}/{handoff_id}/respond",
        headers=_auth(requester_id),
        json={"decision": "ACCEPT"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "ACCEPTED"
    assert accepted.json()["evidence_level"] == "MUTUAL_PUBLIC_INTENTION_NOT_PHYSICAL_PRESENCE"
    assert accepted.json()["handoff_place"] == selected
    for viewer_id in (helper_id, requester_id):
        current = client.get(f"{handoff_path}/current", headers=_auth(viewer_id))
        assert current.status_code == 200
        assert current.json()["items"][0]["handoff_place"] == selected
    assert client.get(f"{handoff_path}/current", headers=_auth(bystander_id)).json() == {"items": []}
    assert handoff_id not in json.dumps(
        client.get(f"/api/v1/journeys/{journey_id}/operator", headers=_auth(bystander_id)).json(),
        sort_keys=True,
    )

    # The requester can revoke the pair agreement; the signal/help record is a separate R78 fact.
    revoked = client.post(f"{handoff_path}/{handoff_id}/revoke", headers=_auth(requester_id))
    assert revoked.status_code == 200, revoked.text
    assert revoked.json() == {"handoff_id": handoff_id, "status": "REVOKED", "shared_plan_changed": False}
    assert client.get(f"{handoff_path}/current", headers=_auth(helper_id)).json() == {"items": []}
    assert client.get(f"{handoff_path}/current", headers=_auth(requester_id)).json() == {"items": []}
    assert "handoff_place" not in client.post(
        f"{handoff_path}/{handoff_id}/revoke", headers=_auth(requester_id)
    ).json()

    assert _counts() == counts_before
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        assert str(journey.active_plan_version_id) == before["active_plan_version_id"]
        assert str(journey.current_activity_id or "") == str(before["current_activity_id"] or "")
        row = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
        assert row is not None and row.status == "REVOKED"
        assert session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.journey_id == journey_id,
            AuditLog.action.like("journey.rendezvous_buddy_handoff.%"),
        )) == 3


def test_r79_stale_audience_expires_handoffs_and_database_index_blocks_dual_current_rows():
    journey_id, helper_id = _active_journey()
    requester_id = _traveler("R79 stale requester")
    _join(journey_id, helper_id, requester_id)
    signal_id, _before = _help_request(journey_id, helper_id, requester_id)
    handoff_path = f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs"
    candidates = client.get(
        f"{handoff_path}/candidates?signal_id={signal_id}", headers=_auth(helper_id)
    ).json()
    offer = client.post(
        handoff_path,
        headers={**_auth(helper_id), "Idempotency-Key": "r79-stale-" + str(uuid4())},
        json=_handoff_payload(signal_id, candidates, candidates["items"][0]["place_id"]),
    )
    assert offer.status_code == 201, offer.text
    handoff_id = str(offer.json()["handoff_id"])

    # The database partial unique index is the cross-worker backstop; it still rejects a
    # duplicate current row when no application-level Journey lock is involved.
    with SessionLocal() as session:
        current = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
        assert current is not None
        duplicate = JourneyRendezvousBuddyHandoff(
            id=str(uuid4()),
            journey_id=current.journey_id,
            signal_id=current.signal_id,
            acknowledgement_id=current.acknowledgement_id,
            requester_member_id=current.requester_member_id,
            requester_traveler_id=current.requester_traveler_id,
            helper_member_id=current.helper_member_id,
            helper_traveler_id=current.helper_traveler_id,
            active_plan_version_id=current.active_plan_version_id,
            current_activity_id=current.current_activity_id,
            original_rendezvous_activity_id=current.original_rendezvous_activity_id,
            original_public_place_id=current.original_public_place_id,
            handoff_place_id=current.handoff_place_id,
            handoff_place_snapshot_json=current.handoff_place_snapshot_json,
            audience_member_ids_json=current.audience_member_ids_json,
            audience_sha256=current.audience_sha256,
            status="OFFERED",
            idempotency_key="db-duplicate-" + str(uuid4()),
            request_hash=current.request_hash,
            offered_at=current.offered_at,
            expires_at=current.expires_at,
            responded_at=None,
            closed_at=None,
            created_at=current.created_at,
            updated_at=current.updated_at,
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    bystander_id = _traveler("R79 later member")
    _join(journey_id, helper_id, bystander_id)
    assert client.get(f"{handoff_path}/current", headers=_auth(requester_id)).json() == {"items": []}
    stale_accept = client.post(
        f"{handoff_path}/{handoff_id}/respond", headers=_auth(requester_id), json={"decision": "ACCEPT"}
    )
    assert stale_accept.status_code == 409, stale_accept.text
    assert "HANDOFF_STALE" in stale_accept.text
    retry_revoke = client.post(f"{handoff_path}/{handoff_id}/revoke", headers=_auth(requester_id))
    assert retry_revoke.status_code == 409, retry_revoke.text
    assert "HANDOFF_NOT_CURRENT" in retry_revoke.text
    with SessionLocal() as session:
        stale = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
        assert stale is not None and stale.status == "STALE"
        assert session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.journey_id == journey_id,
            AuditLog.action == "journey.rendezvous_buddy_handoff.stale",
            AuditLog.entity_id == handoff_id,
        )) == 1


def test_r79_expired_offer_is_hidden_then_durably_retired_on_response():
    journey_id, helper_id = _active_journey()
    requester_id = _traveler("R79 expiring requester")
    _join(journey_id, helper_id, requester_id)
    signal_id, _presence_before = _help_request(journey_id, helper_id, requester_id)
    handoff_path = f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs"
    candidates = client.get(
        f"{handoff_path}/candidates?signal_id={signal_id}", headers=_auth(helper_id)
    ).json()
    offer = client.post(
        handoff_path,
        headers={**_auth(helper_id), "Idempotency-Key": "r79-expiring-" + str(uuid4())},
        json=_handoff_payload(signal_id, candidates, candidates["items"][0]["place_id"]),
    )
    assert offer.status_code == 201, offer.text
    handoff_id = str(offer.json()["handoff_id"])
    with SessionLocal() as session:
        row = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
        assert row is not None
        row.expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        session.commit()

    current = client.get(f"{handoff_path}/current", headers=_auth(requester_id))
    assert current.status_code == 200
    assert current.json() == {"items": []}
    expired_accept = client.post(
        f"{handoff_path}/{handoff_id}/respond",
        headers=_auth(requester_id),
        json={"decision": "ACCEPT"},
    )
    assert expired_accept.status_code == 409, expired_accept.text
    assert "RENDEZVOUS_HANDOFF_EXPIRED" in expired_accept.text
    with SessionLocal() as session:
        row = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
        assert row is not None and row.status == "EXPIRED" and row.closed_at is not None
        assert session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.journey_id == journey_id,
            AuditLog.action == "journey.rendezvous_buddy_handoff.expired",
            AuditLog.entity_id == handoff_id,
        )) == 1


def test_r79_requester_can_decline_and_helper_can_cancel_without_plan_change():
    journey_id, helper_id = _active_journey()
    requester_id = _traveler("R79 decision requester")
    _join(journey_id, helper_id, requester_id)
    signal_id, before = _help_request(journey_id, helper_id, requester_id)
    handoff_path = f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs"
    candidates = client.get(
        f"{handoff_path}/candidates?signal_id={signal_id}", headers=_auth(helper_id)
    ).json()
    payload = _handoff_payload(signal_id, candidates, candidates["items"][0]["place_id"])

    offered = client.post(
        handoff_path,
        headers={**_auth(helper_id), "Idempotency-Key": "r79-decline-" + str(uuid4())},
        json=payload,
    )
    assert offered.status_code == 201, offered.text
    declined_id = str(offered.json()["handoff_id"])
    declined = client.post(
        f"{handoff_path}/{declined_id}/respond",
        headers=_auth(requester_id),
        json={"decision": "DECLINE"},
    )
    assert declined.status_code == 200, declined.text
    assert declined.json()["status"] == "DECLINED"

    offered_again = client.post(
        handoff_path,
        headers={**_auth(helper_id), "Idempotency-Key": "r79-cancel-" + str(uuid4())},
        json=payload,
    )
    assert offered_again.status_code == 201, offered_again.text
    cancelled_id = str(offered_again.json()["handoff_id"])
    # C declined offer #1, but A has since proposed #2. An old tab
    # must not answer "declined" for #1 as if that closed #2.
    stale_decline = client.post(
        f"{handoff_path}/{declined_id}/respond",
        headers=_auth(requester_id),
        json={"decision": "DECLINE"},
    )
    assert stale_decline.status_code == 409
    assert "RENDEZVOUS_HANDOFF_RETIRED_REPLAY_STALE" in stale_decline.text
    assert client.get(f"{handoff_path}/current", headers=_auth(requester_id)).json()["items"][0]["handoff_id"] == cancelled_id
    cancelled = client.post(
        f"{handoff_path}/{cancelled_id}/cancel", headers=_auth(helper_id)
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json() == {
        "handoff_id": cancelled_id,
        "status": "CANCELLED",
        "shared_plan_changed": False,
    }
    # Same-target retry is idempotent while there is no newer handoff.
    assert client.post(
        f"{handoff_path}/{cancelled_id}/cancel", headers=_auth(helper_id)
    ).status_code == 200
    third = client.post(
        handoff_path,
        headers={**_auth(helper_id), "Idempotency-Key": "r79-stale-third-" + str(uuid4())},
        json=payload,
    )
    assert third.status_code == 201, third.text
    third_id = str(third.json()["handoff_id"])
    assert third_id != cancelled_id
    stale_cancel = client.post(
        f"{handoff_path}/{cancelled_id}/cancel", headers=_auth(helper_id)
    )
    assert stale_cancel.status_code == 409
    assert "RENDEZVOUS_HANDOFF_RETIRED_REPLAY_STALE" in stale_cancel.text
    assert [item["handoff_id"] for item in client.get(
        f"{handoff_path}/current", headers=_auth(requester_id)
    ).json()["items"]] == [third_id]
    assert client.post(
        f"{handoff_path}/{third_id}/cancel", headers=_auth(helper_id)
    ).status_code == 200
    assert client.get(f"{handoff_path}/current", headers=_auth(requester_id)).json() == {"items": []}
    presence_after = _presence(journey_id, requester_id)
    assert presence_after["active_plan_version_id"] == before["active_plan_version_id"]
    assert presence_after["current_activity_id"] == before["current_activity_id"]
    with SessionLocal() as session:
        assert session.get(JourneyRendezvousBuddyHandoff, declined_id).status == "DECLINED"
        assert session.get(JourneyRendezvousBuddyHandoff, cancelled_id).status == "CANCELLED"


@pytest.mark.parametrize(
    "tags, metadata, allowed",
    [
        ({"access": "private"}, {"access": "yes"}, False),
        ({"tags": {"access": "no"}}, {"visibility": "public"}, False),
        ({"osm_tags": {"access": "staff"}}, {}, False),
        ({"visibility": "restricted"}, {"access": "yes"}, False),
        ({"access": "yes"}, {"is_public": False}, False),
        ({"access": "yes"}, {"visibility": "public"}, True),
    ],
)
def test_r79_public_handoff_filters_all_sources_without_overriding_private_tags(
    tags: dict, metadata: dict, allowed: bool,
):
    """A later catalog flag must never erase an explicit OSM entry restriction."""
    place = SimpleNamespace(
        tags_json=json.dumps(tags),
        metadata_json=json.dumps(metadata),
    )
    assert _is_public_place(place) is allowed
