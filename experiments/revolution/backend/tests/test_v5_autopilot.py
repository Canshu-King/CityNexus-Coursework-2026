from __future__ import annotations

import time
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as main_module
from app.agent.autorun import run_once, stop_autorun
from app.db.models import Event, GuardianRun, PlanVersion
from app.db.session import SessionLocal
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


def _ready_journey() -> tuple[str, dict]:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert (
        client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
    )
    cruise = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings",
        json={"activity_id": cruise["id"]},
    ).json()
    captured = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
        },
        headers={"Idempotency-Key": f"v5-capture-{uuid4()}"},
    )
    assert captured.status_code == 201, captured.text
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    return journey_id, plan


def _autonomous_policy(journey_id: str) -> None:
    with SessionLocal() as session:
        from app.db.models import Journey

        traveler_id = session.get(Journey, journey_id).traveler_id
    response = client.put(
        f"/api/v1/travelers/{traveler_id}/agent-policy",
        json={
            "mode": "AUTONOMOUS",
            "auto_accept_replan": True,
            "max_auto_replan_cost_delta_minor": 5000,
            "max_auto_loss_minor": 0,
            "max_auto_schedule_drift_min": 120,
            "allowed_activity_types": ["visit", "meal", "transport"],
            "protected_commitment_levels": ["C4_Anchored"],
            "allow_auto_commitment_management": False,
            "allow_auto_when_provider_degraded": False,
            "allow_auto_on_critical_event": False,
        },
    )
    assert response.status_code == 200, response.text


def test_v5_provider_fixture_wakes_autopilot_and_semantically_deduplicates():
    stop_autorun()
    journey_id, base = _ready_journey()
    _autonomous_policy(journey_id)
    mission = client.put(
        f"/api/v1/journeys/{journey_id}/guardian/mission",
        json={
            "command": "游船和晚餐别动，优先室内、少走路，最多多花五十元，允许自动调整。",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    assert mission.status_code == 200, mission.text

    started = client.post(
        f"/api/v1/journeys/{journey_id}/guardian/autopilot/start",
        json={"interval_seconds": 0.05},
    )
    assert started.status_code == 200, started.text
    assert started.json()["enabled"] is True
    fixture = client.post(
        f"/api/v1/journeys/{journey_id}/guardian/provider-fixture",
        json={"state": "DISRUPTED", "payload": {"scenario": "heavy-rain-and-delay"}},
    )
    assert fixture.status_code == 200, fixture.text

    body = None
    for _ in range(100):
        status = client.get(f"/api/v1/journeys/{journey_id}/guardian/status").json()
        latest = status.get("latest")
        if latest and latest["action_status"] == "EXECUTED":
            body = latest
            break
        time.sleep(0.05)
    assert body is not None
    assert body["active_plan_after_id"] != base["id"]
    assert body["evidence"]["observation"]["incident_fingerprints"]
    assert body["evidence"]["solver"]["kind"] == "ORTOOLS_CP_SAT_V2"
    assert body["agent_runtime"]["provider_key"] == "citynexus-guardian"
    assert [step["tool_name"] for step in body["tool_steps"]] == [
        "inspect_twin",
        "request_replan",
        "accept_replan",
    ]

    with SessionLocal() as session:
        before_runs = len(session.query(GuardianRun).filter_by(journey_id=journey_id).all())
        before_plans = len(session.query(PlanVersion).filter_by(journey_id=journey_id).all())
        provider_events = (
            session.query(Event)
            .filter_by(
                journey_id=journey_id,
                provider_key="city-signal-fixture",
            )
            .all()
        )
    assert len(provider_events) == 2

    run = run_once()
    assert run
    with SessionLocal() as session:
        assert len(session.query(GuardianRun).filter_by(journey_id=journey_id).all()) == before_runs
        assert (
            len(session.query(PlanVersion).filter_by(journey_id=journey_id).all()) == before_plans
        )
    stop_autorun()


def test_v5_provider_fixture_clears_before_action_without_stale_mutation():
    stop_autorun()
    journey_id, base = _ready_journey()
    _autonomous_policy(journey_id)
    mission = client.put(
        f"/api/v1/journeys/{journey_id}/guardian/mission",
        json={
            "command": "游船和晚餐别动，最多多花五十元，允许自动调整。",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    assert mission.status_code == 200
    assert (
        client.post(
            f"/api/v1/journeys/{journey_id}/guardian/provider-fixture",
            json={"state": "DISRUPTED"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/journeys/{journey_id}/guardian/provider-fixture",
            json={"state": "NORMAL"},
        ).status_code
        == 200
    )
    run_once()
    status = client.get(f"/api/v1/journeys/{journey_id}/guardian/status").json()
    assert status["current_active_plan_version_id"] == base["id"]
    assert status["latest"]["action_status"] == "NO_ACTION"
    stop_autorun()
