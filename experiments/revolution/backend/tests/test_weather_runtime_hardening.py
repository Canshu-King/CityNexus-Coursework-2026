from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from app.db.models import Activity, Journey, Place
from app.db.session import SessionLocal
from app.integrations.context.open_meteo import WeatherRisk
from app.integrations.context.weather_ingestion import ingest_provider_verified_weather_event
from app.integrations.context.weather_runtime import attribute_weather_fact_to_activity
from app.integrations.context.weather_snapshot import (
    WeatherQueryScope,
    WeatherRiskFact,
    freeze_weather_risks,
)
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)


def test_weather_freeze_normalizes_provider_and_capture_instants_to_utc():
    snapshot = freeze_weather_risks(
        [
            WeatherRisk(
                code="HEAVY_RAIN_RISK",
                severity="HIGH",
                at=datetime.fromisoformat("2026-10-10T15:00:00+08:00"),
                evidence={"precipitation_probability": 90},
            )
        ],
        captured_at="2026-10-10T14:55:00+08:00",
        scope=WeatherQueryScope("place-river-cruise", 39.12, 117.21),
    )

    assert snapshot.captured_at == "2026-10-10T06:55:00+00:00"
    assert snapshot.facts[0].at == "2026-10-10T07:00:00+00:00"
    assert snapshot.facts[0].valid_until == "2026-10-10T08:00:00+00:00"


def test_weather_attribution_requires_exact_scope_coordinates():
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    generated = client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"candidate_count": 1}
    ).json()[0]
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        activity_id = next(
            item["id"]
            for item in generated["activities"]
            if item["place_id"] == "place-river-cruise"
        )
        activity = session.get(Activity, activity_id)
        place = session.get(Place, "place-river-cruise")
        assert journey is not None and activity is not None and place is not None
        fact = WeatherRiskFact(
            code="HEAVY_RAIN_RISK",
            severity="HIGH",
            at="2026-10-10T07:00:00+00:00",
            valid_until="2026-10-10T08:00:00+00:00",
            provider_key="open-meteo",
            evidence={"precipitation_probability": 90},
            scope=WeatherQueryScope(place.id, place.latitude + 0.01, place.longitude),
        )
        match = attribute_weather_fact_to_activity(
            journey=journey,
            activity=activity,
            place=place,
            fact=fact,
        )

    assert match is None


def test_weather_event_identity_includes_scope_coordinates():
    journey_id = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["journey_id"]
    common = {
        "code": "HEAVY_RAIN_RISK",
        "severity": "HIGH",
        "at": "2026-10-10T07:00:00+00:00",
        "valid_until": "2026-10-10T08:00:00+00:00",
        "provider_key": "open-meteo",
        "evidence": {"precipitation_probability": 90},
    }
    first_fact = WeatherRiskFact(
        **common,
        scope=WeatherQueryScope("place-river-cruise", 39.12, 117.21),
    )
    second_fact = WeatherRiskFact(
        **common,
        scope=WeatherQueryScope("place-river-cruise", 39.13, 117.21),
    )

    with SessionLocal() as session:
        first = ingest_provider_verified_weather_event(
            session, journey_id=journey_id, fact=first_fact
        )
        second = ingest_provider_verified_weather_event(
            session, journey_id=journey_id, fact=second_fact
        )
        session.commit()

    assert first.id != second.id
