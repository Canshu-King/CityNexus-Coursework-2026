from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as main_module
from _qoder_explicit_demo_mobility import seed_explicit_mobility
from app.agent.autorun import ensure_enrollment, run_once, stop_autorun
from app.db.models import Journey, PlanVersion
from app.db.session import SessionLocal
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


def _autonomous_policy(traveler_id: str) -> None:
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


def test_v6_predictive_real_city_twin_prevents_failure_before_hard_break(monkeypatch) -> None:
    stop_autorun()
    # V5 (T2 same-env): the predictive recovery hard-gates the real-city replay
    # legs plus the demo legs this offline journey traverses.  Declare them as
    # explicit deterministic FIXTURE facts (scenario/replay values, NOT live
    # mobility).  A leg outside this set stays unseeded/synthetic -> unavailable,
    # so the mock blanket default is never laundered into a hard fact (B1 intact).
    seed_explicit_mobility(
        monkeypatch,
        {
            ("place-food-hall", "place-river-cruise"): 16,
            ("place-food-hall", "real-place-2a59f43244feca0eeced4bfa"): 12,
            ("place-old-town", "place-city-park"): 22,
            ("place-old-town", "place-design-library"): 10,
            ("place-old-town", "real-place-7836d8cd2e948d6d1a0773e7"): 14,
            ("place-old-town", "real-place-b0557d41934f83a861634fe2"): 18,
            ("real-place-2a59f43244feca0eeced4bfa", "place-river-cruise"): 16,
            ("real-place-7836d8cd2e948d6d1a0773e7", "place-river-cruise"): 14,
            ("real-place-b0557d41934f83a861634fe2", "place-river-cruise"): 18,
        },
    )
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 201, generated.text
    base = generated.json()[0]
    assert (
        client.post(f"/api/v1/journeys/{journey_id}/plans/{base['id']}/select").status_code == 200
    )
    cruise = next(item for item in base["activities"] if item["place_id"] == "place-river-cruise")
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
        headers={"Idempotency-Key": f"v6-capture-{uuid4()}"},
    )
    assert captured.status_code == 201, captured.text
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        traveler_id = journey.traveler_id
    _autonomous_policy(traveler_id)
    mission = client.put(
        f"/api/v1/journeys/{journey_id}/guardian/mission",
        json={
            "command": "Keep the cruise and dinner protected, prefer indoor alternatives, allow autonomous reversible recovery.",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    assert mission.status_code == 200, mission.text

    discovered = client.post(
        f"/api/v1/journeys/{journey_id}/real-city/discover",
        json={"query": "indoor", "mode": "replay", "limit": 3},
    )
    assert discovered.status_code == 200, discovered.text
    discovery_body = discovered.json()
    assert discovery_body["provenance"] == "REPLAY"
    assert any(
        "Tianjin" in item["name"] or "Haihe" in item["name"]
        for item in discovery_body["candidates"]
    )
    live_fallback = client.post(
        f"/api/v1/journeys/{journey_id}/real-city/discover",
        json={"query": "indoor", "mode": "live", "limit": 3},
    )
    assert live_fallback.status_code == 200, live_fallback.text
    assert live_fallback.json()["mode"] == "replay-fallback"
    assert live_fallback.json()["provenance"] == "REPLAY"

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        ensure_enrollment(session, journey, interval_seconds=1)
        session.commit()
    fixture = client.post(
        f"/api/v1/journeys/{journey_id}/guardian/provider-fixture",
        json={
            "state": "RISK_FORECAST",
            "payload": {
                "risk_score": 88,
                "forecast_horizon_min": 45,
                "expected_route_delay_min": 18,
            },
        },
    )
    assert fixture.status_code == 200, fixture.text

    results = run_once(enrolled_only=True)
    assert len(results) == 1
    guardian = results[0]["guardian"]
    assert guardian["trigger_kind"] == "PREDICTIVE"
    assert guardian["decision"] == "PREEMPTIVE_RECOVERY"
    assert guardian["action_status"] == "EXECUTED"
    assert guardian["active_plan_before_id"] == base["id"]
    assert guardian["active_plan_after_id"] != base["id"]
    assert guardian["evidence"]["prediction"]["status"] == "PREDICTIVE_WEAKNESS"
    assert guardian["evidence"]["solver"]["kind"] == "ORTOOLS_CP_SAT_V2"

    repeated = run_once(enrolled_only=True)
    assert len(repeated) == 1
    assert repeated[0]["guardian"]["deduplicated"] is True
    assert repeated[0]["guardian"]["id"] == guardian["id"]

    after = client.get(f"/api/v1/plans/{guardian['active_plan_after_id']}").json()
    assert any(item["place_id"] == "place-river-cruise" for item in after["activities"])
    assert any(item["place_id"].startswith("real-place-") for item in after["activities"])

    resilience = client.get(f"/api/v1/journeys/{journey_id}/resilience")
    assert resilience.status_code == 200
    resilience_body = resilience.json()
    assert resilience_body["status"] == "PREDICTIVE_WEAKNESS"
    assert resilience_body["components"]["forecast_risk"] == 88

    counterfactuals = client.get(
        f"/api/v1/journeys/{journey_id}/resilience/counterfactuals"
    ).json()["counterfactuals"]
    assert len(counterfactuals) == 6
    assert all(item["evidence"]["engine"] == "JourneyTwin + CP-SAT V2" for item in counterfactuals)
    assert all(item["evidence"]["domain_truth_mutated"] is False for item in counterfactuals)

    mesh = client.get(f"/api/v1/journeys/{journey_id}/provider-mesh")
    assert mesh.status_code == 200
    mesh_body = mesh.json()
    assert any(
        item["provider_key"] == "openstreetmap-replay"
        for item in mesh_body["real_place_candidates"]
    )

    with SessionLocal() as session:
        plans = session.query(PlanVersion).filter_by(journey_id=journey_id).all()
        assert len(plans) >= 2
