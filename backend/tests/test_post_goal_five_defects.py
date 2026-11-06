from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Activity, Commitment, Journey, PlanVersion, ReplanProposal
from app.db.session import SessionLocal
from app.integrations.context.open_meteo import OpenMeteoContextProvider
from app.integrations.mock.providers import MockMobilityProvider
from app.main import app
from app.replanning.provider_snapshots import MobilitySnapshot
from app.services import validate_plan_feasibility

client = TestClient(app)


def _active_with_c4() -> tuple[dict, dict, dict]:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    jid = ids["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{jid}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    assert client.post(f"/api/v1/journeys/{jid}/plans/{plan['id']}/select").status_code == 200
    cruise = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    booking = client.post(f"/api/v1/journeys/{jid}/bookings", json={"activity_id": cruise["id"]})
    assert booking.status_code == 201, booking.text
    booking_body = booking.json()
    capture = client.post(
        f"/api/v1/journeys/{jid}/transactions",
        json={
            "booking_id": booking_body["id"],
            "transaction_type": "CAPTURE",
            "amount": booking_body["amount"],
        },
        headers={"Idempotency-Key": f"post-goal-capture-{jid}"},
    )
    assert capture.status_code == 201, capture.text
    assert client.post(f"/api/v1/journeys/{jid}/start").status_code == 200
    return ids, plan, cruise


def _closure(jid: str, plan: dict, *, occurred_at: str) -> dict:
    tech = next(item for item in plan["activities"] if item["place_id"] == "place-tech-art")
    response = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={
            "event_type": "CLOSURE",
            "affected_activity_id": tech["id"],
            "occurred_at": occurred_at,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_f3_window_uses_effective_event_time_not_original_slot_start():
    ids, plan, _cruise = _active_with_c4()
    jid = ids["journey_id"]
    event = _closure(jid, plan, occurred_at="2026-10-10T13:25:00+08:00")
    response = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": 3},
    )
    assert response.status_code == 201, response.text
    proposals = client.get(f"/api/v1/journeys/{jid}/replans").json()
    f3 = next(
        item
        for item in proposals
        if item["trigger_event_id"] == event["id"]
        and item["diff"]["candidate_family"] == "F3_OPPORTUNITY_WINDOW"
    )
    assert f3["diff"]["opportunity_window_evidence"]["window_opens_at"] == (
        "2026-10-10T13:25:00+08:00"
    )
    evidence = f3["diff"]["opportunity_window_evidence"]
    expected_visit_start = datetime.fromisoformat(evidence["window_opens_at"]) + timedelta(
        minutes=evidence["inbound_travel_min"]
    )
    with SessionLocal() as session:
        record = session.get(ReplanProposal, f3["id"])
        replacement = session.scalar(
            select(Activity).where(
                Activity.plan_version_id == record.proposed_plan_version_id,
                Activity.place_id == "place-city-park",
            )
        )
        assert replacement is not None
        assert datetime.fromisoformat(replacement.start_at) == expected_visit_start


def test_accept_replan_remaps_active_c4_commitment_activity_to_new_plan():
    ids, plan, _cruise = _active_with_c4()
    jid = ids["journey_id"]
    event = _closure(jid, plan, occurred_at="2026-10-10T13:10:00+08:00")
    proposal = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": 3},
    )
    assert proposal.status_code == 201, proposal.text
    accepted = client.post(
        f"/api/v1/replans/{proposal.json()['id']}/accept",
        headers={"Idempotency-Key": f"post-goal-accept-{jid}"},
    )
    assert accepted.status_code == 200, accepted.text
    active_plan_id = accepted.json()["journey"]["active_plan_version_id"]
    with SessionLocal() as session:
        commitment = session.scalar(
            select(Commitment).where(
                Commitment.journey_id == jid,
                Commitment.status == "ACTIVE",
                Commitment.level == "C4_Anchored",
            )
        )
        assert commitment is not None
        remapped_activity = session.get(Activity, commitment.activity_id)
        assert remapped_activity is not None
        assert remapped_activity.plan_version_id == active_plan_id
        assert remapped_activity.place_id == "place-river-cruise"
    twin_c4 = next(
        item
        for item in accepted.json()["twin"]["upcoming_commitments"]
        if item["level"] == "C4_Anchored"
    )
    assert twin_c4["activity_id"] == commitment.activity_id


def test_validate_plan_feasibility_accepts_injected_frozen_mobility_snapshot():
    ids, plan, _cruise = _active_with_c4()
    jid = ids["journey_id"]
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        persisted_plan = session.get(PlanVersion, plan["id"])
        activities = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == persisted_plan.id)
            .order_by(Activity.sequence_no)
        ).all()
        mock = MockMobilityProvider()
        pairs: dict[tuple[str, str], int] = {}
        for previous, current in zip(activities, activities[1:]):
            origin = previous.place_id or previous.city_service_id
            destination = current.place_id or current.city_service_id
            pairs[(origin, destination)] = mock.estimate_minutes(origin, destination)
        snapshot = MobilitySnapshot.from_minutes(
            source_provider_key="valhalla",
            minutes=pairs,
            captured_at="2026-10-10T08:55:00+08:00",
        )
        gate = validate_plan_feasibility(
            session,
            journey,
            persisted_plan,
            mobility_provider=snapshot,
            include_active_c4=True,
        )
        assert gate.passed, gate.failures


@pytest.mark.asyncio
async def test_open_meteo_accepts_unix_time_and_returns_timezone_aware_observation():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": [1791612000],
                    "precipitation_probability": [85],
                    "weather_code": [65],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenMeteoContextProvider(client=http_client, base_url="https://weather.test")
        observations = await provider.forecast(latitude=39.1, longitude=117.2)
    assert observations[0].at == datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
    assert observations[0].at.tzinfo is not None


def test_traffic_delay_honors_explicit_extra_minutes_semantics():
    ids, plan, _cruise = _active_with_c4()
    jid = ids["journey_id"]
    tech = next(item for item in plan["activities"] if item["place_id"] == "place-tech-art")
    event = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={
            "event_type": "TRAFFIC_DELAY",
            "affected_activity_id": tech["id"],
            "occurred_at": "2026-10-10T12:50:00+08:00",
            "payload": {
                "from_place_id": "place-food-hall",
                "to_place_id": "place-tech-art",
                "extra_minutes": 25,
            },
        },
    )
    assert event.status_code == 201, event.text
    proposal = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event.json()["id"], "candidate_count": 3},
    )
    assert proposal.status_code == 201, proposal.text
    with SessionLocal() as session:
        record = session.get(ReplanProposal, proposal.json()["id"])
        changed = session.scalar(
            select(Activity).where(
                Activity.plan_version_id == record.proposed_plan_version_id,
                Activity.place_id == "place-tech-art",
            )
        )
        assert changed is not None
        delta = datetime.fromisoformat(changed.start_at) - datetime.fromisoformat(tech["start_at"])
        assert int(delta.total_seconds() // 60) == 25


# V2-B1: replan now refuses the mock fixture's blanket default 20 as a hard fact.
# Seed the demo graph with *explicit* mobility so these replan-dependent tests
# exercise trustworthy legs (see tests/_qoder_explicit_demo_mobility.py).
import pytest as _pytest  # noqa: E402
from _qoder_explicit_demo_mobility import POST_GOAL_MOBILITY as _MODULE_MOBILITY, seed_module_mobility as _seed  # noqa: E402
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


@_pytest.fixture(autouse=True)
def _qoder_explicit_demo_mobility(monkeypatch):
    _seed(monkeypatch, _MODULE_MOBILITY)
