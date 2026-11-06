from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.db.models import Activity, Commitment, Event, Journey, JourneyLeg
from app.db.session import SessionLocal
from app.integrations.mobility.snapshot_capture import capture_mobility_snapshot
from app.replanning.provider_snapshots import LIVE_PROVIDER_CAPTURE
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)
TERMINAL = {"COMPLETED", "SKIPPED", "CANCELLED"}


class _TestLiveProvider:
    provider_key = "valhalla-test"
    capture_provenance = LIVE_PROVIDER_CAPTURE
    time_basis = "PROVIDER_CAPTURED_AT"

    def estimate_minutes(self, origin_key: str, destination_key: str) -> int:
        return 1 if origin_key != destination_key else 0

    def fact_quality(self, origin_key: str, destination_key: str) -> str:
        return LIVE_PROVIDER_CAPTURE


def test_active_closure_can_accept_terminal_prefix_replan(monkeypatch) -> None:
    """A closure of the current stop may legitimately end the remaining trip.

    The planner has already produced the child plan; this test makes its suffix
    terminal to model the real ``F1_OPTIONAL_REMOVAL`` shape observed in the
    Kunming run.  Acceptance must carry authority to that child, skip the
    disrupted base activity, and finish the Journey instead of rejecting the
    child because it has no replacement cursor.
    """

    def capture_runtime(_session, _journey, pairs, *, operation_key, captured_at=None):
        return capture_mobility_snapshot(
            pairs,
            primary=_TestLiveProvider(),
            captured_at=captured_at,
            operation_key=operation_key,
        )

    monkeypatch.setattr(main_module, "capture_runtime_mobility_snapshot", capture_runtime)
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()[
        "journey_id"
    ]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert generated.status_code == 201, generated.text
    base_plan = generated.json()[0]
    assert len(base_plan["activities"]) >= 2
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{base_plan['id']}/select"
    ).status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    first = base_plan["activities"][0]["id"]
    completed = client.post(f"/api/v1/journeys/{journey_id}/activities/{first}/complete")
    assert completed.status_code == 200, completed.text
    current = client.get(f"/api/v1/journeys/{journey_id}").json()["current_activity_id"]
    assert current and current != first
    with SessionLocal() as session:
        for activity in session.scalars(
            select(Activity).where(Activity.plan_version_id == base_plan["id"])
        ):
            activity.locked = False
        for commitment in session.scalars(
            select(Commitment).where(Commitment.journey_id == journey_id)
        ):
            # This recovery case intentionally has no protected booking promise,
            # matching the real Kunming coursework Journey.
            commitment.status = "CANCELLED"
        session.commit()

    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": current,
            "payload": {"disruption_class": "COURSEWORK_CONTROLLED_CLOSURE"},
        },
    )
    assert event.status_code == 201, event.text
    event_id = event.json()["id"]
    proposal_response = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event_id, "candidate_count": 1},
        headers={"Idempotency-Key": f"r14-terminal-prefix-{uuid4()}"},
    )
    assert proposal_response.status_code == 201, proposal_response.text
    proposal = proposal_response.json()

    # Model the deterministic planner's terminal child shape without writing
    # authority state directly: the child remains a candidate and acceptance
    # below still goes through the Agent Gateway and the shared authority gate.
    with SessionLocal() as session:
        child_activities = list(
            session.scalars(
                select(Activity)
                .where(Activity.plan_version_id == proposal["proposed_plan_version_id"])
                .order_by(Activity.sequence_no)
            )
        )
        assert child_activities
        assert any(item.status not in TERMINAL for item in child_activities)
        base_current = session.get(Activity, current)
        assert base_current is not None
        for leg in session.scalars(
            select(JourneyLeg).where(
                JourneyLeg.plan_version_id == proposal["proposed_plan_version_id"]
            )
        ):
            # The terminal prefix has no unresolved inter-stop movement.
            session.delete(leg)
        for item in child_activities:
            item.status = "COMPLETED"
        session.commit()
    with SessionLocal() as session:
        base_journey = session.get(Journey, journey_id)
        base_current = session.get(Activity, current)
        trigger = session.get(Event, event_id)
        proposed_rows = list(
            session.scalars(
                select(Activity).where(
                    Activity.plan_version_id == proposal["proposed_plan_version_id"]
                )
            )
        )
        assert base_journey is not None and base_journey.current_activity_id == current
        assert base_current is not None and base_current.status not in TERMINAL
        assert trigger is not None and trigger.affected_activity_id == current
        assert trigger.event_type == "CLOSURE"
        assert proposed_rows and all(item.status in TERMINAL for item in proposed_rows)

    accepted = client.post(
        f"/api/v1/replans/{proposal['id']}/accept",
        headers={"Idempotency-Key": f"r14-terminal-accept-{uuid4()}"},
    )
    assert accepted.status_code == 200, accepted.text
    result = accepted.json()
    assert result["journey"]["status"] == "COMPLETED", result
    assert result["journey"]["active_plan_version_id"] == proposal[
        "proposed_plan_version_id"
    ]
    assert result["journey"]["current_activity_id"] is None
    assert result["proposal"]["status"] == "ACCEPTED"

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        old_current = session.get(Activity, current)
        assert old_current is not None
        assert old_current.status == "SKIPPED"
        assert json.loads(old_current.metadata_json)["skip_reason"] == (
            "ACTIVE_ACTIVITY_DISRUPTED_AND_REMOVED"
        )
        assert journey.status == "COMPLETED"
        assert journey.current_activity_id is None
