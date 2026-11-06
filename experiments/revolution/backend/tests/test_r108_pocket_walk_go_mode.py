from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main_module
from app.core.config import settings
from app.db.models import (
    Event,
    JourneyMember,
    MemberDraftPocketWalk,
    MemberDraftPocketWalkProgress,
    PlanVersion,
    ProviderCall,
    RealPlaceCandidate,
)
from app.db.session import SessionLocal
from test_r107_discovery_to_my_pocket_walk import _candidate, _context


def _save_walk(client: TestClient, journey_id: str, headers: dict[str, str], first: str, second: str) -> dict:
    response = client.post(
        f"/api/v1/journeys/{journey_id}/bootstrap-discovery/pocket-walk",
        headers=headers,
        json={
            "receipt_id": _RECEIPT,
            "first_candidate_id": first,
            "second_candidate_id": second,
            "max_walk_minutes": 30,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


_RECEIPT = ""


def test_r108_go_mode_is_revision_bound_idempotent_and_self_report_only() -> None:
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", "r108-go-mode-secret-0123456789abcdef")
    client = TestClient(main_module.app)
    try:
        global _RECEIPT
        owner, collaborator, journey_id, owner_member_id = _context(client)
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        collaborator_headers = {"Authorization": f"Bearer {collaborator['access_token']}"}
        _RECEIPT = str(uuid4())
        first = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=_RECEIPT,
            osm_id=108001,
        )
        second = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=_RECEIPT,
            osm_id=108002,
            latitude=31.2314,
            longitude=121.4747,
        )
        saved = _save_walk(client, journey_id, owner_headers, first, second)
        pocket = saved["pocket_walk"]
        assert pocket["revision"] == 1
        assert pocket["go_progress"]["state"] == "NOT_STARTED"
        assert pocket["go_progress"]["progress_class"] == "USER_REPORTED_PROGRESS"
        assert pocket["go_progress"]["physical_arrival_state"] == "NOT_PHYSICAL_ARRIVAL_PROVEN"

        restored = client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers=owner_headers,
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["pocket_walk"]["go_progress"]["state"] == "NOT_STARTED"

        def go(action: str, expected: int, key: str, pocket_revision: int = 1):
            return client.post(
                f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/pocket-walk/go",
                headers=owner_headers,
                json={
                    "action": action,
                    "expected_revision": expected,
                    "pocket_walk_revision": pocket_revision,
                    "idempotency_key": key,
                },
            )

        start_key = str(uuid4())
        started = go("START", 0, start_key)
        assert started.status_code == 200, started.text
        assert started.json()["go_progress"]["state"] == "HEADING_TO_FIRST"
        assert started.json()["go_progress"]["revision"] == 1

        duplicate = go("START", 0, start_key)
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["go_progress"]["revision"] == 1

        stale = go("REPORT_FIRST_DONE", 0, str(uuid4()))
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["reason_code"] == "BOOTSTRAP_GO_REVISION_CONFLICT"

        first_done = go("REPORT_FIRST_DONE", 1, str(uuid4()))
        assert first_done.status_code == 200, first_done.text
        assert first_done.json()["go_progress"]["state"] == "FIRST_STOP_DONE_SELF_REPORTED"
        second_started = go("START_SECOND", 2, str(uuid4()))
        assert second_started.status_code == 200, second_started.text
        assert second_started.json()["go_progress"]["state"] == "HEADING_TO_SECOND"
        paused = go("PAUSE", 3, str(uuid4()))
        assert paused.status_code == 200, paused.text
        assert paused.json()["go_progress"]["state"] == "PAUSED"
        assert paused.json()["go_progress"]["paused_from_state"] == "HEADING_TO_SECOND"
        resumed = go("RESUME", 4, str(uuid4()))
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["go_progress"]["state"] == "HEADING_TO_SECOND"
        finished = go("REPORT_WALK_DONE", 5, str(uuid4()))
        assert finished.status_code == 200, finished.text
        assert finished.json()["go_progress"]["state"] == "WALK_DONE_SELF_REPORTED"
        undone = go("UNDO", 6, str(uuid4()))
        assert undone.status_code == 200, undone.text
        assert undone.json()["go_progress"]["state"] == "HEADING_TO_SECOND"
        assert undone.json()["go_progress"]["physical_arrival_state"] == "NOT_PHYSICAL_ARRIVAL_PROVEN"

        collaborator_read = client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers=collaborator_headers,
        )
        assert collaborator_read.status_code == 200, collaborator_read.text
        assert collaborator_read.json()["items"] == []
        assert collaborator_read.json()["pocket_walk"] is None

        with SessionLocal() as session:
            assert session.scalar(
                select(MemberDraftPocketWalkProgress).where(
                    MemberDraftPocketWalkProgress.journey_id == journey_id,
                    MemberDraftPocketWalkProgress.traveler_id == owner["id"],
                )
            ) is not None
            assert session.scalar(
                select(PlanVersion).where(PlanVersion.journey_id == journey_id)
            ) is None
            assert session.scalar(
                select(Event).where(Event.journey_id == journey_id)
            ) is None
            assert session.scalar(
                select(ProviderCall).where(ProviderCall.operation.like("%pocket%"))
            ) is None
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_r108_resaving_order_resets_progress_and_increments_walk_revision() -> None:
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", "r108-go-mode-secret-fedcba9876543210")
    client = TestClient(main_module.app)
    try:
        global _RECEIPT
        owner, _collaborator, journey_id, owner_member_id = _context(client)
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        _RECEIPT = str(uuid4())
        first = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=_RECEIPT,
            osm_id=108011,
        )
        second = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=_RECEIPT,
            osm_id=108012,
            latitude=31.2314,
            longitude=121.4747,
        )
        saved = _save_walk(client, journey_id, headers, first, second)
        pocket_revision = saved["pocket_walk"]["revision"]
        started = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/pocket-walk/go",
            headers=headers,
            json={
                "action": "START",
                "expected_revision": 0,
                "pocket_walk_revision": pocket_revision,
                "idempotency_key": str(uuid4()),
            },
        )
        assert started.status_code == 200, started.text

        swapped = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/pocket-walk",
            headers=headers,
            json={
                "first_shortlist_id": saved["items"][1]["shortlist_id"],
                "second_shortlist_id": saved["items"][0]["shortlist_id"],
                "max_walk_minutes": 30,
            },
        )
        assert swapped.status_code == 200, swapped.text
        assert swapped.json()["revision"] == pocket_revision + 1
        assert swapped.json()["go_progress"]["state"] == "NOT_STARTED"
        assert swapped.json()["go_progress"]["revision"] == 0
        with SessionLocal() as session:
            assert session.scalar(
                select(MemberDraftPocketWalkProgress).where(
                    MemberDraftPocketWalkProgress.journey_id == journey_id,
                    MemberDraftPocketWalkProgress.traveler_id == owner["id"],
                )
            ) is None
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_r108_new_receipt_refreshes_same_osm_snapshot_once_and_resets_go() -> None:
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", "r108-go-mode-refresh-secret-012345")
    client = TestClient(main_module.app)
    try:
        owner, _collaborator, journey_id, owner_member_id = _context(client)
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        first_receipt = str(uuid4())
        global _RECEIPT
        _RECEIPT = first_receipt
        first = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=first_receipt,
            osm_id=108021,
        )
        second = _candidate(
            journey_id=journey_id,
            member_id=owner_member_id,
            traveler_id=owner["id"],
            receipt_id=first_receipt,
            osm_id=108022,
            latitude=31.2314,
            longitude=121.4747,
        )
        saved = _save_walk(client, journey_id, headers, first, second)
        shortlist_ids = [item["shortlist_id"] for item in saved["items"]]
        pocket_revision = saved["pocket_walk"]["revision"]
        started = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/pocket-walk/go",
            headers=headers,
            json={
                "action": "START",
                "expected_revision": 0,
                "pocket_walk_revision": pocket_revision,
                "idempotency_key": str(uuid4()),
            },
        )
        assert started.status_code == 200, started.text

        refreshed_receipt = str(uuid4())
        with SessionLocal() as session:
            for candidate_id, label, latitude, longitude in (
                (first, "R108 refreshed first", 31.2309, 121.4742),
                (second, "R108 refreshed second", 31.2319, 121.4752),
            ):
                row = session.get(RealPlaceCandidate, candidate_id)
                assert row is not None
                evidence = json.loads(row.evidence_json or "{}")
                scope = dict(evidence["bootstrap_scopes"][0])
                scope.update(
                    {
                        "receipt_id": refreshed_receipt,
                        "observed_name": label,
                        "latitude": latitude,
                        "longitude": longitude,
                        "captured_at": "2026-10-12T09:00:00+00:00",
                        "source_url": f"https://www.openstreetmap.org/node/{row.external_id.rsplit('/', 1)[-1]}",
                    }
                )
                evidence["bootstrap_scopes"] = [scope]
                row.evidence_json = json.dumps(evidence)
                row.name = label
                row.latitude = latitude
                row.longitude = longitude
                row.captured_at = "2026-10-12T09:00:00+00:00"
            session.commit()

        refreshed = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-discovery/pocket-walk",
            headers=headers,
            json={
                "receipt_id": refreshed_receipt,
                "first_candidate_id": first,
                "second_candidate_id": second,
                "max_walk_minutes": 30,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        body = refreshed.json()
        assert body["pocket_walk"]["revision"] == pocket_revision + 1
        assert body["pocket_walk"]["go_progress"]["state"] == "NOT_STARTED"
        assert body["pocket_walk"]["go_progress"]["revision"] == 0
        assert [item["shortlist_id"] for item in body["items"]] == shortlist_ids
        assert body["items"][0]["name"] == "R108 refreshed first"
        assert body["items"][1]["name"] == "R108 refreshed second"
        with SessionLocal() as session:
            assert session.scalar(
                select(MemberDraftPocketWalkProgress).where(
                    MemberDraftPocketWalkProgress.journey_id == journey_id,
                    MemberDraftPocketWalkProgress.traveler_id == owner["id"],
                )
            ) is None
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_r108_identical_two_stop_save_does_not_erase_go_progress() -> None:
    """A repeated identical confirmed save is not an authorized replan."""
    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", "REDACTED_FOR_PUBLIC_RELEASE")
    client = TestClient(main_module.app)
    try:
        global _RECEIPT
        owner, collaborator, journey_id, member_id = _context(client)
        owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
        _RECEIPT = str(uuid4())
        first = _candidate(
            journey_id=journey_id,
            member_id=member_id,
            traveler_id=owner["id"],
            receipt_id=_RECEIPT,
            osm_id=108041,
        )
        second = _candidate(
            journey_id=journey_id,
            member_id=member_id,
            traveler_id=owner["id"],
            receipt_id=_RECEIPT,
            osm_id=108042,
            latitude=31.2314,
            longitude=121.4747,
        )
        first_save = _save_walk(client, journey_id, owner_headers, first, second)
        revision = first_save["pocket_walk"]["revision"]
        walk_id = first_save["pocket_walk"]["pocket_walk_id"]
        start = client.post(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/pocket-walk/go",
            headers=owner_headers,
            json={
                "action": "START",
                "expected_revision": 0,
                "pocket_walk_revision": revision,
                "idempotency_key": str(uuid4()),
            },
        )
        assert start.status_code == 200, start.text
        assert start.json()["go_progress"]["state"] == "HEADING_TO_FIRST"
        assert start.json()["go_progress"]["revision"] == 1

        repeated = _save_walk(client, journey_id, owner_headers, first, second)
        assert repeated["pocket_walk"]["revision"] == revision
        assert repeated["pocket_walk"]["pocket_walk_id"] == walk_id
        assert repeated["pocket_walk"]["go_progress"]["state"] == "HEADING_TO_FIRST"
        assert repeated["pocket_walk"]["go_progress"]["revision"] == 1
        assert len(repeated["items"]) == 2

        server = client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers=owner_headers,
        )
        assert server.status_code == 200, server.text
        assert server.json()["pocket_walk"]["go_progress"]["state"] == "HEADING_TO_FIRST"
        collaborator_state = client.get(
            f"/api/v1/journeys/{journey_id}/bootstrap-shortlist",
            headers={"Authorization": f"Bearer {collaborator['access_token']}"},
        )
        assert collaborator_state.status_code == 200, collaborator_state.text
        assert collaborator_state.json()["pocket_walk"] is None
        with SessionLocal() as session:
            assert session.scalar(
                select(MemberDraftPocketWalkProgress).where(
                    MemberDraftPocketWalkProgress.journey_id == journey_id,
                    MemberDraftPocketWalkProgress.traveler_id == owner["id"],
                )
            ) is not None
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)


def test_r108_go_status_get_does_not_issue_journey_update() -> None:
    """Refreshing GO status is a read, not a SQLite write-lock acquisition."""
    from sqlalchemy import event
    from app.db.session import engine

    previous_secret = settings.traveler_auth_secret
    object.__setattr__(settings, "traveler_auth_secret", "r108-read-only-go-secret-0123456789")
    client = TestClient(main_module.app)
    try:
        global _RECEIPT
        owner, _collaborator, journey_id, member_id = _context(client)
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        _RECEIPT = str(uuid4())
        first = _candidate(
            journey_id=journey_id,
            member_id=member_id,
            traveler_id=owner["id"],
            receipt_id=_RECEIPT,
            osm_id=108061,
        )
        second = _candidate(
            journey_id=journey_id,
            member_id=member_id,
            traveler_id=owner["id"],
            receipt_id=_RECEIPT,
            osm_id=108062,
            latitude=31.2314,
            longitude=121.4747,
        )
        saved = _save_walk(client, journey_id, headers, first, second)
        assert saved["pocket_walk"]["go_progress"]["state"] == "NOT_STARTED"

        journey_updates: list[str] = []

        def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("UPDATE JOURNEYS "):
                journey_updates.append(statement)

        event.listen(engine, "before_cursor_execute", record_sql)
        try:
            status = client.get(
                f"/api/v1/journeys/{journey_id}/bootstrap-shortlist/pocket-walk/go",
                headers=headers,
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_sql)

        assert status.status_code == 200, status.text
        assert status.json()["go_progress"]["state"] == "NOT_STARTED"
        assert status.json()["go_progress"]["physical_arrival_state"] == "NOT_PHYSICAL_ARRIVAL_PROVEN"
        assert journey_updates == [], "a GO status GET must never acquire a Journey write lock"
    finally:
        object.__setattr__(settings, "traveler_auth_secret", previous_secret)
