from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db.models import Event, Journey
from app.db.session import SessionLocal
from app.integrations.context.open_meteo import (
    OpenMeteoContextProvider,
    OpenMeteoProviderError,
    WeatherObservation,
    WeatherRisk,
)
from app.integrations.context.weather_runtime import refresh_weather_runtime
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)


def _started_journey_id() -> str:
    jid = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{jid}/plans/generate",
        json={"candidate_count": 1},
    )
    assert generated.status_code == 201, generated.text
    pid = generated.json()[0]["id"]
    assert client.post(f"/api/v1/journeys/{jid}/plans/{pid}/select").status_code == 200
    assert client.post(f"/api/v1/journeys/{jid}/start").status_code == 200
    return jid


@pytest.mark.asyncio
async def test_wrh01_malformed_parallel_open_meteo_arrays_are_rejected_not_silently_truncated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": [1791615600, 1791619200],
                    "precipitation_probability": [90],
                    "weather_code": [65, 65],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenMeteoContextProvider(
            client=http_client,
            base_url="https://weather.test",
        )
        with pytest.raises(OpenMeteoProviderError):
            await provider.forecast(latitude=39.12, longitude=117.21)


class _SequenceProvider:
    provider_key = "open-meteo"

    def __init__(self, probabilities: list[int]):
        self.probabilities = list(probabilities)
        self.round = 0

    async def forecast(
        self,
        *,
        latitude: float,
        longitude: float,
    ) -> list[WeatherObservation]:
        # The demo has two eligible places. Advance after the second place so
        # each probability value represents one complete refresh operation.
        index = min(self.round, len(self.probabilities) - 1)
        probability = self.probabilities[index]
        if longitude >= 117.20:
            self.round += 1
        return [
            WeatherObservation(
                at=datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc),
                precipitation_probability=probability,
                weather_code=65,
            )
        ]

    def risks(self, observations: list[WeatherObservation]) -> list[WeatherRisk]:
        return [
            WeatherRisk(
                code="HEAVY_RAIN_RISK",
                severity="HIGH",
                at=observation.at,
                evidence={
                    "precipitation_probability": observation.precipitation_probability,
                    "weather_code": observation.weather_code,
                },
            )
            for observation in observations
            if (observation.precipitation_probability or 0) >= 70
        ]


@pytest.mark.asyncio
async def test_wrh02_weather_event_persists_snapshot_capture_time_separate_from_forecast_validity():
    jid = _started_journey_id()
    provider = _SequenceProvider([90])
    captured = "2026-10-10T03:00:00+00:00"
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        assert journey is not None
        result = await refresh_weather_runtime(
            session,
            journey,
            provider=provider,
            captured_at=captured,
        )
        session.commit()
        event = session.get(Event, result.event_ids[0])
        assert event is not None
        payload = json.loads(event.payload_json)

    assert payload["snapshot_captured_at"] == captured
    assert payload["forecast_valid_from"] == "2026-10-10T07:00:00+00:00"


@pytest.mark.asyncio
async def test_wrh03_forecast_event_effective_occurrence_is_detection_time_not_future_valid_from():
    jid = _started_journey_id()
    provider = _SequenceProvider([90])
    captured = "2026-10-10T03:00:00+00:00"
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        assert journey is not None
        result = await refresh_weather_runtime(
            session,
            journey,
            provider=provider,
            captured_at=captured,
        )
        session.commit()
        event = session.get(Event, result.event_ids[0])
        assert event is not None

    # Replan uses Event occurrence as its timeline anchor. A 15:00-local
    # forecast discovered at 11:00-local must not pretend the clock is 15:00.
    assert event.occurred_at == captured


@pytest.mark.asyncio
async def test_wrh04_successful_new_forecast_resolves_prior_weather_risk_when_same_bucket_clears():
    jid = _started_journey_id()
    provider = _SequenceProvider([90, 20])
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        assert journey is not None
        first = await refresh_weather_runtime(
            session,
            journey,
            provider=provider,
            captured_at="2026-10-10T03:00:00+00:00",
        )
        session.commit()
        assert first.event_ids
        first_event = session.get(Event, first.event_ids[0])
        assert first_event is not None and first_event.status == "DETECTED"

        second = await refresh_weather_runtime(
            session,
            journey,
            provider=provider,
            captured_at="2026-10-10T03:05:00+00:00",
        )
        session.commit()
        refreshed = session.get(Event, first_event.id)

        assert second.degraded is False
        assert refreshed is not None
        assert refreshed.status == "RESOLVED"
        assert refreshed.resolved_at is not None


@pytest.mark.asyncio
async def test_wrh05_same_forecast_identity_refreshes_evidence_instead_of_stale_probability():
    jid = _started_journey_id()
    provider = _SequenceProvider([80, 95])
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        assert journey is not None
        first = await refresh_weather_runtime(
            session,
            journey,
            provider=provider,
            captured_at="2026-10-10T03:00:00+00:00",
        )
        session.commit()
        event_id = first.event_ids[0]

        second = await refresh_weather_runtime(
            session,
            journey,
            provider=provider,
            captured_at="2026-10-10T03:05:00+00:00",
        )
        session.commit()
        assert second.event_ids == (event_id,)
        event = session.get(Event, event_id)
        assert event is not None
        payload = json.loads(event.payload_json)

    assert payload["provider_evidence"]["precipitation_probability"] == 95
    assert payload["snapshot_captured_at"] == "2026-10-10T03:05:00+00:00"


@pytest.mark.asyncio
async def test_wrh06_weather_replan_uses_capture_time_as_mobility_snapshot_decision_time():
    jid = _started_journey_id()

    class _OldTownRainProvider:
        provider_key = "open-meteo"

        async def forecast(
            self,
            *,
            latitude: float,
            longitude: float,
        ) -> list[WeatherObservation]:
            return [
                WeatherObservation(
                    at=datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc),
                    precipitation_probability=90,
                    weather_code=65,
                )
            ]

        def risks(self, observations: list[WeatherObservation]) -> list[WeatherRisk]:
            return [
                WeatherRisk(
                    code="HEAVY_RAIN_RISK",
                    severity="HIGH",
                    at=observation.at,
                    evidence={
                        "precipitation_probability": observation.precipitation_probability,
                        "weather_code": observation.weather_code,
                    },
                )
                for observation in observations
            ]

    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        assert journey is not None
        result = await refresh_weather_runtime(
            session,
            journey,
            provider=_OldTownRainProvider(),
            captured_at="2026-10-10T00:30:00+00:00",
        )
        session.commit()

    assert len(result.event_ids) == 1
    response = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": result.event_ids[0], "candidate_count": 3},
        headers={"Idempotency-Key": f"wrh06-{uuid4()}"},
    )
    assert response.status_code == 201, response.text
    snapshot = response.json()["diff"]["verified_evidence"]["mobility"]["snapshot"]
    assert snapshot["captured_at"] == "2026-10-10T08:30:00+08:00"
