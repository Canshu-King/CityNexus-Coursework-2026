"""ORD-0541 (lane 82f55d8a) - 重复发起同一枚分享要返回同一枚结果.

Measured at the authoritative tip `0c061d9eb74b21f0b0b297a068778479b08658b4`, before any change of mine:
`create_share_grant` (`backend/app/main.py`) reached
`grant = JourneyShareGrant(id=str(uuid4()), token=secrets.token_urlsafe(24), ...)` and committed
(`backend/app/main.py`) on every call - it never looked at the rows it already had. So pressing
「分享这张图」twice grew a second row, and `GET /api/v1/journeys/{journey_id}/share-grants`
(`backend/app/main.py`) answered two tokens for one intent. The ruling that decides which rows may
be reused is the commander's: only a row whose `visibility` is exactly `LINK_READ` counts, so the other
three values of `ShareVisibility` (`backend/app/schemas.py`) keep minting a new link every time.

Arms, each naming its own lever:
* A1 首次/重复 两向读数 - the same request twice returns the same token and the list door still
  answers one row;
* A2 「恰为」的另一向 - the same request with `PUBLIC` twice answers two rows and two tokens, because a
  row that is not `LINK_READ` is not reusable, ruling or no ruling;
* A3 撤销不是隐身 - after the row is revoked through the product's own revoke door, the next request
  must mint, and the withdrawn row stays in the list;
* A4 过期行不许复用 - one row with a past `expires_at` is placed by a direct write, and the next
  request must not hand it back: reusing it would be the born-dead link ORD-0460 refuses to mint;
* A5 「同一枚分享」里那个「同一」- a different `created_by` asking for a link is a different request,
  so it mints its own row. This key is my reading, not the ruling's: the ruling only fixed which
  visibility is eligible.

The anonymous read path (`GET /api/v1/journeys/{journey_id}`,
`GET /api/v1/shared/journeys/{token}`) and the list door are not touched by this order; A1-A5 read the
list door as it already answers, and the byte-for-byte guard of that anonymous read stays where it was
shipped - `backend/tests/test_qoder_ord0434_share_grants.py`, which compares it against
`review/ord0434/ANON_READ_BASELINE_r1.json`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db.models import JourneyShareGrant, Traveler
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
TZ = timezone(timedelta(hours=8))
START = datetime(2026, 11, 3, 9, 0, tzinfo=TZ)

GRANT_PATH = "/api/v1/journeys/{journey_id}/share-grants"
REVOKE_PATH = "/api/v1/journeys/{journey_id}/share-grants/{grant_id}/revoke"


def _traveler() -> str:
    now = datetime.now(timezone.utc).isoformat()
    traveler_id = str(uuid4())
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="ORD-0541 probe",
                email=None,
                locale="zh-CN",
                timezone="Asia/Shanghai",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return traveler_id


def _journey() -> str:
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": _traveler(),
            "title": "ORD-0541",
            "city_code": "CN-XIAMEN",
            "timezone": "Asia/Shanghai",
            "start_at": START.isoformat(),
            "end_at": (START + timedelta(days=2, hours=8)).isoformat(),
            "budget": {"amount_minor": 90000, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _create(journey_id: str, visibility: str = "LINK_READ", created_by: str = "lane-0541") -> dict:
    response = client.post(
        GRANT_PATH.format(journey_id=journey_id),
        json={"visibility": visibility, "created_by": created_by, "expires_at": None},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _listed(journey_id: str) -> list[dict]:
    response = client.get(GRANT_PATH.format(journey_id=journey_id))
    assert response.status_code == 200, response.text
    return response.json()


def test_a1_the_same_link_request_twice_returns_the_same_row_and_grows_no_row():
    journey_id = _journey()
    first = _create(journey_id)
    rows_after_first = _listed(journey_id)
    second = _create(journey_id)
    rows_after_second = _listed(journey_id)

    assert first["visibility"] == "LINK_READ", first
    assert [r["id"] for r in rows_after_first] == [first["id"]], rows_after_first
    assert second["id"] == first["id"], (first["id"], second["id"])
    assert second["token"] == first["token"], "重复发起换了 token，等于对同一次意图给了两枚链接"
    assert [r["id"] for r in rows_after_second] == [first["id"]], rows_after_second


def test_a2_only_link_read_reuses_a_row_public_still_mints_one_per_request():
    journey_id = _journey()
    first = _create(journey_id, visibility="PUBLIC")
    second = _create(journey_id, visibility="PUBLIC")
    rows = _listed(journey_id)

    assert first["id"] != second["id"], (first["id"], second["id"])
    assert first["token"] != second["token"], rows
    assert len(rows) == 2, [r["id"] for r in rows]


def test_a3_a_revoked_row_is_not_reused_and_stays_visible_in_the_list():
    journey_id = _journey()
    grant = _create(journey_id)
    revoked = client.post(REVOKE_PATH.format(journey_id=journey_id, grant_id=grant["id"]), json={})
    assert revoked.status_code == 200, revoked.text

    again = _create(journey_id)
    rows = _listed(journey_id)

    assert again["id"] != grant["id"], (grant["id"], again["id"])
    assert {r["id"] for r in rows} == {grant["id"], again["id"]}, rows
    assert len(rows) == 2, rows


def test_a4_a_row_whose_expiry_is_past_is_not_handed_back_as_the_reusable_one():
    journey_id = _journey()
    now = datetime.now(timezone.utc)
    stale_id = str(uuid4())
    with SessionLocal() as session:
        session.add(
            JourneyShareGrant(
                id=stale_id,
                journey_id=journey_id,
                token="ord0541-stale-token",
                visibility="LINK_READ",
                plan_version_id=None,
                created_by="lane-0541",
                expires_at=(now - timedelta(minutes=5)).isoformat(),
                revoked_at=None,
                created_at=(now - timedelta(days=1)).isoformat(),
                updated_at=(now - timedelta(days=1)).isoformat(),
            )
        )
        session.commit()

    fresh = _create(journey_id)
    rows = _listed(journey_id)

    assert fresh["id"] != stale_id, fresh
    assert {r["id"] for r in rows} >= {stale_id, fresh["id"]}, rows


def test_a5_a_different_requester_gets_its_own_row_not_somebody_elses_link():
    journey_id = _journey()
    mine = _create(journey_id, created_by="device-a")
    theirs = _create(journey_id, created_by="device-b")
    rows = _listed(journey_id)

    assert theirs["id"] != mine["id"], (mine["id"], theirs["id"])
    assert len(rows) == 2, [r["id"] for r in rows]
