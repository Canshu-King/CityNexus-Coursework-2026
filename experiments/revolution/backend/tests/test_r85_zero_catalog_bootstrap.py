from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.core.config import settings
from app.db.models import (
    AgentRun,
    JourneyMember,
    LocationObservation,
    MemberLiveMomentScoutReceipt,
    Place,
    PlanVersion,
    ProviderCall,
    RealPlaceCandidate,
)
from app.db.session import SessionLocal
from app.integrations.discovery.real_city import DiscoveryCandidate
from app.traveler_principal import issue_traveler_token


client = TestClient(main_module.app)
TEST_SECRET = "r85-zero-catalog-bootstrap-secret-0123456789abcdef"


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
            "title": "R85 empty catalogue",
            "city_code": "CN-R85-EMPTY",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-11T10:00:00+08:00",
            "end_at": "2026-10-12T18:00:00+08:00",
            "budget": {"amount_minor": 10000, "currency": "CNY"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_r85_signed_zero_catalog_preplan_selection_is_private_and_single_place(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    previous_enabled = settings.real_city_enabled
    previous_mode = settings.real_city_mode
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    object.__setattr__(settings, "real_city_enabled", True)
    object.__setattr__(settings, "real_city_mode", "live")
    provider_calls = 0

    def fake_search(_self, _query, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        assert _kwargs["max_attempts"] == 1
        return (
            [
                DiscoveryCandidate(
                    provider_key="overpass",
                    external_id="overpass:node/85101",
                    name="R85 Public Cafe",
                    category="cafe",
                    latitude=39.1205,
                    longitude=117.2005,
                    tags=("cafe", "real_city"),
                    opening_hours={},
                    metadata={"osm_id": "node/85101", "tags": {"name": "R85 Public Cafe", "amenity": "cafe"}},
                    distance_m=80.0,
                    cost_evidence={"status": "UNKNOWN", "source": "overpass"},
                ),
                DiscoveryCandidate(
                    provider_key="overpass",
                    external_id="overpass:node/85102",
                    name="R85 Public Library",
                    category="library",
                    latitude=39.121,
                    longitude=117.201,
                    tags=("library", "real_city"),
                    opening_hours={},
                    metadata={"osm_id": "node/85102", "tags": {"name": "R85 Public Library", "amenity": "library"}},
                    distance_m=120.0,
                    cost_evidence={"status": "UNKNOWN", "source": "overpass"},
                ),
            ],
            [{"endpoint": "https://overpass.test/api/interpreter", "answered": True, "http_status": 200, "elements": 2}],
        )

    monkeypatch.setattr(
        "app.execution.bootstrap_discovery.OverpassPOIProvider.search_with_evidence",
        fake_search,
    )
    traveler = _new_traveler("R85 owner")
    journey = _new_journey(traveler["id"])
    owner_headers = {"Authorization": f"Bearer {traveler['access_token']}"}
    try:
        assert client.get("/api/v1/places?city_code=CN-R85-EMPTY").json() == []

        declined = client.post(
            f"/api/v1/journeys/{journey['id']}/bootstrap-discovery",
            headers=owner_headers,
            json={
                "query": "cafe",
                "radius_m": 2500,
                "limit": 3,
                "source_kind": "MANUAL_PUBLIC_CENTER",
                "provider_consent": False,
                "latitude": 39.12,
                "longitude": 117.20,
                "coordinate_system": "WGS84",
                "mode": "live",
            },
        )
        assert declined.status_code == 422, declined.text
        assert "PROVIDER_CONSENT_REQUIRED" in declined.text
        assert provider_calls == 0

        discovery = client.post(
            f"/api/v1/journeys/{journey['id']}/bootstrap-discovery",
            headers=owner_headers,
            json={
                "query": "cafe",
                "radius_m": 2500,
                "limit": 3,
                "source_kind": "MANUAL_PUBLIC_CENTER",
                "provider_consent": True,
                "latitude": 39.12,
                "longitude": 117.20,
                "coordinate_system": "WGS84",
                "mode": "live",
            },
        )
        assert discovery.status_code == 200, discovery.text
        body = discovery.json()
        assert body["status"] == "LIVE_RESULTS"
        assert len(body["candidates"]) == 2
        assert provider_calls == 1
        rate_limited = client.post(
            f"/api/v1/journeys/{journey['id']}/bootstrap-discovery",
            headers=owner_headers,
            json={
                "query": "cafe",
                "radius_m": 2500,
                "limit": 3,
                "source_kind": "MANUAL_PUBLIC_CENTER",
                "provider_consent": True,
                "latitude": 39.12,
                "longitude": 117.20,
                "coordinate_system": "WGS84",
                "mode": "live",
            },
        )
        assert rate_limited.status_code == 429, rate_limited.text
        assert "BOOTSTRAP_RATE_LIMITED" in rate_limited.text
        assert provider_calls == 1

        # A second signed member cannot use the owner's private receipt/candidate.
        other = _new_traveler("R85 viewer")
        with SessionLocal() as session:
            session.add(
                JourneyMember(
                    id="r85-viewer-member",
                    journey_id=journey["id"],
                    traveler_id=other["id"],
                    role="COLLABORATOR",
                    invited_by=traveler["id"],
                    expires_at=None,
                    joined_at="2026-10-09T00:00:00+00:00",
                    removed_at=None,
                    preferences_json="{}",
                    created_at="2026-10-09T00:00:00+00:00",
                    updated_at="2026-10-09T00:00:00+00:00",
                )
            )
            session.commit()
        viewer_headers = {"Authorization": f"Bearer {other['access_token']}"}
        mesh_before = client.get(f"/api/v1/journeys/{journey['id']}/provider-mesh")
        assert mesh_before.status_code == 200
        assert mesh_before.json()["real_place_candidates"] == []
        forbidden = client.post(
            f"/api/v1/journeys/{journey['id']}/bootstrap-discovery/select",
            headers=viewer_headers,
            json={"receipt_id": body["receipt_id"], "candidate_id": body["candidates"][0]["candidate_id"]},
        )
        assert forbidden.status_code == 403, forbidden.text

        selected_id = body["candidates"][0]["candidate_id"]
        selection = client.post(
            f"/api/v1/journeys/{journey['id']}/bootstrap-discovery/select",
            headers=owner_headers,
            json={"receipt_id": body["receipt_id"], "candidate_id": selected_id},
        )
        assert selection.status_code == 200, selection.text
        place_id = selection.json()["selected_place_id"]
        places = client.get("/api/v1/places?city_code=CN-R85-EMPTY").json()
        assert [item["id"] for item in places] == [place_id]
        mesh_after = client.get(f"/api/v1/journeys/{journey['id']}/provider-mesh").json()
        assert len(mesh_after["real_place_candidates"]) == 1
        assert mesh_after["real_place_candidates"][0]["materialized_place_id"] == place_id

        repeated = client.post(
            f"/api/v1/journeys/{journey['id']}/bootstrap-discovery/select",
            headers=owner_headers,
            json={"receipt_id": body["receipt_id"], "candidate_id": selected_id},
        )
        assert repeated.status_code == 200
        assert repeated.json()["selected_place_id"] == place_id

        blocked = client.post(
            f"/api/v1/journeys/{journey['id']}/plans/generate",
            json={"place_ids": [place_id], "candidate_count": 1, "allow_single_place": True},
        )
        assert blocked.status_code == 422, blocked.text
        assert "PLACE_COST_UNVERIFIED" in blocked.text

        with SessionLocal() as session:
            assert session.scalar(select(func.count(Place.id)).where(Place.city_code == "CN-R85-EMPTY")) == 1
            assert session.scalar(select(func.count(PlanVersion.id)).where(PlanVersion.journey_id == journey["id"])) == 0
            assert session.scalar(select(func.count(LocationObservation.id)).where(LocationObservation.journey_id == journey["id"])) == 0
            assert session.scalar(select(func.count(MemberLiveMomentScoutReceipt.id)).where(MemberLiveMomentScoutReceipt.journey_id == journey["id"])) == 0
            assert session.scalar(select(func.count(AgentRun.id)).where(AgentRun.journey_id == journey["id"])) == 0
            rows = session.scalars(select(RealPlaceCandidate).where(RealPlaceCandidate.journey_id == journey["id"])).all()
            assert sum(row.materialized_place_id is not None for row in rows) == 1
            calls = session.scalars(select(ProviderCall).where(ProviderCall.operation == "bootstrap_city_discovery")).all()
            assert len(calls) == 1
            assert json.loads(calls[0].request_json)["source_kind"] == "MANUAL_PUBLIC_CENTER"
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        object.__setattr__(settings, "real_city_enabled", previous_enabled)
        object.__setattr__(settings, "real_city_mode", previous_mode)
