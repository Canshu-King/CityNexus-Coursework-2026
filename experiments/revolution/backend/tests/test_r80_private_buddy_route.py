from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.execution.private_buddy_safe_return as safe_return_module
import app.main as main_module
from app.core.config import settings
from app.db.models import (
    AgentRun,
    Activity,
    AuditLog,
    Commitment,
    Event,
    GuardianPulse,
    GuardianRun,
    Journey,
    JourneyOutcomeEvent,
    JourneyRendezvousBuddyHandoff,
    LocationObservation,
    MobilityFact,
    PlanVersion,
    PrivateBuddyRouteLocationBinding,
    ProviderCall,
    RouteAcquisitionOperation,
)
from app.db.session import SessionLocal
from app.execution.clock import execution_clock
from app.execution.route_session_contract import route_request_fingerprint
from app.traveler_principal import issue_traveler_token
from demo_stage import demo_reset_headers

client = TestClient(main_module.app)
TEST_SECRET = "REDACTED_FOR_PUBLIC_RELEASE"


@pytest.fixture(autouse=True)
def _private_route_environment():
    previous_secret = settings.traveler_auth_secret
    previous_provider = settings.live_valhalla_url
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    object.__setattr__(settings, "live_valhalla_url", None)
    try:
        yield
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        object.__setattr__(settings, "live_valhalla_url", previous_provider)


def _auth(traveler_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_traveler_token(traveler_id)}"}


def _new_traveler(name: str) -> str:
    response = client.post("/api/v1/travelers", json={
        "display_name": name,
        "email": None,
        "locale": "en-US",
        "timezone": "UTC",
    })
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _join(journey_id: str, owner_id: str, traveler_id: str) -> None:
    offer = client.post(
        f"/api/v1/journeys/{journey_id}/membership-invitations",
        headers={**_auth(owner_id), "Idempotency-Key": "r80-invite-" + str(uuid4())},
        json={"target_traveler_id": traveler_id},
    )
    assert offer.status_code == 201, offer.text
    accepted = client.post(
        f"/api/v1/journey-membership-invitations/{offer.json()['invitation_id']}/respond",
        headers=_auth(traveler_id),
        json={"decision": "ACCEPT"},
    )
    assert accepted.status_code == 200, accepted.text


def _accepted_pair(*, use_original_stop: bool = False) -> tuple[str, str, str, str, str]:
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, reset.text
    journey_id = str(reset.json()["journey_id"])
    helper_id = str(reset.json()["traveler_id"])
    plan = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert plan.status_code == 201, plan.text
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan.json()[0]['id']}/select",
        headers=_auth(helper_id),
    ).status_code == 200
    assert client.post(
        f"/api/v1/journeys/{journey_id}/start", headers=_auth(helper_id)
    ).status_code == 200

    bystander_id = _new_traveler("R80 bystander")
    requester_id = _new_traveler("R80 requester")
    _join(journey_id, helper_id, bystander_id)
    _join(journey_id, helper_id, requester_id)

    presence_response = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(requester_id)
    )
    assert presence_response.status_code == 200, presence_response.text
    presence = presence_response.json()["group_presence"]
    target = presence["rendezvous"]
    audience = sorted(member["member_id"] for member in presence["members"])
    audience_sha = hashlib.sha256(json.dumps(audience, separators=(",", ":")).encode()).hexdigest()
    signal = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-member-signals",
        headers={**_auth(requester_id), "Idempotency-Key": "r80-help-" + str(uuid4())},
        json={
            "signal_kind": "NEEDS_COORDINATION",
            "expected_active_plan_version_id": presence["active_plan_version_id"],
            "expected_current_activity_id": presence["current_activity_id"],
            "expected_rendezvous_activity_id": target["activity_id"],
            "expected_public_place_id": target["place_id"],
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
    candidates_response = client.get(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/candidates?signal_id={signal_id}",
        headers=_auth(helper_id),
    )
    assert candidates_response.status_code == 200, candidates_response.text
    candidates = candidates_response.json()
    selected = candidates["items"][0] if use_original_stop else candidates["items"][-1]
    if use_original_stop:
        assert selected["place_id"] == candidates["original_public_place_id"]
    handoff = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs",
        headers={**_auth(helper_id), "Idempotency-Key": "r80-offer-" + str(uuid4())},
        json={
            "help_signal_id": signal_id,
            "expected_active_plan_version_id": candidates["active_plan_version_id"],
            "expected_current_activity_id": candidates["current_activity_id"],
            "expected_rendezvous_activity_id": candidates["rendezvous_activity_id"],
            "expected_original_public_place_id": candidates["original_public_place_id"],
            "expected_audience_sha256": candidates["audience_sha256"],
            "handoff_place_id": selected["place_id"],
        },
    )
    assert handoff.status_code == 201, handoff.text
    handoff_id = str(handoff.json()["handoff_id"])
    accepted = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}/respond",
        headers=_auth(requester_id),
        json={"decision": "ACCEPT"},
    )
    assert accepted.status_code == 200, accepted.text
    return journey_id, helper_id, bystander_id, requester_id, handoff_id


def _observation(journey_id: str, traveler_id: str, handoff_id: str, latitude: float) -> str:
    response = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}/route/location",
        headers=_auth(traveler_id),
        json={
            "latitude": latitude,
            "longitude": 116.4074,
            "accuracy_m": 12,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["principal_bound"] is True
    assert response.json()["visibility"] == "CALLER_PRIVATE"
    assert response.json()["external_provider_contacted"] is False
    assert str(latitude) not in response.text
    return str(response.json()["location_observation_id"])


def _count(model) -> int:
    with SessionLocal() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _private_route_operations(journey_id: str) -> dict[str, RouteAcquisitionOperation]:
    with SessionLocal() as session:
        rows = session.scalars(select(RouteAcquisitionOperation).where(
            RouteAcquisitionOperation.journey_id == journey_id,
            RouteAcquisitionOperation.request_json.like("%R80_PRIVATE_BUDDY_HANDOFF%"),
        )).all()
        result: dict[str, RouteAcquisitionOperation] = {}
        for row in rows:
            try:
                request_json = json.loads(row.request_json or "{}")
                evidence = request_json.get("origin_evidence") if isinstance(request_json, dict) else None
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(evidence, dict) and evidence.get("route_leg"):
                result[str(evidence["route_leg"])] = row
        return result


def _matching_paths(value, needle: str, path: str = "$") -> list[str]:
    if isinstance(value, dict):
        return [match for key, item in value.items() for match in _matching_paths(item, needle, f"{path}.{key}")]
    if isinstance(value, list):
        return [match for index, item in enumerate(value) for match in _matching_paths(item, needle, f"{path}[{index}]")]
    return [path] if isinstance(value, str) and needle in value else []


def test_r80_private_routes_are_separate_consented_member_bound_and_hidden_from_bystander():
    journey_id, helper_id, bystander_id, requester_id, handoff_id = _accepted_pair()
    handoff_path = f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}"
    helper_current = client.get(handoff_path + "/route/current", headers=_auth(helper_id))
    requester_current = client.get(handoff_path + "/route/current", headers=_auth(requester_id))
    bystander_current = client.get(handoff_path + "/route/current", headers=_auth(bystander_id))
    assert helper_current.status_code == 200, helper_current.text
    assert requester_current.status_code == 200, requester_current.text
    assert helper_current.json()["status"] == "NOT_REQUESTED"
    assert requester_current.json()["status"] == "NOT_REQUESTED"
    assert bystander_current.status_code == 403

    baseline = {
        "observations": _count(LocationObservation),
        "operations": _count(RouteAcquisitionOperation),
        "provider_calls": _count(ProviderCall),
        "plans": _count(PlanVersion),
        "mobility_facts": _count(MobilityFact),
        "agents": _count(AgentRun),
        "guardians": _count(GuardianRun),
        "pulses": _count(GuardianPulse),
        "events": _count(Event),
        "outcomes": _count(JourneyOutcomeEvent),
    }
    assert baseline["observations"] >= 0
    assert baseline["operations"] >= 0

    # The R79 acceptance itself did not capture location or begin a route.
    assert client.get(handoff_path + "/route/current", headers=_auth(helper_id)).json()["status"] == "NOT_REQUESTED"
    assert _count(LocationObservation) == baseline["observations"]
    bystander_capture = client.post(
        handoff_path + "/route/location",
        headers=_auth(bystander_id),
        json={
            "latitude": 35.6762,
            "longitude": 139.6503,
            "accuracy_m": 20,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert bystander_capture.status_code == 403
    assert _count(LocationObservation) == baseline["observations"]

    helper_observation = _observation(journey_id, helper_id, handoff_id, 39.9042)
    assert _count(LocationObservation) == baseline["observations"] + 1
    assert _count(RouteAcquisitionOperation) == baseline["operations"]
    assert client.get(handoff_path + "/route/current", headers=_auth(helper_id)).json()["status"] == "NOT_REQUESTED"
    no_disclosure = client.post(
        handoff_path + "/route",
        headers=_auth(helper_id),
        json={
            "location_observation_id": helper_observation,
            "requested_mode": "WALK",
            "external_provider_disclosure_confirmed": False,
        },
    )
    assert no_disclosure.status_code == 400
    assert _count(RouteAcquisitionOperation) == baseline["operations"]
    assert _count(ProviderCall) == baseline["provider_calls"]

    helper_route = client.post(
        handoff_path + "/route",
        headers=_auth(helper_id),
        json={
            "location_observation_id": helper_observation,
            "requested_mode": "WALK",
            "external_provider_disclosure_confirmed": True,
        },
    )
    assert helper_route.status_code == 200, helper_route.text
    helper_result = helper_route.json()
    assert helper_result["route_state"] == "UNKNOWN"
    assert helper_result["status"] == "UNAVAILABLE"
    assert helper_result["duration_min"] is None
    assert helper_result["provider"] is None
    assert helper_result["shared_plan_changed"] is False
    assert helper_result["physical_arrival_verified"] is False
    assert helper_result["guardian_triggered"] is False
    assert helper_observation not in json.dumps(helper_result)
    assert helper_result["outbound_leg"]["route_state"] == "UNKNOWN"
    assert helper_result["return_leg"]["route_state"] == "UNKNOWN"
    assert helper_result["shared_commitment_risk"]["state"] == "UNKNOWN"
    assert _count(RouteAcquisitionOperation) == baseline["operations"] + 2
    assert _count(ProviderCall) == baseline["provider_calls"]
    assert _count(MobilityFact) == baseline["mobility_facts"]
    generic_route = client.post(
        f"/api/v1/journeys/{journey_id}/group-presence/route",
        headers=_auth(helper_id),
        json={"location_observation_id": helper_observation, "requested_mode": "WALK"},
    )
    assert generic_route.status_code == 409
    assert _count(RouteAcquisitionOperation) == baseline["operations"] + 2
    assert _count(ProviderCall) == baseline["provider_calls"]

    helper_operation = None
    return_operation = None
    with SessionLocal() as session:
        operations = session.scalars(select(RouteAcquisitionOperation).where(
            RouteAcquisitionOperation.journey_id == journey_id,
            RouteAcquisitionOperation.target_key != "",
        )).all()
        private = [row for row in operations if "R80_PRIVATE_BUDDY_HANDOFF" in row.request_json]
        assert len(private) == 2
        private_by_leg = {
            json.loads(row.request_json)["origin_evidence"]["route_leg"]: row
            for row in private
        }
        helper_operation = private_by_leg["TO_HANDOFF"]
        return_operation = private_by_leg["RETURN_TO_SHARED_STOP"]
        request_json = json.loads(helper_operation.request_json)
        evidence = request_json["origin_evidence"]
        assert evidence["journey_member_id"] == session.scalar(select(LocationObservation.journey_member_id).where(
            LocationObservation.id == helper_observation
        ))
        assert evidence["traveler_id"] == helper_id
        assert evidence["handoff_id"] == handoff_id
        assert helper_observation in helper_operation.request_json
        assert "39.9042" not in helper_operation.request_json
        assert "116.4074" not in helper_operation.request_json
        return_request = json.loads(return_operation.request_json)
        return_evidence = return_request["origin_evidence"]
        handoff = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
        assert handoff is not None
        assert return_evidence["route_leg"] == "RETURN_TO_SHARED_STOP"
        assert return_evidence["private_location_binding_id"] == helper_observation
        assert return_evidence["bound_outbound_operation_id"] == helper_operation.id
        assert return_operation.origin_key == handoff.handoff_place_id
        assert return_operation.target_key == handoff.original_public_place_id
        assert return_operation.target_activity_id == handoff.original_rendezvous_activity_id
        assert "39.9042" not in return_operation.request_json
        assert "116.4074" not in return_operation.request_json
        audit_after_json = json.dumps([
            row.after_json for row in session.scalars(select(AuditLog)).all()
        ])
        assert helper_observation not in audit_after_json

    # The legacy operation read endpoint must authenticate and sanitize private rows.
    bystander_operation = client.get(
        f"/api/v1/journeys/{journey_id}/routes/operations/{helper_operation.id}",
        headers=_auth(bystander_id),
    )
    assert bystander_operation.status_code == 403
    helper_operation_read = client.get(
        f"/api/v1/journeys/{journey_id}/routes/operations/{helper_operation.id}",
        headers=_auth(helper_id),
    )
    assert helper_operation_read.status_code == 200, helper_operation_read.text
    assert "geometry" not in json.dumps(helper_operation_read.json()).lower()
    assert helper_observation not in json.dumps(helper_operation_read.json())
    return_operation_read = client.get(
        f"/api/v1/journeys/{journey_id}/routes/operations/{return_operation.id}",
        headers=_auth(bystander_id),
    )
    assert return_operation_read.status_code == 403
    assert "geometry" not in return_operation_read.text.lower()
    assert helper_observation not in return_operation_read.text

    requester_before = client.get(handoff_path + "/route/current", headers=_auth(requester_id)).json()
    assert requester_before["status"] == "NOT_REQUESTED"
    requester_observation = _observation(journey_id, requester_id, handoff_id, 31.2304)
    replayed_observation = client.post(
        handoff_path + "/route",
        headers=_auth(helper_id),
        json={
            "location_observation_id": requester_observation,
            "requested_mode": "WALK",
            "external_provider_disclosure_confirmed": True,
        },
    )
    assert replayed_observation.status_code == 403
    assert _count(RouteAcquisitionOperation) == baseline["operations"] + 2
    requester_route = client.post(
        handoff_path + "/route",
        headers=_auth(requester_id),
        json={
            "location_observation_id": requester_observation,
            "requested_mode": "WALK",
            "external_provider_disclosure_confirmed": True,
        },
    )
    assert requester_route.status_code == 200, requester_route.text
    assert requester_route.json()["route_state"] == "UNKNOWN"
    assert helper_observation not in json.dumps(requester_route.json())
    assert requester_observation not in json.dumps(requester_route.json())
    assert _count(RouteAcquisitionOperation) == baseline["operations"] + 4
    assert _count(ProviderCall) == baseline["provider_calls"]
    assert _count(PlanVersion) == baseline["plans"]
    assert _count(AgentRun) == baseline["agents"]
    assert _count(GuardianRun) == baseline["guardians"]
    assert _count(GuardianPulse) == baseline["pulses"]
    assert _count(Event) == baseline["events"]
    assert _count(JourneyOutcomeEvent) == baseline["outcomes"]

    bystander_handoffs = client.get(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/current",
        headers=_auth(bystander_id),
    )
    assert bystander_handoffs.status_code == 200
    assert bystander_handoffs.json() == {"items": []}
    bystander_operator = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(bystander_id)
    )
    assert bystander_operator.status_code == 200
    visible = json.dumps(bystander_operator.json())
    assert handoff_id not in visible
    assert helper_observation not in visible
    assert requester_observation not in visible, _matching_paths(bystander_operator.json(), requester_observation)
    assert str(helper_operation.id) not in visible
    assert "39.9042" not in visible and "31.2304" not in visible
    helper_operator = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(helper_id)
    )
    assert helper_operator.status_code == 200
    helper_projection = json.dumps(helper_operator.json())
    assert helper_observation not in helper_projection
    assert requester_observation not in helper_projection
    assert "39.9042" not in helper_projection and "31.2304" not in helper_projection
    from app.operations.v9 import refresh_offline_snapshot

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        offline = refresh_offline_snapshot(session, journey)
        offline_payload = json.loads(offline.payload_json)
    assert helper_observation not in json.dumps(offline_payload)
    assert requester_observation not in json.dumps(offline_payload)
    assert "39.9042" not in json.dumps(offline_payload)
    assert "31.2304" not in json.dumps(offline_payload)


def test_r80_route_and_legacy_operation_read_fail_closed_after_handoff_revocation():
    journey_id, helper_id, bystander_id, requester_id, handoff_id = _accepted_pair()
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
    operations = _private_route_operations(journey_id)
    operation_id = str(operations["TO_HANDOFF"].id)

    revoke = client.post(handoff_path + "/revoke", headers=_auth(requester_id))
    assert revoke.status_code == 200, revoke.text
    stale = client.get(handoff_path + "/route/current", headers=_auth(helper_id))
    assert stale.status_code == 200, stale.text
    assert stale.json()["status"] == "STALE"
    assert stale.json()["route_state"] == "UNKNOWN"
    assert stale.json()["duration_min"] is None
    assert stale.json()["shared_commitment_risk"]["state"] == "UNKNOWN"
    assert client.get(
        f"/api/v1/journeys/{journey_id}/routes/operations/{operation_id}",
        headers=_auth(helper_id),
    ).status_code == 409
    assert client.get(
        f"/api/v1/journeys/{journey_id}/routes/operations/{operation_id}",
        headers=_auth(bystander_id),
    ).status_code == 403


def test_r80_replay_is_historical_and_mock_cannot_be_projected_as_live():
    journey_id, helper_id, _bystander_id, _requester_id, handoff_id = _accepted_pair()
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
    operations = _private_route_operations(journey_id)
    assert set(operations) == {"TO_HANDOFF", "RETURN_TO_SHARED_STOP"}
    outbound_id = str(operations["TO_HANDOFF"].id)
    return_id = str(operations["RETURN_TO_SHARED_STOP"].id)
    with SessionLocal() as session:
        stamp = datetime.now(timezone.utc).isoformat()
        for operation_id, duration in ((outbound_id, 17), (return_id, 23)):
            operation = session.get(RouteAcquisitionOperation, operation_id)
            assert operation is not None
            operation.status = "COMPLETED"
            operation.outcome = "ROUTE_READY"
            operation.error_json = "{}"
            operation.result_json = json.dumps({
                "provider": "sealed-route-cache",
                "provenance": "CAPTURED_REPLAY",
                "duration_min": duration,
                "captured_at": stamp,
            })
        session.commit()

    replay = client.get(handoff_path + "/route/current", headers=_auth(helper_id))
    assert replay.status_code == 200, replay.text
    assert replay.json()["route_state"] == "REPLAY"
    assert replay.json()["provider"] == "sealed-route-cache"
    assert replay.json()["duration_min"] == 40
    assert replay.json()["outbound_leg"]["route_state"] == "REPLAY"
    assert replay.json()["outbound_leg"]["reason"] == "CAPTURED_HISTORICAL_ROUTE_NOT_CURRENT_TRAFFIC"
    assert replay.json()["return_leg"]["route_state"] == "REPLAY"
    assert replay.json()["shared_commitment_risk"]["state"] == "UNKNOWN"
    assert observation_id not in replay.text
    assert "39.9042" not in replay.text

    # A replay on only one leg is not enough to claim a current end-to-end route.
    with SessionLocal() as session:
        operation = session.get(RouteAcquisitionOperation, return_id)
        assert operation is not None
        operation.status = "UNAVAILABLE"
        operation.outcome = "ROUTE_UNAVAILABLE"
        operation.error_json = json.dumps({"reason": "ROUTE_PROVIDER_NOT_CONFIGURED"})
        operation.result_json = json.dumps({"provider": None, "provenance": "NONE"})
        session.commit()
    partial = client.get(handoff_path + "/route/current", headers=_auth(helper_id))
    assert partial.status_code == 200, partial.text
    assert partial.json()["route_state"] == "UNKNOWN"
    assert partial.json()["duration_min"] is None
    assert partial.json()["outbound_leg"]["route_state"] == "REPLAY"
    assert partial.json()["return_leg"]["route_state"] == "UNKNOWN"
    assert partial.json()["shared_commitment_risk"]["state"] == "UNKNOWN"

    with SessionLocal() as session:
        operation = session.get(RouteAcquisitionOperation, outbound_id)
        assert operation is not None
        operation.result_json = json.dumps({
            "provider": "mock-mobility",
            "provenance": "LIVE_PROVIDER_CAPTURE",
            "duration_min": 3,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        })
        session.add(ProviderCall(
            id=str(uuid4()),
            provider_key="mock-mobility",
            operation="ROUTE_ACQUISITION",
            request_json=operation.request_json,
            response_json=json.dumps({"operation_id": str(operation.id)}),
            status="SUCCESS",
            latency_ms=1,
            created_at=datetime.now(timezone.utc).isoformat(),
        ))
        session.commit()

    mock = client.get(handoff_path + "/route/current", headers=_auth(helper_id))
    assert mock.status_code == 200, mock.text
    assert mock.json()["route_state"] == "UNKNOWN"
    assert mock.json()["outbound_leg"]["route_state"] == "UNKNOWN"
    assert mock.json()["return_leg"]["route_state"] == "UNKNOWN"
    assert mock.json()["duration_min"] is None

    # An operation result can *claim* a legitimate provider/provenance. An
    # unrelated or failed ProviderCall, even one naming the operation ID,
    # must never convert it into a LIVE route for a traveller.
    with SessionLocal() as session:
        operation = session.get(RouteAcquisitionOperation, outbound_id)
        assert operation is not None
        stamp = datetime.now(timezone.utc).isoformat()
        claimed_result = {
            "operation_id": str(operation.id),
            "provider": "valhalla-http",
            "provenance": "LIVE_PROVIDER_CAPTURE",
            "duration_min": 8,
            "captured_at": stamp,
        }
        operation.result_json = json.dumps(claimed_result)
        for provider_key, status, request_json in [
            ("mock-mobility", "SUCCESS", operation.request_json),
            ("valhalla", "FAILURE", operation.request_json),
            ("valhalla", "SUCCESS", "{}"),
        ]:
            session.add(ProviderCall(
                id=str(uuid4()),
                provider_key=provider_key,
                operation="ROUTE_ACQUISITION",
                request_json=request_json,
                response_json=json.dumps(claimed_result),
                status=status,
                latency_ms=1,
                created_at=stamp,
            ))
        session.commit()

    mismatched_receipts = client.get(handoff_path + "/route/current", headers=_auth(helper_id))
    assert mismatched_receipts.status_code == 200, mismatched_receipts.text
    assert mismatched_receipts.json()["route_state"] == "UNKNOWN"
    assert mismatched_receipts.json()["outbound_leg"]["route_state"] == "UNKNOWN"
    assert mismatched_receipts.json()["duration_min"] is None

    # This is a synthetic *database-contract* test, NOT a real Valhalla run.
    # An internally consistent persisted successful Valhalla receipt is the
    # minimum LIVE receipt contract; only a real configured provider may
    # create such a receipt in normal product operation.
    with SessionLocal() as session:
        operation = session.get(RouteAcquisitionOperation, outbound_id)
        assert operation is not None
        session.add(ProviderCall(
            id=str(uuid4()),
            provider_key="valhalla",
            operation="ROUTE_ACQUISITION",
            request_json=operation.request_json,
            response_json=operation.result_json,
            status="SUCCESS",
            latency_ms=1,
            created_at=datetime.now(timezone.utc).isoformat(),
        ))
        session.commit()
    one_leg_receipt = client.get(handoff_path + "/route/current", headers=_auth(helper_id))
    assert one_leg_receipt.status_code == 200, one_leg_receipt.text
    assert one_leg_receipt.json()["outbound_leg"]["route_state"] == "LIVE"
    assert one_leg_receipt.json()["return_leg"]["route_state"] == "UNKNOWN"
    assert one_leg_receipt.json()["route_state"] == "UNKNOWN"
    assert one_leg_receipt.json()["duration_min"] is None

    with SessionLocal() as session:
        operation = session.get(RouteAcquisitionOperation, return_id)
        assert operation is not None
        stamp = datetime.now(timezone.utc).isoformat()
        live_return = {
            "operation_id": str(operation.id),
            "provider": "valhalla-http",
            "provenance": "LIVE_PROVIDER_CAPTURE",
            "duration_min": 6,
            "captured_at": stamp,
        }
        operation.status = "COMPLETED"
        operation.outcome = "ROUTE_READY"
        operation.error_json = "{}"
        operation.result_json = json.dumps(live_return)
        session.add(ProviderCall(
            id=str(uuid4()),
            provider_key="valhalla",
            operation="ROUTE_ACQUISITION",
            request_json=operation.request_json,
            response_json=json.dumps(live_return),
            status="SUCCESS",
            latency_ms=1,
            created_at=stamp,
        ))
        session.commit()
    both_legs_live = client.get(handoff_path + "/route/current", headers=_auth(helper_id))
    assert both_legs_live.status_code == 200, both_legs_live.text
    assert both_legs_live.json()["route_state"] == "LIVE"
    assert both_legs_live.json()["outbound_leg"]["route_state"] == "LIVE"
    assert both_legs_live.json()["return_leg"]["route_state"] == "LIVE"
    assert both_legs_live.json()["duration_min"] == 14


@pytest.mark.parametrize(
    "minutes_until_commitment, expected_state",
    [(15, "COMMITMENT_AT_RISK"), (75, "UNKNOWN")],
)
def test_r81_controlled_two_leg_route_keeps_c4_risk_test_only_and_private(
    monkeypatch, minutes_until_commitment: int, expected_state: str,
):
    journey_id, helper_id, bystander_id, _requester_id, handoff_id = _accepted_pair()
    handoff_path = f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}"
    observation_id = _observation(journey_id, helper_id, handoff_id, 39.9042)
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        handoff = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
        assert handoff is not None
        activity = session.get(Activity, handoff.original_rendezvous_activity_id)
        journey = session.get(Journey, journey_id)
        assert activity is not None and journey is not None
        plan_before = str(journey.active_plan_version_id)
        session.add(Commitment(
            id=str(uuid4()),
            journey_id=journey_id,
            activity_id=str(activity.id),
            kind="APPOINTMENT",
            level="C4_Anchored",
            status="ACTIVE",
            monetary_value_minor=0,
            monetary_value_status="KNOWN",
            currency="JPY",
            external_ref="R81_TWO_LEG_TEST_COMMITMENT",
            policy_json=json.dumps({
                "scheduled_start_at": (now + timedelta(minutes=minutes_until_commitment)).isoformat(),
                "safety_buffer_min": 5,
                "source": "R81_CONTROLLED_TEST_ADAPTER",
            }),
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        ))
        session.commit()

    captured_requests: list[dict] = []

    def _controlled_submit(session, journey, route_request):
        payload = route_request.semantic_payload()
        captured_requests.append(payload)
        evidence = payload["origin_evidence"]
        duration = 8 if evidence["route_leg"] == "TO_HANDOFF" else 10
        operation_id = str(uuid4())
        stamp = datetime.now(timezone.utc).isoformat()
        result = {
            "operation_id": operation_id,
            "provider": "valhalla-http",
            "provenance": "TEST_PROVIDER_EMULATOR",
            "duration_min": duration,
            "captured_at": stamp,
        }
        row = RouteAcquisitionOperation(
            id=operation_id,
            journey_id=str(journey.id),
            fingerprint=route_request_fingerprint(route_request),
            origin_key=route_request.origin_key,
            target_key=route_request.target_key,
            target_activity_id=route_request.target_activity_id,
            requested_mode=route_request.requested_mode,
            provider_policy_version=route_request.provider_policy_version,
            route_truth_policy_version=route_request.route_truth_policy_version,
            status="COMPLETED",
            phase="COMPLETE",
            outcome="ROUTE_READY",
            request_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            result_json=json.dumps(result),
            error_json="{}",
            queued_at=stamp,
            started_at=stamp,
            finished_at=stamp,
            updated_at=stamp,
        )
        session.add(row)
        session.add(ProviderCall(
            id=str(uuid4()),
            provider_key="valhalla",
            operation="ROUTE_ACQUISITION",
            request_json=row.request_json,
            response_json=json.dumps(result),
            status="SUCCESS",
            latency_ms=1,
            created_at=stamp,
        ))
        session.commit()
        return row, False

    object.__setattr__(settings, "live_valhalla_url", "http://r81-test-provider.invalid")
    monkeypatch.setattr(safe_return_module, "submit_operation", _controlled_submit)
    before = {
        "operations": _count(RouteAcquisitionOperation),
        "provider_calls": _count(ProviderCall),
        "mobility_facts": _count(MobilityFact),
        "agents": _count(AgentRun),
        "guardians": _count(GuardianRun),
        "pulses": _count(GuardianPulse),
        "plans": _count(PlanVersion),
    }
    response = client.post(
        handoff_path + "/route",
        headers=_auth(helper_id),
        json={
            "location_observation_id": observation_id,
            "requested_mode": "WALK",
            "external_provider_disclosure_confirmed": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["route_state"] == "TEST_PROVIDER_EMULATOR"
    assert body["status"] == "COMPLETED"
    assert body["duration_min"] == 18
    assert body["outbound_leg"]["route_state"] == "TEST_PROVIDER_EMULATOR"
    assert body["outbound_leg"]["duration_min"] == 8
    assert body["return_leg"]["route_state"] == "TEST_PROVIDER_EMULATOR"
    assert body["return_leg"]["duration_min"] == 10
    assert body["shared_commitment_risk"]["state"] == expected_state
    assert body["shared_commitment_risk"]["evidence_scope"] == "TEST_ONLY"
    if expected_state == "UNKNOWN":
        assert body["shared_commitment_risk"]["basis"] == "TWO_LEG_TRAVEL_ONLY_HANDOFF_DWELL_UNKNOWN"
        assert body["shared_commitment_risk"]["slack_after_buffer_min"] is None
        assert body["shared_commitment_risk"]["handoff_dwell_time_known"] is False
        assert body["shared_commitment_risk"]["travel_only_slack_after_buffer_min"] > 0
    else:
        assert body["shared_commitment_risk"]["basis"] == "TWO_LEG_ROUTE_TO_HANDOFF_AND_CURRENT_SHARED_STOP"
    assert body["shared_commitment_risk"]["legs_used"] == ["TO_HANDOFF", "RETURN_TO_SHARED_STOP"]
    assert body["shared_plan_changed"] is False
    assert body["physical_arrival_verified"] is False
    assert body["guardian_triggered"] is False
    assert observation_id not in json.dumps(body)
    assert len(captured_requests) == 2
    payloads_by_leg = {
        item["origin_evidence"]["route_leg"]: item
        for item in captured_requests
    }
    outbound = payloads_by_leg["TO_HANDOFF"]
    returned = payloads_by_leg["RETURN_TO_SHARED_STOP"]
    with SessionLocal() as session:
        handoff = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
        assert handoff is not None
        assert outbound["origin_evidence"]["route_leg"] == "TO_HANDOFF"
        assert outbound["origin_key"] == f"location-observation:{observation_id}"
        assert outbound["target_key"] == handoff.handoff_place_id
        assert returned["origin_evidence"]["route_leg"] == "RETURN_TO_SHARED_STOP"
        assert returned["origin_evidence"]["private_location_binding_id"] == observation_id
        assert returned["origin_key"] == handoff.handoff_place_id
        assert returned["target_key"] == handoff.original_public_place_id
        assert returned["target_activity_id"] == handoff.original_rendezvous_activity_id
        assert "39.9042" not in json.dumps(captured_requests)
        assert "116.4074" not in json.dumps(captured_requests)
        assert str(session.get(Journey, journey_id).active_plan_version_id) == plan_before
    assert _count(RouteAcquisitionOperation) == before["operations"] + 2
    assert _count(ProviderCall) == before["provider_calls"] + 2
    assert _count(MobilityFact) == before["mobility_facts"]
    assert _count(AgentRun) == before["agents"]
    assert _count(GuardianRun) == before["guardians"]
    assert _count(GuardianPulse) == before["pulses"]
    assert _count(PlanVersion) == before["plans"]

    operations = _private_route_operations(journey_id)
    for operation in operations.values():
        bystander_read = client.get(
            f"/api/v1/journeys/{journey_id}/routes/operations/{operation.id}",
            headers=_auth(bystander_id),
        )
        assert bystander_read.status_code == 403
        assert "geometry" not in bystander_read.text.lower()
        assert observation_id not in bystander_read.text


def test_r81_expired_private_binding_reads_as_stale_not_not_requested():
    journey_id, helper_id, _bystander_id, _requester_id, handoff_id = _accepted_pair()
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
    assert route.json()["status"] == "UNAVAILABLE"

    with SessionLocal() as session:
        binding = session.get(PrivateBuddyRouteLocationBinding, observation_id)
        assert binding is not None
        expires_at = datetime.fromisoformat(binding.expires_at)

    with execution_clock.freeze(expires_at + timedelta(seconds=1)):
        current = client.get(handoff_path + "/route/current", headers=_auth(helper_id))
    assert current.status_code == 200, current.text
    assert current.json()["status"] == "STALE"
    assert current.json()["reason"] == "R81_PRIVATE_LOCATION_BINDING_STALE"
    assert current.json()["route_state"] == "UNKNOWN"
    assert current.json()["duration_min"] is None
    assert current.json()["shared_commitment_risk"]["state"] == "UNKNOWN"
    assert observation_id not in current.text


def test_r81_rechecks_handoff_in_second_session_before_releasing_return_leg(monkeypatch):
    journey_id, helper_id, _bystander_id, _requester_id, handoff_id = _accepted_pair()
    handoff_path = f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}"
    observation_id = _observation(journey_id, helper_id, handoff_id, 39.9042)
    object.__setattr__(settings, "live_valhalla_url", "http://r81-test-provider.invalid")
    submitted_legs: list[str] = []

    def _submit_then_revoke(session, journey, route_request):
        payload = route_request.semantic_payload()
        evidence = payload["origin_evidence"]
        leg = str(evidence["route_leg"])
        submitted_legs.append(leg)
        operation_id = str(uuid4())
        stamp = datetime.now(timezone.utc).isoformat()
        result = {
            "operation_id": operation_id,
            "provider": "valhalla-http",
            "provenance": "TEST_PROVIDER_EMULATOR",
            "duration_min": 8,
            "captured_at": stamp,
        }
        row = RouteAcquisitionOperation(
            id=operation_id,
            journey_id=str(journey.id),
            fingerprint=route_request_fingerprint(route_request),
            origin_key=route_request.origin_key,
            target_key=route_request.target_key,
            target_activity_id=route_request.target_activity_id,
            requested_mode=route_request.requested_mode,
            provider_policy_version=route_request.provider_policy_version,
            route_truth_policy_version=route_request.route_truth_policy_version,
            status="COMPLETED",
            phase="COMPLETE",
            outcome="ROUTE_READY",
            request_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            result_json=json.dumps(result),
            error_json="{}",
            queued_at=stamp,
            started_at=stamp,
            finished_at=stamp,
            updated_at=stamp,
        )
        session.add(row)
        session.commit()
        if leg == "TO_HANDOFF":
            # Model another process revoking the pair agreement after the slow
            # outbound operation commits but before a second route is released.
            with SessionLocal() as other_session:
                fresh_handoff = other_session.get(JourneyRendezvousBuddyHandoff, handoff_id)
                assert fresh_handoff is not None
                fresh_handoff.status = "REVOKED"
                fresh_handoff.closed_at = stamp
                fresh_handoff.updated_at = stamp
                other_session.commit()
        return row, False

    monkeypatch.setattr(safe_return_module, "submit_operation", _submit_then_revoke)
    before = _count(RouteAcquisitionOperation)
    response = client.post(
        handoff_path + "/route",
        headers=_auth(helper_id),
        json={
            "location_observation_id": observation_id,
            "requested_mode": "WALK",
            "external_provider_disclosure_confirmed": True,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "STALE"
    assert response.json()["route_state"] == "UNKNOWN"
    assert response.json()["duration_min"] is None
    assert response.json()["shared_commitment_risk"]["state"] == "UNKNOWN"
    assert submitted_legs == ["TO_HANDOFF"]
    assert _count(RouteAcquisitionOperation) == before + 1


def test_r80_generic_route_operation_requires_signed_member_and_private_origin_owner():
    """Legacy route reads cannot expose another signed member's geometry by UUID."""
    journey_id, helper_id, bystander_id, requester_id, handoff_id = _accepted_pair()
    observation_id = _observation(journey_id, helper_id, handoff_id, 39.9042)
    operation_id = str(uuid4())
    with SessionLocal() as session:
        observed = session.get(LocationObservation, observation_id)
        assert observed is not None and observed.journey_member_id
        captured = datetime.now(timezone.utc).isoformat()
        session.add(RouteAcquisitionOperation(
            id=operation_id,
            journey_id=journey_id,
            fingerprint="r80-generic-own-route-" + str(uuid4()),
            origin_key=f"location-observation:{observation_id}",
            target_key="synthetic-public-target",
            target_activity_id=None,
            requested_mode="WALK",
            provider_policy_version="narrow-route-read-regression",
            route_truth_policy_version="narrow-route-read-regression",
            status="UNAVAILABLE",
            phase="COMPLETE",
            outcome="ROUTE_UNAVAILABLE",
            request_json=json.dumps({
                "origin_evidence": {
                    "journey_member_id": observed.journey_member_id,
                    "traveler_id": helper_id,
                    "source": "BUDDY_HANDOFF_ROUTE_PRIVATE",
                },
            }),
            result_json=json.dumps({
                "geometry": [[39.9042, 116.4074]],
                "classification": "SYNTHETIC_TEST_ONLY",
            }),
            error_json="{}",
            queued_at=captured,
            started_at=None,
            finished_at=captured,
            updated_at=captured,
        ))
        session.commit()

    endpoint = f"/api/v1/journeys/{journey_id}/routes/operations/{operation_id}"
    assert client.get(endpoint).status_code in {401, 403}
    for other_traveler in (bystander_id, requester_id):
        response = client.get(endpoint, headers=_auth(other_traveler))
        assert response.status_code == 404, response.text
        assert "39.9042" not in response.text
    own_response = client.get(endpoint, headers=_auth(helper_id))
    assert own_response.status_code == 200, own_response.text
    assert own_response.json()["result"]["classification"] == "SYNTHETIC_TEST_ONLY"

    # General grounded route acquisition is a write/provider entrypoint and
    # must not be callable anonymously in deployed signed mode either.
    unauthenticated_acquire = client.post(
        f"/api/v1/journeys/{journey_id}/routes/acquire", json={}
    )
    assert unauthenticated_acquire.status_code in {401, 403}


def test_r81_original_shared_stop_fallback_needs_no_second_route_operation():
    """An agreed handoff at the original stop is a one-leg navigation, not a broken two-leg read."""
    journey_id, helper_id, _bystander_id, _requester_id, handoff_id = _accepted_pair(
        use_original_stop=True
    )
    obs_id = _observation(journey_id, helper_id, handoff_id, 39.9042)
    endpoint = f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}/route"
    route = client.post(
        endpoint, headers=_auth(helper_id),
        json={
            "location_observation_id": obs_id,
            "requested_mode": "WALK",
            "external_provider_disclosure_confirmed": True,
        },
    )
    assert route.status_code == 200, route.text
    body = route.json()
    assert body["outbound_leg"]["route_state"] == "UNKNOWN"
    assert body["return_leg"]["route_state"] == "SAME_PUBLIC_DESTINATION"
    assert body["return_leg"]["duration_min"] == 0
    assert body["status"] == "UNAVAILABLE"
    assert body["duration_min"] is None
    operations = _private_route_operations(journey_id)
    assert len(operations) == 1
    assert set(
        json.loads(operation.request_json)["origin_evidence"]["route_leg"]
        for operation in operations.values()
    ) == {"TO_HANDOFF"}
    current = client.get(endpoint + "/current", headers=_auth(helper_id))
    assert current.status_code == 200, current.text
    assert current.json()["return_leg"]["route_state"] == "SAME_PUBLIC_DESTINATION"
    assert current.json()["shared_commitment_risk"]["state"] == "UNKNOWN"
