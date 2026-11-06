"""ORD-0460 - an expiry already in the past is refused before any token exists.

Reproduced on the authoritative tip `4ee14229f80691cf8e5ea913375c0caf83ac02ee` with
`review/ord0460/repro_before_r2.json`: a POST carrying an `expires_at` one day in the past answered
`http=201` with a 32-character token, and reading that same token answered `409
SHARE_GRANT_EXPIRED` -- the grant was dead at birth.  The instant exactly equal to "now" behaved the
same way, because the read door has always treated `expires <= now` as spent.  The same door already
refused an expiry with no offset (`SHARE_GRANT_EXPIRY_NO_OFFSET`), so the validation was not missing
in principle; one case was missing.

The fix keeps the comparison in one function (`_expiry_verdict`) that both doors read, so the two
cannot drift into holding different definitions of "already over".

Measured vocabulary: `SHARE_GRANT_` is the prefix this slice already uses for every grant refusal, and
the new name is checked against the tree below before it was written down.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db.models import JourneyShareGrant, Traveler
from app.db.session import SessionLocal
from app.main import app, _expiry_verdict

client = TestClient(app, raise_server_exceptions=False)

GRANT_PATH = "/api/v1/journeys/{journey_id}/share-grants"
TOKEN_PATH = "/api/v1/shared/journeys/{token}"


def _journey() -> str:
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        traveler = Traveler(
            id=str(uuid4()),
            display_name="ORD-0460 probe",
            email=None,
            locale="zh-CN",
            timezone="Asia/Shanghai",
            created_at=now,
            updated_at=now,
        )
        session.add(traveler)
        session.commit()
        traveler_id = traveler.id
    answer = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler_id,
            "title": "ORD-0460 probe trip",
            "city_code": "CN-TIANJIN",
            "timezone": "Asia/Shanghai",
            "start_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
            "end_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
            "budget": {"amount_minor": 120000, "currency": "CNY"},
        },
    )
    assert answer.status_code == 201, (answer.status_code, answer.text[:200])
    return answer.json()["id"]


def _create(journey_id: str, expires_at: str | None):
    payload = {"visibility": "LINK_READ", "created_by": "ord0460-probe"}
    if expires_at is not None:
        payload["expires_at"] = expires_at
    return client.post(GRANT_PATH.format(journey_id=journey_id), json=payload)


def _reason(answer) -> str | None:
    try:
        return (answer.json().get("error") or {}).get("reason_code")
    except ValueError:
        return None


def _stored_rows(journey_id: str) -> int:
    with SessionLocal() as session:
        return len(
            session.query(JourneyShareGrant)
            .filter(JourneyShareGrant.journey_id == journey_id)
            .all()
        )


def test_an_expiry_in_the_past_is_refused_and_no_token_is_minted() -> None:
    journey_id = _journey()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    answer = _create(journey_id, past)
    assert answer.status_code == 422, (answer.status_code, answer.text[:200])
    assert _reason(answer) == "SHARE_GRANT_EXPIRY_IN_PAST", answer.text[:200]
    # "No token" is the half of the requirement that a status code alone cannot carry.
    assert "token" not in (answer.json() if isinstance(answer.json(), dict) else {}), answer.text[:200]
    assert _stored_rows(journey_id) == 0, "a refused grant must not leave a row behind"


def test_the_instant_exactly_equal_to_now_is_refused_too() -> None:
    """The boundary is written down because both doors have to agree on it.

    The read door treats `expires <= now` as spent, so accepting equality at write time is exactly the
    born-dead token this order is about.  Refusing it is not a stricter taste: it is the same verdict.
    """
    journey_id = _journey()
    now = datetime.now(timezone.utc)
    answer = _create(journey_id, now.isoformat())
    assert answer.status_code == 422, (answer.status_code, answer.text[:200])
    assert _reason(answer) == "SHARE_GRANT_EXPIRY_IN_PAST", answer.text[:200]
    assert _stored_rows(journey_id) == 0
    assert _expiry_verdict(now.isoformat(), now) == "PAST", "the boundary belongs to PAST, not FUTURE"


def test_a_future_expiry_still_answers_201_and_the_token_reads() -> None:
    journey_id = _journey()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    answer = _create(journey_id, future)
    assert answer.status_code == 201, (answer.status_code, answer.text[:200])
    token = answer.json()["token"]
    assert len(token) >= 16, "a grant that works must still carry a usable token"
    read = client.get(TOKEN_PATH.format(token=token))
    assert read.status_code == 200, (read.status_code, read.text[:200])
    assert read.json()["id"] == journey_id


def test_the_two_doors_share_one_expiry_comparison() -> None:
    """Drift is what this order could not prevent before: the write had its own arithmetic.

    Both verdicts come from `_expiry_verdict`, so the write cannot accept what the read would reject.
    A no-offset instant, which the write already refused, still reads `UNREADABLE` rather than
    silently comparing against the server's own zone.
    """
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    assert _expiry_verdict(None, now) == "NO_EXPIRY"
    assert _expiry_verdict("2026-09-29T12:00:01+00:00", now) == "FUTURE"
    assert _expiry_verdict("2026-09-29T11:59:59+00:00", now) == "PAST"
    assert _expiry_verdict("2026-09-29T12:00:00", now) == "UNREADABLE"
    assert _expiry_verdict("not-an-instant", now) == "UNREADABLE"


def test_the_no_offset_refusal_is_unchanged() -> None:
    """The check this order says was already right keeps answering with its own name."""
    journey_id = _journey()
    answer = _create(journey_id, "2030-01-01T00:00:00")
    assert answer.status_code == 422, (answer.status_code, answer.text[:200])
    assert _reason(answer) == "SHARE_GRANT_EXPIRY_NO_OFFSET", answer.text[:200]
    assert _stored_rows(journey_id) == 0


def test_a_grant_without_an_expiry_is_still_accepted_and_reads() -> None:
    journey_id = _journey()
    answer = _create(journey_id, None)
    assert answer.status_code == 201, (answer.status_code, answer.text[:200])
    assert answer.json()["expires_at"] is None, "no deadline stays null, it is not a date far away"
    read = client.get(TOKEN_PATH.format(token=answer.json()["token"]))
    assert read.status_code == 200, (read.status_code, read.text[:200])


def test_the_new_name_is_not_already_used_for_something_else() -> None:
    """The naming rule this lane works under: a code is added, never overloaded."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    text = (root / "backend/app/main.py").read_text(encoding="utf-8")
    assert text.count('"SHARE_GRANT_EXPIRY_IN_PAST"') == 1, "one emitter, at the write door"
    # The rule is "a code is added, never overloaded", so what is pinned is the exact literal and how
    # many emitters it has -- not the suffix.  ORD-0461 r3 added `MEMBER_EXPIRY_IN_PAST`, a different
    # name with its own door, and this arm would otherwise call a second door a reuse of the first.
    assert text.count('"SHARE_GRANT_EXPIRY_IN_PAST"') == 1, "one grant emitter, at the grant write door"
    assert text.count('"MEMBER_EXPIRY_IN_PAST"') == 1, "one member emitter, at the member write door"
    assert '"EXPIRY_IN_PAST"' not in text, "a bare EXPIRY_IN_PAST would be one name for two doors"
