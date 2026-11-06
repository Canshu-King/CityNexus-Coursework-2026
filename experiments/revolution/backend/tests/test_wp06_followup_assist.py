from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Place, ReplanProposal
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)


def _active_journey() -> tuple[dict, dict]:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    jid = ids["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{jid}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    assert client.post(f"/api/v1/journeys/{jid}/plans/{plan['id']}/select").status_code == 200
    assert client.post(f"/api/v1/journeys/{jid}/start").status_code == 200
    return ids, plan


def _closure(jid: str, plan: dict) -> dict:
    affected = next(a for a in plan["activities"] if a["place_id"] == "place-tech-art")
    event = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={
            "event_type": "CLOSURE",
            "affected_activity_id": affected["id"],
            "occurred_at": "2026-10-10T13:00:00+08:00",
        },
    )
    assert event.status_code == 201, event.text
    return event.json()


def _family(jid: str, event_id: str, name: str) -> dict:
    proposals = [
        item
        for item in client.get(f"/api/v1/journeys/{jid}/replans").json()
        if item["trigger_event_id"] == event_id and item["diff"]["candidate_family"] == name
    ]
    assert proposals, (event_id, name)
    return proposals[0]


def test_candidate_count_counts_feasible_candidates_not_raw_specs():
    ids, plan = _active_journey()
    jid = ids["journey_id"]
    with SessionLocal() as session:
        library = session.get(Place, "place-design-library")
        library.opening_hours_json = json.dumps({"open": "15:00", "close": "19:00"})
        session.commit()

    event = _closure(jid, plan)
    response = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": 1},
    )
    assert response.status_code == 201, response.text
    assert response.json()["diff"]["added"] == ["place-city-park"]
    with SessionLocal() as session:
        rows = session.scalars(
            select(ReplanProposal).where(ReplanProposal.trigger_event_id == event["id"])
        ).all()
        assert len(rows) == 1


def _preference_score(preference_key: str) -> float:
    ids, plan = _active_journey()
    jid = ids["journey_id"]
    traveler_id = ids["traveler_id"]
    replace = client.put(
        f"/api/v1/travelers/{traveler_id}/preferences",
        json=[
            {
                "category": "interest",
                "key": preference_key,
                "value": True,
                "weight": 1.0,
            }
        ],
    )
    assert replace.status_code == 200, replace.text
    event = _closure(jid, plan)
    response = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": 3},
    )
    assert response.status_code == 201, response.text
    proposal = _family(jid, event["id"], "F0_SAME_SLOT_REPLACEMENT")
    return float(proposal["score_breakdown"]["preference_fit"])


def test_verified_preference_fit_uses_persisted_traveler_preferences():
    culture = _preference_score("culture")
    waterfront = _preference_score("waterfront")
    assert culture == 1.0
    assert waterfront == 0.0


def test_replan_generation_optional_idempotency_key_replays_without_new_candidates():
    ids, plan = _active_journey()
    jid = ids["journey_id"]
    event = _closure(jid, plan)
    headers = {"Idempotency-Key": "REDACTED_FOR_PUBLIC_RELEASE"}
    payload = {"trigger_event_id": event["id"], "candidate_count": 3}
    first = client.post(f"/api/v1/journeys/{jid}/replans", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    before = [
        item
        for item in client.get(f"/api/v1/journeys/{jid}/replans").json()
        if item["trigger_event_id"] == event["id"]
    ]
    second = client.post(f"/api/v1/journeys/{jid}/replans", json=payload, headers=headers)
    assert second.status_code == 201
    assert second.json() == first.json()
    after = [
        item
        for item in client.get(f"/api/v1/journeys/{jid}/replans").json()
        if item["trigger_event_id"] == event["id"]
    ]
    assert len(after) == len(before)

    conflict = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": 1},
        headers=headers,
    )
    assert conflict.status_code == 409


def test_completed_c4_is_not_projected_as_upcoming():
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    jid = ids["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{jid}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    assert client.post(f"/api/v1/journeys/{jid}/plans/{plan['id']}/select").status_code == 200
    cruise = next(a for a in plan["activities"] if a["place_id"] == "place-river-cruise")
    booking = client.post(
        f"/api/v1/journeys/{jid}/bookings", json={"activity_id": cruise["id"]}
    ).json()
    capture = client.post(
        f"/api/v1/journeys/{jid}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
        },
        headers={"Idempotency-Key": "followup-upcoming-c4-capture"},
    )
    assert capture.status_code == 201
    twin = client.post(f"/api/v1/journeys/{jid}/start").json()
    while twin["current_activity_id"] != cruise["id"]:
        current = twin["current_activity_id"]
        twin = client.post(f"/api/v1/journeys/{jid}/activities/{current}/complete").json()
    twin = client.post(f"/api/v1/journeys/{jid}/activities/{cruise['id']}/complete").json()
    assert all(item["activity_id"] != cruise["id"] for item in twin["upcoming_commitments"])


def test_opportunity_value_is_neutral_until_window_evidence_is_wired():
    ids, plan = _active_journey()
    jid = ids["journey_id"]
    event = _closure(jid, plan)
    response = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": 3},
    )
    assert response.status_code == 201, response.text
    for proposal in client.get(f"/api/v1/journeys/{jid}/replans").json():
        if proposal["trigger_event_id"] == event["id"]:
            assert proposal["score_breakdown"]["opportunity_value"] == 0.5


def test_f3_family_is_backed_by_next_c4_window_evidence():
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    jid = ids["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{jid}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    assert client.post(f"/api/v1/journeys/{jid}/plans/{plan['id']}/select").status_code == 200
    cruise = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    booking = client.post(f"/api/v1/journeys/{jid}/bookings", json={"activity_id": cruise["id"]})
    assert booking.status_code == 201, booking.text
    booking = booking.json()
    captured = client.post(
        f"/api/v1/journeys/{jid}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
        },
        headers={"Idempotency-Key": "followup-f3-capture-0001"},
    )
    assert captured.status_code == 201
    assert client.post(f"/api/v1/journeys/{jid}/start").status_code == 200
    event = _closure(jid, plan)
    response = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": 3},
    )
    assert response.status_code == 201
    f3 = next(
        item
        for item in client.get(f"/api/v1/journeys/{jid}/replans").json()
        if item["trigger_event_id"] == event["id"]
        and item["diff"]["candidate_family"] == "F3_OPPORTUNITY_WINDOW"
    )
    evidence = f3["diff"]["opportunity_window_evidence"]
    assert evidence["next_commitment_start"].startswith("2026-10-10T15:30")
    assert evidence["inbound_travel_min"] >= 0
    assert evidence["outbound_travel_min"] >= 0
    assert evidence["remaining_buffer_min"] >= 0
    assert f3["score_breakdown"]["opportunity_value"] != 0.5
    verified = f3["diff"]["verified_evidence"]
    assert verified["feasible"] is True
    assert verified["hard_constraint_result"]["passed"] is True
    assert verified["commitment_preservation"]["protected_count"] >= 1
    assert verified["mobility"]["provider"] == "mock-mobility"
    assert verified["opportunity"]["evidence"] == evidence


# V2-B1: replan now refuses the mock fixture's blanket default 20 as a hard fact.
# Seed the demo graph with *explicit* mobility so these replan-dependent tests
# exercise trustworthy legs (see tests/_qoder_explicit_demo_mobility.py).
import pytest as _pytest  # noqa: E402
from _qoder_explicit_demo_mobility import WP06_FOLLOWUP_MOBILITY as _MODULE_MOBILITY, seed_module_mobility as _seed  # noqa: E402
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


@_pytest.fixture(autouse=True)
def _qoder_explicit_demo_mobility(monkeypatch):
    _seed(monkeypatch, _MODULE_MOBILITY)
