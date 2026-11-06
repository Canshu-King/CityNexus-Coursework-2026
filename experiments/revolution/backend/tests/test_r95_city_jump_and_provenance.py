from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.core.config import settings
from app.db.models import Place
from app.db.session import SessionLocal
from app.integrations.discovery.real_city import DiscoveryCandidate


client = TestClient(main_module.app)
TEST_SECRET = "REDACTED_FOR_PUBLIC_RELEASE"


def _traveler(name: str) -> dict:
    response = client.post(
        "/api/v1/travelers",
        json={"display_name": name, "timezone": "Asia/Shanghai", "locale": "en-GB"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_r95_city_search_requires_consent_and_controlled_provider_grade_survives_downstream(monkeypatch):
    previous = {
        "secret": settings.traveler_auth_secret,
        "enabled": settings.real_city_enabled,
        "mode": settings.real_city_mode,
        "nominatim": settings.nominatim_url,
        "city_search_enabled": settings.city_search_enabled,
        "city_search_provider": settings.city_search_provider,
        "city_search_base_url": settings.city_search_base_url,
        "city_search_test_provider": settings.city_search_test_provider,
    }
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    object.__setattr__(settings, "real_city_enabled", True)
    object.__setattr__(settings, "real_city_mode", "live")
    object.__setattr__(settings, "nominatim_url", "http://127.0.0.1:8213")
    object.__setattr__(settings, "city_search_enabled", True)
    object.__setattr__(settings, "city_search_provider", "nominatim")
    object.__setattr__(settings, "city_search_base_url", "http://127.0.0.1:8213")
    object.__setattr__(settings, "city_search_test_provider", True)

    def fake_city_search(_self, query, **kwargs):
        assert query == "Shanghai"
        assert kwargs["city"] == ""
        city_calls.append(query)
        return [
            DiscoveryCandidate(
                provider_key="nominatim",
                external_id="nominatim:relation/95001",
                name="Shanghai",
                category="city",
                latitude=31.2304,
                longitude=121.4737,
                tags=("place", "real_city"),
                opening_hours={},
                metadata={"display_name": "Shanghai, China", "osm_type": "relation"},
            )
        ]

    def fake_overpass(_self, query, **kwargs):
        assert kwargs["max_attempts"] == 1
        candidates = []
        for index, name in enumerate(("R95 Gallery", "R95 Library"), start=1):
            candidates.append(
                DiscoveryCandidate(
                    provider_key="TEST_PROVIDER",
                    external_id=f"overpass:node/95{index:03d}",
                    name=name,
                    category="gallery" if index == 1 else "library",
                    latitude=31.2304 + index / 10000,
                    longitude=121.4737 + index / 10000,
                    tags=("real_city",),
                    opening_hours={"status": "NORMALIZED_VALID", "raw": "10:00-18:00"},
                    metadata={"osm_id": f"node/95{index:03d}", "tags": {"name": name}},
                    distance_m=100.0 + index,
                    cost_evidence={"status": "UNKNOWN", "source": "TEST_PROVIDER"},
                )
            )
        return candidates, [{"endpoint": "http://127.0.0.1:8211/interpreter", "answered": True, "http_status": 200, "elements": 2}]

    city_calls: list[str] = []
    monkeypatch.setattr("app.execution.bootstrap_discovery.NominatimGeocoder.search", fake_city_search)
    monkeypatch.setattr("app.execution.bootstrap_discovery.OverpassPOIProvider.search_with_evidence", fake_overpass)
    owner = _traveler("R95 city jump owner")
    journey_response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": owner["id"],
            "title": "R95 city jump",
            "city_code": f"CN-R95-SH-{uuid4().hex[:8].upper()}",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-15T10:00:00+08:00",
            "end_at": "2026-10-16T18:00:00+08:00",
            "budget": {"amount_minor": 10000, "currency": "CNY"},
        },
    )
    assert journey_response.status_code == 201, journey_response.text
    journey_id = journey_response.json()["id"]
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    try:
        no_consent = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-city-search",
            headers=headers,
            json={"query": "Shanghai", "provider_consent": False, "limit": 3},
        )
        assert no_consent.status_code == 422

        city = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-city-search",
            headers=headers,
            json={"query": "Shanghai", "provider_consent": True, "limit": 3},
        )
        assert city.status_code == 200, city.text
        city_body = city.json()
        assert city_body["status"] == "CITY_RESULTS"
        assert city_body["provider_provenance"] == "TEST_PROVIDER"
        assert city_body["results"][0]["selection_state"] == "CAMERA_ONLY_TAP_REQUIRED"
        assert city_body["results"][0]["source_url"].endswith("/relation/95001")
        cached_city = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-city-search",
            headers=headers,
            json={"query": "Shanghai", "provider_consent": True, "limit": 3},
        )
        assert cached_city.status_code == 200, cached_city.text
        assert cached_city.json()["cache_hit"] is True
        assert city_calls == ["Shanghai"]

        discovery = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery",
            headers=headers,
            json={
                "query": "gallery",
                "radius_m": 2500,
                "limit": 3,
                "source_kind": "MANUAL_PUBLIC_CENTER",
                "provider_consent": True,
                "latitude": 31.2304,
                "longitude": 121.4737,
                "coordinate_system": "WGS84",
                "mode": "live",
            },
        )
        assert discovery.status_code == 200, discovery.text
        body = discovery.json()
        assert body["provider_provenance"] == "TEST_PROVIDER"
        assert all(item["provider_provenance"] == "TEST_PROVIDER" for item in body["candidates"])
        first, second = body["candidates"][:2]

        saved = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers=headers,
            json={"receipt_id": body["receipt_id"], "candidate_id": first["candidate_id"]},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["source_provenance"] == "TEST_PROVIDER"

        preview = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/preview",
            headers=headers,
            json={
                "receipt_id": body["receipt_id"],
                "candidate_id": first["candidate_id"],
                "public_map_consent": True,
                "latitude": first["latitude"],
                "longitude": first["longitude"],
                "coordinate_system": "WGS84",
            },
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["provider_provenance"] == "TEST_PROVIDER"

        selected = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/select",
            headers=headers,
            json={"receipt_id": body["receipt_id"], "candidate_id": second["candidate_id"]},
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["provider_provenance"] == "TEST_PROVIDER"
        with SessionLocal() as session:
            place = session.scalar(select(Place).where(Place.id == selected.json()["selected_place_id"]))
            assert place is not None
            assert json.loads(place.metadata_json)["provider_provenance"] == "TEST_PROVIDER"
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous["secret"])
        object.__setattr__(settings, "real_city_enabled", previous["enabled"])
        object.__setattr__(settings, "real_city_mode", previous["mode"])
        object.__setattr__(settings, "nominatim_url", previous["nominatim"])
        object.__setattr__(settings, "city_search_enabled", previous["city_search_enabled"])
        object.__setattr__(settings, "city_search_provider", previous["city_search_provider"])
        object.__setattr__(settings, "city_search_base_url", previous["city_search_base_url"])
        object.__setattr__(settings, "city_search_test_provider", previous["city_search_test_provider"])


def test_r95_source_grade_operator_scheme_and_global_rate_guard(monkeypatch):
    """Public-provider claims require TLS and a durable >=1s application gate."""

    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete

    import app.execution.bootstrap_discovery as bootstrap
    from app.db.models import IdempotencyKey

    assert bootstrap._provider_provenance([
        {"endpoint": "http://overpass-api.de/api/interpreter", "answered": True}
    ]) == "UNVERIFIED"
    assert bootstrap._provider_provenance([
        {"endpoint": "https://overpass-api.de/api/interpreter", "answered": True}
    ]) == "LIVE_PROVIDER"
    assert bootstrap._provider_provenance([
        {"endpoint": "https://nominatim.openstreetmap.org/search", "answered": True}
    ]) == "LIVE_PROVIDER"
    assert bootstrap._provider_provenance([
        {"endpoint": "http://127.0.0.1:8214/interpreter", "answered": True}
    ]) == "TEST_PROVIDER"
    assert bootstrap._provider_provenance([
        {"endpoint": "https://[broken", "answered": True}
    ]) == "UNVERIFIED"

    keys = (
        "city_search_enabled",
        "city_search_provider",
        "city_search_base_url",
        "city_search_user_agent",
        "city_search_test_provider",
        "city_search_global_min_interval_seconds",
    )
    previous = {key: getattr(settings, key) for key in keys}
    try:
        object.__setattr__(settings, "city_search_enabled", True)
        object.__setattr__(settings, "city_search_provider", "nominatim")
        object.__setattr__(settings, "city_search_user_agent", "CityNexus/1.0 (ops@example.org)")
        object.__setattr__(settings, "city_search_test_provider", True)
        object.__setattr__(settings, "city_search_base_url", "http://nominatim.openstreetmap.org")
        assert bootstrap._city_search_config() is None
        object.__setattr__(settings, "city_search_base_url", "https://nominatim.openstreetmap.org")
        object.__setattr__(settings, "city_search_user_agent", "")
        assert bootstrap._city_search_config() is None
        object.__setattr__(settings, "city_search_user_agent", "CityNexus/1.0 (ops@example.org)")
        assert bootstrap._city_search_config() is not None

        object.__setattr__(settings, "city_search_global_min_interval_seconds", 0.1)
        unique_key = f"r95-test-city-global-rate:{uuid4()}"
        monkeypatch.setattr(bootstrap, "_CITY_RATE_KEY", unique_key)
        now = datetime.now(timezone.utc)
        with SessionLocal() as session:
            try:
                assert bootstrap._reserve_city_search_global_slot(session, now=now)
                session.commit()
                # Even an erroneous 100ms operator setting cannot permit
                # another public city geocode at 200ms.
                assert not bootstrap._reserve_city_search_global_slot(
                    session, now=now + timedelta(milliseconds=200)
                )
            finally:
                session.rollback()
                session.execute(delete(IdempotencyKey).where(IdempotencyKey.key == unique_key))
                session.commit()
    finally:
        for key, value in previous.items():
            object.__setattr__(settings, key, value)
