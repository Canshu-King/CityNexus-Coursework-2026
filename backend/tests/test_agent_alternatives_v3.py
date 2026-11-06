from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as main_module
from app.db.models import Activity, Journey
from app.db.session import SessionLocal
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


def _selected_plan() -> tuple[str, dict[str, object]]:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    selected = client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select")
    assert selected.status_code == 200, selected.text
    return journey_id, plan


def test_alt01_server_place_search_and_materialized_replacement_can_auto_execute():
    journey_id, plan = _selected_plan()
    target = next(item for item in plan["activities"] if item["place_id"] == "place-tech-art")
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        traveler_id = journey.traveler_id

    policy = client.put(
        f"/api/v1/travelers/{traveler_id}/agent-policy",
        json={
            "mode": "AUTONOMOUS",
            "auto_accept_replan": True,
            "max_auto_replan_cost_delta_minor": 3000,
            "max_auto_loss_minor": 0,
            "max_auto_schedule_drift_min": 30,
            "allowed_activity_types": ["visit"],
            "allow_auto_commitment_management": False,
        },
    )
    assert policy.status_code == 200, policy.text

    search = client.post(
        "/api/v1/agent/tools/search_city_services",
        json={
            "request_id": "alt-search-" + str(uuid4()),
            "journey_id": journey_id,
            "q": "indoor",
        },
    )
    assert search.status_code == 200, search.text
    assert any(item["id"] == "place-design-library" for item in search.json()["places"])

    replaced = client.post(
        "/api/v1/agent/tools/v2/replace_activity",
        json={
            "request_id": "alt-replace-" + str(uuid4()),
            "journey_id": journey_id,
            "activity_id": target["id"],
            "replacement_place_id": "place-design-library",
        },
    )
    assert replaced.status_code == 200, replaced.text
    body = replaced.json()
    assert body["decision"] == "ALLOW_AUTO"
    assert body["executed"] is True
    assert body["result"]["replacement_source"] == "SERVER_PLACE"
    assert body["result"]["activity"]["place_id"] == "place-design-library"

    with SessionLocal() as session:
        row = session.get(Activity, target["id"])
        assert row is not None
        assert row.place_id == "place-design-library"
