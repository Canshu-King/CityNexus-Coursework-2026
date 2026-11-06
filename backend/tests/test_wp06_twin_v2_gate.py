from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Activity, Event, ReplanProposal
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)


def _active_with_c4() -> tuple[str, dict, dict]:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plan = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    assert (
        client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
    )
    cruise = next(a for a in plan["activities"] if a["place_id"] == "place-river-cruise")
    booking = client.post(
        f"/api/v1/journeys/{journey_id}/bookings", json={"activity_id": cruise["id"]}
    ).json()
    assert (
        client.post(
            f"/api/v1/journeys/{journey_id}/transactions",
            json={
                "booking_id": booking["id"],
                "transaction_type": "CAPTURE",
                "amount": booking["amount"],
            },
            headers={"Idempotency-Key": "wp06-twin-c4-capture"},
        ).status_code
        == 201
    )
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    return journey_id, plan, cruise


def test_twin_v2_derives_envelope_opportunity_and_degradation_at_injected_time():
    journey_id, _plan, _cruise = _active_with_c4()
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "OTHER",
            "payload": {"degraded_providers": ["valhalla"]},
        },
    )
    assert event.status_code == 201
    twin = client.get(
        f"/api/v1/journeys/{journey_id}/twin",
        params={"now": "2026-10-10T13:00:00+08:00"},
    ).json()
    context = twin["context"]
    assert context["degraded_providers"] == ["valhalla"]
    assert context["feasibility_envelope"]["preserved_commitment_count"] == 1
    assert "PROVIDER_DEGRADED" in context["feasibility_envelope"]["risk_reason_codes"]
    assert context["opportunity_window"]["next_commitment_start"] == "2026-10-10T15:30:00+08:00"
    assert context["opportunity_window"]["available_minutes"] >= 0


def test_delay_family_persists_real_diff_and_envelopes_while_preserving_c4():
    journey_id, plan, cruise = _active_with_c4()
    tech = next(a for a in plan["activities"] if a["place_id"] == "place-tech-art")
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "SCHEDULE_DELAY",
            "affected_activity_id": tech["id"],
            "payload": {"delay_minutes": 15},
        },
    ).json()
    proposal = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": 3},
    )
    assert proposal.status_code == 201, proposal.text
    body = proposal.json()
    assert body["locked_commitments_preserved"] is True
    assert body["diff"]["moved"]
    assert body["diff"]["before_feasibility_envelope"]
    assert body["diff"]["after_feasibility_envelope"]
    with SessionLocal() as session:
        record = session.get(ReplanProposal, body["id"])
        proposed_cruise = session.scalar(
            select(Activity).where(
                Activity.plan_version_id == record.proposed_plan_version_id,
                Activity.place_id == "place-river-cruise",
            )
        )
        assert datetime.fromisoformat(proposed_cruise.start_at) == datetime.fromisoformat(
            cruise["start_at"]
        )


def test_weather_family_uses_deterministic_indoor_replacement():
    journey_id, plan, _cruise = _active_with_c4()
    tech = next(a for a in plan["activities"] if a["place_id"] == "place-tech-art")
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={"event_type": "WEATHER", "affected_activity_id": tech["id"]},
    ).json()
    proposal = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event["id"]},
    ).json()
    assert "place-tech-art" in proposal["diff"]["removed"]
    assert "place-design-library" in proposal["diff"]["added"]


def test_non_actionable_event_and_stale_proposal_fail_safely():
    journey_id, _plan, _cruise = _active_with_c4()
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events", json={"event_type": "CLOSURE"}
    ).json()
    with SessionLocal() as session:
        row = session.get(Event, event["id"])
        row.status = "RESOLVED"
        session.commit()
    assert (
        client.post(
            f"/api/v1/journeys/{journey_id}/replans", json={"trigger_event_id": event["id"]}
        ).status_code
        == 409
    )
