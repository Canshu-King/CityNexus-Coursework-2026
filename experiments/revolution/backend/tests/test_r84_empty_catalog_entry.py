from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module


client = TestClient(main_module.app)


def _fresh_empty_catalog_journey() -> str:
    traveler = client.post(
        "/api/v1/travelers",
        json={"display_name": "R84 empty catalog traveler", "locale": "en-US", "timezone": "Asia/Shanghai"},
    )
    assert traveler.status_code == 201, traveler.text
    journey = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler.json()["id"],
            "title": "R84 empty catalog Journey",
            "city_code": "CN-R84-EMPTY",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-10T09:00:00+08:00",
            "end_at": "2026-10-12T18:00:00+08:00",
            "budget": {"amount_minor": 100000, "currency": "CNY"},
            "party_size": 1,
            "arrival_anchor": {"at": "2026-10-10T09:00:00+08:00", "location": "R84 public city center"},
            "departure_anchor": {"at": "2026-10-12T18:00:00+08:00", "location": "R84 public city center"},
        },
    )
    assert journey.status_code == 201, journey.text
    return journey.json()["id"]


def test_fresh_product_journey_starts_with_zero_public_places_and_reports_catalog_blocker() -> None:
    journey_id = _fresh_empty_catalog_journey()
    places = client.get("/api/v1/places", params={"city_code": "CN-R84-EMPTY"})
    assert places.status_code == 200, places.text
    assert places.json() == []

    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 422, generated.text
    body = generated.json()
    assert body["error"]["reason_code"] == "CATALOG_EMPTY"
    assert body["error"]["details"]["places_in_catalog"] == 0
    assert body["error"]["details"]["city_code"] == "CN-R84-EMPTY"
