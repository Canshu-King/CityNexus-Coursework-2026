from __future__ import annotations

import json

from fastapi.testclient import TestClient
from test_solver_runtime_gate import _prepare_disruption

import app.main as main_module
from app.db.models import Activity, Event, Journey, Place
from app.db.session import SessionLocal
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


def _traveler(journey_id: str) -> str:
    with SessionLocal() as session:
        return session.get(Journey, journey_id).traveler_id


def _policy(journey_id: str, *, max_cost: int = 5000) -> None:
    response = client.put(
        f"/api/v1/travelers/{_traveler(journey_id)}/agent-policy",
        json={
            "mode": "AUTONOMOUS",
            "auto_accept_replan": True,
            "max_auto_replan_cost_delta_minor": max_cost,
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


def _mission(journey_id: str) -> dict:
    response = client.put(
        f"/api/v1/journeys/{journey_id}/guardian/mission",
        json={
            "command": "游船和晚餐别动，优先室内、少走路，最多多花五十元，允许自动调整。",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_guardian_no_action_is_quiet_and_deduplicated():
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    first = client.post(f"/api/v1/journeys/{journey_id}/guardian/scan")
    second = client.post(f"/api/v1/journeys/{journey_id}/guardian/scan")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["decision"] == "NO_ACTION"
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["deduplicated"] is True


def test_guardian_flagship_executes_once_and_survives_reload():
    journey_id, base, event = _prepare_disruption()
    cruise = next(item for item in base["activities"] if item["place_id"] == "place-river-cruise")
    traffic = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "TRAFFIC_DELAY",
            "severity": "MINOR",
            "affected_activity_id": cruise["id"],
            "occurred_at": "2026-10-10T13:26:00+08:00",
            "payload": {"extra_minutes": 20, "reason": "river corridor congestion"},
        },
    )
    assert traffic.status_code == 201, traffic.text
    _policy(journey_id)
    mission = _mission(journey_id)
    assert mission["max_cost_delta_minor"] == 5000
    assert mission["resolved_intent"]["max_cost_delta_minor"] == 5000
    protected = set(mission["protected_activity_ids"])
    assert len(protected) >= 2

    scanned = client.post(f"/api/v1/journeys/{journey_id}/guardian/scan")
    assert scanned.status_code == 200, scanned.text
    body = scanned.json()
    assert body["decision"] == "RECOVERY_REQUIRED"
    assert body["action_status"] == "EXECUTED"
    assert body["active_plan_after_id"] != base["id"]
    assert len(body["evidence"]["observation"]["actionable_event_ids"]) == 2
    assert body["evidence"]["solver"]["kind"] == "ORTOOLS_CP_SAT_V2"
    assert [step["tool_name"] for step in body["tool_steps"]] == [
        "inspect_twin",
        "request_replan",
        "accept_replan",
    ]

    duplicate = client.post(f"/api/v1/journeys/{journey_id}/guardian/scan").json()
    assert duplicate["id"] == body["id"]
    assert duplicate["deduplicated"] is True

    reloaded = client.get(f"/api/v1/journeys/{journey_id}/guardian/status")
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["latest"]["id"] == body["id"]
    assert reloaded.json()["standing_mission"]["id"] == mission["id"]
    assert reloaded.json()["current_active_plan_version_id"] == body["active_plan_after_id"]
    assert reloaded.json()["latest"]["trigger_event_id"] == event["id"]


def test_guardian_over_mission_budget_requires_confirmation():
    journey_id, _base, event = _prepare_disruption()
    with SessionLocal() as session:
        session.get(Event, event["id"]).status = "RESOLVED"
        journey = session.get(Journey, journey_id)
        tech = (
            session.query(Activity)
            .filter_by(
                plan_version_id=journey.active_plan_version_id,
                place_id="place-tech-art",
            )
            .one()
        )
        tech_id = tech.id
        for place in session.query(Place).all():
            if place.id in {"place-riverside-museum", "place-city-park", "place-design-library"}:
                metadata = json.loads(place.metadata_json or "{}")
                metadata["cost_minor"] = 6000
                place.metadata_json = json.dumps(metadata)
        session.commit()
    created = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": tech_id,
            "occurred_at": "2026-10-10T13:26:00+08:00",
            "payload": {"reason": "guardian mission budget gate"},
        },
    )
    assert created.status_code == 201, created.text
    _policy(journey_id, max_cost=5000)
    response = client.put(
        f"/api/v1/journeys/{journey_id}/guardian/mission",
        json={
            "command": "游船和晚餐别动，最多多花零元，允许自动调整。",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    assert response.status_code == 200
    scanned = client.post(f"/api/v1/journeys/{journey_id}/guardian/scan")
    assert scanned.status_code == 200, scanned.text
    body = scanned.json()
    assert body["action_status"] == "REQUIRE_CONFIRMATION"
    assert "MISSION_COST_LIMIT_EXCEEDED" in body["evidence"]["policy"]["reason_codes"]
    assert body["evidence"]["mutation"]["executed"] is False


def test_guardian_never_replaces_a_mission_protected_activity():
    journey_id, base, _event = _prepare_disruption()
    _policy(journey_id)
    response = client.put(
        f"/api/v1/journeys/{journey_id}/guardian/mission",
        json={
            "command": "Tech Art Center别动，游船和晚餐也别动，最多多花五十元，允许自动调整。",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    assert response.status_code == 200
    assert len(response.json()["protected_activity_ids"]) >= 3
    scanned = client.post(f"/api/v1/journeys/{journey_id}/guardian/scan")
    assert scanned.status_code == 200, scanned.text
    body = scanned.json()
    assert body["decision"] == "RECOVERY_REQUIRED"
    assert body["action_status"] == "NO_FEASIBLE_RECOVERY"
    assert "protected by the Journey mission" in body["reasoning_summary"]
    assert body["active_plan_after_id"] == base["id"]
