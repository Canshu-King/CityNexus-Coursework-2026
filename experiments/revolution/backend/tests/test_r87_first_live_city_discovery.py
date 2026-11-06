from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.core.config import settings
from app.db.models import JourneyMember, Place, ProviderCall, RealPlaceCandidate
from app.db.session import SessionLocal
from app.integrations.discovery.real_city import DiscoveryCandidate


client = TestClient(main_module.app)
TEST_SECRET = "r87-first-live-city-discovery-secret-0123456789"


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
            "title": "R87 first live city discovery",
            "city_code": "CN-R87-LIVE-DISCOVERY",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-13T10:00:00+08:00",
            "end_at": "2026-10-14T18:00:00+08:00",
            "budget": {"amount_minor": 10000, "currency": "CNY"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _discovery_payload(query: str = "gallery") -> dict:
    return {
        "query": query,
        "radius_m": 2500,
        "limit": 3,
        "source_kind": "MANUAL_PUBLIC_CENTER",
        "provider_consent": True,
        "latitude": 39.12,
        "longitude": 117.20,
        "coordinate_system": "WGS84",
        "mode": "live",
    }


def test_r87_live_discovery_keeps_member_receipts_private_and_blocks_ungrounded_opening(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    previous_enabled = settings.real_city_enabled
    previous_mode = settings.real_city_mode
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    object.__setattr__(settings, "real_city_enabled", True)
    object.__setattr__(settings, "real_city_mode", "live")
    provider_calls = 0

    def fake_search(_self, _query, **kwargs):
        nonlocal provider_calls
        provider_calls += 1
        assert kwargs["max_attempts"] == 1
        prefix = "A" if provider_calls == 1 else "B"
        return (
            [
                DiscoveryCandidate(
                    provider_key="overpass",
                    external_id=f"overpass:node/870{provider_calls}1",
                    name=f"{prefix} OSM Gallery Unknown Hours",
                    category="gallery",
                    latitude=39.1205,
                    longitude=117.2005,
                    tags=("gallery", "real_city"),
                    opening_hours={"status": "UNKNOWN"},
                    metadata={
                        "osm_id": f"node/870{provider_calls}1",
                        "tags": {"name": f"{prefix} OSM Gallery Unknown Hours", "tourism": "gallery"},
                    },
                    distance_m=80.0,
                    cost_evidence={"status": "UNKNOWN", "source": "overpass"},
                ),
                DiscoveryCandidate(
                    provider_key="overpass",
                    external_id=f"overpass:node/870{provider_calls}2",
                    name=f"{prefix} OSM Gallery Grounded Hours",
                    category="gallery",
                    latitude=39.121,
                    longitude=117.201,
                    tags=("gallery", "real_city"),
                    opening_hours={
                        "status": "VERIFIED_PROVIDER",
                        "open": "09:00",
                        "close": "20:00",
                    },
                    metadata={
                        "osm_id": f"node/870{provider_calls}2",
                        "tags": {"name": f"{prefix} OSM Gallery Grounded Hours", "tourism": "gallery"},
                    },
                    distance_m=120.0,
                    cost_evidence={"status": "UNKNOWN", "source": "overpass"},
                ),
            ],
            [
                {
                    "endpoint": "https://overpass-api.de/api/interpreter",
                    "answered": True,
                    "http_status": 200,
                    "elements": 2,
                }
            ],
        )

    monkeypatch.setattr(
        "app.execution.bootstrap_discovery.OverpassPOIProvider.search_with_evidence",
        fake_search,
    )
    owner = _new_traveler("R87 owner")
    viewer = _new_traveler("R87 viewer")
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

        first = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery",
            headers=owner_headers,
            json=_discovery_payload("owner gallery"),
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["status"] == "LIVE_RESULTS"
        assert first_body["attempts"] == [
            {
                "endpoint": "https://overpass-api.de/api/interpreter",
                "answered": True,
                "http_status": 200,
                "elements": 2,
            }
        ]
        unknown, grounded = first_body["candidates"]
        assert unknown["selection_state"] == "OPENING_UNVERIFIED"
        assert grounded["selection_state"] == "READY_FOR_NATIVE_PLAN"
        assert client.get("/api/v1/places?city_code=CN-R87-LIVE-DISCOVERY").json() == []

        viewer_receipt = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery",
            headers=viewer_headers,
            json=_discovery_payload("viewer gallery"),
        )
        assert viewer_receipt.status_code == 200, viewer_receipt.text
        viewer_body = viewer_receipt.json()
        assert all("A OSM" not in item["name"] for item in viewer_body["candidates"])
        assert all("owner gallery" not in json.dumps(item) for item in viewer_body["candidates"])
        assert provider_calls == 2

        viewer_cannot_select_owner_candidate = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/select",
            headers=viewer_headers,
            json={"receipt_id": first_body["receipt_id"], "candidate_id": grounded["candidate_id"]},
        )
        assert viewer_cannot_select_owner_candidate.status_code == 403, viewer_cannot_select_owner_candidate.text

        blocked_opening = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/select",
            headers=owner_headers,
            json={"receipt_id": first_body["receipt_id"], "candidate_id": unknown["candidate_id"]},
        )
        assert blocked_opening.status_code == 422, blocked_opening.text
        assert blocked_opening.json()["error"]["reason_code"] == "BOOTSTRAP_OPENING_UNVERIFIED"
        assert blocked_opening.json()["error"]["details"]["next_actions"] == [
            "VERIFY_OPENING_SOURCE",
            "CHOOSE_ANOTHER_CANDIDATE",
        ]
        assert client.get("/api/v1/places?city_code=CN-R87-LIVE-DISCOVERY").json() == []

        selected = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/select",
            headers=owner_headers,
            json={"receipt_id": first_body["receipt_id"], "candidate_id": grounded["candidate_id"]},
        )
        assert selected.status_code == 200, selected.text
        place_id = selected.json()["selected_place_id"]
        places = client.get("/api/v1/places?city_code=CN-R87-LIVE-DISCOVERY").json()
        assert [item["id"] for item in places] == [place_id]
        assert places[0]["opening_hours"]["status"] == "VERIFIED_PROVIDER"

        with SessionLocal() as session:
            rows = session.scalars(
                select(RealPlaceCandidate).where(RealPlaceCandidate.journey_id == journey_id)
            ).all()
            assert len(rows) == 4
            assert sum(row.materialized_place_id is not None for row in rows) == 1
            calls = session.scalars(
                select(ProviderCall).where(
                    ProviderCall.operation == "bootstrap_city_discovery",
                    ProviderCall.request_json.like(f"%{journey_id}%"),
                )
            ).all()
            assert len(calls) == 2
            assert all(len(json.loads(row.response_json or "{}").get("attempts") or []) == 1 for row in calls)
            assert session.scalar(select(func.count(Place.id)).where(Place.city_code == "CN-R87-LIVE-DISCOVERY")) == 1
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        object.__setattr__(settings, "real_city_enabled", previous_enabled)
        object.__setattr__(settings, "real_city_mode", previous_mode)

