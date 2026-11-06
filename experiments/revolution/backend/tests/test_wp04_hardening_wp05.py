from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.session import ensure_schema, make_engine
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)


def _selected() -> tuple[str, dict]:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plans = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()
    plan = plans[0]
    assert (
        client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
    )
    return journey_id, plan


def test_sqlite_foreign_keys_are_enabled_on_every_file_connection(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'fk.db'}"
    engine = make_engine(url)
    ensure_schema(engine)
    connections = [engine.connect() for _ in range(4)]
    try:
        assert all(conn.execute(text("PRAGMA foreign_keys")).scalar() == 1 for conn in connections)
        with pytest.raises(IntegrityError):
            connections[2].execute(
                text(
                    "INSERT INTO preferences (id, traveler_id, category, preference_key, value_json, weight, source, created_at, updated_at) VALUES ('orphan','missing','x','x','{}',0.5,'test','now','now')"
                )
            )
    finally:
        for conn in connections:
            conn.close()
        engine.dispose()


def test_booking_requires_exact_provider_slot_and_bookable_service():
    journey_id, plan = _selected()
    cruise = next(a for a in plan["activities"] if a["place_id"] == "place-river-cruise")
    with client as c:
        # Existing generated cruise is canonical 15:30 and remains bookable.
        assert (
            c.post(
                f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
            ).status_code
            == 201
        )
    journey_id, plan = _selected()
    old_town = next(a for a in plan["activities"] if a["place_id"] == "place-old-town")
    assert (
        client.post(
            f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": old_town["id"]}
        ).status_code
        == 422
    )


def test_duplicate_booking_and_capture_are_semantically_blocked():
    journey_id, plan = _selected()
    cruise = next(a for a in plan["activities"] if a["place_id"] == "place-river-cruise")
    first = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    )
    assert first.status_code == 201
    second = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    )
    assert second.status_code == 409
    booking = first.json()
    payload = {
        "booking_id": booking["id"],
        "transaction_type": "CAPTURE",
        "amount": booking["amount"],
    }
    assert (
        client.post(
            f"/api/v1/journeys/{journey_id}/transactions",
            json=payload,
            headers={"Idempotency-Key": "hardening-capture-0001"},
        ).status_code
        == 201
    )
    duplicate = client.post(
        f"/api/v1/journeys/{journey_id}/transactions",
        json=payload,
        headers={"Idempotency-Key": "hardening-capture-0002"},
    )
    assert duplicate.status_code == 409


def test_journey_twin_start_and_activity_completion_projection():
    journey_id, plan = _selected()
    started = client.post(f"/api/v1/journeys/{journey_id}/start")
    assert started.status_code == 200
    twin = started.json()
    assert twin["projection_status"] == "READY"
    assert twin["current_activity_id"] == plan["activities"][0]["id"]
    completed = client.post(
        f"/api/v1/journeys/{journey_id}/activities/{twin['current_activity_id']}/complete"
    )
    assert completed.status_code == 200
    assert completed.json()["current_activity_id"] != twin["current_activity_id"]
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events", json={"event_type": "CLOSURE", "severity": "MAJOR", "payload": {"at_current_location": True}}
    )
    assert event.status_code == 201
    assert client.get(f"/api/v1/journeys/{journey_id}/events").status_code == 200
    assert client.get(f"/api/v1/journeys/{journey_id}/history").status_code == 200


def test_deterministic_replan_proposal_can_be_accepted():
    journey_id, _plan = _selected()
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events", json={"event_type": "CLOSURE", "payload": {"at_current_location": True}}
    ).json()
    proposal = client.post(
        f"/api/v1/journeys/{journey_id}/replans", json={"trigger_event_id": event["id"]}
    )
    assert proposal.status_code == 201
    body = proposal.json()
    assert body["locked_commitments_preserved"] is True
    accepted = client.post(
        f"/api/v1/replans/{body['id']}/accept", headers={"Idempotency-Key": "wp05-accept-0001"}
    )
    assert accepted.status_code == 200
    assert accepted.json()["proposal"]["status"] == "ACCEPTED"
