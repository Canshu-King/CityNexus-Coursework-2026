from fastapi.testclient import TestClient

import app.main as main_module
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


def test_v10_generic_compose_uses_explicit_pois_and_gate() -> None:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": ids["traveler_id"],
            "title": "Explicit POI composition",
            "city_code": "CNX-DEMO",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-09T08:00:00+08:00",
            "end_at": "2026-10-09T20:00:00+08:00",
            "budget": {"amount_minor": 30000, "currency": "CNY"},
        },
    )
    assert journey.status_code == 201, journey.text
    journey_id = journey.json()["id"]

    response = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate",
        json={
            "candidate_count": 1,
            "place_ids": ["place-old-town", "place-riverside-museum", "place-city-park"],
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()[0]
    assert plan["activities"]
    assert [item["place_id"] for item in plan["activities"]] == [
        "place-old-town",
        "place-riverside-museum",
        "place-city-park",
    ]
    assert plan["score_breakdown"]["total"] > 0
