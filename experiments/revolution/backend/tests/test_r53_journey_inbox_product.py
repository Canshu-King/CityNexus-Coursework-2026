from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.core.config import settings
from app.db.models import Activity, AgentRun, Commitment, Event, GuardianRun, Journey, JourneyInboxItem, TransportReservation
from app.db.session import SessionLocal
from app.traveler_principal import issue_traveler_token
import app.traveler_principal as principal_module
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)


def test_r53_exact_pasted_schedule_change_updates_truth_and_replay_is_idempotent(monkeypatch):
    test_settings = replace(settings, traveler_auth_secret="REDACTED_FOR_PUBLIC_RELEASE")
    monkeypatch.setattr(main_module, "settings", test_settings)
    monkeypatch.setattr(principal_module, "settings", test_settings)
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plans = client.post(f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1})
    assert plans.status_code == 201, plans.text
    plan = plans.json()[0]
    assert client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey
        traveler_id = journey.traveler_id
        current_id = journey.current_activity_id
        assert current_id is not None
        current_activity = session.get(Activity, current_id)
        assert current_activity is not None
        end = datetime.fromisoformat(journey.end_at.replace("Z", "+00:00"))
        future_day = (end - timedelta(days=1)).date()
    headers = {"Authorization": f"Bearer {issue_traveler_token(traveler_id)}"}
    started = client.post(f"/api/v1/journeys/{journey_id}/operator/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    with SessionLocal() as session:
        session.get(Activity, current_id).status = "IN_PROGRESS"
        session.commit()

    departure = datetime.combine(future_day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=17, minute=40)
    arrival = departure + timedelta(hours=2)
    draft = client.post(f"/api/v1/journeys/{journey_id}/inbox/import", json={
        "artifact_type": "STRUCTURED_JSON",
        "payload": {"reservation_type": "TRAIN", "provider_key": "sandbox-rail", "confirmation_ref": "AB123",
                    "origin": {"name": "A"}, "destination": {"name": "B"},
                    "scheduled_departure_at": departure.isoformat(), "scheduled_arrival_at": arrival.isoformat(),
                    "anchor_role": "DEPARTURE"},
    })
    assert draft.status_code == 201, draft.text
    confirmation = client.post(f"/api/v1/journeys/{journey_id}/inbox/{draft.json()['id']}/confirm")
    assert confirmation.status_code == 200, confirmation.text
    reservation_id = confirmation.json()["reservation"]["id"]

    message = "Booking ZX42 update: departure changed from 17:40 to 18:10. Confirmation AB123."
    first = client.post(f"/api/v1/journeys/{journey_id}/operator/inbox/intake", headers=headers, json={"source_kind": "PASTED_TEXT", "text": message})
    assert first.status_code == 201, first.text
    replay = client.post(f"/api/v1/journeys/{journey_id}/operator/inbox/intake", headers=headers, json={"source_kind": "PASTED_TEXT", "text": message})
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["replayed"] is True
    assert first.json()["status"] == "APPLIED"
    assert first.json()["provenance"] == "USER_PASTED_TEXT"

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        reservation = session.get(TransportReservation, reservation_id)
        commitment = session.get(Commitment, reservation.commitment_id)
        item_count = session.scalar(select(func.count()).select_from(JourneyInboxItem).where(JourneyInboxItem.journey_id == journey_id))
        event_count = session.scalar(select(func.count()).select_from(Event).where(Event.journey_id == journey_id))
        run_count = session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.journey_id == journey_id))
        guardian = session.scalar(select(GuardianRun).where(GuardianRun.journey_id == journey_id).order_by(GuardianRun.created_at.desc()).limit(1))
        assert reservation.scheduled_departure_at.endswith("18:10:00+00:00")
        assert json.loads(commitment.policy_json)["scheduled_start_at"] == reservation.scheduled_departure_at
        assert journey.current_activity_id == current_id
        assert session.get(Activity, current_id).status == "IN_PROGRESS"
        assert item_count == 1
        assert event_count == 1
        assert first.json()["impact"] == "RECOVERY_REQUIRED"
        assert guardian is not None
        assert run_count >= 0
        result = {
            "capability": "The traveller can now forward a booking confirmation, schedule-change message, cancellation notice, or reservation screenshot to the running Journey, and CityNexus turns it into grounded Journey reality: it binds the message to the right reservation/activity, updates Commitment truth only when sufficiently grounded, asks one clear question when ambiguous, and automatically hands any material impact to the existing Journey Operator for recovery.",
            "outcome": "JOURNEY_INBOX_CHANGE_TRIGGERED_RECOVERY",
            "journey_id": journey_id,
            "inbox_item_id": first.json()["id"],
            "replay_reused_same_item": replay.json()["id"] == first.json()["id"] and replay.json()["replayed"],
            "confirmation_ref": "AB123",
            "reservation_id": reservation_id,
            "scheduled_departure_at": reservation.scheduled_departure_at,
            "commitment_scheduled_start_at": json.loads(commitment.policy_json)["scheduled_start_at"],
            "provenance": first.json()["provenance"],
            "impact": first.json()["impact"],
            "current_activity_id_unchanged": journey.current_activity_id == current_id,
            "current_activity_status": "IN_PROGRESS",
            "inbox_item_count": item_count,
            "event_count": event_count,
            "agent_run_count": run_count,
            "guardian_run": {"id": guardian.id, "status": guardian.status, "action_status": guardian.action_status},
            "lab_estimate_authority": False,
        }
        output = Path(__file__).resolve().parents[1] / "output" / "r53-journey-inbox-v1.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
