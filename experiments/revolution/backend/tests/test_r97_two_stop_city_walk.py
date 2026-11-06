from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.core.config import settings
from app.db.models import (
    AgentRun,
    Booking,
    GuardianRun,
    JourneyMember,
    MemberDraftShortlistItem,
    Place,
    PlanVersion,
)
from app.db.session import SessionLocal


client: TestClient | None = None


def _traveler(label: str) -> dict:
    assert client is not None
    response = client.post(
        "/api/v1/travelers",
        json={"display_name": label, "timezone": "Asia/Shanghai", "locale": "en-GB"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_r97_two_stop_public_walk_is_signed_ordered_and_explore_only():
    global client
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", "r97-two-stop-secret-012345678901")
    client = TestClient(main_module.app)
    owner = _traveler(f"R97 two-stop owner {uuid4().hex[:6]}")
    bystander = _traveler(f"R97 two-stop bystander {uuid4().hex[:6]}")
    city_code = f"CN-R97-{uuid4().hex[:8].upper()}"
    journey_response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": owner["id"],
            "title": "R97 two-stop public walk",
            "city_code": city_code,
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-16T10:00:00+08:00",
            "end_at": "2026-10-17T18:00:00+08:00",
            "budget": {"amount_minor": 10000, "currency": "CNY"},
        },
    )
    assert journey_response.status_code == 201, journey_response.text
    journey_id = journey_response.json()["id"]
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    bystander_headers = {"Authorization": f"Bearer {bystander['access_token']}"}
    try:
        with SessionLocal() as session:
            owner_member = session.scalar(
                select(JourneyMember).where(
                    JourneyMember.journey_id == journey_id,
                    JourneyMember.traveler_id == owner["id"],
                )
            )
            assert owner_member is not None
            stamp = "2026-10-10T13:00:00+00:00"
            session.add(
                JourneyMember(
                    id=str(uuid4()),
                    journey_id=journey_id,
                    traveler_id=bystander["id"],
                    role="COLLABORATOR",
                    invited_by=owner["id"],
                    expires_at=None,
                    joined_at=stamp,
                    removed_at=None,
                    preferences_json="{}",
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            first_id = str(uuid4())
            second_id = str(uuid4())
            for item_id, external_id, name, latitude, longitude in (
                (first_id, "overpass:node/97001", "R97 Museum", 31.2304, 121.4737),
                (second_id, "overpass:node/97002", "R97 Garden", 31.2314, 121.4757),
            ):
                session.add(
                    MemberDraftShortlistItem(
                        id=item_id,
                        journey_id=journey_id,
                        member_id=owner_member.id,
                        traveler_id=owner["id"],
                        provider_key="overpass",
                        external_id=external_id,
                        name=name,
                        category="public",
                        latitude=latitude,
                        longitude=longitude,
                        coordinate_system="WGS84",
                        source_url=f"https://www.openstreetmap.org/node/{external_id.rsplit('/', 1)[1]}",
                        source_provenance="TEST_PROVIDER",
                        captured_at=stamp,
                        opening_hours_json='{"status":"UNKNOWN"}',
                        cost_evidence_json='{"status":"UNKNOWN"}',
                        source_tags_json="{}",
                        distance_m=None,
                        distance_basis=None,
                        status="SAVED",
                        created_at=stamp,
                        updated_at=stamp,
                    )
                )
            session.commit()

        no_consent = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/two-stop-walk",
            headers=owner_headers,
            json={
                "first_shortlist_id": first_id,
                "second_shortlist_id": second_id,
                "public_map_consent": False,
            },
        )
        assert no_consent.status_code == 422, no_consent.text

        released = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/two-stop-walk",
            headers=owner_headers,
            json={
                "first_shortlist_id": first_id,
                "second_shortlist_id": second_id,
                "public_map_consent": True,
            },
        )
        assert released.status_code == 200, released.text
        body = released.json()
        assert body["status"] == "PUBLIC_TWO_STOP_WALK_READY"
        assert body["authorization_basis"] == "TWO_SAVED_PUBLIC_IDENTITIES"
        assert body["first_stop"]["shortlist_id"] == first_id
        assert body["second_stop"]["shortlist_id"] == second_id
        assert body["navigation_url"] == (
            "https://www.google.com/maps/dir/?api=1&origin=31.2304000%2C121.4737000"
            "&destination=31.2314000%2C121.4757000&travelmode=walking"
        )
        assert body["route_time_state"] == "UNKNOWN"
        assert body["explore_state"] == "DRAFT_EXPLORE_ONLY"
        assert body["plan_version_id"] is None
        assert body["selected_place_id"] is None
        for forbidden in ("member_id", "traveler_id", "foreground_gps", "receipt_id", "query_center"):
            assert forbidden in body["withheld_fields"]
            assert forbidden not in body["navigation_url"]

        cross_member = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/two-stop-walk",
            headers=bystander_headers,
            json={
                "first_shortlist_id": first_id,
                "second_shortlist_id": second_id,
                "public_map_consent": True,
            },
        )
        assert cross_member.status_code == 403, cross_member.text

        with SessionLocal() as session:
            assert session.scalar(select(func.count(Place.id)).where(Place.city_code == city_code)) == 0
            assert session.scalar(select(func.count(PlanVersion.id)).where(PlanVersion.journey_id == journey_id)) == 0
            assert session.scalar(select(func.count(AgentRun.id)).where(AgentRun.journey_id == journey_id)) == 0
            assert session.scalar(select(func.count(GuardianRun.id)).where(GuardianRun.journey_id == journey_id)) == 0
            assert session.scalar(select(func.count(Booking.id)).where(Booking.journey_id == journey_id)) == 0
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
