from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

import app.main as main_module
from app.integrations.context.open_meteo import WeatherObservation, WeatherRisk
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)


class _EndpointWeatherProvider:
    provider_key = "open-meteo"

    def __init__(self) -> None:
        self.calls: list[tuple[float, float]] = []

    async def forecast(self, *, latitude: float, longitude: float) -> list[WeatherObservation]:
        self.calls.append((latitude, longitude))
        return [
            WeatherObservation(
                at=datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc),
                precipitation_probability=90,
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
                },
            )
            for item in observations
        ]


def _started_journey_id() -> str:
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    )
    assert generated.status_code == 201, generated.text
    plan_id = generated.json()[0]["id"]
    selected = client.post(f"/api/v1/journeys/{journey_id}/plans/{plan_id}/select")
    assert selected.status_code == 200, selected.text
    started = client.post(f"/api/v1/journeys/{journey_id}/start")
    assert started.status_code == 200, started.text
    return journey_id


def test_weather_refresh_endpoint_uses_only_server_owned_provider_input(monkeypatch):
    journey_id = _started_journey_id()
    provider = _EndpointWeatherProvider()
    monkeypatch.setattr(
        main_module,
        "build_open_meteo_provider",
        lambda _client: provider,
        raising=False,
    )

    response = client.post(f"/api/v1/journeys/{journey_id}/weather/refresh")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["provider"] == "open-meteo"
    assert payload["queried_place_ids"] == ["place-old-town", "place-river-cruise"]
    assert len(payload["event_ids"]) == 1
    assert provider.calls == [(39.1, 117.18), (39.12, 117.21)]


def test_weather_refresh_endpoint_rejects_client_supplied_weather_facts(monkeypatch):
    journey_id = _started_journey_id()
    provider = _EndpointWeatherProvider()
    monkeypatch.setattr(
        main_module,
        "build_open_meteo_provider",
        lambda _client: provider,
        raising=False,
    )

    response = client.post(
        f"/api/v1/journeys/{journey_id}/weather/refresh",
        json={
            "provider_key": "open-meteo",
            "provenance": "PROVIDER_VERIFIED",
            "affected_activity_id": "client-chosen",
        },
    )

    assert response.status_code == 422
    assert provider.calls == []
