"""ORD-0461 r3 - the member door refuses a deadline it would already read as spent.

Scope note, measured before writing: at `d7c08d1293835adc7b2628f7100263066d610322` the two member write
doors this lane once proposed are already on the line, shipped and tested by ORD-0503
(`backend/tests/test_qoder_ord0503_member_doors.py`), together with the administrator cap as a named
constant (`MAX_ADMINISTRATORS_PER_JOURNEY`) and its contract key.  This file therefore adds none of
that.  It carries the one half that is still missing on this tip: `MEMBER_EXPIRY_IN_PAST` did not exist
in `backend/app/main.py` or in `contracts/openapi.yaml` at that commit (each read back 0 occurrences),
so `POST /api/v1/journeys/{journey_id}/members` still stored a membership whose own deadline the server
would immediately report as spent.

Which arm holds which promise:

1. `the past deadline is refused by name and no row is written` - the answer is read from the body's
   `error.reason_code`, not from the status, and the set is proved empty by the list door and by the
   store itself.
2. `the instant equal to now is refused by the same name` - the boundary the order asked to be written
   out.  `PAST` includes equality because that is what the read path has always meant, so a row created
   here would be born `MEMBER_LAPSED`.
3. `a deadline still ahead is accepted` - the facing control on the same request shape: 201, one row,
   `MEMBER_IN_FORCE`.  Arms 1-3 together are what stop "the door simply rejects everything".
4. `the door asks the one comparison instead of holding its own` - reads the function's own source
   slice and requires the call to `_expiry_verdict`, and forbids a second `<= now` written here.  This
   is the arm that goes red if somebody bolts a duplicate clock check onto the door, which is exactly
   how the grant door and the member door would have drifted apart.
5. `the contract and the generated client both carry the name` - the 422 line of the members door in
   `contracts/openapi.yaml` and the single generated description line in
   `frontend/src/generated/api.ts`.  The client line was replayed from a contract-only delta, so this
   arm is also the proof that the replay landed rather than a wholesale regeneration.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Journey, JourneyMember, Traveler
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"
GENERATED = ROOT / "frontend" / "src" / "generated" / "api.ts"
MEMBER_PATH = "/api/v1/journeys/{journey}/members"
MOMENT = datetime.now(timezone.utc).isoformat()
CODE = "MEMBER_EXPIRY_IN_PAST"


def _reason(body: object) -> str:
    error = (body or {}).get("error") if isinstance(body, dict) else None
    return str(error.get("reason_code")) if isinstance(error, dict) else "NO_ERROR_ENVELOPE"


def _traveller(label: str) -> str:
    identifier = str(uuid4())
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=identifier, display_name=f"ORD-0461 r3 探针 {label}", email=None, locale="zh-CN",
                timezone="Asia/Shanghai", created_at=MOMENT, updated_at=MOMENT,
            )
        )
        session.commit()
    return identifier


def _journey(traveler_id: str) -> str:
    identifier = str(uuid4())
    with SessionLocal() as session:
        session.add(
            Journey(
                id=identifier, traveler_id=traveler_id, title="沿海一周 ORD-0461 r3",
                city_code="CN-XIAMEN", timezone="Asia/Shanghai",
                start_at="2031-05-01T00:00:00+00:00", end_at="2031-05-03T00:00:00+00:00",
                budget_amount_minor=120000, budget_currency="CNY", status="PLANNED",
                created_at=MOMENT, updated_at=MOMENT,
            )
        )
        session.commit()
    return identifier


def _add(journey_id: str, traveler_id: str, expires_at: str | None):
    return client.post(
        MEMBER_PATH.format(journey=journey_id),
        json={"traveler_id": traveler_id, "role": "COLLABORATOR", "invited_by": "lane-rel",
              "expires_at": expires_at},
    )


def _rows(journey_id: str) -> list[dict]:
    answer = client.get(MEMBER_PATH.format(journey=journey_id))
    assert answer.status_code == 200, answer.text
    return list(answer.json())


def _stored_rows(journey_id: str) -> int:
    with SessionLocal() as session:
        return len(session.scalars(
            select(JourneyMember).where(JourneyMember.journey_id == journey_id)).all())


def test_a_deadline_already_in_the_past_is_refused_by_name_and_no_row_is_written() -> None:
    journey_id = _journey(_traveller("已过期"))
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    refused = _add(journey_id, _traveller("已被拒"), past)
    assert refused.status_code == 422, refused.text
    assert _reason(refused.json()) == CODE, refused.text
    details = ((refused.json().get("error") or {}).get("details")) or {}
    assert details.get("field") == "expires_at", refused.text
    assert _rows(journey_id) == [], "a refused invitation must leave the set empty"
    assert _stored_rows(journey_id) == 0, "and must leave nothing in the store either"


def test_the_instant_equal_to_now_is_refused_by_the_same_name() -> None:
    """The boundary the order asked to be written out: equality with `now` is spent, not usable."""
    journey_id = _journey(_traveller("边界"))
    boundary = datetime.now(timezone.utc).isoformat()
    refused = _add(journey_id, _traveller("边界探针"), boundary)
    assert _reason(refused.json()) == CODE, refused.text
    assert refused.status_code == 422, refused.text
    assert _stored_rows(journey_id) == 0, refused.text


def test_a_deadline_still_ahead_is_accepted_and_reads_as_in_force() -> None:
    journey_id = _journey(_traveller("未来"))
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    created = _add(journey_id, _traveller("受邀者"), future)
    assert created.status_code == 201, created.text
    assert created.json()["member_state"] == "MEMBER_IN_FORCE", created.text
    rows = _rows(journey_id)
    assert len(rows) == 1 and rows[0]["expires_at"] == created.json()["expires_at"], rows


def test_the_door_asks_the_one_comparison_rather_than_holding_its_own() -> None:
    """A second definition of "already over" is how the grant door and the member door drift apart."""
    text = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    start = text.index("def _member_expiry(")
    end = text.index("@app.post", start)
    body = text[start:end]
    assert "_expiry_verdict(" in body, "the member door no longer asks the shared verdict: %s" % body[-300:]
    assert re.search(r"expires\s*<=\s*now", body) is None, (
        "a second clock comparison was written into the door itself")
    assert CODE in body, "the refusal name disappeared from the door"


def test_the_contract_and_the_generated_client_both_carry_the_name() -> None:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    members = document["paths"]["/journeys/{journey_id}/members"]["post"]
    descriptions = [str((response or {}).get("description", ""))
                    for response in (members.get("responses") or {}).values()]
    joined = " ".join(descriptions)
    assert CODE in joined, "the 422 of the members door does not advertise %s" % CODE
    generated = GENERATED.read_text(encoding="utf-8")
    assert CODE in generated, (
        "the checked-in client is behind the contract -- the per-order replay did not land")
    assert generated.count(CODE) == joined.count(CODE), (
        "the client carries a different number of mentions than the contract line it was replayed from")
