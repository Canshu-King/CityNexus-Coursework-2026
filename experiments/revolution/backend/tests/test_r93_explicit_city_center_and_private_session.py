import json
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as main_module
from app.core.config import settings
from app.db.models import JourneyMember, ProviderCall
from app.db.session import SessionLocal
from app.integrations.discovery.real_city import DiscoveryCandidate


client = TestClient(main_module.app)
TEST_SECRET = "r93-explicit-center-private-session-secret-0123456789"


def _traveler(name: str) -> dict:
    response = client.post(
        "/api/v1/travelers",
        json={"display_name": name, "timezone": "Asia/Shanghai", "locale": "en-GB"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_r93_requires_an_explicit_public_center_and_keeps_signed_shortlists_private(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    previous_enabled = settings.real_city_enabled
    previous_mode = settings.real_city_mode
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    object.__setattr__(settings, "real_city_enabled", True)
    object.__setattr__(settings, "real_city_mode", "live")
    calls: list[dict] = []

    def fake_search(_self, query, **kwargs):
        calls.append({"query": query, **kwargs})
        assert kwargs["latitude"] == 31.2304
        assert kwargs["longitude"] == 121.4737
        candidate = DiscoveryCandidate(
            provider_key="TEST_PROVIDER",
            external_id="overpass:node/93001",
            name="R93 Shanghai Gallery",
            category="gallery",
            latitude=31.2308,
            longitude=121.4741,
            tags=("gallery", "real_city"),
            opening_hours={"status": "UNKNOWN"},
            metadata={
                "osm_id": "node/93001",
                "tags": {"name": "R93 Shanghai Gallery", "tourism": "gallery"},
            },
            distance_m=120.0,
            cost_evidence={"status": "UNKNOWN", "source": "TEST_PROVIDER"},
        )
        return [candidate], [{"endpoint": "TEST_PROVIDER", "answered": True, "http_status": 200, "elements": 1}]

    monkeypatch.setattr(
        "app.execution.bootstrap_discovery.OverpassPOIProvider.search_with_evidence",
        fake_search,
    )
    owner = _traveler("R93 owner")
    viewer = _traveler("R93 viewer")
    journey_response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": owner["id"],
            "title": "R93 explicit city center",
            "city_code": f"CN-R93-SH-{uuid4().hex[:8].upper()}",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-15T10:00:00+08:00",
            "end_at": "2026-10-16T18:00:00+08:00",
            "budget": {"amount_minor": 10000, "currency": "CNY"},
        },
    )
    assert journey_response.status_code == 201, journey_response.text
    journey_id = journey_response.json()["id"]
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    viewer_headers = {"Authorization": f"Bearer {viewer['access_token']}"}
    try:
        with SessionLocal() as session:
            session.add(
                JourneyMember(
                    id=str(uuid4()),
                    journey_id=journey_id,
                    traveler_id=viewer["id"],
                    role="COLLABORATOR",
                    invited_by=owner["id"],
                    expires_at=None,
                    joined_at="2026-10-10T00:00:00+00:00",
                    removed_at=None,
                    preferences_json="{}",
                    created_at="2026-10-10T00:00:00+00:00",
                    updated_at="2026-10-10T00:00:00+00:00",
                )
            )
            session.commit()

        payload = {
            "query": "gallery",
            "radius_m": 2500,
            "limit": 3,
            "source_kind": "MANUAL_PUBLIC_CENTER",
            "provider_consent": True,
            "latitude": 31.2304,
            "longitude": 121.4737,
            "coordinate_system": "WGS84",
            "mode": "live",
        }
        discovery = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery",
            headers=owner_headers,
            json=payload,
        )
        assert discovery.status_code == 200, discovery.text
        body = discovery.json()
        assert body["source_kind"] == "MANUAL_PUBLIC_CENTER"
        assert body["provider_provenance"] == "LIVE_OPENSTREETMAP_OVERPASS"
        assert body["candidates"][0]["name"] == "R93 Shanghai Gallery"
        assert body["candidates"][0]["latitude"] == 31.2308
        assert len(calls) == 1
        assert calls[0]["latitude"] == 31.2304
        assert calls[0]["longitude"] == 121.4737

        save = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers=owner_headers,
            json={
                "receipt_id": body["receipt_id"],
                "candidate_id": body["candidates"][0]["candidate_id"],
            },
        )
        assert save.status_code == 200, save.text
        assert save.json()["name"] == "R93 Shanghai Gallery"

        viewer_list = client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers=viewer_headers,
        )
        assert viewer_list.status_code == 200, viewer_list.text
        assert viewer_list.json()["items"] == []

        with SessionLocal() as session:
            provider_call = session.query(ProviderCall).filter(
                ProviderCall.operation == "bootstrap_city_discovery",
                ProviderCall.request_json.like(f"%{journey_id}%"),
            ).one()
            request = json.loads(provider_call.request_json)
            assert request["city_code"].startswith("CN-R93-SH-")
            assert request["center"] == {"latitude": 31.23, "longitude": 121.474}
            assert request["source_kind"] == "MANUAL_PUBLIC_CENTER"
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        object.__setattr__(settings, "real_city_enabled", previous_enabled)
        object.__setattr__(settings, "real_city_mode", previous_mode)
