from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.core.config import settings
from app.db.models import JourneyMember, RealPlaceCandidate
from app.db.session import SessionLocal
from app.integrations.discovery.real_city import DiscoveryCandidate


client = TestClient(main_module.app)
TEST_SECRET = "r89-member-shared-osm-identity-secret-0123456789"
CONTROLLED_PROVIDER = "TEST_PROVIDER"


def _new_traveler(name: str) -> dict:
    response = client.post(
        "/api/v1/travelers",
        json={"display_name": name, "timezone": "Asia/Shanghai", "locale": "en-GB"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_r89_same_public_osm_identity_gets_private_member_scopes(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    previous_enabled = settings.real_city_enabled
    previous_mode = settings.real_city_mode
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    object.__setattr__(settings, "real_city_enabled", True)
    object.__setattr__(settings, "real_city_mode", "live")

    def fake_search(_self, query, **kwargs):
        assert kwargs["max_attempts"] == 1
        return (
            [
                DiscoveryCandidate(
                    provider_key=CONTROLLED_PROVIDER,
                    external_id="overpass:node/89001",
                    name="R89 Shared OSM Gallery",
                    category="gallery",
                    latitude=39.1205,
                    longitude=117.2005,
                    tags=("gallery", "real_city"),
                    opening_hours={"status": "UNKNOWN"},
                    metadata={
                        "osm_id": "node/89001",
                        "tags": {
                            "name": "R89 Shared OSM Gallery",
                            "tourism": "gallery",
                            "website": "https://example.test/shared-gallery",
                        },
                    },
                    distance_m=80.0,
                    cost_evidence={"status": "UNKNOWN", "source": "overpass"},
                )
            ],
            [
                {
                    "endpoint": "https://overpass-api.de/api/interpreter",
                    "answered": True,
                    "http_status": 200,
                    "elements": 1,
                    "query_hint": str(query),
                }
            ],
        )

    monkeypatch.setattr(
        "app.execution.bootstrap_discovery.OverpassPOIProvider.search_with_evidence",
        fake_search,
    )
    owner = _new_traveler("R89 owner")
    viewer = _new_traveler("R89 viewer")
    journey_response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": owner["id"],
            "title": "R89 shared identity",
            "city_code": f"CN-R89-{uuid4().hex[:8].upper()}",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-13T10:00:00+08:00",
            "end_at": "2026-10-14T18:00:00+08:00",
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
            "query": "owner gallery",
            "radius_m": 2500,
            "limit": 3,
            "source_kind": "MANUAL_PUBLIC_CENTER",
            "provider_consent": True,
            "latitude": 39.12,
            "longitude": 117.20,
            "coordinate_system": "WGS84",
            "mode": "live",
        }
        owner_result = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery",
            headers=owner_headers,
            json=payload,
        )
        assert owner_result.status_code == 200, owner_result.text
        owner_body = owner_result.json()
        owner_candidate = owner_body["candidates"][0]

        viewer_payload = {**payload, "query": "viewer gallery"}
        viewer_result = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery",
            headers=viewer_headers,
            json=viewer_payload,
        )
        assert viewer_result.status_code == 200, viewer_result.text
        viewer_body = viewer_result.json()
        viewer_candidate = viewer_body["candidates"][0]
        assert viewer_candidate["candidate_id"] == owner_candidate["candidate_id"]
        assert viewer_body["receipt_id"] != owner_body["receipt_id"]
        assert owner_body["receipt_id"] not in json.dumps(viewer_body)
        assert "owner gallery" not in json.dumps(viewer_body)
        assert "viewer gallery" not in json.dumps(owner_body)

        viewer_preview = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/preview",
            headers=viewer_headers,
            json={
                "receipt_id": viewer_body["receipt_id"],
                "candidate_id": viewer_candidate["candidate_id"],
                "public_map_consent": True,
                "latitude": viewer_candidate["latitude"],
                "longitude": viewer_candidate["longitude"],
                "coordinate_system": "WGS84",
            },
        )
        assert viewer_preview.status_code == 200, viewer_preview.text

        with SessionLocal() as session:
            rows = session.scalars(
                select(RealPlaceCandidate).where(RealPlaceCandidate.journey_id == journey_id)
            ).all()
            assert len(rows) == 1
            evidence = json.loads(rows[0].evidence_json)
            assert len(evidence["bootstrap_scopes"]) == 2
            assert session.scalar(
                select(func.count(RealPlaceCandidate.id)).where(
                    RealPlaceCandidate.journey_id == journey_id,
                    RealPlaceCandidate.external_id == "overpass:node/89001",
                )
            ) == 1
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        object.__setattr__(settings, "real_city_enabled", previous_enabled)
        object.__setattr__(settings, "real_city_mode", previous_mode)
