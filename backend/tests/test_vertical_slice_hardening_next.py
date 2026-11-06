from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from app.integrations.mock.providers import MockMobilityProvider
from app.main import app

client = TestClient(app)


def _active_with_c4_progressed_to_tech_art() -> tuple[str, dict, dict]:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    jid = ids["journey_id"]
    plan_response = client.post(
        f"/api/v1/journeys/{jid}/plans/generate", json={"candidate_count": 1}
    )
    assert plan_response.status_code == 201, plan_response.text
    plan = plan_response.json()[0]
    assert client.post(f"/api/v1/journeys/{jid}/plans/{plan['id']}/select").status_code == 200

    cruise = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    booking_response = client.post(
        f"/api/v1/journeys/{jid}/bookings", json={"activity_id": cruise["id"]}
    )
    assert booking_response.status_code == 201, booking_response.text
    booking = booking_response.json()
    capture = client.post(
        f"/api/v1/journeys/{jid}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
        },
        headers={"Idempotency-Key": f"hardening-capture-{booking['id']}"},
    )
    assert capture.status_code == 201, capture.text

    twin_response = client.post(f"/api/v1/journeys/{jid}/start")
    assert twin_response.status_code == 200, twin_response.text
    twin = twin_response.json()
    tech = next(item for item in plan["activities"] if item["place_id"] == "place-tech-art")
    for activity in plan["activities"]:
        if activity["id"] == tech["id"]:
            break
        completed = client.post(f"/api/v1/journeys/{jid}/activities/{activity['id']}/complete")
        assert completed.status_code == 200, completed.text
        twin = completed.json()
    assert twin["current_activity_id"] == tech["id"]
    return jid, plan, tech


def _closure_replans(jid: str, tech: dict, *, occurred_at: str) -> list[dict]:
    event_response = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": tech["id"],
            "occurred_at": occurred_at,
            "payload": {"reason": "vertical-slice hardening"},
        },
    )
    assert event_response.status_code == 201, event_response.text
    event = event_response.json()
    response = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": 5},
    )
    assert response.status_code == 201, response.text
    return [
        item
        for item in client.get(f"/api/v1/journeys/{jid}/replans").json()
        if item["trigger_event_id"] == event["id"]
    ]


def test_f3_inbound_uses_authoritative_current_execution_location():
    jid, _plan, tech = _active_with_c4_progressed_to_tech_art()
    proposals = _closure_replans(jid, tech, occurred_at="2026-10-10T13:25:00+08:00")
    f3 = next(
        item for item in proposals if item["diff"]["candidate_family"] == "F3_OPPORTUNITY_WINDOW"
    )
    evidence = f3["diff"]["opportunity_window_evidence"]
    added_key = f3["diff"]["added"][0]
    expected = MockMobilityProvider().estimate_minutes("place-tech-art", added_key)
    assert evidence["origin_key"] == "place-tech-art"
    assert evidence["inbound_travel_min"] == expected


def test_aware_utc_event_is_normalized_to_journey_local_schedule_before_f3():
    jid, _plan, tech = _active_with_c4_progressed_to_tech_art()
    proposals = _closure_replans(jid, tech, occurred_at="2026-10-10T05:25:00+00:00")
    f3 = next(
        item for item in proposals if item["diff"]["candidate_family"] == "F3_OPPORTUNITY_WINDOW"
    )
    evidence = f3["diff"]["opportunity_window_evidence"]
    local_event = datetime.fromisoformat(evidence["window_opens_at"])
    assert local_event.isoformat() == "2026-10-10T13:25:00+08:00"
    assert evidence["origin_key"] == "place-tech-art"


# V2-B1: replan now refuses the mock fixture's blanket default 20 as a hard fact.
# Seed the demo graph with *explicit* mobility so these replan-dependent tests
# exercise trustworthy legs (see tests/_qoder_explicit_demo_mobility.py).
import pytest as _pytest  # noqa: E402
from _qoder_explicit_demo_mobility import VERTICAL_SLICE_MOBILITY as _MODULE_MOBILITY, seed_module_mobility as _seed  # noqa: E402
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


@_pytest.fixture(autouse=True)
def _qoder_explicit_demo_mobility(monkeypatch):
    _seed(monkeypatch, _MODULE_MOBILITY)
