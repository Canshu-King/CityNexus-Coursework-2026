from __future__ import annotations

import builtins
import json

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.planning.authoritative_v12 as authoritative_v12
from app.db.models import JourneyTripContext, PlanVersion
from app.db.session import SessionLocal
from app.main import app
from test_v12_authoritative_trip_execution import _journey_with_inputs


def test_departure_reservation_updates_only_departure_anchor_and_uses_departure_time() -> None:
    client = TestClient(app)
    journey_id, _place_ids, _commitment_id = _journey_with_inputs()

    with SessionLocal() as session:
        context = session.get(JourneyTripContext, journey_id)
        assert context is not None
        arrival_before = json.loads(context.arrival_anchor_json)

    imported = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/import",
        json={
            "artifact_type": "STRUCTURED_JSON",
            "payload": {
                "reservation": {
                    "reservation_type": "RAIL",
                    "provider_key": "user-confirmed-departure-rail",
                    "confirmation_ref": "V1311-DEPARTURE",
                    "origin": {
                        "name": "Tianjin West Station",
                        "latitude": 39.1366,
                        "longitude": 117.1696,
                    },
                    "destination": {
                        "name": "Beijing South Station",
                        "latitude": 39.8652,
                        "longitude": 116.3789,
                    },
                    "scheduled_departure_at": "2026-10-03T20:00:00+08:00",
                    "scheduled_arrival_at": "2026-10-03T20:35:00+08:00",
                    "price_amount_minor": 5500,
                    "currency": "CNY",
                    "price_status": "KNOWN",
                    "anchor_role": "DEPARTURE",
                }
            },
        },
    )
    assert imported.status_code == 201, imported.text

    confirmed = client.post(
        f"/api/v1/journeys/{journey_id}/inbox/{imported.json()['id']}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    commitment = confirmed.json()["commitment"]
    assert commitment["policy"]["anchor_role"] == "DEPARTURE"
    assert commitment["policy"]["semantic_node_key"] == "departure-anchor"
    assert commitment["policy"]["scheduled_start_at"] == "2026-10-03T20:00:00+08:00"

    with SessionLocal() as session:
        context = session.get(JourneyTripContext, journey_id)
        assert context is not None
        assert json.loads(context.arrival_anchor_json) == arrival_before
        departure = json.loads(context.departure_anchor_json)
        assert departure["at"] == "2026-10-03T20:00:00+08:00"
        assert departure["location"] == "Tianjin West Station"
        assert departure["latitude"] == 39.1366
        assert departure["longitude"] == 117.1696
        assert departure["commitment_id"] == commitment["id"]


def test_day_assignment_reports_deterministic_fallback_when_ortools_import_is_unavailable(monkeypatch) -> None:
    real_import = builtins.__import__

    def import_without_ortools(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "ortools.sat.python":
            raise ImportError("forced optional solver absence")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_ortools)
    assignment, optimizer_kind = authoritative_v12._solve_day_assignment(
        ["p0", "p1", "p2", "p3"],
        day_count=2,
        variation=0,
        fixed_days={"p0": 1},
    )
    assert optimizer_kind == "CITYNEXUS_DETERMINISTIC_ASSIGNMENT"
    assert assignment["p0"] == 1


def test_authoritative_compose_persists_actual_fallback_optimizer_provenance(monkeypatch) -> None:
    client = TestClient(app)
    journey_id, place_ids, _commitment_id = _journey_with_inputs()

    def deterministic_assignment(place_ids, *, day_count, variation, fixed_days=None):
        fixed_days = fixed_days or {}
        return (
            {
                place_id: fixed_days.get(place_id, index % day_count)
                for index, place_id in enumerate(place_ids)
            },
            "CITYNEXUS_DETERMINISTIC_ASSIGNMENT",
        )

    monkeypatch.setattr(authoritative_v12, "_solve_day_assignment", deterministic_assignment)
    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/authoritative-compose",
        json={"place_ids": place_ids, "candidate_count": 1},
    )
    assert response.status_code == 201, response.text
    assert response.json()["optimizer"] == "CITYNEXUS_DETERMINISTIC_ASSIGNMENT"

    plan_id = response.json()["top_k"][0]["id"]
    with SessionLocal() as session:
        plan = session.scalar(select(PlanVersion).where(PlanVersion.id == plan_id))
        assert plan is not None
        score = json.loads(plan.score_breakdown_json)
        explanation = json.loads(plan.explanation_json)
        assert score["optimizer"] == "CITYNEXUS_DETERMINISTIC_ASSIGNMENT"
        assert explanation["optimizer_kind"] == "CITYNEXUS_DETERMINISTIC_ASSIGNMENT"
