from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.core.config import settings
from app.db.models import JourneyMember, MemberDraftResumeIntent, MemberDraftShortlistItem, PlanVersion, ProviderCall, RealPlaceCandidate
from app.db.session import SessionLocal
from app.integrations.discovery.real_city import DiscoveryCandidate


client = TestClient(main_module.app)
TEST_SECRET = "r90-private-exploration-board-secret-0123456789"


def _traveler(name: str) -> dict:
    response = client.post(
        "/api/v1/travelers",
        json={"display_name": name, "timezone": "Asia/Shanghai", "locale": "en-GB"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_r90_shortlist_is_private_persistent_and_reauthorizes_after_receipt_expiry(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    previous_enabled = settings.real_city_enabled
    previous_mode = settings.real_city_mode
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET)
    object.__setattr__(settings, "real_city_enabled", True)
    object.__setattr__(settings, "real_city_mode", "live")

    def fake_search(_self, query, **kwargs):
        assert kwargs["max_attempts"] == 1
        candidate = DiscoveryCandidate(
            provider_key="TEST_PROVIDER",
            external_id="overpass:node/90001",
            name="R90 Private City Gallery",
            category="gallery",
            latitude=39.1205,
            longitude=117.2005,
            tags=("gallery", "real_city"),
            opening_hours={"status": "UNKNOWN"},
            metadata={
                "osm_id": "node/90001",
                "tags": {"name": "R90 Private City Gallery", "tourism": "gallery"},
            },
            distance_m=80.0,
            cost_evidence={"status": "UNKNOWN", "source": "TEST_PROVIDER"},
        )
        return [candidate], [{"endpoint": "TEST_PROVIDER", "answered": True, "http_status": 200, "elements": 1, "query_hint": str(query)}]

    monkeypatch.setattr(
        "app.execution.bootstrap_discovery.OverpassPOIProvider.search_with_evidence",
        fake_search,
    )
    owner = _traveler("R90 owner")
    viewer = _traveler("R90 viewer")
    journey_response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": owner["id"],
            "title": "R90 private exploration board",
            "city_code": f"CN-R90-{uuid4().hex[:8].upper()}",
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

        discovery_payload = {
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
        owner_discovery = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery",
            headers=owner_headers,
            json=discovery_payload,
        )
        viewer_discovery = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery",
            headers=viewer_headers,
            json={**discovery_payload, "query": "viewer gallery"},
        )
        assert owner_discovery.status_code == 200, owner_discovery.text
        assert viewer_discovery.status_code == 200, viewer_discovery.text
        owner_body = owner_discovery.json()
        viewer_body = viewer_discovery.json()
        owner_candidate = owner_body["candidates"][0]
        viewer_candidate = viewer_body["candidates"][0]
        assert owner_candidate["candidate_id"] == viewer_candidate["candidate_id"]
        assert owner_body["receipt_id"] != viewer_body["receipt_id"]
        assert "owner gallery" not in json.dumps(viewer_body)
        assert "viewer gallery" not in json.dumps(owner_body)

        save_owner = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers=owner_headers,
            json={"receipt_id": owner_body["receipt_id"], "candidate_id": owner_candidate["candidate_id"]},
        )
        assert save_owner.status_code == 200, save_owner.text
        owner_item = save_owner.json()
        assert owner_item["opening_state"] == "UNKNOWN"
        assert owner_item["price_state"] == "UNKNOWN"
        assert owner_item["eta_state"] == "UNKNOWN"
        cross_member_release = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/{owner_item['shortlist_id']}/preview",
            headers=viewer_headers,
            json={"public_map_consent": True},
        )
        assert cross_member_release.status_code == 403, cross_member_release.text
        viewer_list_before = client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist", headers=viewer_headers
        )
        assert viewer_list_before.status_code == 200, viewer_list_before.text
        assert viewer_list_before.json()["items"] == []

        save_viewer = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers=viewer_headers,
            json={"receipt_id": viewer_body["receipt_id"], "candidate_id": viewer_candidate["candidate_id"]},
        )
        assert save_viewer.status_code == 200, save_viewer.text
        viewer_item = save_viewer.json()
        assert viewer_item["shortlist_id"] != owner_item["shortlist_id"]
        assert client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist", headers=owner_headers
        ).json()["items"][0]["shortlist_id"] == owner_item["shortlist_id"]

        # The private discovery scopes expire, but the member-owned public identity remains.
        with SessionLocal() as session:
            row = session.scalar(
                select(RealPlaceCandidate).where(RealPlaceCandidate.id == owner_candidate["candidate_id"])
            )
            assert row is not None
            evidence = json.loads(row.evidence_json)
            expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            for scope in evidence["bootstrap_scopes"]:
                scope["expires_at"] = expired
            row.evidence_json = json.dumps(evidence)
            row.expires_at = expired
            session.commit()

        expired_preview = client.post(
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
        assert expired_preview.status_code == 410, expired_preview.text

        saved_preview = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/{viewer_item['shortlist_id']}/preview",
            headers=viewer_headers,
            json={"public_map_consent": True},
        )
        assert saved_preview.status_code == 200, saved_preview.text
        saved_body = saved_preview.json()
        assert saved_body["authorization_basis"] == "SAVED_PUBLIC_IDENTITY"
        assert "receipt_id" not in saved_body
        assert "query_center" in saved_body["withheld_fields"]
        assert saved_body["destination"]["map_url"] == (
            "https://www.openstreetmap.org/?mlat=39.1205000&mlon=117.2005000#map=18/39.1205000/117.2005000"
        )
        navigation = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/{viewer_item['shortlist_id']}/navigate",
            headers=viewer_headers,
            json={"public_map_consent": True},
        )
        assert navigation.status_code == 200, navigation.text
        navigation_body = navigation.json()
        assert navigation_body["navigation_mode"] == "WALKING_DIRECTIONS"
        assert navigation_body["navigation_url"] == (
            "https://www.google.com/maps/dir/?api=1&destination=39.1205000%2C117.2005000&travelmode=walking"
        )
        assert "origin" not in navigation_body["navigation_url"]

        removed = client.delete(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/{viewer_item['shortlist_id']}",
            headers=viewer_headers,
        )
        assert removed.status_code == 200, removed.text
        assert client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist", headers=viewer_headers
        ).json()["items"] == []

        with SessionLocal() as session:
            assert session.scalar(select(func.count(PlanVersion.id)).where(PlanVersion.journey_id == journey_id)) == 0
            assert session.scalar(select(func.count(ProviderCall.id)).where(ProviderCall.operation == "bootstrap_city_discovery", ProviderCall.request_json.like(f"%{journey_id}%"))) == 2
            assert session.scalar(select(func.count(MemberDraftShortlistItem.id)).where(MemberDraftShortlistItem.journey_id == journey_id)) == 1
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        object.__setattr__(settings, "real_city_enabled", previous_enabled)
        object.__setattr__(settings, "real_city_mode", previous_mode)


def test_r91_shortlist_limit_is_serialized_under_concurrent_saves(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    previous_enabled = settings.real_city_enabled
    previous_mode = settings.real_city_mode
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET + "-limit")
    object.__setattr__(settings, "real_city_enabled", True)
    object.__setattr__(settings, "real_city_mode", "live")

    def fake_search(_self, query, **kwargs):
        candidates = []
        for index in range(1, 4):
            osm_id = f"9100{index}"
            candidates.append(
                DiscoveryCandidate(
                    provider_key="TEST_PROVIDER",
                    external_id=f"overpass:node/{osm_id}",
                    name=f"R91 bounded place {index}",
                    category="gallery",
                    latitude=39.1205 + index / 10000,
                    longitude=117.2005 + index / 10000,
                    tags=("gallery",),
                    opening_hours={"status": "UNKNOWN"},
                    metadata={"osm_id": f"node/{osm_id}", "tags": {"name": f"R91 bounded place {index}"}},
                    distance_m=80.0 + index,
                    cost_evidence={"status": "UNKNOWN", "source": "TEST_PROVIDER"},
                )
            )
        return candidates, [{"endpoint": "TEST_PROVIDER", "answered": True, "http_status": 200, "elements": 3, "query_hint": str(query)}]

    monkeypatch.setattr(
        "app.execution.bootstrap_discovery.OverpassPOIProvider.search_with_evidence",
        fake_search,
    )
    owner = _traveler("R91 limit owner")
    journey_response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": owner["id"],
            "title": "R91 bounded shortlist",
            "city_code": f"CN-R91-{uuid4().hex[:8].upper()}",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-13T10:00:00+08:00",
            "end_at": "2026-10-14T18:00:00+08:00",
            "budget": {"amount_minor": 10000, "currency": "CNY"},
        },
    )
    assert journey_response.status_code == 201, journey_response.text
    journey_id = journey_response.json()["id"]
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    try:
        discovery = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery",
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
        body = discovery.json()
        assert len(body["candidates"]) == 3
        saved = []
        for candidate in body["candidates"][:2]:
            response = client.post(
                f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
                headers=headers,
                json={"receipt_id": body["receipt_id"], "candidate_id": candidate["candidate_id"]},
            )
            assert response.status_code == 200, response.text
            saved.append(response.json())

        # Add a fourth scoped candidate only for this bounded-concurrency fixture;
        # the public endpoint itself still caps every discovery response at three.
        with SessionLocal() as session:
            source = session.get(RealPlaceCandidate, body["candidates"][0]["candidate_id"])
            assert source is not None
            evidence = json.loads(source.evidence_json)
            extra_id = str(uuid4())
            evidence["candidate_id"] = extra_id
            evidence["osm_id"] = "91004"
            evidence["source_url"] = "https://www.openstreetmap.org/node/91004"
            session.add(
                RealPlaceCandidate(
                    id=extra_id,
                    journey_id=journey_id,
                    provider_key="overpass",
                    external_id="overpass:node/91004",
                    city_code=source.city_code,
                    name="R91 bounded place 4",
                    category="gallery",
                    latitude=39.1210,
                    longitude=117.2010,
                    tags_json=source.tags_json,
                    opening_hours_json=source.opening_hours_json,
                    metadata_json=source.metadata_json,
                    captured_at=source.captured_at,
                    expires_at=source.expires_at,
                    distance_m=84.0,
                    evidence_json=json.dumps(evidence),
                    materialized_place_id=None,
                    status="BOOTSTRAP_PRIVATE",
                )
            )
            session.commit()

        candidate_ids = [body["candidates"][2]["candidate_id"], extra_id]

        def save(candidate_id: str):
            response = client.post(
                f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
                headers=headers,
                json={"receipt_id": body["receipt_id"], "candidate_id": candidate_id},
            )
            return response.status_code, response.text

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save, candidate_ids))
        assert sorted(status for status, _text in outcomes) == [200, 409], outcomes
        assert sum("BOOTSTRAP_SHORTLIST_LIMIT" in text for _status, text in outcomes) == 1
        with SessionLocal() as session:
            assert session.scalar(
                select(func.count(MemberDraftShortlistItem.id)).where(
                    MemberDraftShortlistItem.journey_id == journey_id,
                    MemberDraftShortlistItem.member_id == select(JourneyMember.id).where(
                        JourneyMember.journey_id == journey_id,
                        JourneyMember.traveler_id == owner["id"],
                    ).scalar_subquery(),
                )
            ) == 3
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        object.__setattr__(settings, "real_city_enabled", previous_enabled)
        object.__setattr__(settings, "real_city_mode", previous_mode)


def test_r92_resume_intent_is_member_private_and_clears_after_saved_place_removal(monkeypatch):
    previous_secret = settings.traveler_auth_secret
    previous_enabled = settings.real_city_enabled
    previous_mode = settings.real_city_mode
    object.__setattr__(settings, "traveler_auth_secret", TEST_SECRET + "-resume")
    object.__setattr__(settings, "real_city_enabled", True)
    object.__setattr__(settings, "real_city_mode", "live")

    def fake_search(_self, query, **kwargs):
        candidate = DiscoveryCandidate(
            provider_key="TEST_PROVIDER",
            external_id="overpass:node/92001",
            name="R92 Resume Gallery",
            category="gallery",
            latitude=39.1215,
            longitude=117.2015,
            tags=("gallery", "real_city"),
            opening_hours={"status": "UNKNOWN"},
            metadata={
                "osm_id": "node/92001",
                "tags": {"name": "R92 Resume Gallery", "tourism": "gallery"},
            },
            distance_m=90.0,
            cost_evidence={"status": "UNKNOWN", "source": "TEST_PROVIDER"},
        )
        return [candidate], [{"endpoint": "TEST_PROVIDER", "answered": True, "http_status": 200, "elements": 1, "query_hint": str(query)}]

    monkeypatch.setattr(
        "app.execution.bootstrap_discovery.OverpassPOIProvider.search_with_evidence",
        fake_search,
    )
    owner = _traveler("R92 resume owner")
    bystander = _traveler("R92 resume bystander")
    journey_response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": owner["id"],
            "title": "R92 private outing resume",
            "city_code": f"CN-R92-{uuid4().hex[:8].upper()}",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-13T10:00:00+08:00",
            "end_at": "2026-10-14T18:00:00+08:00",
            "budget": {"amount_minor": 10000, "currency": "CNY"},
        },
    )
    assert journey_response.status_code == 201, journey_response.text
    journey_id = journey_response.json()["id"]
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    bystander_headers = {"Authorization": f"Bearer {bystander['access_token']}"}
    try:
        with SessionLocal() as session:
            session.add(
                JourneyMember(
                    id=str(uuid4()),
                    journey_id=journey_id,
                    traveler_id=bystander["id"],
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

        discovery = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery",
            headers=owner_headers,
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
        candidate = discovery_body["candidates"][0]
        saved = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers=owner_headers,
            json={"receipt_id": discovery_body["receipt_id"], "candidate_id": candidate["candidate_id"]},
        )
        assert saved.status_code == 200, saved.text
        item = saved.json()

        remember = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/{item['shortlist_id']}/resume",
            headers=owner_headers,
        )
        assert remember.status_code == 200, remember.text
        resume_body = remember.json()
        assert resume_body["shortlist_id"] == item["shortlist_id"]
        assert resume_body["name"] == item["name"]
        assert resume_body["status"] == "CHOSEN"
        assert "navigation_url" not in resume_body
        assert "member_id" not in json.dumps(resume_body)
        owner_board = client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist", headers=owner_headers
        )
        assert owner_board.status_code == 200, owner_board.text
        assert owner_board.json()["resume"]["shortlist_id"] == item["shortlist_id"]

        bystander_board = client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist", headers=bystander_headers
        )
        assert bystander_board.status_code == 200, bystander_board.text
        assert bystander_board.json()["items"] == []
        assert bystander_board.json()["resume"] is None
        cross_member = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/{item['shortlist_id']}/resume",
            headers=bystander_headers,
        )
        assert cross_member.status_code == 403, cross_member.text

        removed = client.delete(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/{item['shortlist_id']}",
            headers=owner_headers,
        )
        assert removed.status_code == 200, removed.text
        owner_after_removal = client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist", headers=owner_headers
        )
        assert owner_after_removal.status_code == 200, owner_after_removal.text
        assert owner_after_removal.json()["items"] == []
        assert owner_after_removal.json()["resume"] is None
        with SessionLocal() as session:
            assert session.scalar(
                select(func.count(MemberDraftResumeIntent.id)).where(
                    MemberDraftResumeIntent.journey_id == journey_id
                )
            ) == 0
            assert session.scalar(
                select(func.count(PlanVersion.id)).where(PlanVersion.journey_id == journey_id)
            ) == 0
            assert session.scalar(
                select(func.count(ProviderCall.id)).where(
                    ProviderCall.operation == "bootstrap_city_discovery",
                    ProviderCall.request_json.like(f"%{journey_id}%"),
                )
            ) == 1
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        object.__setattr__(settings, "real_city_enabled", previous_enabled)
        object.__setattr__(settings, "real_city_mode", previous_mode)
