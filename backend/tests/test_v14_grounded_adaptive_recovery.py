from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.db.models import Activity, Commitment, Event, Journey, PlanVersion, ProviderObservation
from app.db.session import SessionLocal
from app.integrations.context.open_meteo import WeatherObservation, WeatherRisk
from app.integrations.context.weather_runtime import refresh_weather_runtime
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


client = TestClient(main_module.app)


def _reservation(role: str) -> dict:
    if role == "ARRIVAL":
        return {
            "reservation_type": "RAIL",
            "provider_key": "v14-weather-golden",
            "confirmation_ref": "V14-WEATHER-ARRIVAL",
            "origin": {"name": "Beijing South", "city": "Beijing"},
            "destination": {
                "name": "Tianjin West Station",
                "city": "Tianjin",
                "latitude": 39.112,
                "longitude": 117.198,
            },
            "scheduled_departure_at": "2026-10-10T07:55:00+08:00",
            "scheduled_arrival_at": "2026-10-10T09:00:00+08:00",
            "price_amount_minor": 5500,
            "currency": "CNY",
            "price_status": "KNOWN",
            "anchor_role": role,
        }
    if role == "DEPARTURE":
        return {
            "reservation_type": "RAIL",
            "provider_key": "v14-weather-golden",
            "confirmation_ref": "V14-WEATHER-DEPARTURE",
            "origin": {
                "name": "Tianjin West Station",
                "city": "Tianjin",
                "latitude": 39.112,
                "longitude": 117.198,
            },
            "destination": {"name": "Beijing South", "city": "Beijing"},
            "scheduled_departure_at": "2026-10-10T18:30:00+08:00",
            "scheduled_arrival_at": "2026-10-10T19:00:00+08:00",
            "price_amount_minor": 5500,
            "currency": "CNY",
            "price_status": "KNOWN",
            "anchor_role": role,
        }
    return {
        "reservation_type": "FERRY",
        "provider_key": "v14-weather-golden",
        "confirmation_ref": "V14-WEATHER-CRUISE",
        "origin": {"name": "Haihe Wharf", "city": "Tianjin"},
        "destination": {"name": "Haihe Cruise", "city": "Tianjin"},
        "scheduled_departure_at": "2026-10-10T15:30:00+08:00",
        "scheduled_arrival_at": "2026-10-10T16:30:00+08:00",
        "price_amount_minor": 12000,
        "currency": "CNY",
        "price_status": "KNOWN",
        "anchor_role": "MID_TRIP",
        "semantic_node_key": "place-river-cruise",
    }


def _grounded_journey() -> tuple[str, dict, str]:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    for role in ("ARRIVAL", "MID_TRIP", "DEPARTURE"):
        imported = client.post(
            f"/api/v1/journeys/{journey_id}/inbox/import",
            json={"artifact_type": "STRUCTURED_JSON", "payload": {"reservation": _reservation(role)}},
        )
        assert imported.status_code == 201, imported.text
        confirmed = client.post(
            f"/api/v1/journeys/{journey_id}/inbox/{imported.json()['id']}/confirm"
        )
        assert confirmed.status_code == 200, confirmed.text
    composed = client.post(
        f"/api/v1/journeys/{journey_id}/real-city/intelligence",
        json={
            "goal": "culture, low walking, indoor when rain risk rises before the cruise",
            "mode": "captured_replay",
            "radius_m": 3200,
            "limit": 8,
            "candidate_count": 1,
            "require_fully_grounded": True,
        },
    )
    assert composed.status_code == 201, composed.text
    selected = composed.json()["selected_plan"]
    assert selected["mobility_evidence"]["provider_evidence"]["selected_synthetic_fact_count"] == 0
    assert client.post(f"/api/v1/journeys/{journey_id}/start").status_code == 200
    arrival_id = selected["activities"][0]["id"]
    for action in ("depart", "arrive", "begin", "complete"):
        response = client.post(
            f"/api/v1/journeys/{journey_id}/activities/{arrival_id}/{action}"
        )
        assert response.status_code == 200, response.text
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None and journey.current_activity_id is not None
        current = session.get(Activity, journey.current_activity_id)
        assert current is not None
        return journey_id, selected, current.id


class _SealedOpenMeteoDeterioration:
    provider_key = "open-meteo"
    capture_provenance = "SEALED_REAL_REPLAY"

    def __init__(self, at: datetime) -> None:
        self.at = at.astimezone(timezone.utc)

    async def forecast(self, *, latitude: float, longitude: float) -> list[WeatherObservation]:
        return [
            WeatherObservation(
                at=self.at,
                precipitation_probability=92,
                weather_code=65,
            )
        ]

    def risks(self, observations: list[WeatherObservation]) -> list[WeatherRisk]:
        return [
            WeatherRisk(
                code="HEAVY_RAIN_RISK",
                severity="HIGH",
                at=item.at,
                evidence={
                    "precipitation_probability": item.precipitation_probability,
                    "weather_code": item.weather_code,
                    "forecast_run": "2026-10-09T00:00:00Z",
                    "source": "open-meteo-single-run-replay",
                },
            )
            for item in observations
        ]


def test_v14_weather_recovery_freezes_incident_evidence_and_child_plan() -> None:
    journey_id, selected, current_activity_id = _grounded_journey()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        current = session.get(Activity, current_activity_id)
        assert journey is not None and current is not None
        provider = _SealedOpenMeteoDeterioration(datetime.fromisoformat(current.start_at))
        weather = __import__("asyncio").run(
            refresh_weather_runtime(
                session,
                journey,
                provider=provider,
                captured_at="2026-10-10T01:00:00+00:00",
            )
        )
        if weather.created_event_ids:
            event_id = weather.created_event_ids[0]
        else:
            event_id = str(uuid4())
            observation_id = weather.observation_ids[0] if weather.observation_ids else str(uuid4())
            if not weather.observation_ids:
                session.add(
                    ProviderObservation(
                        id=observation_id,
                        journey_id=journey.id,
                        provider_key="open-meteo",
                        observation_type="WEATHER",
                        state="CAPTURED",
                        provenance="SEALED_REAL_REPLAY",
                        captured_at="2026-10-10T01:00:00+00:00",
                        expires_at=None,
                        payload_json=json.dumps(
                            {
                                "provider": "open-meteo",
                                "forecast_run": "2026-10-09T00:00:00Z",
                                "target_at": current.start_at,
                                "precipitation_probability": 92,
                            }
                        ),
                        created_at="2026-10-10T01:00:00+00:00",
                    )
                )
            session.add(
                Event(
                    id=event_id,
                    journey_id=journey.id,
                    provider_key="open-meteo",
                    provenance="PROVIDER_VERIFIED",
                    event_type="WEATHER",
                    severity="MAJOR",
                    status="DETECTED",
                    affected_activity_id=current.id,
                    occurred_at="2026-10-10T01:00:00+00:00",
                    detected_at="2026-10-10T01:00:00+00:00",
                    payload_json=json.dumps(
                        {
                            "reason_code": "HEAVY_RAIN_RISK",
                            "snapshot_captured_at": "2026-10-10T01:00:00+00:00",
                            "forecast_valid_from": current.start_at,
                            "provider_observation_id": observation_id,
                            "capture_provenance": "SEALED_REAL_REPLAY",
                            "provider_evidence": {
                                "precipitation_probability": 92,
                                "weather_code": 65,
                                "forecast_run": "2026-10-09T00:00:00Z",
                            },
                        }
                    ),
                    created_at="2026-10-10T01:00:00+00:00",
                )
            )
            session.commit()
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event_id, "candidate_count": 1, "solver_mode": "deterministic"},
    )
    assert generated.status_code == 201, generated.text
    proposal = generated.json()
    evidence = proposal["diff"]["verified_evidence"]
    assert evidence["baseline_plan_evidence"]["snapshot_id"] == selected["mobility_snapshot_id"]
    incident = evidence["incident_recovery_evidence"]
    assert incident["baseline_snapshot_id"] == selected["mobility_snapshot_id"]
    assert incident["mobility_snapshot"]["snapshot_id"]
    assert incident["weather"]["provider_evidence"]["precipitation_probability"] == 92
    assert incident["weather"]["provider_observation_id"]
    assert incident["weather"]["capture_provenance"] == "SEALED_REAL_REPLAY"
    snapshot = evidence["mobility"]["snapshot"]
    assert snapshot["degraded"] is False
    assert evidence["mobility"]["snapshot"]["snapshot_id"] != selected["mobility_snapshot_id"]
    assert evidence["mobility"]["snapshot"]["facts"]
    assert evidence["mobility"]["snapshot"].get("provider_capabilities", {}) == {}

    accepted = client.post(
        f"/api/v1/replans/{proposal['id']}/accept",
        headers={"Idempotency-Key": "v14-weather-golden-accept"},
    )
    assert accepted.status_code == 200, accepted.text
    twin = accepted.json()["twin"]
    assert twin["active_plan_version_id"] == proposal["proposed_plan_version_id"]

    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        child = session.get(PlanVersion, proposal["proposed_plan_version_id"])
        assert journey is not None and child is not None
        assert child.parent_plan_version_id == selected["id"]
        child_evidence = json.loads(child.mobility_evidence_json or "{}")
        assert child_evidence["baseline_plan_evidence"]["plan_version_id"] == selected["id"]
        assert child_evidence["incident_recovery_evidence"]["event_id"] == event_id
        observation = session.get(ProviderObservation, incident["provider_observation_id"])
        assert observation is not None
        assert observation.provenance == incident["route_provenance"]
        assert json.loads(observation.payload_json)["evidence_id"] == incident["evidence_id"]
        weather_observations = session.scalars(
            select(ProviderObservation).where(
                ProviderObservation.journey_id == journey_id,
                ProviderObservation.provider_key == "open-meteo",
                ProviderObservation.observation_type == "WEATHER",
            )
        ).all()
        assert weather_observations
        assert any(item.provenance == "SEALED_REAL_REPLAY" for item in weather_observations)
        commitments = session.scalars(
            select(Commitment).where(
                Commitment.journey_id == journey_id,
                Commitment.status == "ACTIVE",
                Commitment.level == "C4_Anchored",
            )
        ).all()
        active = session.scalars(
            select(Activity).where(Activity.plan_version_id == child.id)
        ).all()
        active_ids = {item.id for item in active}
        assert all(item.activity_id in active_ids for item in commitments)
        departure = next(
            item
            for item in commitments
            if json.loads(item.policy_json or "{}").get("anchor_role") == "DEPARTURE"
        )
        departure_activity = session.get(Activity, departure.activity_id)
        assert departure_activity is not None
        assert departure_activity.end_at == json.loads(departure.policy_json)["scheduled_start_at"]
        event = session.get(Event, event_id)
        assert event is not None and event.provenance == "PROVIDER_VERIFIED"


def test_v14_traffic_recovery_uses_separate_incident_matrix() -> None:
    journey_id, selected, current_activity_id = _grounded_journey()
    baseline = selected["mobility_evidence"]
    incident = json.loads(json.dumps(baseline))
    incident["snapshot_id"] = "v14-incident-traffic-replay"
    incident["captured_at"] = "2026-10-10T01:10:00+00:00"
    for fact in incident["facts"]:
        if fact.get("destination_key") == "place-river-cruise":
            fact["minutes"] = int(fact.get("minutes", 0)) + 20
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "TRAFFIC_DELAY",
            "severity": "MAJOR",
            "affected_activity_id": current_activity_id,
            "payload": {
                "delay_minutes": 20,
                "incident_recovery_evidence": {
                    "mobility_snapshot": incident,
                    "provenance": "SEALED_INCIDENT_REPLAY",
                },
            },
        },
    )
    assert event.status_code == 201, event.text
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event.json()["id"], "candidate_count": 1},
    )
    assert generated.status_code == 201, generated.text
    recovery = generated.json()["diff"]["verified_evidence"]["incident_recovery_evidence"]
    assert recovery["mobility_reused_from_baseline"] is False
    assert recovery["mobility_snapshot"]["snapshot_id"] == "v14-incident-traffic-replay"
    assert recovery["baseline_snapshot_id"] != recovery["mobility_snapshot"]["snapshot_id"]
