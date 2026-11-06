from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
import app.traveler_principal as principal_module
from app.core.config import settings
from app.db.models import (
    Activity, Commitment, Event, GuardianRun, Journey, JourneyInboxItem,
    ProviderObservation, RealityClaim, RealityConflict, TransportReservation,
)
from app.db.session import SessionLocal
from app.traveler_principal import issue_traveler_token
from demo_stage import demo_reset_headers


client = TestClient(main_module.app)


def _count(session, model, journey_id):
    return int(session.scalar(select(func.count()).select_from(model).where(model.journey_id == journey_id)) or 0)


def test_r54_fresh_reservation_time_conflict_pauses_until_one_user_resolution(monkeypatch):
    isolated = replace(settings, traveler_auth_secret="REDACTED_FOR_PUBLIC_RELEASE", lab_estimate_authority=False)
    monkeypatch.setattr(main_module, "settings", isolated)
    monkeypatch.setattr(principal_module, "settings", isolated)
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    plans = client.post(f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1})
    assert plans.status_code == 201, plans.text
    plan = plans.json()[0]
    assert client.post(f"/api/v1/journeys/{journey_id}/plans/{plan['id']}/select").status_code == 200
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey and journey.current_activity_id
        traveler_id, current_activity_id = journey.traveler_id, journey.current_activity_id
        future_day = (datetime.fromisoformat(journey.end_at.replace("Z", "+00:00")) - timedelta(days=1)).date()
    headers = {"Authorization": f"Bearer {issue_traveler_token(traveler_id)}"}
    started = client.post(f"/api/v1/journeys/{journey_id}/operator/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    with SessionLocal() as session:
        session.get(Activity, current_activity_id).status = "IN_PROGRESS"
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
    confirmed = client.post(f"/api/v1/journeys/{journey_id}/inbox/{draft.json()['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    reservation_id = confirmed.json()["reservation"]["id"]

    older = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    old_observation_id = str(uuid4())
    with SessionLocal() as session:
        session.add(ProviderObservation(id=old_observation_id, journey_id=journey_id,
            provider_key="sealed-real-replay", observation_type="RESERVATION_TIME", state="CAPTURED",
            incident_fingerprint=None, provenance="REPLAY", captured_at=older, expires_at=None,
            payload_json=json.dumps({"confirmation_ref": "AB123", "scheduled_departure_at": departure.isoformat()}),
            created_at=older))
        session.commit()
    seeded = client.post(f"/api/v1/journeys/{journey_id}/operator/reality/provider-replay", headers=headers, json={
        "provider_observation_id": old_observation_id,
    })
    assert seeded.status_code == 200, seeded.text
    assert seeded.json()["provenance"] == "CAPTURED_REPLAY"

    message = "Booking ZX42 update: departure changed from 17:40 to 18:10. Confirmation AB123."
    pasted = client.post(f"/api/v1/journeys/{journey_id}/operator/inbox/intake", headers=headers,
                         json={"source_kind": "PASTED_TEXT", "text": message})
    assert pasted.status_code == 201, pasted.text
    item_id = pasted.json()["id"]
    with SessionLocal() as session:
        reservation = session.get(TransportReservation, reservation_id)
        assert reservation and reservation.scheduled_departure_at.endswith("18:10:00+00:00")
        assert reservation.source_provenance == "USER_PASTED_TEXT"
        events_before_provider = _count(session, Event, journey_id)
        guardians_before_provider = _count(session, GuardianRun, journey_id)

    fresh_observation_id = str(uuid4())
    fresh_captured_at = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(ProviderObservation(id=fresh_observation_id, journey_id=journey_id,
            provider_key="sealed-real-replay", observation_type="RESERVATION_TIME", state="CAPTURED",
            incident_fingerprint=None, provenance="REPLAY", captured_at=fresh_captured_at, expires_at=None,
            payload_json=json.dumps({"confirmation_ref": "AB123", "scheduled_departure_at": departure.isoformat()}),
            created_at=fresh_captured_at))
        session.commit()
    fresh = client.post(f"/api/v1/journeys/{journey_id}/operator/reality/provider-replay", headers=headers, json={
        "provider_observation_id": fresh_observation_id,
    })
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["provenance"] == "CAPTURED_REPLAY"
    assert fresh.json()["reconciliation"] == "UNRESOLVED_MATERIAL_CONFLICT"
    conflict_id = fresh.json()["conflict_id"]
    operator = client.get(f"/api/v1/journeys/{journey_id}/operator", headers=headers)
    assert operator.status_code == 200, operator.text
    projection = operator.json()
    assert projection["attention"]["kind"] == "REALITY_CONFLICT_REQUIRED"
    assert projection["attention"]["pending_count"] == 1
    choice = next(option for option in projection["attention"]["options"] if option["label"] == "Use 18:10")
    with SessionLocal() as session:
        reservation = session.get(TransportReservation, reservation_id)
        assert reservation and reservation.scheduled_departure_at.endswith("18:10:00+00:00")
        assert _count(session, Event, journey_id) == events_before_provider
        assert _count(session, GuardianRun, journey_id) == guardians_before_provider
        conflict = session.get(RealityConflict, conflict_id)
        assert conflict and conflict.status == "UNRESOLVED_MATERIAL_CONFLICT"
        generation = conflict.generation

    resolved = client.post(f"/api/v1/journeys/{journey_id}/operator/reality-conflicts/{conflict_id}/resolve",
                           headers=headers, json={"selected_claim_id": choice["value"]})
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == "RESOLVED" and body["generation"] == generation
    assert body["resolution_provenance"] == "USER_CONFIRMED_REALITY_RESOLUTION"
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        reservation = session.get(TransportReservation, reservation_id)
        commitment = session.get(Commitment, reservation.commitment_id)
        conflict = session.get(RealityConflict, conflict_id)
        item = session.get(JourneyInboxItem, item_id)
        assert journey and journey.current_activity_id == current_activity_id
        assert session.get(Activity, current_activity_id).status == "IN_PROGRESS"
        assert reservation and reservation.scheduled_departure_at.endswith("18:10:00+00:00")
        assert reservation.source_provenance == "USER_CONFIRMED_REALITY_RESOLUTION"
        assert json.loads(commitment.policy_json)["source"] == "USER_CONFIRMED_REALITY_RESOLUTION"
        assert conflict and conflict.status == "RESOLVED" and conflict.generation == generation
        assert conflict.resolution_provenance == "USER_CONFIRMED_REALITY_RESOLUTION"
        assert item and item.status == "APPLIED"
        assert session.scalar(select(func.count()).select_from(RealityClaim).where(
            RealityClaim.journey_id == journey_id, RealityClaim.provenance == "USER_CONFIRMED_REALITY_RESOLUTION")) == 1
        assert _count(session, Event, journey_id) == events_before_provider
        final_guardian = session.scalar(select(GuardianRun).where(GuardianRun.journey_id == journey_id)
            .order_by(GuardianRun.created_at.desc()).limit(1))
        assert final_guardian is not None
        result = {
            "capability": "When CityNexus receives conflicting travel facts from different sources, it no longer silently overwrites reality or follows “last write wins.” It reconciles source authority and freshness, freezes unsafe automation when the truth is genuinely contested, asks the traveller one bounded question only when needed, and resumes the same Journey once reality is resolved.",
            "outcome": "REALITY_RECONCILIATION_USER_RESOLVED",
            "journey_id": journey_id, "reservation_id": reservation_id, "confirmation_ref": "AB123",
            "old_provider_claim": {"departure_at": departure.isoformat(), "provenance": "CAPTURED_REPLAY", "freshness": "OLDER"},
            "traveller_claim": {"departure_at": reservation.scheduled_departure_at, "provenance": "USER_PASTED_TEXT"},
            "fresh_provider_claim": {"departure_at": departure.isoformat(), "provenance": "CAPTURED_REPLAY", "is_live": False},
            "conflict_id": conflict_id, "conflict_generation": generation,
            "conflict_attention_kind": projection["attention"]["kind"],
            "conflict_choice_count": len(projection["attention"]["options"]),
            "no_truth_flip_while_conflicted": True,
            "no_new_event_or_guardian_while_conflicted": True,
            "selected_label": "Use 18:10", "resolution_provenance": body["resolution_provenance"],
            "reservation_departure_at_after_resolution": reservation.scheduled_departure_at,
            "current_activity_id_unchanged": journey.current_activity_id == current_activity_id,
            "current_activity_status": "IN_PROGRESS", "event_count": _count(session, Event, journey_id),
            "guardian_after_resolution": {"id": final_guardian.id, "status": final_guardian.status,
                "action_status": final_guardian.action_status},
            "lab_estimate_authority": bool(main_module.settings.lab_estimate_authority),
            "agent_estimate_authority": 0,
        }
        assert result["lab_estimate_authority"] is False
        output = Path(__file__).resolve().parents[1] / "output" / "r54-reality-reconciliation-v1.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
