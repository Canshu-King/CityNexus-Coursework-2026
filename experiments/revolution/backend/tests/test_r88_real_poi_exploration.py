from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.core.config import settings
from app.db.models import JourneyMember, Place, PlanVersion, ProviderCall, RealPlaceCandidate
from app.db.session import SessionLocal
from app.integrations.discovery.real_city import DiscoveryCandidate


client = TestClient(main_module.app)
TEST_SECRET = "r88-real-poi-exploration-secret-0123456789"


def _new_traveler(name: str) -> dict:
    response = client.post(
        "/api/v1/travelers",
        json={"display_name": name, "timezone": "Asia/Shanghai", "locale": "en-GB"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _new_journey(traveler_id: str) -> dict:
    response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler_id,
            "title": "R88 public POI exploration",
            "city_code": f"CN-R88-{uuid4().hex[:8].upper()}",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-13T10:00:00+08:00",
            "end_at": "2026-10-14T18:00:00+08:00",
            "budget": {"amount_minor": 10000, "currency": "CNY"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _discovery_payload() -> dict:
    return {
        "query": "gallery",
        "radius_m": 2500,
        "limit": 3,
        "source_kind": "MANUAL_PUBLIC_CENTER",
        "provider_consent": True,
        "latitude": 39.12,
        "longitude": 117.20,
        "coordinate_system": "WGS84",
        "mode": "live",
    }


def test_r88_unknown_opening_has_consent_bound_public_map_preview(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    previous_enabled = settings.real_city_enabled
    previous_mode = settings.real_city_mode
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    object.__setattr__(settings, "real_city_enabled", True)
    object.__setattr__(settings, "real_city_mode", "live")

    def fake_search(_self, _query, **kwargs):
        assert kwargs["max_attempts"] == 1
        return (
            [
                DiscoveryCandidate(
                    provider_key="overpass",
                    external_id="overpass:node/88001",
                    name="R88 Gallery With Unknown Opening",
                    category="gallery",
                    latitude=39.1205,
                    longitude=117.2005,
                    tags=("gallery", "real_city"),
                    opening_hours={"status": "UNKNOWN", "raw": "Mo-Fr 09:00-18:00"},
                    metadata={
                        "osm_id": "node/88001",
                        "tags": {
                            "name": "R88 Gallery With Unknown Opening",
                            "tourism": "gallery",
                            "opening_hours": "Mo-Fr 09:00-18:00",
                            "website": "https://gallery.example.test",
                            "contact:phone": "+86 13800000000",
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
                }
            ],
        )

    monkeypatch.setattr(
        "app.execution.bootstrap_discovery.OverpassPOIProvider.search_with_evidence",
        fake_search,
    )
    owner = _new_traveler("R88 owner")
    viewer = _new_traveler("R88 viewer")
    journey = _new_journey(owner["id"])
    journey_id = journey["id"]
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

        discovered = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery",
            headers=owner_headers,
            json=_discovery_payload(),
        )
        assert discovered.status_code == 200, discovered.text
        body = discovered.json()
        candidate = body["candidates"][0]
        assert candidate["opening_state"] == "UNKNOWN"
        assert candidate["opening_claim"] == "OSM_TAG_CLAIM"
        assert candidate["opening_verification_state"] == "EXTERNAL_NOT_ATTESTED"
        assert candidate["opening_observed_at"]
        assert candidate["source_tags"]["website"] == "https://gallery.example.test"
        assert candidate["source_tags"]["contact:phone"] == "+86 13800000000"

        preview_path = f"/api/v1/journeys/{journey_id}/bootstrap-discovery/preview"
        missing_consent = client.post(
            preview_path,
            headers=owner_headers,
            json={
                "receipt_id": body["receipt_id"],
                "candidate_id": candidate["candidate_id"],
                "public_map_consent": False,
                "latitude": candidate["latitude"],
                "longitude": candidate["longitude"],
                "coordinate_system": "WGS84",
            },
        )
        assert missing_consent.status_code == 422, missing_consent.text
        assert missing_consent.json()["error"]["reason_code"] == "BOOTSTRAP_PUBLIC_MAP_CONSENT_REQUIRED"

        released = client.post(
            preview_path,
            headers=owner_headers,
            json={
                "receipt_id": body["receipt_id"],
                "candidate_id": candidate["candidate_id"],
                "public_map_consent": True,
                "latitude": candidate["latitude"],
                "longitude": candidate["longitude"],
                "coordinate_system": "WGS84",
            },
        )
        assert released.status_code == 200, released.text
        release = released.json()
        assert release["status"] == "PUBLIC_DESTINATION_READY"
        assert release["destination"]["map_url"].startswith("https://www.openstreetmap.org/?mlat=")
        assert "origin" not in release["destination"]["map_url"]
        assert "center" not in release["destination"]["map_url"]
        assert "query" not in release["destination"]["map_url"]
        assert release["opening"]["verification_state"] == "EXTERNAL_NOT_ATTESTED"
        assert release["opening"]["source_tags"]["opening_hours"] == "Mo-Fr 09:00-18:00"
        assert release["plan_version_id"] is None
        assert release["selected_place_id"] is None
        assert release["explore_state"] == "DRAFT_EXPLORE_ONLY"

        viewer_release = client.post(
            preview_path,
            headers=viewer_headers,
            json={
                "receipt_id": body["receipt_id"],
                "candidate_id": candidate["candidate_id"],
                "public_map_consent": True,
                "latitude": candidate["latitude"],
                "longitude": candidate["longitude"],
                "coordinate_system": "WGS84",
            },
        )
        assert viewer_release.status_code == 403, viewer_release.text

        mismatch = client.post(
            preview_path,
            headers=owner_headers,
            json={
                "receipt_id": body["receipt_id"],
                "candidate_id": candidate["candidate_id"],
                "public_map_consent": True,
                "latitude": candidate["latitude"] + 0.001,
                "longitude": candidate["longitude"],
                "coordinate_system": "WGS84",
            },
        )
        assert mismatch.status_code == 409, mismatch.text
        assert mismatch.json()["error"]["reason_code"] == "BOOTSTRAP_MAP_PREVIEW_COORDINATE_MISMATCH"

        with SessionLocal() as session:
            row = session.get(RealPlaceCandidate, candidate["candidate_id"])
            assert row is not None
            row.expires_at = "2020-01-01T00:00:00+00:00"
            session.commit()
        stale = client.post(
            preview_path,
            headers=owner_headers,
            json={
                "receipt_id": body["receipt_id"],
                "candidate_id": candidate["candidate_id"],
                "public_map_consent": True,
                "latitude": candidate["latitude"],
                "longitude": candidate["longitude"],
                "coordinate_system": "WGS84",
            },
        )
        assert stale.status_code == 410, stale.text
        assert stale.json()["error"]["reason_code"] == "BOOTSTRAP_RECEIPT_EXPIRED"

        with SessionLocal() as session:
            assert session.scalar(select(func.count(Place.id)).where(Place.city_code == journey["city_code"])) == 0
            assert session.scalar(select(func.count(PlanVersion.id)).where(PlanVersion.journey_id == journey_id)) == 0
            calls = session.scalars(
                select(ProviderCall).where(
                    ProviderCall.operation == "bootstrap_city_discovery",
                    ProviderCall.request_json.like(f"%{journey_id}%"),
                )
            ).all()
            assert len(calls) == 1
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        object.__setattr__(settings, "real_city_enabled", previous_enabled)
        object.__setattr__(settings, "real_city_mode", previous_mode)
