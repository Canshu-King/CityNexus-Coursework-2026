from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module
from app.agent.autorun import ensure_enrollment, run_once, stop_autorun
from app.db.models import Journey
from app.db.session import SessionLocal
from app.execution.traveler_twin import planning_preference_items
from app.replanning.candidate_evidence import verified_interest_preference_fit
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


def _policy(traveler_id: str) -> None:
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


def _new_journey(traveler_id: str, day: str, title: str) -> str:
    response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler_id,
            "title": title,
            "city_code": "CNX-DEMO",
            "timezone": "Asia/Shanghai",
            "start_at": f"{day}T09:00:00+08:00",
            "end_at": f"{day}T19:00:00+08:00",
            "budget": {"amount_minor": 50000, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_v7_flagship_journey_a_to_b_to_c_traveler_twin_loop() -> None:
    """Prove learning, predictive recovery, and an explicit mission override."""

    stop_autorun()
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    traveler_id = ids["traveler_id"]

    # Journey A supplies actual outcome evidence: skip the outdoor opening and
    # complete indoor stops. The domain lifecycle, not a synthetic rating,
    # creates the learned soft preference.
    generated = client.post(
        f"/api/v1/journeys/{ids['journey_id']}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert (
        client.post(f"/api/v1/journeys/{ids['journey_id']}/plans/{plan['id']}/select").status_code
        == 200
    )
    assert client.post(f"/api/v1/journeys/{ids['journey_id']}/start").status_code == 200
    activities = plan["activities"]
    assert (
        client.post(
            f"/api/v1/journeys/{ids['journey_id']}/activities/{activities[0]['id']}/skip"
        ).status_code
        == 200
    )
    for activity in activities[1:4]:
        response = client.post(
            f"/api/v1/journeys/{ids['journey_id']}/activities/{activity['id']}/complete"
        )
        assert response.status_code == 200, response.text
    cruise = next(item for item in activities if item["place_id"] == "place-river-cruise")
    dinner = activities[-1]
    assert (
        client.post(
            f"/api/v1/journeys/{ids['journey_id']}/activities/{cruise['id']}/skip"
        ).status_code
        == 200
    )
    completed = client.post(
        f"/api/v1/journeys/{ids['journey_id']}/activities/{dinner['id']}/complete"
    )
    assert completed.status_code == 200, completed.text

    twin_a = client.get(f"/api/v1/travelers/{traveler_id}/traveler-twin")
    assert twin_a.status_code == 200, twin_a.text
    learned_a = twin_a.json()["learned_soft_preferences"]
    indoor = next(item for item in learned_a if item["direction"] == "indoor")
    assert indoor["source"] == "LEARNED_SOFT"
    assert indoor["evidence_count"] >= 1
    assert client.post(f"/api/v1/journeys/{ids['journey_id']}/reflect/process").status_code == 200

    # Journey B consumes the learned evidence in the same deterministic
    # candidate ranking and then performs a predictive recovery without an
    # authority escalation.
    journey_b = _new_journey(traveler_id, "2026-10-11", "Personalized predictive recovery")
    generated_b = client.post(
        f"/api/v1/journeys/{journey_b}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated_b.status_code == 201, generated_b.text
    plan_b = generated_b.json()[0]
    assert (
        client.post(f"/api/v1/journeys/{journey_b}/plans/{plan_b['id']}/select").status_code == 200
    )
    assert client.post(f"/api/v1/journeys/{journey_b}/start").status_code == 200
    _policy(traveler_id)
    mission_b = client.put(
        f"/api/v1/journeys/{journey_b}/guardian/mission",
        json={
            "command": "Autonomously recover from a forecast risk while preserving the cruise.",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    assert mission_b.status_code == 200, mission_b.text
    assert (
        client.post(
            f"/api/v1/journeys/{journey_b}/real-city/discover",
            json={"query": "indoor", "mode": "replay", "limit": 3},
        ).status_code
        == 200
    )
    with SessionLocal() as session:
        journey = session.get(Journey, journey_b)
        assert journey is not None
        items = planning_preference_items(session, traveler_id, journey_b)
        assert any(
            getattr(item, "source", "") == "LEARNED_SOFT"
            and getattr(item, "preference_key", "") == "indoor"
            for item in items
        )
        assert verified_interest_preference_fit(
            items, candidate_tags={"indoor", "culture"}
        ) > verified_interest_preference_fit(items, candidate_tags={"outdoor"})
        ensure_enrollment(session, journey, interval_seconds=1)
        session.commit()
    fixture = client.post(
        f"/api/v1/journeys/{journey_b}/guardian/provider-fixture",
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
    recovery = run_once(enrolled_only=True)
    assert len(recovery) == 1
    guardian = recovery[0]["guardian"]
    assert guardian["trigger_kind"] == "PREDICTIVE"
    assert guardian["action_status"] == "EXECUTED"
    assert guardian["evidence"]["policy"]["decision"] == "ALLOW_AUTO"
    trace_response = client.get(f"/api/v1/journeys/{journey_b}/observability/traces")
    assert trace_response.status_code == 200, trace_response.text
    trace_names = {item["name"] for item in trace_response.json()["spans"]}
    assert {
        "provider.capture",
        "traveler_twin.lookup",
        "resilience.evaluate",
        "mission.compile",
        "agent.tool_calls",
        "cp_sat.solve",
        "gateway.policy_decision",
        "domain.mutation",
    }.issubset(trace_names)

    # Journey C is an explicit override. It hides the learned indoor signal
    # for this mission while retaining the same user policy and envelope.
    journey_c = _new_journey(traveler_id, "2026-10-12", "Explicit outdoor override")
    mission_c = client.put(
        f"/api/v1/journeys/{journey_c}/guardian/mission",
        json={
            "command": "I explicitly want outdoor parks today; override the learned indoor preference.",
            "requested_autonomy": "EXECUTE_WITHIN_BOUNDS",
        },
    )
    assert mission_c.status_code == 200, mission_c.text
    with SessionLocal() as session:
        items_c = planning_preference_items(session, traveler_id, journey_c)
        assert any(
            getattr(item, "source", "") == "EXPLICIT_MISSION"
            and getattr(item, "preference_key", "") == "outdoor"
            for item in items_c
        )
        assert not any(getattr(item, "source", "") == "LEARNED_SOFT" for item in items_c)
    policy_after = client.get(f"/api/v1/travelers/{traveler_id}/agent-policy").json()
    assert policy_after["mode"] == "AUTONOMOUS"
    assert policy_after["max_auto_replan_cost_delta_minor"] == 5000
    captured_replay = client.post(
        f"/api/v1/journeys/{journey_c}/real-city/discover",
        json={"query": "museum", "mode": "captured_replay", "limit": 3},
    )
    assert captured_replay.status_code == 200, captured_replay.text
    captured_body = captured_replay.json()
    assert captured_body["provenance"] == "SEALED_LIVE_CAPTURE"
    assert captured_body["candidates"]
    assert all(
        "opening_hours" in item and "cost_evidence" in item for item in captured_body["candidates"]
    )

    cleared = client.delete(f"/api/v1/travelers/{traveler_id}/traveler-twin/learned")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["learned_soft_preferences"] == []


def test_v7_second_cross_journey_dimension_learns_low_walking_soft_evidence() -> None:
    """A separate behavioral dimension changes later planning utility without authority growth."""

    stop_autorun()
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    traveler_id = ids["traveler_id"]
    generated = client.post(
        f"/api/v1/journeys/{ids['journey_id']}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    assert (
        client.post(f"/api/v1/journeys/{ids['journey_id']}/plans/{plan['id']}/select").status_code
        == 200
    )
    assert client.post(f"/api/v1/journeys/{ids['journey_id']}/start").status_code == 200

    accepted = client.post(
        f"/api/v1/journeys/{ids['journey_id']}/outcomes",
        json={
            "event_type": "USER_ACCEPTED",
            "activity_id": plan["activities"][0]["id"],
            "payload": {
                "preference_dimension": "low_walking",
                "direction": "low_walking",
            },
        },
    )
    assert accepted.status_code == 201, accepted.text
    reflected = client.post(f"/api/v1/journeys/{ids['journey_id']}/reflect/process")
    assert reflected.status_code == 200, reflected.text

    journey_b = _new_journey(traveler_id, "2026-10-13", "Low walking personalization")
    with SessionLocal() as session:
        items = planning_preference_items(session, traveler_id, journey_b)
        assert any(
            getattr(item, "source", "") == "LEARNED_SOFT"
            and getattr(item, "preference_key", "") == "low_walking"
            for item in items
        )
        assert verified_interest_preference_fit(
            items, candidate_tags={"low_walking", "indoor"}
        ) > verified_interest_preference_fit(items, candidate_tags={"outdoor"})
    twin = client.get(f"/api/v1/travelers/{traveler_id}/traveler-twin")
    assert twin.status_code == 200, twin.text
    learned = twin.json()["learned_soft_preferences"]
    low_walking = next(item for item in learned if item["direction"] == "low_walking")
    assert low_walking["source"] == "LEARNED_SOFT"
    assert low_walking["evidence_count"] == 1
    policy = client.get(f"/api/v1/travelers/{traveler_id}/agent-policy")
    assert policy.status_code == 200
    assert policy.json()["max_auto_loss_minor"] == 0
