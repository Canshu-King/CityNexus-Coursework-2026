from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Event, PlanVersion, ReplanProposal
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)


def _active_journey():
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    assert (
        client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
    )
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    return journey_id, plan


def test_replan_candidate_count_creates_distinct_feasible_ranked_families_and_fresh_scores():
    journey_id, base_plan = _active_journey()
    affected = next(
        activity for activity in base_plan["activities"] if activity["place_id"] == "place-tech-art"
    )
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "affected_activity_id": affected["id"],
            "occurred_at": "2026-10-10T13:00:00+08:00",
        },
    ).json()
    response = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": 3},
    )
    assert response.status_code == 201, response.text
    proposals = [
        item
        for item in client.get(f"/api/v1/journeys/{journey_id}/replans").json()
        if item["trigger_event_id"] == event["id"]
    ]
    assert len(proposals) == 3
    assert len({item["proposed_plan_version_id"] for item in proposals}) == 3
    assert {item["diff"]["candidate_family"] for item in proposals} == {
        "F0_SAME_SLOT_REPLACEMENT",
        "F0_ALTERNATE_REPLACEMENT",
        "F1_OPTIONAL_REMOVAL",
    }
    assert response.json()["utility_score"] == max(item["utility_score"] for item in proposals)

    with SessionLocal() as session:
        rows = session.scalars(
            select(ReplanProposal).where(ReplanProposal.trigger_event_id == event["id"])
        ).all()
        for proposal in rows:
            plan = session.get(PlanVersion, proposal.proposed_plan_version_id)
            assert plan is not None
            assert plan.status == "CANDIDATE"
            assert plan.utility_score == proposal.utility_score
            assert json.loads(proposal.score_breakdown_json) != base_plan["score_breakdown"]


def test_public_event_provenance_is_server_assigned_and_demo_time_is_deterministic():
    journey_id, plan = _active_journey()
    explicit = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "WEATHER",
            "occurred_at": "2026-10-10T12:34:00+08:00",
            "provenance": "SIMULATED",
        },
    )
    assert explicit.status_code == 201
    assert explicit.json()["provider_key"] == "demo-simulator"
    assert explicit.json()["provenance"] == "SIMULATED"
    assert explicit.json()["occurred_at"] == "2026-10-10T12:34:00+08:00"

    forged_provider = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={"event_type": "OTHER", "provider_key": "open-meteo"},
    )
    assert forged_provider.status_code == 422
    forged_provenance = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={"event_type": "OTHER", "provenance": "PROVIDER_VERIFIED"},
    )
    assert forged_provenance.status_code == 422

    fallback = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={"event_type": "USER_CHANGE", "provenance": "USER_REPORTED"},
    )
    assert fallback.status_code == 201
    assert fallback.json()["provider_key"] == "user-report"
    assert fallback.json()["occurred_at"] == plan["activities"][0]["start_at"]
    with SessionLocal() as session:
        stored = session.get(Event, fallback.json()["id"])
        assert stored.provenance == "USER_REPORTED"


# V2-B1: replan now refuses the mock fixture's blanket default 20 as a hard fact.
# Seed the demo graph with *explicit* mobility so these replan-dependent tests
# exercise trustworthy legs (see tests/_qoder_explicit_demo_mobility.py).
import pytest as _pytest  # noqa: E402
from _qoder_explicit_demo_mobility import WP06_POSTGATE_MOBILITY as _MODULE_MOBILITY, seed_module_mobility as _seed  # noqa: E402
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


@_pytest.fixture(autouse=True)
def _qoder_explicit_demo_mobility(monkeypatch):
    _seed(monkeypatch, _MODULE_MOBILITY)
