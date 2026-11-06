from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Activity, Commitment, PlanVersion
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)


def test_backend_connected_golden_scenario_contract():
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    jid = ids["journey_id"]

    generated = client.post(f"/api/v1/journeys/{jid}/plans/generate", json={"candidate_count": 1})
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    selected = client.post(f"/api/v1/journeys/{jid}/plans/{plan['id']}/select")
    assert selected.status_code == 200, selected.text

    cruise = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    booking = client.post(f"/api/v1/journeys/{jid}/bookings", json={"activity_id": cruise["id"]})
    assert booking.status_code == 201, booking.text
    booking = booking.json()
    capture = client.post(
        f"/api/v1/journeys/{jid}/transactions",
        json={
            "booking_id": booking["id"],
            "transaction_type": "CAPTURE",
            "amount": booking["amount"],
        },
        headers={"Idempotency-Key": "golden-backend-capture-0001"},
    )
    assert capture.status_code == 201, capture.text

    started = client.post(f"/api/v1/journeys/{jid}/start")
    assert started.status_code == 200, started.text
    assert started.json()["projection_status"] == "READY"
    c4 = next(
        item for item in started.json()["upcoming_commitments"] if item["level"] == "C4_Anchored"
    )
    assert c4["activity_id"] == cruise["id"]

    # The recovery demo must model a journey that has actually progressed to
    # the disrupted activity. Injecting a 13:25 closure while execution is
    # still on the 09:00 first stop proves API plumbing, not continuity.
    tech = next(item for item in plan["activities"] if item["place_id"] == "place-tech-art")
    twin = started.json()
    for activity in plan["activities"]:
        if activity["id"] == tech["id"]:
            break
        completed = client.post(f"/api/v1/journeys/{jid}/activities/{activity['id']}/complete")
        assert completed.status_code == 200, completed.text
        twin = completed.json()
    assert twin["current_activity_id"] == tech["id"]

    event = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": tech["id"],
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": "golden scenario closure"},
        },
    )
    assert event.status_code == 201, event.text
    event = event.json()

    generated_replan = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": 3},
    )
    assert generated_replan.status_code == 201, generated_replan.text
    proposals = [
        item
        for item in client.get(f"/api/v1/journeys/{jid}/replans").json()
        if item["trigger_event_id"] == event["id"]
    ]
    assert {item["diff"]["candidate_family"] for item in proposals} >= {
        "F0_SAME_SLOT_REPLACEMENT",
        "F3_OPPORTUNITY_WINDOW",
    }
    assert all(item["hard_constraints_passed"] for item in proposals)
    assert all(item["locked_commitments_preserved"] for item in proposals)
    f3 = next(
        item for item in proposals if item["diff"]["candidate_family"] == "F3_OPPORTUNITY_WINDOW"
    )
    assert f3["diff"]["opportunity_window_evidence"]["window_opens_at"] == event["occurred_at"]
    assert f3["diff"]["opportunity_window_evidence"]["origin_key"] == "place-tech-art"

    chosen = generated_replan.json()
    accepted = client.post(
        f"/api/v1/replans/{chosen['id']}/accept",
        headers={"Idempotency-Key": "golden-backend-accept-0001"},
    )
    assert accepted.status_code == 200, accepted.text
    accepted = accepted.json()
    new_plan_id = accepted["journey"]["active_plan_version_id"]
    assert new_plan_id != plan["id"]
    assert accepted["proposal"]["status"] == "ACCEPTED"
    assert accepted["twin"]["active_plan_version_id"] == new_plan_id

    with SessionLocal() as session:
        old_plan = session.get(PlanVersion, plan["id"])
        new_plan = session.get(PlanVersion, new_plan_id)
        assert old_plan.status == "SUPERSEDED"
        assert new_plan.status == "ACTIVE"
        durable_c4 = session.scalar(
            select(Commitment).where(
                Commitment.journey_id == jid,
                Commitment.level == "C4_Anchored",
                Commitment.status == "ACTIVE",
            )
        )
        assert durable_c4 is not None
        active_c4_activity = session.get(Activity, durable_c4.activity_id)
        assert active_c4_activity is not None
        assert active_c4_activity.plan_version_id == new_plan_id
        assert active_c4_activity.place_id == "place-river-cruise"

    reloaded = client.get(
        f"/api/v1/journeys/{jid}/twin", params={"now": "2026-10-10T13:25:00+08:00"}
    )
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["active_plan_version_id"] == new_plan_id
    reloaded_c4 = next(
        item for item in reloaded.json()["upcoming_commitments"] if item["level"] == "C4_Anchored"
    )
    with SessionLocal() as session:
        assert session.get(Activity, reloaded_c4["activity_id"]).plan_version_id == new_plan_id


# V2-B1: the F3 opportunity-window family routes through legs the demo fixture
# only supplied via the blanket default.  Give the demo graph *explicit* mobility
# so this replan contract test exercises trustworthy facts (B1), not a 20 that
# replan now refuses.
import pytest as _pytest  # noqa: E402
from _qoder_explicit_demo_mobility import GOLDEN_CONTRACT_MOBILITY as _MODULE_MOBILITY, seed_module_mobility as _seed  # noqa: E402
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


@_pytest.fixture(autouse=True)
def _qoder_explicit_demo_mobility(monkeypatch):
    _seed(monkeypatch, _MODULE_MOBILITY)
