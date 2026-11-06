from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.core.config import settings
from app.db.models import (
    Journey,
    JourneyMember,
    MemberDraftPocketWalk,
    MemberDraftShortlistItem,
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


def test_r100_pocket_walk_is_private_ordered_and_durable():
    global client
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", "REDACTED_FOR_PUBLIC_RELEASE")
    client = TestClient(main_module.app)
    owner = _traveler(f"R100 owner {uuid4().hex[:6]}")
    collaborator = _traveler(f"R100 collaborator {uuid4().hex[:6]}")
    city_code = f"CN-R100-{uuid4().hex[:8].upper()}"
    journey_response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": owner["id"],
            "title": "R100 Pocket Walk draft",
            "city_code": city_code,
            "timezone": "Asia/Shanghai",
            "start_at": "2026-10-18T10:00:00+08:00",
            "end_at": "2026-10-19T18:00:00+08:00",
            "budget": {"amount_minor": 10000, "currency": "CNY"},
        },
    )
    assert journey_response.status_code == 201, journey_response.text
    journey_id = journey_response.json()["id"]
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    collaborator_headers = {"Authorization": f"Bearer {collaborator['access_token']}"}
    stamp = "2026-10-11T11:00:00+00:00"
    try:
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
            shortlist_ids: list[str] = []
            for index in range(3):
                shortlist_id = str(uuid4())
                shortlist_ids.append(shortlist_id)
                session.add(
                    MemberDraftShortlistItem(
                        id=shortlist_id,
                        journey_id=journey_id,
                        member_id=owner_member.id,
                        traveler_id=owner["id"],
                        provider_key="overpass",
                        external_id=f"overpass:node/{100001 + index}",
                        name=f"R100 public stop {index + 1}",
                        category="public",
                        latitude=31.2304 + index / 1000,
                        longitude=121.4737 + index / 1000,
                        coordinate_system="WGS84",
                        source_url=f"https://www.openstreetmap.org/node/{100001 + index}",
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

        save = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/pocket-walk",
            headers=owner_headers,
            json={
                "first_shortlist_id": shortlist_ids[0],
                "second_shortlist_id": shortlist_ids[2],
                "max_walk_minutes": 20,
            },
        )
        assert save.status_code == 200, save.text
        saved = save.json()
        assert saved["status"] == "POCKET_WALK_SAVED"
        assert saved["first_stop"]["shortlist_id"] == shortlist_ids[0]
        assert saved["second_stop"]["shortlist_id"] == shortlist_ids[2]
        assert saved["max_walk_minutes"] == 20
        assert saved["map_authorization"] == "NONE"
        assert saved["plan_version_id"] is None
        assert "journey_id" in saved["withheld_fields"]
        assert owner["id"] in saved["withheld_fields"] or "traveler_id" in saved["withheld_fields"]

        restored = client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers=owner_headers,
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["pocket_walk"]["first_stop"]["name"] == "R100 public stop 1"
        assert restored.json()["pocket_walk"]["second_stop"]["name"] == "R100 public stop 3"

        collaborator_read = client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers=collaborator_headers,
        )
        assert collaborator_read.status_code == 200, collaborator_read.text
        assert collaborator_read.json()["items"] == []
        assert collaborator_read.json()["pocket_walk"] is None

        cross_member = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/pocket-walk",
            headers=collaborator_headers,
            json={
                "first_shortlist_id": shortlist_ids[0],
                "second_shortlist_id": shortlist_ids[1],
                "max_walk_minutes": 45,
            },
        )
        assert cross_member.status_code == 403, cross_member.text

        changed = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/pocket-walk",
            headers=owner_headers,
            json={
                "first_shortlist_id": shortlist_ids[1],
                "second_shortlist_id": shortlist_ids[0],
                "max_walk_minutes": None,
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["first_stop"]["shortlist_id"] == shortlist_ids[1]
        assert changed.json()["second_stop"]["shortlist_id"] == shortlist_ids[0]
        assert changed.json()["max_walk_minutes"] is None

        with SessionLocal() as session:
            row = session.scalar(
                select(MemberDraftPocketWalk).where(
                    MemberDraftPocketWalk.journey_id == journey_id,
                    MemberDraftPocketWalk.traveler_id == owner["id"],
                )
            )
            assert row is not None
            assert row.first_shortlist_id == shortlist_ids[1]
            assert row.second_shortlist_id == shortlist_ids[0]
            assert row.max_walk_minutes is None
            assert not hasattr(MemberDraftPocketWalk, "latitude")
            assert session.scalar(select(func.count(PlanVersion.id)).where(PlanVersion.journey_id == journey_id)) == 0

        cleared = client.delete(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/pocket-walk",
            headers=owner_headers,
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["cleared"] is True
        clear_read = client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers=owner_headers,
        )
        assert clear_read.status_code == 200, clear_read.text
        assert clear_read.json()["pocket_walk"] is None
        restored_order = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/pocket-walk",
            headers=owner_headers,
            json={
                "first_shortlist_id": shortlist_ids[1],
                "second_shortlist_id": shortlist_ids[0],
                "max_walk_minutes": None,
            },
        )
        assert restored_order.status_code == 200, restored_order.text

        removed = client.delete(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/{shortlist_ids[1]}",
            headers=owner_headers,
        )
        assert removed.status_code == 200, removed.text
        after_remove = client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers=owner_headers,
        )
        assert after_remove.status_code == 200, after_remove.text
        assert after_remove.json()["pocket_walk"] is None
        with SessionLocal() as session:
            assert session.scalar(
                select(func.count(MemberDraftPocketWalk.id)).where(
                    MemberDraftPocketWalk.journey_id == journey_id,
                    MemberDraftPocketWalk.traveler_id == owner["id"],
                )
            ) == 0
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
        client = None
