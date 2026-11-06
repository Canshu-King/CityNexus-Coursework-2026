from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Activity, Journey, JourneyTwin, PlanVersion, ReplanProposal
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)


def _selected(start: bool = False):
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    jid = ids["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{jid}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    assert client.post(f"/api/v1/journeys/{jid}/plans/{plan['id']}/select").status_code == 200
    if start:
        assert client.post(f"/api/v1/journeys/{jid}/start").status_code == 200
    return ids, plan


def test_start_and_completion_persist_journey_twin_projection():
    ids, _plan = _selected(start=False)
    jid = ids["journey_id"]
    started = client.post(f"/api/v1/journeys/{jid}/start")
    assert started.status_code == 200
    current_id = started.json()["current_activity_id"]
    with SessionLocal() as session:
        twin = session.get(JourneyTwin, jid)
        assert twin is not None
        assert twin.current_activity_id == current_id
    completed = client.post(f"/api/v1/journeys/{jid}/activities/{current_id}/complete")
    assert completed.status_code == 200
    next_id = completed.json()["current_activity_id"]
    with SessionLocal() as session:
        twin = session.get(JourneyTwin, jid)
        assert twin is not None
        assert twin.current_activity_id == next_id


def test_active_journey_mutation_cannot_break_active_plan():
    ids, _ = _selected(start=True)
    jid = ids["journey_id"]
    patch = client.patch(
        f"/api/v1/journeys/{jid}",
        json={"budget": {"amount_minor": 1, "currency": "CNY"}},
    )
    assert patch.status_code == 422
    hard = client.post(
        f"/api/v1/journeys/{jid}/constraints",
        json={
            "kind": "HARD",
            "category": "budget",
            "rule_code": "BUDGET_MAX",
            "params": {"amount_minor": 1, "currency": "CNY"},
            "weight": 1.0,
        },
    )
    assert hard.status_code == 422


def test_replan_requires_active_journey():
    ids, _ = _selected(start=False)
    jid = ids["journey_id"]
    event = client.post(f"/api/v1/journeys/{jid}/events", json={"event_type": "CLOSURE", "payload": {"at_current_location": True}}).json()
    response = client.post(
        f"/api/v1/journeys/{jid}/replans", json={"trigger_event_id": event["id"]}
    )
    assert response.status_code == 409


# RETIRED (RX-1 absorption): test_replan_trigger_event_must_belong_to_same_journey
# asserted only `status_code == 422`, and its WEATHER event carries no affected
# activity, so target resolution answered 422 regardless of the journey scope guard.
# Replaced by tests/test_rx1_cross_journey_trigger_gate.py, which reaches the guard
# with a real CLOSURE event, asserts the guard's own message, proves the same event
# is actionable on its own journey, and documents why the old assertion was vacuous.


def test_replan_accept_remaps_current_activity_to_new_active_plan():
    ids, _ = _selected(start=True)
    jid = ids["journey_id"]
    event = client.post(f"/api/v1/journeys/{jid}/events", json={"event_type": "CLOSURE", "payload": {"at_current_location": True}}).json()
    proposal = client.post(
        f"/api/v1/journeys/{jid}/replans", json={"trigger_event_id": event["id"]}
    ).json()
    accepted = client.post(
        f"/api/v1/replans/{proposal['id']}/accept", headers={"Idempotency-Key": "wp05-accept-0002"}
    )
    assert accepted.status_code == 200
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        current = session.get(Activity, journey.current_activity_id)
        assert current is not None
        assert current.plan_version_id == journey.active_plan_version_id
        active_plan = session.get(PlanVersion, journey.active_plan_version_id)
        assert active_plan.status == "ACTIVE"
        assert journey.status == "ACTIVE"


def test_replan_proposal_is_presented_before_acceptance():
    ids, _ = _selected(start=True)
    jid = ids["journey_id"]
    event = client.post(f"/api/v1/journeys/{jid}/events", json={"event_type": "CLOSURE", "payload": {"at_current_location": True}}).json()
    proposal = client.post(
        f"/api/v1/journeys/{jid}/replans", json={"trigger_event_id": event["id"]}
    )
    assert proposal.status_code == 201
    assert proposal.json()["status"] == "PRESENTED"
    accepted = client.post(
        f"/api/v1/replans/{proposal.json()['id']}/accept",
        headers={"Idempotency-Key": "wp05-accept-0003"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["proposal"]["status"] == "ACCEPTED"


def test_replan_accept_revalidates_durable_c4_commitment_anchor():
    ids, plan = _selected(start=False)
    jid = ids["journey_id"]
    cruise = next(a for a in plan["activities"] if a["place_id"] == "place-river-cruise")
    booking = client.post(
        f"/api/v1/journeys/{jid}/bookings", json={"activity_id": cruise["id"]}
    ).json()
    capture = client.post(
        f"/api/v1/journeys/{jid}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
        },
        headers={"Idempotency-Key": "REDACTED_FOR_PUBLIC_RELEASE"},
    )
    assert capture.status_code == 201
    assert client.post(f"/api/v1/journeys/{jid}/start").status_code == 200
    event = client.post(f"/api/v1/journeys/{jid}/events", json={"event_type": "CLOSURE", "payload": {"at_current_location": True}}).json()
    proposal = client.post(
        f"/api/v1/journeys/{jid}/replans", json={"trigger_event_id": event["id"]}
    ).json()
    with SessionLocal() as session:
        p = session.get(ReplanProposal, proposal["id"])
        proposed_cruise = session.scalar(
            select(Activity).where(
                Activity.plan_version_id == p.proposed_plan_version_id,
                Activity.place_id == "place-river-cruise",
            )
        )
        proposed_cruise.start_at = "2026-10-10T16:00:00+08:00"
        proposed_cruise.end_at = "2026-10-10T17:00:00+08:00"
        session.commit()
    accepted = client.post(
        f"/api/v1/replans/{proposal['id']}/accept",
        headers={"Idempotency-Key": "wp05-accept-c4-stale-0001"},
    )
    assert accepted.status_code == 409
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        base = session.get(PlanVersion, proposal["base_plan_version_id"])
        assert journey.active_plan_version_id == base.id
        assert base.status == "ACTIVE"


def test_canonical_closure_replan_replaces_tech_art_and_preserves_cruise():
    ids, plan = _selected(start=False)
    jid = ids["journey_id"]
    cruise = next(a for a in plan["activities"] if a["place_id"] == "place-river-cruise")
    tech = next(a for a in plan["activities"] if a["place_id"] == "place-tech-art")
    booking = client.post(
        f"/api/v1/journeys/{jid}/bookings", json={"activity_id": cruise["id"]}
    ).json()
    assert (
        client.post(
            f"/api/v1/journeys/{jid}/transactions",
            json={
                "booking_id": booking["id"],
                "transaction_type": "CAPTURE",
                "amount": booking["amount"],
            },
            headers={"Idempotency-Key": "golden-closure-c4-0001"},
        ).status_code
        == 201
    )
    assert client.post(f"/api/v1/journeys/{jid}/start").status_code == 200
    event = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={"event_type": "CLOSURE", "severity": "MAJOR", "affected_activity_id": tech["id"]},
    )
    assert event.status_code == 201
    proposal = client.post(
        f"/api/v1/journeys/{jid}/replans", json={"trigger_event_id": event.json()["id"]}
    )
    assert proposal.status_code == 201, proposal.text
    body = proposal.json()
    assert body["locked_commitments_preserved"] is True
    assert "place-tech-art" in body["diff"]["removed"]
    assert "place-design-library" in body["diff"]["added"]
    with SessionLocal() as session:
        record = session.get(ReplanProposal, body["id"])
        proposed_places = [
            a.place_id
            for a in session.scalars(
                select(Activity)
                .where(Activity.plan_version_id == record.proposed_plan_version_id)
                .order_by(Activity.sequence_no)
            ).all()
        ]
        assert "place-tech-art" not in proposed_places
        assert "place-design-library" in proposed_places
        proposed_cruise = session.scalar(
            select(Activity).where(
                Activity.plan_version_id == record.proposed_plan_version_id,
                Activity.place_id == "place-river-cruise",
            )
        )
        assert datetime.fromisoformat(proposed_cruise.start_at) == datetime.fromisoformat(
            "2026-10-10T15:30:00+08:00"
        )
