from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

import app.execution.private_buddy_route as private_route_module
from app.core.config import settings
from app.db.models import (
    AuditLog,
    Journey,
    JourneyMember,
    JourneyRendezvousBuddyHandoff,
    LocationObservation,
    PrivateBuddyRouteLocationBinding,
    RouteAcquisitionOperation,
)
from app.db.session import SessionLocal
from app.traveler_principal import TravelerPrincipal
from test_r80_private_buddy_route import (
    _accepted_pair,
    _auth,
    _observation,
    client,
)


@pytest.fixture(autouse=True)
def _private_route_environment():
    previous_secret = settings.traveler_auth_secret
    previous_provider = settings.live_valhalla_url
    object.__setattr__(settings, "traveler_auth_secret", "r81-private-location-test-secret-0123456789")
    object.__setattr__(settings, "live_valhalla_url", None)
    try:
        yield
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        object.__setattr__(settings, "live_valhalla_url", previous_provider)


def _another_accepted_handoff(journey_id: str, helper_id: str, requester_id: str) -> str:
    presence_response = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(requester_id)
    )
    assert presence_response.status_code == 200, presence_response.text
    presence = presence_response.json()["group_presence"]
    audience = sorted(member["member_id"] for member in presence["members"])
    audience_sha = hashlib.sha256(json.dumps(audience, separators=(",", ":")).encode()).hexdigest()
    signal = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-member-signals",
        headers={**_auth(requester_id), "Idempotency-Key": "r81-help-" + str(uuid4())},
        json={
            "signal_kind": "NEEDS_COORDINATION",
            "expected_active_plan_version_id": presence["active_plan_version_id"],
            "expected_current_activity_id": presence["current_activity_id"],
            "expected_rendezvous_activity_id": presence["rendezvous"]["activity_id"],
            "expected_public_place_id": presence["rendezvous"]["place_id"],
            "expected_audience_sha256": audience_sha,
        },
    )
    assert signal.status_code == 201, signal.text
    signal_id = str(signal.json()["signal_id"])
    acknowledgement = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-member-signals/{signal_id}/acknowledge",
        headers=_auth(helper_id),
    )
    assert acknowledgement.status_code == 200, acknowledgement.text
    candidates = client.get(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/candidates?signal_id={signal_id}",
        headers=_auth(helper_id),
    )
    assert candidates.status_code == 200, candidates.text
    selection = candidates.json()
    offered = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs",
        headers={**_auth(helper_id), "Idempotency-Key": "r81-offer-" + str(uuid4())},
        json={
            "help_signal_id": signal_id,
            "expected_active_plan_version_id": selection["active_plan_version_id"],
            "expected_current_activity_id": selection["current_activity_id"],
            "expected_rendezvous_activity_id": selection["rendezvous_activity_id"],
            "expected_original_public_place_id": selection["original_public_place_id"],
            "expected_audience_sha256": selection["audience_sha256"],
            "handoff_place_id": selection["items"][-1]["place_id"],
        },
    )
    assert offered.status_code == 201, offered.text
    handoff_id = str(offered.json()["handoff_id"])
    accepted = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}/respond",
        headers=_auth(requester_id),
        json={"decision": "ACCEPT"},
    )
    assert accepted.status_code == 200, accepted.text
    return handoff_id


def test_r81_binding_is_exact_durable_indexed_write_once_and_audit_redacted():
    journey_id, helper_id, _bystander_id, _requester_id, handoff_id = _accepted_pair()
    handoff_path = f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}"
    observation_id = _observation(journey_id, helper_id, handoff_id, 39.9042)

    with SessionLocal() as session:
        observation = session.get(LocationObservation, observation_id)
        binding = session.get(PrivateBuddyRouteLocationBinding, observation_id)
        assert observation is not None and binding is not None
        assert binding.observation_id == observation_id
        assert binding.journey_id == journey_id
        assert binding.handoff_id == handoff_id
        assert binding.journey_member_id == observation.journey_member_id
        assert binding.traveler_id == helper_id
        assert binding.active_plan_version_id
        assert binding.current_activity_id is not None
        assert binding.destination_place_id
        assert len(binding.destination_place_snapshot_sha256) == 64
        assert len(binding.audience_sha256) == 64
        assert binding.handoff_accepted_at
        assert binding.captured_at == observation.captured_at
        assert binding.expires_at > binding.captured_at
        assert binding.bound_at

        audit = session.scalar(select(AuditLog).where(
            AuditLog.journey_id == journey_id,
            AuditLog.action == "journey.private_location.capture_recorded",
        ).order_by(AuditLog.created_at.desc()).limit(1))
        assert audit is not None and audit.entity_id is None
        audit_text = json.dumps({
            "before": audit.before_json,
            "after": audit.after_json,
            "entity_type": audit.entity_type,
            "entity_id": audit.entity_id,
        })
        assert observation_id not in audit_text
        assert handoff_id not in audit_text
        assert "39.9042" not in audit_text
        assert "116.4074" not in audit_text

        index_names = {
            row[1] for row in session.connection().exec_driver_sql(
                "PRAGMA index_list(private_buddy_route_location_bindings)"
            )
        }
        assert "idx_private_buddy_route_bindings_context_expiry" in index_names
        trigger = session.connection().exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_private_buddy_route_binding_no_update'"
        ).scalar_one_or_none()
        assert trigger == "trg_private_buddy_route_binding_no_update"

        with pytest.raises(IntegrityError):
            session.execute(update(PrivateBuddyRouteLocationBinding).where(
                PrivateBuddyRouteLocationBinding.observation_id == observation_id
            ).values(expires_at="2099-01-01T00:00:00+00:00"))

    route = client.post(
        handoff_path + "/route",
        headers=_auth(helper_id),
        json={
            "location_observation_id": observation_id,
            "requested_mode": "WALK",
            "external_provider_disclosure_confirmed": True,
        },
    )
    assert route.status_code == 200, route.text
    with SessionLocal() as session:
        operations = session.scalars(select(RouteAcquisitionOperation).where(
            RouteAcquisitionOperation.journey_id == journey_id,
            RouteAcquisitionOperation.request_json.like("%R80_PRIVATE_BUDDY_HANDOFF%"),
        )).all()
        outbound = [
            row for row in operations
            if json.loads(row.request_json).get("origin_evidence", {}).get("route_leg") == "TO_HANDOFF"
        ]
        assert len(outbound) == 1
        evidence = json.loads(outbound[0].request_json)["origin_evidence"]
        assert evidence["private_location_binding_id"] == observation_id


def test_r81_binding_rejects_cross_handoff_and_legacy_location_source():
    journey_id, helper_id, bystander_id, _requester_id, handoff_id = _accepted_pair()
    observation_id = _observation(journey_id, helper_id, handoff_id, 39.9042)
    other_handoff_id = _another_accepted_handoff(journey_id, helper_id, bystander_id)
    other_path = f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{other_handoff_id}"

    cross_handoff = client.post(
        other_path + "/route",
        headers=_auth(helper_id),
        json={
            "location_observation_id": observation_id,
            "requested_mode": "WALK",
            "external_provider_disclosure_confirmed": True,
        },
    )
    assert cross_handoff.status_code == 409, cross_handoff.text
    assert "R81_PRIVATE_LOCATION_BINDING_CONTEXT_MISMATCH" in cross_handoff.text

    legacy_id = str(uuid4())
    with SessionLocal() as session:
        member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == helper_id,
        ))
        assert member is not None
        session.add(LocationObservation(
            id=legacy_id,
            journey_id=journey_id,
            traveler_id=helper_id,
            journey_member_id=str(member.id),
            latitude=39.9042,
            longitude=116.4074,
            accuracy_m=12,
            captured_at=datetime.now(timezone.utc).isoformat(),
            source="FOREGROUND_WEB",
        ))
        session.commit()
    legacy = client.post(
        other_path + "/route",
        headers=_auth(helper_id),
        json={
            "location_observation_id": legacy_id,
            "requested_mode": "WALK",
            "external_provider_disclosure_confirmed": True,
        },
    )
    assert legacy.status_code == 409, legacy.text
    assert "R81_PRIVATE_LOCATION_BINDING_REQUIRED" in legacy.text


def test_r81_binding_expiry_rejects_observation_while_retaining_its_receipt(monkeypatch):
    journey_id, helper_id, _bystander_id, _requester_id, handoff_id = _accepted_pair()
    observation_id = _observation(journey_id, helper_id, handoff_id, 39.9042)
    with SessionLocal() as session:
        binding = session.get(PrivateBuddyRouteLocationBinding, observation_id)
        assert binding is not None
        future = datetime.fromisoformat(binding.captured_at) + timedelta(seconds=301)
    monkeypatch.setattr(private_route_module, "_now", lambda: future)

    response = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}/route",
        headers=_auth(helper_id),
        json={
            "location_observation_id": observation_id,
            "requested_mode": "WALK",
            "external_provider_disclosure_confirmed": True,
        },
    )
    assert response.status_code == 409, response.text
    assert "R80_LOCATION_OBSERVATION_STALE" in response.text
    with SessionLocal() as session:
        assert session.get(LocationObservation, observation_id) is not None
        assert session.get(PrivateBuddyRouteLocationBinding, observation_id) is not None
        assert session.scalar(select(func.count()).select_from(RouteAcquisitionOperation).where(
            RouteAcquisitionOperation.request_json.like("%R80_PRIVATE_BUDDY_HANDOFF%")
        )) == 0


@pytest.mark.parametrize("mutation", ["revoke", "plan", "cursor", "member"])
def test_r81_binding_rejects_changed_live_handoff_context(mutation: str):
    journey_id, helper_id, _bystander_id, requester_id, handoff_id = _accepted_pair()
    handoff_path = f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}"
    observation_id = _observation(journey_id, helper_id, handoff_id, 39.9042)
    route = client.post(
        handoff_path + "/route",
        headers=_auth(helper_id),
        json={
            "location_observation_id": observation_id,
            "requested_mode": "WALK",
            "external_provider_disclosure_confirmed": True,
        },
    )
    assert route.status_code == 200, route.text

    if mutation == "revoke":
        revoked = client.post(handoff_path + "/revoke", headers=_auth(requester_id))
        assert revoked.status_code == 200, revoked.text
    else:
        alternative_id = None
        if mutation == "plan":
            generated = client.post(
                f"/api/v1/journeys/{journey_id}/plans/generate",
                json={"candidate_count": 1},
            )
            assert generated.status_code == 201, generated.text
            alternative_id = str(generated.json()[0]["id"])
        with SessionLocal() as session:
            journey = session.get(Journey, journey_id)
            assert journey is not None
            if mutation == "plan":
                assert alternative_id is not None and alternative_id != journey.active_plan_version_id
                journey.active_plan_version_id = alternative_id
            elif mutation == "cursor":
                journey.current_activity_id = None
            elif mutation == "member":
                member = session.scalar(select(JourneyMember).where(
                    JourneyMember.journey_id == journey_id,
                    JourneyMember.traveler_id == helper_id,
                ))
                assert member is not None
                member.removed_at = datetime.now(timezone.utc).isoformat()
            session.commit()

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        member = session.scalar(select(JourneyMember).where(
            JourneyMember.journey_id == journey_id,
            JourneyMember.traveler_id == helper_id,
        ))
        handoff = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
        assert journey is not None and member is not None and handoff is not None
        assert session.get(PrivateBuddyRouteLocationBinding, observation_id) is not None
        principal = TravelerPrincipal(
            helper_id, "TEST", int(datetime.now(timezone.utc).timestamp()) + 3600
        )
        with pytest.raises(HTTPException) as rejected:
            private_route_module.validate_private_location_binding(
                session,
                journey=journey,
                handoff=handoff,
                member=member,
                principal=principal,
                observation_id=observation_id,
                place_snapshot=json.loads(handoff.handoff_place_snapshot_json),
            )
        assert rejected.value.status_code in {403, 409}
