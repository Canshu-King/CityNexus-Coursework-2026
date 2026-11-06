from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.db.models import Journey, ProviderCall
from app.db.session import SessionLocal
from app.integrations.discovery.real_city import capture_real_city_candidates

client = TestClient(main_module.app)


def _generic_journey() -> str:
    traveler = client.post(
        "/api/v1/travelers",
        json={"display_name": "Trial traveler", "locale": "zh-CN", "timezone": "Asia/Shanghai"},
    )
    assert traveler.status_code == 201, traveler.text
    journey = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler.json()["id"],
            "title": "Fuzhou trial",
            "city_code": "CN-FUZHOU",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-01T09:00:00+08:00",
            "end_at": "2026-10-03T18:00:00+08:00",
            "budget": {"amount_minor": 200000, "currency": "CNY"},
            "stay_budget": {"amount_minor": 80000, "currency": "CNY"},
            "party_size": 1,
            "occupancy": {"adults": 1, "rooms": 1},
            "arrival_anchor": {"at": "2026-10-01T09:00:00+08:00", "location": "Fuzhou Railway Station"},
            "departure_anchor": {"at": "2026-10-03T18:00:00+08:00", "location": "Fuzhou Railway Station"},
        },
    )
    assert journey.status_code == 201, journey.text
    return journey.json()["id"]


def test_non_tianjin_captured_replay_is_rejected() -> None:
    journey_id = _generic_journey()
    response = client.post(
        f"/api/v1/journeys/{journey_id}/real-city/intelligence",
        json={
            "goal": "history and culture",
            "mode": "captured_replay",
            "radius_m": 2500,
            "limit": 4,
            "candidate_count": 1,
            "require_fully_grounded": True,
        },
    )
    assert response.status_code == 422, response.text
    assert "CAPTURED_REPLAY_CITY_MISMATCH" in response.text


def test_non_tianjin_live_unavailable_never_injects_tianjin_replay() -> None:
    journey_id = _generic_journey()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        result = capture_real_city_candidates(
            session, journey, query="history and culture", radius_m=2500, limit=4, mode="live"
        )
        assert result["candidates"] == []
        assert result["mode"] == "live-unavailable"
        assert result["provider_status"]["wrong_city_replay_blocked"] is True
        assert result["provider_status"]["nominatim"] == "UNAVAILABLE"
        calls = list(session.scalars(select(ProviderCall).order_by(ProviderCall.created_at.desc())).all())
        relevant = next(
            item for item in calls
            if json.loads(item.request_json).get("city_code") == "CN-FUZHOU"
        )
        payload = json.loads(relevant.response_json)
        assert payload["count"] == 0
