from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import app.main as main_module
from app.db.models import Event
from app.db.session import SessionLocal
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)


def test_event_api_exposes_server_resolution_time_and_reason() -> None:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    journey_id = ids["journey_id"]
    created = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={"event_type": "OPPORTUNITY_WINDOW", "severity": "INFO"},
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]
    resolved_at = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        event = session.get(Event, event_id)
        assert event is not None
        event.status = "RESOLVED"
        event.resolved_at = resolved_at
        payload = json.loads(event.payload_json or "{}")
        payload["resolution_reason"] = "OPPORTUNITY_CONSUMED"
        event.payload_json = json.dumps(payload)
        session.commit()

    listed = client.get(f"/api/v1/journeys/{journey_id}/events")
    assert listed.status_code == 200, listed.text
    projected = next(item for item in listed.json() if item["id"] == event_id)
    assert projected["status"] == "RESOLVED"
    assert datetime.fromisoformat(projected["resolved_at"].replace("Z", "+00:00")) == datetime.fromisoformat(resolved_at)
    assert projected["resolution_reason"] == "OPPORTUNITY_CONSUMED"
