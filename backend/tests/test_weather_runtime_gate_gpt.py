from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Activity, Commitment, Event, Journey, Place
from app.db.session import SessionLocal
from app.integrations.context.open_meteo import (
    OpenMeteoContextProvider,
    OpenMeteoProviderError,
    WeatherObservation,
    WeatherRisk,
)
from app.integrations.context.weather_runtime import (
    attribute_weather_fact_to_activity,
    refresh_weather_runtime,
)
from app.integrations.context.weather_snapshot import (
    WeatherQueryScope,
    WeatherRiskFact,
    freeze_weather_risks,
)
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)


class _WeatherProvider:
    provider_key = "open-meteo"

    def __init__(
        self,
        *,
        risk_at: datetime | None = None,
        fail: bool = False,
        probability: int = 90,
    ) -> None:
        self.risk_at = risk_at
        self.fail = fail
        self.probability = probability
        self.calls: list[tuple[float, float]] = []

    async def forecast(self, *, latitude: float, longitude: float) -> list[WeatherObservation]:
        self.calls.append((latitude, longitude))
        if self.fail:
            raise OpenMeteoProviderError("simulated outage")
        if self.risk_at is None:
            return []
        return [
            WeatherObservation(
                at=self.risk_at,
                precipitation_probability=self.probability,
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
            if item.precipitation_probability is not None and item.precipitation_probability >= 70
        ]


def _prepare_started_journey(*, durable_c4: bool = False) -> tuple[str, dict]:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    jid = ids["journey_id"]
    generated = client.post(f"/api/v1/journeys/{jid}/plans/generate", json={"candidate_count": 1})
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    selected = client.post(f"/api/v1/journeys/{jid}/plans/{plan['id']}/select")
    assert selected.status_code == 200, selected.text

    if durable_c4:
        cruise = next(
            item for item in plan["activities"] if item["place_id"] == "place-river-cruise"
        )
        booking = client.post(
            f"/api/v1/journeys/{jid}/bookings", json={"activity_id": cruise["id"]}
        )
        assert booking.status_code == 201, booking.text
        booking_body = booking.json()
        capture = client.post(
            f"/api/v1/journeys/{jid}/transactions",
            json={
                "booking_id": booking_body["id"],
                "transaction_type": "CAPTURE",
                "amount": booking_body["amount"],
            },
            headers={"Idempotency-Key": f"weather-capture-{uuid4()}"},
        )
        assert capture.status_code == 201, capture.text

    started = client.post(f"/api/v1/journeys/{jid}/start")
    assert started.status_code == 200, started.text
    return jid, plan


def _get_entities(jid: str, plan: dict, place_id: str) -> tuple[Journey, Activity, Place]:
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        activity_id = next(
            item["id"] for item in plan["activities"] if item["place_id"] == place_id
        )
        activity = session.get(Activity, activity_id)
        place = session.get(Place, place_id)
        assert journey is not None and activity is not None and place is not None
        session.expunge(journey)
        session.expunge(activity)
        session.expunge(place)
    return journey, activity, place


@pytest.mark.asyncio
async def test_wrt01_open_meteo_unix_utc_contract_freezes_scoped_hourly_risk():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["timezone"] == "UTC"
        assert request.url.params["timeformat"] == "unixtime"
        return httpx.Response(
            200,
            json={
                "hourly": {
                    "time": [1791615600],  # 2026-10-10 07:00:00 UTC
                    "precipitation_probability": [91],
                    "weather_code": [65],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenMeteoContextProvider(client=http_client, base_url="https://weather.test")
        observations = await provider.forecast(latitude=39.12, longitude=117.21)
        snapshot = freeze_weather_risks(
            provider.risks(observations),
            captured_at="2026-10-10T06:55:00+00:00",
            scope=WeatherQueryScope("place-river-cruise", 39.12, 117.21),
        )

    assert len(snapshot.facts) == 1
    fact = snapshot.facts[0]
    assert fact.at.endswith("+00:00")
    assert fact.valid_until == "2026-10-10T08:00:00+00:00"
    assert fact.scope is not None and fact.scope.place_id == "place-river-cruise"
    assert fact.evidence["precipitation_probability"] == 91


def test_wrt02_utc_fact_is_mapped_to_journey_timezone_before_activity_overlap():
    jid, plan = _prepare_started_journey()
    journey, cruise, place = _get_entities(jid, plan, "place-river-cruise")
    fact = WeatherRiskFact(
        code="HEAVY_RAIN_RISK",
        severity="HIGH",
        at="2026-10-10T07:00:00+00:00",
        valid_until="2026-10-10T08:00:00+00:00",
        provider_key="open-meteo",
        evidence={"precipitation_probability": 90},
        scope=WeatherQueryScope(place.id, place.latitude, place.longitude),
    )
    match = attribute_weather_fact_to_activity(
        journey=journey, activity=cruise, place=place, fact=fact
    )
    assert match is not None
    assert match.local_risk_start_at == "2026-10-10T15:00:00+08:00"
    assert match.local_risk_end_at == "2026-10-10T16:00:00+08:00"


def test_wrt03_non_overlapping_weather_bucket_does_not_target_activity():
    jid, plan = _prepare_started_journey()
    journey, cruise, place = _get_entities(jid, plan, "place-river-cruise")
    fact = WeatherRiskFact(
        code="HEAVY_RAIN_RISK",
        severity="HIGH",
        at="2026-10-10T03:00:00+00:00",
        valid_until="2026-10-10T04:00:00+00:00",
        provider_key="open-meteo",
        evidence={"precipitation_probability": 90},
        scope=WeatherQueryScope(place.id, place.latitude, place.longitude),
    )
    assert (
        attribute_weather_fact_to_activity(journey=journey, activity=cruise, place=place, fact=fact)
        is None
    )


@pytest.mark.asyncio
async def test_wrt04_runtime_persists_exact_query_scope_in_provider_event_evidence():
    jid, _plan = _prepare_started_journey()
    provider = _WeatherProvider(risk_at=datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc))
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        assert journey is not None
        result = await refresh_weather_runtime(
            session, journey, provider=provider, captured_at="2026-10-10T06:55:00+00:00"
        )
        session.commit()
        events = session.scalars(
            select(Event).where(Event.journey_id == jid, Event.event_type == "WEATHER")
        ).all()

    assert result.queried_place_ids == ("place-old-town", "place-river-cruise")
    assert len(events) == 1
    payload = json.loads(events[0].payload_json)
    assert events[0].affected_activity_id is not None
    assert payload["query_scope"] == {
        "place_id": "place-river-cruise",
        "latitude": 39.12,
        "longitude": 117.21,
    }
    assert "WEATHER_SCOPE_MATCH" in result.attributions[0].reason_codes


@pytest.mark.asyncio
async def test_wrt05_explicit_indoor_outdoor_policy_skips_indoor_places():
    jid, _plan = _prepare_started_journey()
    provider = _WeatherProvider(risk_at=None)
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        assert journey is not None
        result = await refresh_weather_runtime(session, journey, provider=provider)

    assert "place-river-cruise" in result.queried_place_ids
    assert "place-old-town" in result.queried_place_ids
    assert "place-tech-art" not in result.queried_place_ids
    assert "place-riverside-museum" not in result.queried_place_ids
    assert "place-food-hall" not in result.queried_place_ids
    assert "place-design-library" not in result.queried_place_ids


@pytest.mark.asyncio
async def test_wrt06_completed_or_past_execution_activity_is_not_queried_again():
    jid, plan = _prepare_started_journey()
    old_town = next(item for item in plan["activities"] if item["place_id"] == "place-old-town")
    completed = client.post(f"/api/v1/journeys/{jid}/activities/{old_town['id']}/complete")
    assert completed.status_code == 200, completed.text

    provider = _WeatherProvider(risk_at=None)
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        assert journey is not None
        result = await refresh_weather_runtime(session, journey, provider=provider)

    assert "place-old-town" not in result.queried_place_ids
    assert "place-river-cruise" in result.queried_place_ids


def test_wrt07_public_event_api_cannot_forge_provider_verified_weather():
    jid, _plan = _prepare_started_journey()
    response = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={
            "provider_key": "open-meteo",
            "provenance": "PROVIDER_VERIFIED",
            "event_type": "WEATHER",
            "severity": "MAJOR",
            "occurred_at": "2026-10-10T07:00:00+00:00",
            "payload": {"reason_code": "HEAVY_RAIN_RISK"},
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_wrt08_repeated_polling_is_idempotent_for_same_weather_fact_activity_and_scope():
    jid, _plan = _prepare_started_journey()
    provider = _WeatherProvider(risk_at=datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc))
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        assert journey is not None
        first = await refresh_weather_runtime(
            session, journey, provider=provider, captured_at="2026-10-10T06:55:00+00:00"
        )
        session.commit()
        second = await refresh_weather_runtime(
            session, journey, provider=provider, captured_at="2026-10-10T06:56:00+00:00"
        )
        session.commit()
        events = session.scalars(
            select(Event).where(
                Event.journey_id == jid,
                Event.provider_key == "open-meteo",
                Event.provenance == "PROVIDER_VERIFIED",
                Event.event_type == "WEATHER",
            )
        ).all()

    assert len(events) == 1
    assert first.event_ids == second.event_ids
    assert len(first.created_event_ids) == 1
    assert second.created_event_ids == ()


@pytest.mark.asyncio
async def test_wrt09_provider_failure_records_one_degradation_and_no_fake_weather_event():
    jid, _plan = _prepare_started_journey()
    provider = _WeatherProvider(fail=True)
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        assert journey is not None
        first = await refresh_weather_runtime(
            session, journey, provider=provider, captured_at="2026-10-10T06:55:00+00:00"
        )
        session.commit()
        second = await refresh_weather_runtime(
            session, journey, provider=provider, captured_at="2026-10-10T06:56:00+00:00"
        )
        session.commit()
        weather_events = session.scalars(
            select(Event).where(Event.journey_id == jid, Event.event_type == "WEATHER")
        ).all()
        outages = session.scalars(
            select(Event).where(
                Event.journey_id == jid,
                Event.event_type == "SERVICE_UNAVAILABLE",
                Event.provider_key == "open-meteo",
                Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
            )
        ).all()

    assert first.degraded is True and second.degraded is True
    assert weather_events == []
    assert len(outages) == 1
    twin = client.get(f"/api/v1/journeys/{jid}/twin", params={"now": "2026-10-10T13:00:00+08:00"})
    assert twin.status_code == 200
    assert twin.json()["projection_status"] == "DEGRADED"
    assert "open-meteo" in twin.json()["context"]["degraded_providers"]


@pytest.mark.asyncio
async def test_wrt10_clean_poll_after_outage_resolves_degradation_and_twin_returns_ready():
    jid, _plan = _prepare_started_journey()
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        assert journey is not None
        failed = await refresh_weather_runtime(
            session,
            journey,
            provider=_WeatherProvider(fail=True),
            captured_at="2026-10-10T06:55:00+00:00",
        )
        assert failed.degraded is True
        session.commit()
        recovered = await refresh_weather_runtime(
            session,
            journey,
            provider=_WeatherProvider(risk_at=None),
            captured_at="2026-10-10T07:00:00+00:00",
        )
        assert recovered.degraded is False
        session.commit()

    twin = client.get(f"/api/v1/journeys/{jid}/twin", params={"now": "2026-10-10T15:00:00+08:00"})
    assert twin.status_code == 200
    assert twin.json()["projection_status"] == "READY"
    assert twin.json()["context"]["degraded_providers"] == []


@pytest.mark.asyncio
async def test_wrt11_weather_can_identify_c4_but_replan_cannot_silently_remove_commitment():
    jid, _plan = _prepare_started_journey(durable_c4=True)
    provider = _WeatherProvider(risk_at=datetime(2026, 10, 10, 7, 0, tzinfo=timezone.utc))
    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        assert journey is not None
        result = await refresh_weather_runtime(
            session, journey, provider=provider, captured_at="2026-10-10T06:55:00+00:00"
        )
        session.commit()
        weather_event = session.get(Event, result.event_ids[0])
        assert weather_event is not None
        assert weather_event.affected_activity_id is not None
        before = session.scalar(
            select(Commitment).where(
                Commitment.journey_id == jid,
                Commitment.level == "C4_Anchored",
                Commitment.status == "ACTIVE",
            )
        )
        assert before is not None
        before_id, before_activity_id = before.id, before.activity_id

    response = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": result.event_ids[0], "candidate_count": 3},
        headers={"Idempotency-Key": f"weather-c4-replan-{uuid4()}"},
    )
    assert response.status_code == 422

    with SessionLocal() as session:
        after = session.get(Commitment, before_id)
        assert after is not None
        assert after.status == "ACTIVE"
        assert after.activity_id == before_activity_id


def test_wrt12_offline_closure_golden_path_still_generates_replan_without_weather_runtime():
    jid, plan = _prepare_started_journey(durable_c4=True)
    tech = next(item for item in plan["activities"] if item["place_id"] == "place-tech-art")
    for activity in plan["activities"]:
        if activity["id"] == tech["id"]:
            break
        response = client.post(f"/api/v1/journeys/{jid}/activities/{activity['id']}/complete")
        assert response.status_code == 200, response.text

    event = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": tech["id"],
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": "offline weather regression guard"},
        },
    )
    assert event.status_code == 201, event.text
    replan = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event.json()["id"], "candidate_count": 3},
        headers={"Idempotency-Key": f"weather-offline-golden-{uuid4()}"},
    )
    assert replan.status_code == 201, replan.text
    assert replan.json()["hard_constraints_passed"] is True
    assert replan.json()["locked_commitments_preserved"] is True
