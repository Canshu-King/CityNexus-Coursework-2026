from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as main_module
from app.db.models import Journey, PlanVersion, ReplanProposal
from app.db.session import SessionLocal
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)


def test_completion_invalidates_presented_replans_and_keeps_history_read_only() -> None:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": plan["activities"][0]["id"],
            "occurred_at": "2026-10-11T10:00:00+08:00",
            "payload": {"reason": "completion lifecycle regression"},
        },
    )
    assert event.status_code == 201, event.text
    created = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event.json()["id"], "candidate_count": 1},
        headers={"Idempotency-Key": f"completion-replan-{uuid4()}"},
    )
    assert created.status_code == 201, created.text

    proposals_before = client.get(f"/api/v1/journeys/{journey_id}/replans").json()
    assert any(item["status"] == "PRESENTED" for item in proposals_before)

    for activity in plan["activities"]:
        completed = client.post(
            f"/api/v1/journeys/{journey_id}/activities/{activity['id']}/complete"
        )
        assert completed.status_code == 200, completed.text

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None and journey.status == "COMPLETED"
        proposals = session.query(ReplanProposal).filter_by(journey_id=journey_id).all()
        assert proposals and all(item.status == "INVALIDATED" for item in proposals)
        candidate_ids = {item.proposed_plan_version_id for item in proposals}
        candidates = session.query(PlanVersion).filter(PlanVersion.id.in_(candidate_ids)).all()
        assert all(item.status == "REJECTED" for item in candidates)

    history = client.get(f"/api/v1/journeys/{journey_id}/replans")
    assert history.status_code == 200
    assert history.json() and all(item["status"] == "INVALIDATED" for item in history.json())
