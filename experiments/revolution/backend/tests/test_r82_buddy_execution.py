from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.db.models import (
    AgentRun,
    GuardianPulse,
    GuardianRun,
    Journey,
    JourneyOutcomeEvent,
    JourneyRendezvousBuddyExecution,
    JourneyRendezvousBuddyExecutionReceipt,
    JourneyRendezvousMemberSignal,
    LocationObservation,
    PlanVersion,
    ProviderCall,
    RouteAcquisitionOperation,
)
from app.db.session import SessionLocal
from app.execution.clock import execution_clock
from test_r80_private_buddy_route import _accepted_pair, _auth, _join, _new_traveler, client

TEST_SECRET = "REDACTED_FOR_PUBLIC_RELEASE"


@pytest.fixture(autouse=True)
def _execution_environment():
    previous_secret = settings.traveler_auth_secret
    previous_provider = settings.live_valhalla_url
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    object.__setattr__(settings, "live_valhalla_url", None)
    try:
        yield
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        object.__setattr__(settings, "live_valhalla_url", previous_provider)


def _window_offer(journey_id: str, handoff_id: str, helper_id: str, minutes: int = 60):
    return client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}/execution-window",
        headers={**_auth(helper_id), "Idempotency-Key": "r82-window-" + str(uuid4())},
        json={"execution_window_minutes": minutes},
    )


def _window_accept(journey_id: str, handoff_id: str, requester_id: str):
    return client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}/execution-window/accept",
        headers=_auth(requester_id),
    )


def _report(journey_id: str, handoff_id: str, execution_id: str, traveler_id: str,
            receipt_kind: str, key: str | None = None):
    return client.post(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}/execution/{execution_id}/report",
        headers={**_auth(traveler_id), "Idempotency-Key": key or "r82-report-" + str(uuid4())},
        json={"receipt_kind": receipt_kind},
    )


def _current(journey_id: str, traveler_id: str) -> dict:
    response = client.get(
        f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/current",
        headers=_auth(traveler_id),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _counts(models: tuple[type, ...]) -> dict[str, int]:
    with SessionLocal() as session:
        return {
            model.__tablename__: int(session.scalar(select(func.count()).select_from(model)) or 0)
            for model in models
        }


def test_r82_pair_lease_survives_offer_and_signal_expiry_and_requires_both_signed_reports():
    start = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    with execution_clock.freeze(start):
        journey_id, helper_id, bystander_id, requester_id, handoff_id = _accepted_pair()
        offered = _window_offer(journey_id, handoff_id, helper_id)
        assert offered.status_code == 200, offered.text
        assert offered.json()["status"] == "WINDOW_OFFERED"
        activated = _window_accept(journey_id, handoff_id, requester_id)
        assert activated.status_code == 200, activated.text
        execution = activated.json()
        execution_id = str(execution["execution_id"])
        assert execution["status"] == "ACTIVE"
        assert execution["execution_window_minutes"] == 60
        assert datetime.fromisoformat(execution["expires_at"]) == start + timedelta(minutes=60)
        before_operator = client.get(
            f"/api/v1/journeys/{journey_id}/operator", headers=_auth(helper_id)
        ).json()
        before_plan_id = before_operator["group_presence"]["active_plan_version_id"]
        before_cursor = before_operator["group_presence"]["current_activity_id"]

    with execution_clock.freeze(start + timedelta(minutes=25)):
        # A member added later cannot discover the original pair-private details,
        # and does not replace the pair's already consented execution audience.
        later_member_id = _new_traveler("R82 later member D")
        _join(journey_id, helper_id, later_member_id)
        pair_view = _current(journey_id, helper_id)["items"][0]
        assert pair_view["execution"]["execution_id"] == execution_id
        assert pair_view["execution"]["status"] == "ACTIVE"
        assert pair_view["execution"]["context_current"] is True
        assert _current(journey_id, later_member_id) == {"items": []}
        later_operator = client.get(
            f"/api/v1/journeys/{journey_id}/operator", headers=_auth(later_member_id)
        ).json()
        assert handoff_id not in str(later_operator)
        assert execution_id not in str(later_operator)

        # A still-current pair lease can support a new caller-owned R81 sample
        # after the original 20-minute handoff deadline; the binding remains
        # capped by the new sample's freshness and the finite execution lease.
        location = client.post(
            f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}/route/location",
            headers=_auth(helper_id),
            json={
                "latitude": 39.91,
                "longitude": 116.40,
                "accuracy_m": 12,
                "captured_at": (start + timedelta(minutes=25)).isoformat(),
            },
        )
        assert location.status_code == 201, location.text
        assert location.json()["external_provider_contacted"] is False
        assert location.json()["visibility"] == "CALLER_PRIVATE"

    no_side_effects = (
        PlanVersion, LocationObservation, RouteAcquisitionOperation, ProviderCall,
        AgentRun, GuardianRun, GuardianPulse, JourneyOutcomeEvent,
    )
    side_effect_counts = _counts(no_side_effects)
    with execution_clock.freeze(start + timedelta(minutes=50)):
        with SessionLocal() as session:
            signal = session.scalar(select(JourneyRendezvousMemberSignal).where(
                JourneyRendezvousMemberSignal.journey_id == journey_id,
            ))
            assert signal is not None
            assert datetime.fromisoformat(signal.expires_at) < start + timedelta(minutes=50)

        # The proposal has expired at 20 minutes and the help signal at 45,
        # while the mutually accepted 60-minute lease remains in force.
        current = _current(journey_id, helper_id)["items"][0]["execution"]
        assert current["execution_id"] == execution_id
        assert current["status"] == "ACTIVE"
        assert current["can_report"] is True

        unauthorized = _report(
            journey_id, handoff_id, execution_id, later_member_id, "BUDDY_MET_ACK"
        )
        assert unauthorized.status_code == 404

        helper_key = "r82-helper-ack-" + str(uuid4())
        helper_ack = _report(
            journey_id, handoff_id, execution_id, helper_id, "BUDDY_MET_ACK", helper_key
        )
        assert helper_ack.status_code == 200, helper_ack.text
        assert helper_ack.json()["status"] == "ACTIVE"
        assert helper_ack.json()["helper_confirmed"] is True
        assert helper_ack.json()["requester_confirmed"] is False
        assert helper_ack.json()["can_return_to_shared_stop"] is False

        requester_ack = _report(
            journey_id, handoff_id, execution_id, requester_id, "BUDDY_MET_ACK"
        )
        assert requester_ack.status_code == 200, requester_ack.text
        assert requester_ack.json()["status"] == "MUTUALLY_ACKNOWLEDGED"
        assert requester_ack.json()["outcome_code"] == "MUTUALLY_ACKNOWLEDGED_ASSIST_NOT_PHYSICALLY_VERIFIED"
        assert requester_ack.json()["physical_arrival_verified"] is False
        assert requester_ack.json()["can_return_to_shared_stop"] is True

        replay = _report(
            journey_id, handoff_id, execution_id, helper_id, "BUDDY_MET_ACK", helper_key
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["status"] == "MUTUALLY_ACKNOWLEDGED"
        assert _counts(no_side_effects) == side_effect_counts

        after_operator = client.get(
            f"/api/v1/journeys/{journey_id}/operator", headers=_auth(helper_id)
        ).json()
        assert after_operator["group_presence"]["active_plan_version_id"] == before_plan_id
        assert after_operator["group_presence"]["current_activity_id"] == before_cursor

    with SessionLocal() as session:
        execution = session.get(JourneyRendezvousBuddyExecution, execution_id)
        assert execution is not None
        assert execution.status == "MUTUALLY_ACKNOWLEDGED"
        assert session.scalar(select(func.count()).select_from(JourneyRendezvousBuddyExecutionReceipt).where(
            JourneyRendezvousBuddyExecutionReceipt.execution_id == execution_id,
        )) == 2


def test_r82_legacy_handoff_upgrade_requires_requesters_separate_signature():
    start = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
    with execution_clock.freeze(start):
        journey_id, helper_id, _bystander_id, requester_id, handoff_id = _accepted_pair()
        offered = _window_offer(journey_id, handoff_id, helper_id, 90)
        assert offered.status_code == 200, offered.text
        proposal = offered.json()
        assert proposal["status"] == "WINDOW_OFFERED"
        assert proposal["requester_consented_at"] is None
        assert proposal["can_report"] is False

        wrong_actor = _window_accept(journey_id, handoff_id, helper_id)
        assert wrong_actor.status_code == 404
        assert _current(journey_id, helper_id)["items"][0]["execution"]["status"] == "WINDOW_OFFERED"

        requester_view = _current(journey_id, requester_id)["items"][0]["execution"]
        assert requester_view["can_accept_window"] is True
        accepted = _window_accept(journey_id, handoff_id, requester_id)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == "ACTIVE"
        assert accepted.json()["requester_consented_at"] == accepted.json()["started_at"]
        assert accepted.json()["expires_at"] == (start + timedelta(minutes=90)).isoformat()


def test_r82_unaccepted_window_expires_at_original_r79_deadline():
    start = datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc)
    with execution_clock.freeze(start):
        journey_id, helper_id, _bystander_id, requester_id, handoff_id = _accepted_pair()
        offered = _window_offer(journey_id, handoff_id, helper_id, 30)
        assert offered.status_code == 200, offered.text
        execution_id = offered.json()["execution_id"]
        proposal_expiry = datetime.fromisoformat(offered.json()["proposal_expires_at"])
        assert proposal_expiry == start + timedelta(minutes=20)

    with execution_clock.freeze(proposal_expiry + timedelta(seconds=1)):
        accepted = _window_accept(journey_id, handoff_id, requester_id)
        assert accepted.status_code == 409
        assert "WINDOW_EXPIRED" in accepted.text
        with SessionLocal() as session:
            execution = session.get(JourneyRendezvousBuddyExecution, execution_id)
            assert execution is not None and execution.status == "EXPIRED"
            assert execution.started_at is None
            assert execution.requester_consented_at is None


def test_r82_explicit_revocation_cancels_pair_lease_without_resurrection():
    start = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)
    with execution_clock.freeze(start):
        journey_id, helper_id, _bystander_id, requester_id, handoff_id = _accepted_pair()
        offered = _window_offer(journey_id, handoff_id, helper_id, 60)
        assert offered.status_code == 200, offered.text
        execution_id = offered.json()["execution_id"]
        accepted = _window_accept(journey_id, handoff_id, requester_id)
        assert accepted.status_code == 200, accepted.text

        revoked = client.post(
            f"/api/v1/journeys/{journey_id}/rendezvous-buddy-handoffs/{handoff_id}/revoke",
            headers=_auth(requester_id),
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["status"] == "REVOKED"
        assert _current(journey_id, helper_id) == {"items": []}
        late_report = _report(
            journey_id, handoff_id, execution_id, helper_id, "BUDDY_MET_ACK"
        )
        assert late_report.status_code == 409

        with SessionLocal() as session:
            execution = session.get(JourneyRendezvousBuddyExecution, execution_id)
            assert execution is not None
            assert execution.status == "CANCELLED"
            assert execution.outcome_code == "HANDOFF_CLOSED_BY_PARTICIPANT"


def test_r82_cannot_meet_is_one_members_replan_receipt_not_a_pair_completion():
    start = datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)
    with execution_clock.freeze(start):
        journey_id, helper_id, _bystander_id, requester_id, handoff_id = _accepted_pair()
        offered = _window_offer(journey_id, handoff_id, helper_id, 60)
        assert offered.status_code == 200, offered.text
        execution_id = offered.json()["execution_id"]
        accepted = _window_accept(journey_id, handoff_id, requester_id)
        assert accepted.status_code == 200, accepted.text

        cannot_meet = _report(journey_id, handoff_id, execution_id, helper_id, "CANNOT_MEET")
        assert cannot_meet.status_code == 200, cannot_meet.text
        assert cannot_meet.json()["status"] == "REPLAN_REQUIRED"
        assert cannot_meet.json()["helper_receipt_kind"] == "CANNOT_MEET"
        assert cannot_meet.json()["requester_receipt_kind"] is None
        assert cannot_meet.json()["physical_arrival_verified"] is False
        assert cannot_meet.json()["can_return_to_shared_stop"] is True

        requester_view = _current(journey_id, requester_id)["items"][0]["execution"]
        assert requester_view["status"] == "REPLAN_REQUIRED"
        assert requester_view["helper_receipt_kind"] == "CANNOT_MEET"
        assert requester_view["requester_receipt_kind"] is None
        rejected_ack = _report(
            journey_id, handoff_id, execution_id, requester_id, "BUDDY_MET_ACK"
        )
        assert rejected_ack.status_code == 409

        with SessionLocal() as session:
            execution = session.get(JourneyRendezvousBuddyExecution, execution_id)
            assert execution is not None
            assert execution.status == "REPLAN_REQUIRED"
            assert session.scalar(select(func.count()).select_from(JourneyRendezvousBuddyExecutionReceipt).where(
                JourneyRendezvousBuddyExecutionReceipt.execution_id == execution_id,
            )) == 1
