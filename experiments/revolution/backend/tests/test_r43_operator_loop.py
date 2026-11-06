"""Focused R43 product-loop tests; no historical Revolution closure campaign."""

from __future__ import annotations

from sqlalchemy import func, select
from fastapi.testclient import TestClient

import app.main as main_module
from app.db.models import AgentRun, AutopilotEnrollment, Event, GuardianRun, Journey
from app.db.session import SessionLocal
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)


def _active_journey() -> tuple[str, dict]:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert client.post(
        f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select"
    ).status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    return journey_id, plan


def test_operator_start_is_one_product_action_and_stop_preserves_history():
    journey_id, _plan = _active_journey()
    started = client.post(
        f"/api/v1/journeys/{journey_id}/operator/start",
        json={"interval_seconds": 3600},
    )
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["state"] == "WATCHING"
    assert body["enabled"] is True
    assert body["standing_mission_id"]

    with SessionLocal() as session:
        enrollment = session.get(AutopilotEnrollment, journey_id)
        assert enrollment is not None and enrollment.enabled is True
        mission = session.get(Journey, journey_id)
        assert mission is not None and mission.status == "ACTIVE"

    stopped = client.post(f"/api/v1/journeys/{journey_id}/operator/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "OFF"
    assert stopped.json()["timeline"][-1]["label"] == "Journey Operator paused"


def test_operator_fixture_enters_guardian_and_blocks_without_external_agent():
    journey_id, plan = _active_journey()
    started = client.post(
        f"/api/v1/journeys/{journey_id}/operator/start",
        json={"interval_seconds": 3600},
    )
    assert started.status_code == 200, started.text
    target = next(item for item in plan["activities"] if item["place_id"] == "place-tech-art")
    fixture = client.post(
        f"/api/v1/journeys/{journey_id}/guardian/provider-fixture",
        json={
            "state": "DISRUPTED",
            "payload": {
                "target_place_id": target["place_id"],
                "description": "LAB_FIXTURE: selected place is temporarily closed",
            },
        },
    )
    assert fixture.status_code == 200, fixture.text
    pulse = client.post(f"/api/v1/journeys/{journey_id}/guardian/pulse")
    assert pulse.status_code == 200, pulse.text
    operator = client.get(f"/api/v1/journeys/{journey_id}/operator")
    assert operator.status_code == 200
    projected = operator.json()
    assert projected["state"] == "DEGRADED"
    assert projected["incident"]["provenance"] == "PROVIDER_VERIFIED"
    assert any(item["label"] == "Detected: Closure" for item in projected["timeline"])
    assert any("could not reach" in item["label"] for item in projected["timeline"])

    with SessionLocal() as session:
        events = session.scalars(select(Event).where(Event.journey_id == journey_id)).all()
        guardian = session.scalar(
            select(GuardianRun)
            .where(GuardianRun.journey_id == journey_id)
            .order_by(GuardianRun.created_at.desc())
        )
        agent_count = session.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.journey_id == journey_id)
        )
        assert len([row for row in events if row.event_type == "CLOSURE"]) == 1
        assert guardian is not None
        assert guardian.action_status == "OPERATOR_EXTERNAL_AGENT_BLOCKED"
        assert agent_count == 0

    client.post(f"/api/v1/journeys/{journey_id}/operator/stop")


def test_rejected_operator_approval_is_not_represented_as_pending():
    from app.operator_projection import classify_operator_state

    assert (
        classify_operator_state(
            enabled=True,
            mission_active=True,
            approval_status="REJECTED",
            pending_status="SUPERSEDED",
            material_event_open=True,
        )
        == "NEEDS_ATTENTION"
    )
