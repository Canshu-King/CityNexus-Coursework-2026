from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.core.config import settings
from app.db.models import JourneyMember, MemberDraftPocketWalk, MemberDraftShortlistItem, RealPlaceCandidate
from app.db.session import SessionLocal


def _traveler(client: TestClient, label: str) -> dict:
    response = client.post(
        "/api/v1/travelers",
        json={"display_name": label, "timezone": "Asia/Shanghai", "locale": "en-GB"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _context(client: TestClient) -> tuple[dict, dict, str, str]:
    owner = _traveler(client, f"R107 owner {uuid4().hex[:8]}")
    collaborator = _traveler(client, f"R107 collaborator {uuid4().hex[:8]}")
    response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": owner["id"],
            "title": "R107 discovery Pocket Walk",
            "city_code": f"CN-R107-{uuid4().hex[:8].upper()}",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-18T10:00:00+08:00",
            "end_at": "2026-10-19T18:00:00+08:00",
            "budget": {"amount_minor": 10000, "currency": "CNY"},
        },
    )
    assert response.status_code == 201, response.text
    journey_id = response.json()["id"]
    stamp = "2026-10-12T08:00:00+00:00"
    with SessionLocal() as session:
        owner_member = session.scalar(
            select(JourneyMember).where(
                JourneyMember.journey_id == journey_id,
                JourneyMember.traveler_id == owner["id"],
            )
        )
        assert owner_member is not None
        session.add(
            JourneyMember(
                id=str(uuid4()),
                journey_id=journey_id,
                traveler_id=collaborator["id"],
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
        session.commit()
        return owner, collaborator, journey_id, owner_member.id


def _candidate(
    *,
    journey_id: str,
    member_id: str,
    traveler_id: str,
    receipt_id: str,
    osm_id: int,
    source_kind: str = "MANUAL_PUBLIC_CENTER",
    expires_at: str | None = "2099-01-01T00:00:00+00:00",
    latitude: float = 31.2304,
    longitude: float = 121.4737,
    external_id: str | None = None,
) -> str:
    candidate_id = str(uuid4())
    external_id = external_id or f"overpass:node/{osm_id}"
    name = f"R107 public place {osm_id}"
    captured_at = "2026-10-12T08:00:00+00:00"
    scope = {
        "member_id": member_id,
        "traveler_id": traveler_id,
        "receipt_id": receipt_id,
        "source_kind": source_kind,
        "expires_at": expires_at,
        "provider_provenance": "TEST_PROVIDER",
        "observed_name": name,
        "observed_category": "museum",
        "latitude": latitude,
        "longitude": longitude,
        "captured_at": captured_at,
        "opening_hours": {"status": "UNKNOWN"},
        "cost_evidence": {"status": "UNKNOWN"},
        "osm_tags": {"name": name, "tourism": "museum"},
        "public_access_state": "PUBLIC_ACCESS_UNVERIFIED",
        "source_url": f"https://www.openstreetmap.org/node/{osm_id}",
    }
    with SessionLocal() as session:
        session.add(
            RealPlaceCandidate(
                id=candidate_id,
                journey_id=journey_id,
                provider_key="overpass",
                external_id=external_id,
                city_code="CN-R107",
                name=name,
                category="museum",
                latitude=latitude,
                longitude=longitude,
                tags_json=json.dumps(["museum"]),
                opening_hours_json=json.dumps({"status": "UNKNOWN"}),
                metadata_json=json.dumps({"osm_tags": scope["osm_tags"]}),
                captured_at=captured_at,
                expires_at=expires_at,
                distance_m=120.0,
                evidence_json=json.dumps(
                    {
                        "bootstrap": True,
                        "osm_type": "node",
                        "osm_id": str(osm_id),
                        "bootstrap_scopes": [scope],
                    }
                ),
                materialized_place_id=None,
                status="BOOTSTRAP_PRIVATE",
            )
        )
        session.commit()
    return candidate_id


def _owner_count(journey_id: str, traveler_id: str) -> int:
    with SessionLocal() as session:
        return int(
            session.scalar(
                select(func.count(MemberDraftShortlistItem.id)).where(
                    MemberDraftShortlistItem.journey_id == journey_id,
                    MemberDraftShortlistItem.traveler_id == traveler_id,
                    MemberDraftShortlistItem.status == "SAVED",
                )
            )
            or 0
        )


def test_r107_manual_discovery_to_private_pocket_walk_is_atomic_and_source_bound() -> None:
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", "r107-discovery-pocket-secret-0123456789")
    client = TestClient(main_module.app)
    try:
        owner, collaborator, journey_id, owner_member_id = _context(client)
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        collaborator_headers = {"Authorization": f"Bearer {collaborator['access_token']}"}
        receipt_id = str(uuid4())
        first_id = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=receipt_id,
            osm_id=107001,
        )
        second_id = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=receipt_id,
            osm_id=107002,
            latitude=31.2314,
            longitude=121.4747,
        )
        response = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/pocket-walk",
            headers=owner_headers,
            json={
                "receipt_id": receipt_id,
                "first_candidate_id": first_id,
                "second_candidate_id": second_id,
                "max_walk_minutes": 20,
            },
        )
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["pocket_walk"]["first_stop"]["external_id"] == "overpass:node/107001"
        assert saved["pocket_walk"]["second_stop"]["external_id"] == "overpass:node/107002"
        assert saved["pocket_walk"]["max_walk_minutes"] == 20
        assert {item["source_provenance"] for item in saved["items"]} == {"TEST_PROVIDER"}
        assert _owner_count(journey_id, owner["id"]) == 2

        restored = client.get(f"/api/v1/journeys/{journey_id}/bootstrap-shortlist", headers=owner_headers)
        assert restored.status_code == 200, restored.text
        assert restored.json()["pocket_walk"]["first_stop"]["external_id"] == "overpass:node/107001"
        collaborator_read = client.get(f"/api/v1/journeys/{journey_id}/bootstrap-shortlist", headers=collaborator_headers)
        assert collaborator_read.status_code == 200, collaborator_read.text
        assert collaborator_read.json()["items"] == []
        assert collaborator_read.json()["pocket_walk"] is None

        before_rejects = _owner_count(journey_id, owner["id"])
        duplicate = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/pocket-walk",
            headers=owner_headers,
            json={"receipt_id": receipt_id, "first_candidate_id": first_id, "second_candidate_id": first_id},
        )
        assert duplicate.status_code == 422, duplicate.text
        assert _owner_count(journey_id, owner["id"]) == before_rejects

        foreign_id = _candidate(
            journey_id=journey_id,
            member_id=str(uuid4()),
            traveler_id=collaborator["id"],
            receipt_id=receipt_id,
            osm_id=107003,
        )
        foreign = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/pocket-walk",
            headers=owner_headers,
            json={"receipt_id": receipt_id, "first_candidate_id": first_id, "second_candidate_id": foreign_id},
        )
        assert foreign.status_code == 403, foreign.text
        assert _owner_count(journey_id, owner["id"]) == before_rejects

        shared_receipt = str(uuid4())
        shared_first = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=shared_receipt,
            osm_id=107004,
            source_kind="SHARED_PUBLIC_REFERENCE",
        )
        shared_second = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=shared_receipt,
            osm_id=107005,
            source_kind="SHARED_PUBLIC_REFERENCE",
        )
        shared = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/pocket-walk",
            headers=owner_headers,
            json={"receipt_id": shared_receipt, "first_candidate_id": shared_first, "second_candidate_id": shared_second},
        )
        assert shared.status_code == 403, shared.text
        assert _owner_count(journey_id, owner["id"]) == before_rejects

        expired_receipt = str(uuid4())
        expired_first = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=expired_receipt,
            osm_id=107006,
            expires_at="2020-01-01T00:00:00+00:00",
        )
        expired_second = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=expired_receipt,
            osm_id=107007,
            expires_at="2020-01-01T00:00:00+00:00",
        )
        expired = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/pocket-walk",
            headers=owner_headers,
            json={"receipt_id": expired_receipt, "first_candidate_id": expired_first, "second_candidate_id": expired_second},
        )
        assert expired.status_code == 410, expired.text
        assert _owner_count(journey_id, owner["id"]) == before_rejects

        malformed_receipt = str(uuid4())
        valid_for_atomicity = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=malformed_receipt,
            osm_id=107008,
        )
        invalid_identity = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=malformed_receipt,
            osm_id=107009,
            external_id="provider:bad/identity",
        )
        malformed = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/pocket-walk",
            headers=owner_headers,
            json={"receipt_id": malformed_receipt, "first_candidate_id": valid_for_atomicity, "second_candidate_id": invalid_identity},
        )
        assert malformed.status_code == 422, malformed.text
        assert _owner_count(journey_id, owner["id"]) == before_rejects
        with SessionLocal() as session:
            assert session.scalar(
                select(MemberDraftPocketWalk).where(
                    MemberDraftPocketWalk.journey_id == journey_id,
                    MemberDraftPocketWalk.traveler_id == owner["id"],
                )
            ) is not None
            assert session.scalar(
                select(MemberDraftShortlistItem).where(
                    MemberDraftShortlistItem.external_id == "provider:bad/identity",
                    MemberDraftShortlistItem.traveler_id == owner["id"],
                )
            ) is None
        # The first OSM identity is already privately saved. A later public
        # discovery for this SAME member and identity must refresh its
        # observation, not leave an old destination in the Pocket Walk.
        refreshed_receipt_id = str(uuid4())
        with SessionLocal() as session:
            for candidate_id in (first_id, second_id):
                row = session.get(RealPlaceCandidate, candidate_id)
                assert row is not None
                evidence = json.loads(row.evidence_json)
                scope = evidence["bootstrap_scopes"][0]
                scope["receipt_id"] = refreshed_receipt_id
                scope["captured_at"] = "2026-10-14T08:00:00+00:00"
                if candidate_id == first_id:
                    scope["observed_name"] = "R107 updated public museum"
                    scope["latitude"] = 31.2504
                    scope["longitude"] = 121.4937
                row.evidence_json = json.dumps(evidence)
            session.commit()
        refreshed = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/pocket-walk",
            headers=owner_headers,
            json={
                "receipt_id": refreshed_receipt_id,
                "first_candidate_id": first_id,
                "second_candidate_id": second_id,
                "max_walk_minutes": 30,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        refreshed_body = refreshed.json()
        assert refreshed_body["pocket_walk"]["first_stop"]["name"] == "R107 updated public museum"
        assert refreshed_body["pocket_walk"]["first_stop"]["source_provenance"] == "TEST_PROVIDER"
        assert refreshed_body["pocket_walk"]["max_walk_minutes"] == 30
        refreshed_first = next(item for item in refreshed_body["items"] if item["external_id"] == "overpass:node/107001")
        assert refreshed_first["latitude"] == 31.2504
        assert refreshed_first["longitude"] == 121.4937
        assert refreshed_first["captured_at"] == "2026-10-14T08:00:00+00:00"
        assert _owner_count(journey_id, owner["id"]) == 2
        collaborator_read = client.get(f"/api/v1/journeys/{journey_id}/bootstrap-shortlist", headers=collaborator_headers)
        assert collaborator_read.status_code == 200
        assert collaborator_read.json()["items"] == []

    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
