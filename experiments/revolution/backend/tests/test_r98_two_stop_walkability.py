from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.execution.bootstrap_discovery as bootstrap_module
import app.main as main_module
from app.core.config import settings
from app.db.models import (
    AgentRun,
    Booking,
    GuardianRun,
    JourneyMember,
    MemberDraftShortlistItem,
    MobilityFact,
    Place,
    PlanVersion,
    ProviderCall,
    RouteAcquisitionOperation,
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


def test_r98_two_stop_walkability_is_provider_grounded_or_honestly_unknown(monkeypatch):
    global client
    previous_secret = settings.traveler_auth_secret
    previous_valhalla_url = settings.live_valhalla_url
    object.__setattr__(settings, "traveler_auth_secret", "REDACTED_FOR_PUBLIC_RELEASE")
    object.__setattr__(settings, "live_valhalla_url", "http://127.0.0.1:8998")
    client = TestClient(main_module.app)
    owner = _traveler(f"R98 walkability owner {uuid4().hex[:6]}")
    bystander = _traveler(f"R98 walkability bystander {uuid4().hex[:6]}")
    city_code = f"CN-R98-{uuid4().hex[:8].upper()}"
    journey_response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": owner["id"],
            "title": "R98 walkability draft",
            "city_code": city_code,
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-17T10:00:00+08:00",
            "end_at": "2026-10-18T18:00:00+08:00",
            "budget": {"amount_minor": 10000, "currency": "CNY"},
        },
    )
    assert journey_response.status_code == 201, journey_response.text
    journey_id = journey_response.json()["id"]
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    bystander_headers = {"Authorization": f"Bearer {bystander['access_token']}"}
    calls: list[tuple[str, str]] = []

    class FakeValhalla:
        provider_key = "valhalla-http"
        capture_provenance = "TEST_PROVIDER_EMULATOR"

        def __init__(self, **kwargs):
            self._lookup = kwargs["place_lookup"]

        def estimate(self, origin: str, destination: str):
            first = self._lookup(origin)
            second = self._lookup(destination)
            calls.append((f"{first['latitude']},{first['longitude']}", f"{second['latitude']},{second['longitude']}"))
            return SimpleNamespace(duration_min=18, distance_m=1250)

    monkeypatch.setattr(bootstrap_module, "ValhallaHTTPMobilityProvider", FakeValhalla)
    try:
        with SessionLocal() as session:
            owner_member = session.scalar(
                select(JourneyMember).where(
                    JourneyMember.journey_id == journey_id,
                    JourneyMember.traveler_id == owner["id"],
                )
            )
            assert owner_member is not None
            stamp = "2026-10-11T10:00:00+00:00"
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
                (first_id, "overpass:node/98001", "R98 Museum", 31.2304, 121.4737),
                (second_id, "overpass:node/98002", "R98 Garden", 31.2314, 121.4757),
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
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/two-stop-walkability",
            headers=owner_headers,
            json={
                "first_shortlist_id": first_id,
                "second_shortlist_id": second_id,
                "public_route_consent": False,
            },
        )
        assert no_consent.status_code == 422, no_consent.text

        grounded = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/two-stop-walkability",
            headers=owner_headers,
            json={
                "first_shortlist_id": first_id,
                "second_shortlist_id": second_id,
                "public_route_consent": True,
            },
        )
        assert grounded.status_code == 200, grounded.text
        body = grounded.json()
        assert body["status"] == "ROUTE_VERIFIED"
        assert body["route_state"] == "ROUTE_VERIFIED"
        assert body["duration_min"] == 18
        assert body["distance_m"] == 1250
        assert body["provider"] == "valhalla-http"
        assert body["provenance"] == "TEST_PROVIDER_EMULATOR"
        assert body["route_time_state"] == "PROVIDER_CAPTURED"
        assert body["explore_state"] == "DRAFT_EXPLORE_ONLY"
        assert body["plan_version_id"] is None
        assert body["selected_place_id"] is None
        assert body["navigation_url"] == (
            "https://www.google.com/maps/dir/?api=1&origin=31.2304000%2C121.4737000"
            "&destination=31.2314000%2C121.4757000&travelmode=walking"
        )
        assert calls == [("31.2304,121.4737", "31.2314,121.4757")]
        for forbidden in ("foreground_gps", "query_center", "member_id", "traveler_id", "receipt_id", "private_intent"):
            assert forbidden in body["withheld_fields"]
            assert forbidden not in body["navigation_url"]

        cross_member = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/two-stop-walkability",
            headers=bystander_headers,
            json={
                "first_shortlist_id": first_id,
                "second_shortlist_id": second_id,
                "public_route_consent": True,
            },
        )
        assert cross_member.status_code == 403, cross_member.text

        # A configured public plaintext endpoint must not be contacted or
        # graded as LIVE_PROVIDER_CAPTURE. Only loopback HTTP test emulators
        # and TLS-protected remote routers are accepted.
        object.__setattr__(settings, "live_valhalla_url", "http://routing.public.example")
        unsafe = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/two-stop-walkability",
            headers=owner_headers,
            json={
                "first_shortlist_id": first_id,
                "second_shortlist_id": second_id,
                "public_route_consent": True,
            },
        )
        assert unsafe.status_code == 200, unsafe.text
        assert unsafe.json()["status"] == "ROUTE_UNVERIFIED"
        assert unsafe.json()["reason_code"] == "ROUTE_PROVIDER_ENDPOINT_UNTRUSTED"
        assert unsafe.json()["duration_min"] is None
        assert unsafe.json()["distance_m"] is None
        assert len(calls) == 1

        object.__setattr__(settings, "live_valhalla_url", None)
        unavailable = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/two-stop-walkability",
            headers=owner_headers,
            json={
                "first_shortlist_id": first_id,
                "second_shortlist_id": second_id,
                "public_route_consent": True,
            },
        )
        assert unavailable.status_code == 200, unavailable.text
        unavailable_body = unavailable.json()
        assert unavailable_body["status"] == "ROUTE_UNVERIFIED"
        assert unavailable_body["reason_code"] == "ROUTE_PROVIDER_DISABLED"
        assert unavailable_body["duration_min"] is None
        assert unavailable_body["distance_m"] is None
        assert unavailable_body["provenance"] == "UNVERIFIED"
        assert len(calls) == 1

        with SessionLocal() as session:
            assert session.scalar(select(func.count(Place.id)).where(Place.city_code == city_code)) == 0
            assert session.scalar(select(func.count(PlanVersion.id)).where(PlanVersion.journey_id == journey_id)) == 0
            assert session.scalar(select(func.count(MobilityFact.id)).where(MobilityFact.journey_id == journey_id)) == 0
            assert session.scalar(select(func.count(RouteAcquisitionOperation.id)).where(RouteAcquisitionOperation.journey_id == journey_id)) == 0
            assert session.scalar(
                select(func.count(ProviderCall.id)).where(ProviderCall.request_json.like(f"%{journey_id}%"))
            ) == 0
            assert session.scalar(select(func.count(AgentRun.id)).where(AgentRun.journey_id == journey_id)) == 0
            assert session.scalar(select(func.count(GuardianRun.id)).where(GuardianRun.journey_id == journey_id)) == 0
            assert session.scalar(select(func.count(Booking.id)).where(Booking.journey_id == journey_id)) == 0
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        object.__setattr__(settings, "live_valhalla_url", previous_valhalla_url)
