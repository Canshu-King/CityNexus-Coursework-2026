from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import app.main as main_module
from app.core.config import settings
from app.db.models import (
    Activity,
    AgentRun,
    AuditLog,
    Booking,
    Event,
    GuardianPulse,
    GuardianRun,
    Journey,
    JourneyMember,
    JourneyRendezvousMemberSignal,
    JourneyRendezvousSignalAcknowledgement,
    LocationObservation,
    PaymentTransaction,
    PlanVersion,
    ProviderCall,
    RouteAcquisitionOperation,
)
from app.db.session import SessionLocal
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
    selected = client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select", headers=_auth(owner_id)
    )
    assert selected.status_code == 200, selected.text
    started = client.post(f"/api/v1/journeys/{journey_id}/start", headers=_auth(owner_id))
    assert started.status_code == 200, started.text
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


def _signal_payload(presence: dict, signal_kind: str) -> dict:
    target = presence["rendezvous"]
    audience_json = json.dumps(
        sorted(member["member_id"] for member in presence["members"]),
        separators=(",", ":"),
    )
    expected_digest = hashlib.sha256(audience_json.encode("utf-8")).hexdigest()
    # Browser clients now use this server-signed-member audience digest instead
    # of requiring crypto.subtle on local-network HTTP devices.
    assert presence["coordination_audience_sha256"] == expected_digest
    return {
        "signal_kind": signal_kind,
        "expected_active_plan_version_id": presence["active_plan_version_id"],
        "expected_current_activity_id": presence["current_activity_id"],
        "expected_rendezvous_activity_id": target["activity_id"],
        "expected_public_place_id": target["place_id"],
        "expected_audience_sha256": presence["coordination_audience_sha256"],
    }


def _share(journey_id: str, traveler_id: str, presence: dict, kind: str, key: str):
    return client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-member-signals",
        headers={**_auth(traveler_id), "Idempotency-Key": key},
        json=_signal_payload(presence, kind),
    )


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
        Booking,
        PaymentTransaction,
    )
    with SessionLocal() as session:
        return {
            model.__tablename__: int(session.scalar(select(func.count()).select_from(model)) or 0)
            for model in models
        }


def test_member_status_is_voluntary_audience_bound_and_closes_help_acknowledgement():
    journey_id, owner_id = _active_journey()
    peer_id = _traveler("R78 existing companion")
    _join(journey_id, owner_id, peer_id)
    newcomer_id = _traveler("R78 new companion")
    _join(journey_id, owner_id, newcomer_id)
    late_id = _traveler("R78 later companion")

    initial = _presence(journey_id, newcomer_id)
    assert initial["coordination_signals"] == []
    counts_before = _counts()

    unauthenticated = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-member-signals",
        json=_signal_payload(initial, "HEADING_TO_RENDEZVOUS"),
    )
    assert unauthenticated.status_code == 401

    key = "r78-heading-" + journey_id
    duplicate_barrier = Barrier(2)

    def duplicate_share():
        duplicate_barrier.wait()
        return _share(
            journey_id, newcomer_id, initial, "HEADING_TO_RENDEZVOUS", key
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = [pool.submit(duplicate_share) for _ in range(2)]
        responses = [reply.result(timeout=10) for reply in replies]
    assert [response.status_code for response in responses] == [201, 201]
    signal_id = str(responses[0].json()["signal_id"])
    assert responses[1].json()["signal_id"] == signal_id
    assert responses[0].json()["evidence_level"] == "SELF_REPORTED_NOT_VERIFIED"

    for viewer_id in (owner_id, peer_id, newcomer_id):
        signals = _presence(journey_id, viewer_id)["coordination_signals"]
        assert [item["signal_id"] for item in signals] == [signal_id]
        assert signals[0]["signal_kind"] == "HEADING_TO_RENDEZVOUS"
        assert signals[0]["display_name"] == "R78 new companion"
        assert signals[0]["evidence_level"] == "SELF_REPORTED_NOT_VERIFIED"
        serialized = json.dumps(signals, sort_keys=True)
        for forbidden in (
            "location_observation_id", "latitude", "longitude", "route_geometry",
            "private_origin", "route_eta_minutes",
        ):
            assert forbidden not in serialized

    late_membership = client.get(
        f"/api/v1/travelers/me/journey-membership-invitations",
        headers=_auth(late_id),
    )
    assert late_membership.status_code == 200 and late_membership.json() == []
    late_operator_before = client.get(
        f"/api/v1/journeys/{journey_id}/operator", headers=_auth(late_id)
    )
    assert late_operator_before.status_code == 403
    _join(journey_id, owner_id, late_id)
    # A member-set change invalidates the old audience for future reads. D does
    # not receive C's prior disclosure, and the original viewers must refresh.
    assert _presence(journey_id, late_id)["coordination_signals"] == []
    assert _presence(journey_id, owner_id)["coordination_signals"] == []
    assert _presence(journey_id, newcomer_id)["coordination_signals"] == []
    stale_audience_share = _share(
        journey_id, newcomer_id, initial,
        "HEADING_TO_RENDEZVOUS", "r78-stale-audience-" + journey_id,
    )
    assert stale_audience_share.status_code == 409
    assert "AUDIENCE_CHANGED" in stale_audience_share.text

    renewed_presence = _presence(journey_id, newcomer_id)
    renewed = _share(
        journey_id, newcomer_id, renewed_presence,
        "HEADING_TO_RENDEZVOUS", "r78-heading-after-membership-" + journey_id,
    )
    assert renewed.status_code == 201, renewed.text
    renewed_id = str(renewed.json()["signal_id"])
    assert renewed_id != signal_id
    late_signals = _presence(journey_id, late_id)["coordination_signals"]
    assert [item["signal_id"] for item in late_signals] == [renewed_id]
    assert late_signals[0]["signal_kind"] == "HEADING_TO_RENDEZVOUS"

    withdrawn = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-member-signals/{renewed_id}/withdraw",
        headers=_auth(newcomer_id),
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "REVOKED"
    assert _presence(journey_id, owner_id)["coordination_signals"] == []

    help_presence = _presence(journey_id, newcomer_id)
    help_request = _share(
        journey_id, newcomer_id, help_presence,
        "NEEDS_COORDINATION", "r78-help-" + journey_id,
    )
    assert help_request.status_code == 201, help_request.text
    help_id = str(help_request.json()["signal_id"])
    for viewer_id in (owner_id, peer_id, late_id):
        signal = _presence(journey_id, viewer_id)["coordination_signals"][0]
        assert signal["signal_id"] == help_id
        assert signal["signal_kind"] == "NEEDS_COORDINATION"
        assert signal["viewer_can_acknowledge"] is True

    owner_ack = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-member-signals/{help_id}/acknowledge",
        headers=_auth(owner_id),
    )
    assert owner_ack.status_code == 200, owner_ack.text
    assert owner_ack.json()["responder_display_name"]
    replayed_ack = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-member-signals/{help_id}/acknowledge",
        headers=_auth(owner_id),
    )
    assert replayed_ack.status_code == 200
    assert replayed_ack.json()["acknowledged_at"] == owner_ack.json()["acknowledged_at"]
    other_ack = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-member-signals/{help_id}/acknowledge",
        headers=_auth(peer_id),
    )
    assert other_ack.status_code == 409
    assert "ALREADY_ACKNOWLEDGED" in other_ack.text

    newcomer_signals = _presence(journey_id, newcomer_id)["coordination_signals"]
    assert len(newcomer_signals) == 1
    assert isinstance(newcomer_signals[0]["acknowledgement"]["display_name"], str)
    assert newcomer_signals[0]["acknowledgement"]["display_name"]
    assert newcomer_signals[0]["acknowledgement"]["evidence_level"] == "SIGNED_ACKNOWLEDGEMENT_NOT_PHYSICAL_PRESENCE"
    assert newcomer_signals[0]["evidence_level"] == "SELF_REPORTED_NOT_VERIFIED"

    own_ack = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-member-signals/{help_id}/acknowledge",
        headers=_auth(newcomer_id),
    )
    assert own_ack.status_code == 409
    assert "CANNOT_ACKNOWLEDGE_OWN_REQUEST" in own_ack.text
    bad_signal = client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-member-signals",
        headers={**_auth(newcomer_id), "Idempotency-Key": "r78-forged-member-" + journey_id},
        json={**_signal_payload(help_presence, "HEADING_TO_RENDEZVOUS"), "member_id": "forged"},
    )
    assert bad_signal.status_code == 422

    # The active plan/cursor and provider/agent/arrival records do not move.
    assert _counts() == counts_before
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        current = session.scalar(select(func.count()).select_from(JourneyRendezvousMemberSignal).where(
            JourneyRendezvousMemberSignal.journey_id == journey_id,
            JourneyRendezvousMemberSignal.status == "ACTIVE",
        ))
        assert current == 1
        cursor_after = str(journey.current_activity_id or "")
        assert session.scalar(select(func.count()).select_from(JourneyRendezvousSignalAcknowledgement).where(
            JourneyRendezvousSignalAcknowledgement.signal_id == help_id,
        )) == 1
        assert session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.journey_id == journey_id,
            AuditLog.action.in_((
                "journey.rendezvous_signal.shared",
                "journey.rendezvous_signal.withdrawn",
                "journey.rendezvous_signal.help_acknowledged",
            )),
        )) == 5
        plan_before = str(journey.active_plan_version_id)
    assert plan_before == help_presence["active_plan_version_id"]
    assert cursor_after == str(help_presence["current_activity_id"] or "")


def test_signal_target_cas_rejects_stale_page_and_expiry_releases_current_slot():
    journey_id, owner_id = _active_journey()
    peer_id = _traveler("R78 CAS companion")
    _join(journey_id, owner_id, peer_id)
    newcomer_id = _traveler("R78 CAS newcomer")
    _join(journey_id, owner_id, newcomer_id)

    stale_view = _presence(journey_id, newcomer_id)
    first = _share(
        journey_id, newcomer_id, stale_view,
        "HEADING_TO_RENDEZVOUS", "r78-cas-first-" + journey_id,
    )
    assert first.status_code == 201, first.text
    first_id = str(first.json()["signal_id"])

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        alternative = session.scalar(select(Activity).where(
            Activity.plan_version_id == journey.active_plan_version_id,
            Activity.id != journey.current_activity_id,
        ).order_by(Activity.sequence_no, Activity.id))
        if alternative is None:
            alternative = session.scalar(select(Activity).where(
                Activity.plan_version_id == journey.active_plan_version_id,
            ).order_by(Activity.sequence_no, Activity.id))
        assert alternative is not None
        journey.current_activity_id = str(alternative.id)
        session.commit()

    stale_post = _share(
        journey_id, newcomer_id, stale_view,
        "NEEDS_COORDINATION", "r78-cas-stale-" + journey_id,
    )
    assert stale_post.status_code == 409
    assert "CONTEXT_CHANGED" in stale_post.text
    assert _presence(journey_id, owner_id)["coordination_signals"] == []

    current_view = _presence(journey_id, newcomer_id)
    # A changed target/cursor makes the previous disclosure stale. An explicit
    # new share is required and does not reuse its idempotency key.
    fresh = _share(
        journey_id, newcomer_id, current_view,
        "NEEDS_COORDINATION", "r78-cas-fresh-" + journey_id,
    )
    assert fresh.status_code == 201, fresh.text
    fresh_id = str(fresh.json()["signal_id"])
    with SessionLocal() as session:
        old = session.get(JourneyRendezvousMemberSignal, first_id)
        assert old is not None and old.status == "STALE"
        new = session.get(JourneyRendezvousMemberSignal, fresh_id)
        assert new is not None and new.status == "ACTIVE"
        fresh_member_id = str(new.journey_member_id)
        new.expires_at = "2000-01-01T00:00:00+00:00"
        session.commit()

    assert _presence(journey_id, owner_id)["coordination_signals"] == []
    refreshed = _share(
        journey_id, newcomer_id, current_view,
        "HEADING_TO_RENDEZVOUS", "r78-cas-after-expiry-" + journey_id,
    )
    assert refreshed.status_code == 201, refreshed.text
    assert str(refreshed.json()["signal_id"]) != fresh_id
    with SessionLocal() as session:
        expired = session.get(JourneyRendezvousMemberSignal, fresh_id)
        assert expired is not None and expired.status == "EXPIRED"
        assert session.scalar(select(func.count()).select_from(JourneyRendezvousMemberSignal).where(
            JourneyRendezvousMemberSignal.journey_id == journey_id,
            JourneyRendezvousMemberSignal.journey_member_id == fresh_member_id,
            JourneyRendezvousMemberSignal.status == "ACTIVE",
        )) == 1
        # The partial unique index is the database backstop across processes,
        # independently of the in-process Journey lock.
        current = session.scalar(select(JourneyRendezvousMemberSignal).where(
            JourneyRendezvousMemberSignal.journey_id == journey_id,
            JourneyRendezvousMemberSignal.journey_member_id == fresh_member_id,
            JourneyRendezvousMemberSignal.status == "ACTIVE",
        ))
        assert current is not None
        duplicate = JourneyRendezvousMemberSignal(
            id=str(uuid4()),
            journey_id=current.journey_id,
            journey_member_id=current.journey_member_id,
            traveler_id=current.traveler_id,
            active_plan_version_id=current.active_plan_version_id,
            current_activity_id=current.current_activity_id,
            rendezvous_activity_id=current.rendezvous_activity_id,
            public_place_id=current.public_place_id,
            signal_kind=current.signal_kind,
            status="ACTIVE",
            audience_member_ids_json=current.audience_member_ids_json,
            idempotency_key="r78-db-collision-" + str(uuid4()),
            request_hash=current.request_hash,
            reported_at=current.reported_at,
            expires_at=current.expires_at,
            revoked_at=None,
            stale_at=None,
            created_at=current.created_at,
            updated_at=current.updated_at,
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_r78_old_tab_cannot_claim_new_active_status_was_withdrawn():
    """A stale signal id must not tell the traveler their NEW status was stopped."""
    journey_id, owner_id = _active_journey()
    companion_id = _traveler("R78 stale-tab companion")
    _join(journey_id, owner_id, companion_id)
    view = _presence(journey_id, companion_id)

    first = _share(
        journey_id, companion_id, view, "HEADING_TO_RENDEZVOUS",
        "r78-stale-tab-first-" + journey_id,
    )
    assert first.status_code == 201, first.text
    first_id = str(first.json()["signal_id"])
    next_share = _share(
        journey_id, companion_id, view, "NEEDS_COORDINATION",
        "r78-stale-tab-second-" + journey_id,
    )
    assert next_share.status_code == 201, next_share.text
    second_id = str(next_share.json()["signal_id"])
    assert second_id != first_id

    url = f"/api/v1/journeys/{journey_id}/rendezvous-member-signals"
    old_tab_withdraw = client.post(f"{url}/{first_id}/withdraw", headers=_auth(companion_id))
    assert old_tab_withdraw.status_code == 409, old_tab_withdraw.text
    assert "RENDEZVOUS_SIGNAL_WITHDRAW_TARGET_STALE" in old_tab_withdraw.text
    assert [item["signal_id"] for item in _presence(journey_id, owner_id)["coordination_signals"]] == [second_id]

    other_member_withdraw = client.post(f"{url}/{second_id}/withdraw", headers=_auth(owner_id))
    assert other_member_withdraw.status_code == 404

    stopped = client.post(f"{url}/{second_id}/withdraw", headers=_auth(companion_id))
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "REVOKED"
    assert client.post(f"{url}/{second_id}/withdraw", headers=_auth(companion_id)).status_code == 200
    assert _presence(journey_id, owner_id)["coordination_signals"] == []

    newer = _share(
        journey_id, companion_id, view, "HEADING_TO_RENDEZVOUS",
        "r78-stale-tab-third-" + journey_id,
    )
    assert newer.status_code == 201, newer.text
    newer_id = str(newer.json()["signal_id"])
    after_new_share = client.post(f"{url}/{second_id}/withdraw", headers=_auth(companion_id))
    assert after_new_share.status_code == 409, after_new_share.text
    assert "RENDEZVOUS_SIGNAL_WITHDRAW_TARGET_STALE" in after_new_share.text
    assert [item["signal_id"] for item in _presence(journey_id, owner_id)["coordination_signals"]] == [newer_id]
