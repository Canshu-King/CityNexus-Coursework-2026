from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import Activity, AuditLog, Event, PlanVersion, ReplanProposal
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)


def _active(*, c4: bool = False):
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    jid = ids["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{jid}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    assert client.post(f"/api/v1/journeys/{jid}/plans/{plan['id']}/select").status_code == 200
    if c4:
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
            headers={"Idempotency-Key": "postgate-capture-0001"},
        )
        assert capture.status_code == 201
    assert client.post(f"/api/v1/journeys/{jid}/start").status_code == 200
    return ids, plan


def _proposal(jid: str, *, event_type: str = "SCHEDULE_DELAY", affected_activity_id=None):
    payload = {"event_type": event_type}
    if affected_activity_id is not None:
        payload["affected_activity_id"] = affected_activity_id
    if event_type == "SCHEDULE_DELAY":
        payload["payload"] = {"delay_minutes": 10}
    event = client.post(f"/api/v1/journeys/{jid}/events", json=payload)
    assert event.status_code == 201
    proposal = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event.json()["id"]},
    )
    assert proposal.status_code == 201, proposal.text
    return event.json(), proposal.json()


def test_replan_accept_requires_header_and_same_key_replay_is_audit_idempotent():
    ids, _ = _active()
    event, proposal = _proposal(ids["journey_id"])
    assert client.post(f"/api/v1/replans/{proposal['id']}/accept").status_code == 422

    headers = {"Idempotency-Key": "replan-accept-replay-0001"}
    first = client.post(f"/api/v1/replans/{proposal['id']}/accept", headers=headers)
    assert first.status_code == 200, first.text
    with SessionLocal() as session:
        count_before = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.journey_id == ids["journey_id"],
                AuditLog.action == "replan.accepted",
            )
        )
    second = client.post(f"/api/v1/replans/{proposal['id']}/accept", headers=headers)
    assert second.status_code == 200
    assert second.json() == first.json()
    with SessionLocal() as session:
        count_after = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.journey_id == ids["journey_id"],
                AuditLog.action == "replan.accepted",
            )
        )
    assert count_before == count_after == 1
    with SessionLocal() as session:
        row = session.get(Event, event["id"])
        assert row.status == "ACKNOWLEDGED"


def test_accepted_event_cannot_generate_another_replan():
    ids, _ = _active()
    event, proposal = _proposal(ids["journey_id"])
    accepted = client.post(
        f"/api/v1/replans/{proposal['id']}/accept",
        headers={"Idempotency-Key": "replan-event-handled-0001"},
    )
    assert accepted.status_code == 200
    again = client.post(
        f"/api/v1/journeys/{ids['journey_id']}/replans",
        json={"trigger_event_id": event["id"]},
    )
    assert again.status_code == 409


def test_accept_invalidates_competing_presented_proposals_and_candidate_plans():
    ids, _ = _active()
    jid = ids["journey_id"]
    event = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={"event_type": "SCHEDULE_DELAY", "payload": {"delay_minutes": 5}},
    ).json()
    p1 = client.post(
        f"/api/v1/journeys/{jid}/replans", json={"trigger_event_id": event["id"]}
    ).json()
    p2 = client.post(
        f"/api/v1/journeys/{jid}/replans", json={"trigger_event_id": event["id"]}
    ).json()
    accepted = client.post(
        f"/api/v1/replans/{p1['id']}/accept",
        headers={"Idempotency-Key": "replan-invalidate-0001"},
    )
    assert accepted.status_code == 200
    with SessionLocal() as session:
        other = session.get(ReplanProposal, p2["id"])
        other_plan = session.get(PlanVersion, other.proposed_plan_version_id)
        assert other.status == "INVALIDATED"
        assert other_plan.status == "REJECTED"
    assert (
        client.post(
            f"/api/v1/replans/{p2['id']}/accept",
            headers={"Idempotency-Key": "replan-invalidate-0002"},
        ).status_code
        == 409
    )


def test_reject_retires_candidate_plan_and_is_idempotent():
    ids, _ = _active()
    _event, proposal = _proposal(ids["journey_id"])
    first = client.post(f"/api/v1/replans/{proposal['id']}/reject")
    assert first.status_code == 200
    assert first.json()["status"] == "REJECTED"
    second = client.post(f"/api/v1/replans/{proposal['id']}/reject")
    assert second.status_code == 200
    with SessionLocal() as session:
        row = session.get(ReplanProposal, proposal["id"])
        candidate = session.get(PlanVersion, row.proposed_plan_version_id)
        assert candidate.status == "REJECTED"
        audits = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.entity_id == proposal["id"], AuditLog.action == "replan.rejected")
        )
        assert audits == 1


def test_twin_aggregates_unresolved_provider_degradation_and_marks_projection_degraded():
    ids, _ = _active()
    jid = ids["journey_id"]
    first = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={"event_type": "OTHER", "payload": {"degraded_providers": ["valhalla"]}},
    ).json()
    client.post(f"/api/v1/journeys/{jid}/events", json={"event_type": "OTHER", "payload": {}})
    twin = client.get(
        f"/api/v1/journeys/{jid}/twin", params={"now": "2026-10-10T13:00:00+08:00"}
    ).json()
    assert twin["projection_status"] == "DEGRADED"
    assert twin["context"]["degraded_providers"] == ["valhalla"]
    with SessionLocal() as session:
        row = session.get(Event, first["id"])
        row.status = "RESOLVED"
        row.resolved_at = datetime.now().astimezone().isoformat()
        session.commit()
    refreshed = client.get(
        f"/api/v1/journeys/{jid}/twin", params={"now": "2026-10-10T13:00:00+08:00"}
    ).json()
    assert refreshed["projection_status"] == "READY"
    assert refreshed["context"]["degraded_providers"] == []


def test_replan_recomputes_duration_metadata_after_delay():
    ids, plan = _active(c4=True)
    jid = ids["journey_id"]
    tech = next(a for a in plan["activities"] if a["place_id"] == "place-tech-art")
    event = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={
            "event_type": "SCHEDULE_DELAY",
            "affected_activity_id": tech["id"],
            "payload": {"delay_minutes": 15},
        },
    ).json()
    proposal = client.post(
        f"/api/v1/journeys/{jid}/replans", json={"trigger_event_id": event["id"]}
    ).json()
    with SessionLocal() as session:
        row = session.get(ReplanProposal, proposal["id"])
        proposed = session.get(PlanVersion, row.proposed_plan_version_id)
        activities = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == proposed.id)
            .order_by(Activity.sequence_no)
        ).all()
        actual = int(
            (
                max(datetime.fromisoformat(item.end_at) for item in activities)
                - min(datetime.fromisoformat(item.start_at) for item in activities)
            ).total_seconds()
            // 60
        )
        assert proposed.estimated_duration_min == actual


def test_replan_diff_marks_durable_c4_protection_even_if_activity_lock_flag_is_false():
    ids, plan = _active(c4=True)
    jid = ids["journey_id"]
    cruise = next(a for a in plan["activities"] if a["place_id"] == "place-river-cruise")
    tech = next(a for a in plan["activities"] if a["place_id"] == "place-tech-art")
    with SessionLocal() as session:
        row = session.get(Activity, cruise["id"])
        row.locked = False
        session.commit()
    event = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={"event_type": "CLOSURE", "affected_activity_id": tech["id"]},
    ).json()
    proposal = client.post(
        f"/api/v1/journeys/{jid}/replans", json={"trigger_event_id": event["id"]}
    )
    assert proposal.status_code == 201, proposal.text
    assert proposal.json()["locked_commitments_preserved"] is True
    assert "place-river-cruise" in proposal.json()["diff"]["preserved_protected"]
