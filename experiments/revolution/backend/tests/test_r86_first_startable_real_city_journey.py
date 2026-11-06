from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.core.config import settings
from app.db.models import (
    AgentRun,
    Booking,
    GuardianRun,
    Journey,
    JourneyMember,
    Order,
    PaymentTransaction,
    Place,
    PlanVersion,
    ProviderCall,
    RealPlaceCandidate,
)
from app.db.session import SessionLocal
from app.integrations.discovery.real_city import DiscoveryCandidate


client = TestClient(main_module.app)
TEST_SECRET = "r86-first-startable-secret-0123456789abcdef"


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
            "title": "R86 first startable city journey",
            "city_code": "CN-R86-FIRST-STARTABLE",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-11T10:00:00+08:00",
            "end_at": "2026-10-12T18:00:00+08:00",
            "budget": {"amount_minor": 10000, "currency": "CNY"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_r86_zero_catalog_unknown_cost_can_start_only_after_explicit_ack(monkeypatch):
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
        return (
            [
                DiscoveryCandidate(
                    provider_key="overpass",
                    external_id="overpass:node/86101",
                    name="R86 Public Gallery",
                    category="gallery",
                    latitude=39.1205,
                    longitude=117.2005,
                    tags=("gallery", "real_city"),
                    opening_hours={
                        "status": "VERIFIED_PROVIDER",
                        "open": "09:00",
                        "close": "20:00",
                    },
                    metadata={
                        "osm_id": "node/86101",
                        "tags": {"name": "R86 Public Gallery", "tourism": "gallery"},
                    },
                    distance_m=80.0,
                    cost_evidence={"status": "UNKNOWN", "source": "overpass"},
                ),
                DiscoveryCandidate(
                    provider_key="overpass",
                    external_id="overpass:node/86102",
                    name="R86 Public Library",
                    category="library",
                    latitude=39.121,
                    longitude=117.201,
                    tags=("library", "real_city"),
                    opening_hours={
                        "status": "VERIFIED_PROVIDER",
                        "open": "09:00",
                        "close": "20:00",
                    },
                    metadata={
                        "osm_id": "node/86102",
                        "tags": {"name": "R86 Public Library", "amenity": "library"},
                    },
                    distance_m=120.0,
                    cost_evidence={"status": "UNKNOWN", "source": "overpass"},
                ),
            ],
            [
                {
                    "endpoint": "https://overpass.test/api/interpreter",
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
    traveler = _new_traveler("R86 owner")
    journey = _new_journey(traveler["id"])
    headers = {"Authorization": f"Bearer {traveler['access_token']}"}
    try:
        assert client.get("/api/v1/places?city_code=CN-R86-FIRST-STARTABLE").json() == []
        discovery = client.post(
            f"/api/v1/journeys/{journey['id']}/bootstrap-discovery",
            headers=headers,
            json={
                "query": "gallery",
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
        discovery_body = discovery.json()
        assert discovery_body["candidates"][0]["opening_state"] == "VERIFIED_PROVIDER"
        assert len(discovery_body["candidates"]) == 2
        assert provider_calls == 1

        rate_limited = client.post(
            f"/api/v1/journeys/{journey['id']}/bootstrap-discovery",
            headers=headers,
            json={
                "query": "gallery",
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
        assert provider_calls == 1
        assert rate_limited.json()["error"]["details"]["scope"] == "journey_member_traveler"

        candidate_id = discovery_body["candidates"][0]["candidate_id"]
        expired_candidate_id = discovery_body["candidates"][1]["candidate_id"]
        invalid = client.post(
            f"/api/v1/journeys/{journey['id']}/bootstrap-discovery/select",
            headers=headers,
            json={
                "receipt_id": discovery_body["receipt_id"],
                "candidate_id": "00000000-0000-4000-8000-000000000099",
            },
        )
        assert invalid.status_code == 404, invalid.text
        other = _new_traveler("R86 viewer")
        with SessionLocal() as session:
            session.add(
                JourneyMember(
                    id="r86-viewer-member",
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
            expired_row = session.get(RealPlaceCandidate, expired_candidate_id)
            assert expired_row is not None
            expired_row.expires_at = "2020-01-01T00:00:00+00:00"
            session.commit()
        viewer = client.post(
            f"/api/v1/journeys/{journey['id']}/bootstrap-discovery/select",
            headers={"Authorization": f"Bearer {other['access_token']}"},
            json={"receipt_id": discovery_body["receipt_id"], "candidate_id": candidate_id},
        )
        assert viewer.status_code == 403, viewer.text
        expired = client.post(
            f"/api/v1/journeys/{journey['id']}/bootstrap-discovery/select",
            headers=headers,
            json={"receipt_id": discovery_body["receipt_id"], "candidate_id": expired_candidate_id},
        )
        assert expired.status_code == 410, expired.text
        selected = client.post(
            f"/api/v1/journeys/{journey['id']}/bootstrap-discovery/select",
            headers=headers,
            json={"receipt_id": discovery_body["receipt_id"], "candidate_id": candidate_id},
        )
        assert selected.status_code == 200, selected.text
        place_id = selected.json()["selected_place_id"]
        places = client.get("/api/v1/places?city_code=CN-R86-FIRST-STARTABLE").json()
        assert [item["id"] for item in places] == [place_id]
        assert places[0]["opening_hours"]["status"] == "VERIFIED_PROVIDER"

        blocked = client.post(
            f"/api/v1/journeys/{journey['id']}/plans/generate",
            headers=headers,
            json={
                "place_ids": [place_id],
                "candidate_count": 1,
                "allow_single_place": True,
            },
        )
        assert blocked.status_code == 422, blocked.text
        assert "PLACE_COST_UNVERIFIED" in blocked.text
        with SessionLocal() as session:
            assert session.scalar(select(func.count(PlanVersion.id)).where(PlanVersion.journey_id == journey["id"])) == 0

        generated = client.post(
            f"/api/v1/journeys/{journey['id']}/plans/generate",
            headers={**headers, "Idempotency-Key": "r86-native-plan-first-attempt"},
            json={
                "place_ids": [place_id],
                "candidate_count": 1,
                "allow_single_place": True,
                "allow_unknown_cost": True,
            },
        )
        assert generated.status_code == 201, generated.text
        plan = generated.json()[0]
        assert plan["total_cost"]["amount_minor"] == 0
        assert plan["total_cost_status"] == "PARTIAL_UNKNOWN"
        assert plan["explanation"]["authority_state"] == "COST_UNKNOWN_ACCEPTED"
        assert plan["explanation"]["cost_unknown_policy"] == "USER_ACCEPTS_UNKNOWN_PRICE_NOT_INCLUDED"
        assert plan["explanation"]["cost_summary"] == {
            "known_subtotal_minor": 0,
            "unknown_cost_included": False,
            "unknown_item_count": 1,
            "unknown_place_ids": [place_id],
            "total_is_not_free": True,
        }
        assert plan["activities"][0]["cost_status"] == "UNKNOWN"
        assert plan["activities"][0]["metadata"]["price_known"] is False
        assert plan["activities"][0]["metadata"]["cost_included_in_total"] is False
        replayed = client.post(
            f"/api/v1/journeys/{journey['id']}/plans/generate",
            headers={**headers, "Idempotency-Key": "r86-native-plan-first-attempt"},
            json={
                "place_ids": [place_id],
                "candidate_count": 1,
                "allow_single_place": True,
                "allow_unknown_cost": True,
            },
        )
        assert replayed.status_code == 201, replayed.text
        assert replayed.json()[0]["id"] == plan["id"]

        selected_plan = client.post(
            f"/api/v1/journeys/{journey['id']}/plans/{plan['id']}/select",
            headers=headers,
        )
        assert selected_plan.status_code == 200, selected_plan.text
        activity_id = plan["activities"][0]["id"]
        booking = client.post(
            f"/api/v1/journeys/{journey['id']}/bookings",
            headers=headers,
            json={"activity_id": activity_id},
        )
        assert booking.status_code == 422, booking.text
        assert "unknown cost is not bookable" in booking.text

        started = client.post(f"/api/v1/journeys/{journey['id']}/start", headers=headers)
        assert started.status_code == 200, started.text
        assert client.get(f"/api/v1/journeys/{journey['id']}").json()["status"] == "ACTIVE"

        with SessionLocal() as session:
            assert session.scalar(select(func.count(Place.id)).where(Place.city_code == "CN-R86-FIRST-STARTABLE")) == 1
            assert session.scalar(select(func.count(PlanVersion.id)).where(PlanVersion.journey_id == journey["id"])) == 1
            assert session.scalar(select(func.count(RealPlaceCandidate.id)).where(RealPlaceCandidate.journey_id == journey["id"])) == 2
            assert session.scalar(
                select(func.count(RealPlaceCandidate.id)).where(
                    RealPlaceCandidate.journey_id == journey["id"],
                    RealPlaceCandidate.materialized_place_id.is_not(None),
                )
            ) == 1
            assert session.scalar(select(func.count(ProviderCall.id)).where(ProviderCall.operation == "bootstrap_city_discovery", ProviderCall.request_json.like(f'%{journey["id"]}%'))) == 1
            assert session.scalar(select(func.count(AgentRun.id)).where(AgentRun.journey_id == journey["id"])) == 0
            assert session.scalar(select(func.count(GuardianRun.id)).where(GuardianRun.journey_id == journey["id"])) == 0
            assert session.scalar(select(func.count(Booking.id)).where(Booking.journey_id == journey["id"])) == 0
            assert session.scalar(select(func.count(Order.id)).where(Order.journey_id == journey["id"])) == 0
            assert session.scalar(select(func.count(PaymentTransaction.id)).where(PaymentTransaction.journey_id == journey["id"])) == 0
            persisted_plan = session.get(PlanVersion, plan["id"])
            assert persisted_plan is not None
            assert json.loads(persisted_plan.explanation_json)["cost_unknown_acknowledgement"]["policy"] == "USER_ACCEPTS_UNKNOWN_PRICE_NOT_INCLUDED"
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        object.__setattr__(settings, "real_city_enabled", previous_enabled)
        object.__setattr__(settings, "real_city_mode", previous_mode)

