"""ORD-0490 -- the link still works after the owner takes the trip back out of the recycle bin.

ORD-0465 put a named refusal on the bin (`SHARE_GRANT_JOURNEY_IN_RECYCLE_BIN`, 409, raised at
`backend/app/main.py`).  That is only honest if the refusal is about a state and not about a
decision: `backend/app/main.py` (DELETE) and `backend/app/main.py` (找回) are a soft delete
and its inverse, so a binned trip is a trip that can come back -- and if it comes back, the same
token has to read again.  A link that stayed dead after its trip had been 找回 would make the
sentence this door's own refusal tells the traveller ("能把它找回来的只有主人",
`frontend/src/i18n/LocaleContext.tsx`) a promise this server does not keep.

What was measured before this file existed, on the authoritative tip
`dcb90cccc68b81f2317b28db19051dedb9f06c80`, and why these tests are mostly round trips rather than
new assertions about the bin:

  * `DELETE /api/v1/journeys/{id}` writes one column (`journeys.deleted_at`) and touches no grant row
    (`backend/app/main.py`), and `POST .../undelete` clears that same column and touches
    nothing else (`backend/app/main.py`).  So the round trip has nothing in its way -- the
    risk is not a refusal that sticks, it is an answer that is quietly *different* after the trip
    comes back.  That is what `test_..._is_the_same_answer_the_trip_gave_before_the_bin` measures,
    body against body.
  * The grant's own state is decided before the bin is (`backend/app/main.py`), so an
    expired link stays expired whichever way the trip moves.  `backend/tests/test_qoder_ord0465_shared_landing.py::test_ord0465_a_dead_grant_is_still_named_dead_when_the_trip_was_also_binned`
    pins that; nothing here re-litigates it.
  * The door holds no cache and `journey_schema` does not emit `deleted_at`
    (`backend/app/schemas.py`), so every reading below is a fresh read of the column.  Two tests
    still walk bin -> restore -> bin, because "it reads the column every time" is a claim about this
    door's behaviour and is only evidence if the same token is asked three times.

One rule from ORD-0465 carries through all of this: the 找回 is done through the owner's door, not by
writing the column here.  A fixture that set `deleted_at = None` itself would be testing the
projection rather than the product.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Activity, Journey, JourneyShareGrant, Place, PlanVersion, Traveler
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

TOKEN_PATH = "/api/v1/shared/journeys/{token}"
UNDELETE_PATH = "/api/v1/journeys/{journey_id}/undelete"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trip_with_plan(*, stops: int = 2) -> str:
    """A trip with a plan of `stops` stops, seeded the way the store holds it.

    It has to have stops: "the link reads again" would pass on a trip that never had anything to
    show, and the interesting failure would be a restored read that answers 200 with an empty day
    list.  The place rows carry coordinates so a stop's position survives the round trip too.
    """
    moment = _now()
    batch = str(uuid4())
    with SessionLocal() as session:
        traveler = Traveler(
            id=str(uuid4()), display_name="ORD-0490 probe", email=None,
            locale="zh-CN", timezone="Asia/Shanghai", created_at=moment, updated_at=moment,
        )
        session.add(traveler)
        session.flush()
        journey = Journey(
            id=str(uuid4()),
            traveler_id=traveler.id,
            title="沿海一周",
            city_code="CN-XIAMEN",
            timezone="Asia/Shanghai",
            start_at="2031-06-01T00:00:00+00:00",
            end_at="2031-06-03T00:00:00+00:00",
            budget_amount_minor=120000,
            budget_currency="CNY",
            status="PLANNED",
            created_at=moment,
            updated_at=moment,
        )
        session.add(journey)
        plan = PlanVersion(
            id=str(uuid4()), journey_id=journey.id, version_no=4, planning_mode="balanced",
            status="ACTIVE", source="REPLAN", total_cost_minor=40000, currency="CNY",
            estimated_duration_min=600, created_at=moment,
        )
        session.add(plan)
        place_ids = []
        for index in range(1, stops + 1):
            place = Place(
                id=str(uuid4()),
                provider_key="ord0490-probe",
                external_id=f"ord0490-{batch}-place-{index}",
                city_code="CN-XIAMEN",
                name=f"鼓浪屿轮渡码头 {index}",
                category="ferry",
                latitude=24.45 + index / 1000,
                longitude=118.07 + index / 1000,
                opening_hours_json="{}", tags_json="{}", metadata_json="{}",
                updated_at=moment,
            )
            session.add(place)
            place_ids.append(place.id)
        # Places before activities: SQLite enforces `activities.place_id` at insert and the unit of
        # work does not order a string-assigned FK for you.
        session.flush()
        for index, place_id in enumerate(place_ids, start=1):
            session.add(Activity(
                id=str(uuid4()), plan_version_id=plan.id, sequence_no=index,
                activity_type="visit", title=f"stop-{index}", place_id=place_id,
                city_service_id=None,
                start_at=f"2031-06-0{index}T01:00:00+00:00",
                end_at=f"2031-06-0{index}T02:00:00+00:00",
                travel_from_prev_min=10, estimated_cost_minor=0, currency="CNY",
                status="PLANNED", locked=False, metadata_json="{}",
                created_at=moment, updated_at=moment,
            ))
        journey.active_plan_version_id = plan.id
        session.commit()
        return journey.id


def _grant(journey_id: str, *, scoped_to_plan: bool = False) -> str:
    with SessionLocal() as session:
        plan_id = session.get(Journey, journey_id).active_plan_version_id
    payload: dict[str, object] = {"visibility": "LINK_READ", "created_by": "ord0490-probe"}
    if scoped_to_plan:
        payload["plan_version_id"] = plan_id
    answer = client.post(f"/api/v1/journeys/{journey_id}/share-grants", json=payload)
    assert answer.status_code == 201, (answer.status_code, answer.text[:300])
    return answer.json()["token"]


def _read(token: str):
    return client.get(TOKEN_PATH.format(token=token))


def _reason_code(answer) -> str | None:
    error = json.loads(answer.text).get("error", {})
    return error.get("reason_code")


def _undelete(journey_id: str):
    return client.post(UNDELETE_PATH.format(journey_id=journey_id))


def _grant_row(token: str) -> dict:
    """Every column this order could have quietly written on the grant while it was reading it."""
    with SessionLocal() as session:
        grant = session.scalars(
            select(JourneyShareGrant).where(JourneyShareGrant.token == token)
        ).first()
        assert grant is not None, token
        return {
            "id": grant.id,
            "journey_id": grant.journey_id,
            "token": grant.token,
            "visibility": grant.visibility,
            "expires_at": grant.expires_at,
            "revoked_at": grant.revoked_at,
            "created_at": grant.created_at,
            "plan_version_id": grant.plan_version_id,
        }


# ------------------------------------------------------------------ the round trip, by name


def test_ord0490_the_same_token_reads_again_after_the_owner_bring_it_back():
    """The order's first sentence, read off the door rather than declared: 409 in the bin, 200 back
    out of it, and it is the *same* token both times -- nothing here mints a second link."""
    journey_id = _trip_with_plan()
    token = _grant(journey_id)

    assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204
    refused = _read(token)
    assert refused.status_code == 409, refused.text[:300]
    assert _reason_code(refused) == "SHARE_GRANT_JOURNEY_IN_RECYCLE_BIN"

    restored = _undelete(journey_id)
    assert restored.status_code == 200, restored.text[:300]
    assert restored.json()["id"] == journey_id

    again = _read(token)
    assert again.status_code == 200, again.text[:300]
    body = again.json()
    assert body["plan_state"] == "PLAN_PRESENT"
    assert [item["place_name"] for item in body["active_plan"]["stops"]] == [
        "鼓浪屿轮渡码头 1", "鼓浪屿轮渡码头 2",
    ]


def test_ord0490_the_restored_read_is_the_read_the_trip_gave_before_the_bin():
    """Body against body, which is the only way this claim can be checked.

    "Still readable" is not the same promise as "readable in the same way": a restored answer that
    came back 200 but with `plan_state: NO_PLAN`, or with the stops' coordinates gone, would satisfy
    the first and break the screen.  The comparison is against a reading taken before the trip was
    binned, so nothing about the bin's own effect is assumed away.
    """
    journey_id = _trip_with_plan()
    token = _grant(journey_id)

    before = _read(token).json()
    assert _read(token).status_code == 200

    client.delete(f"/api/v1/journeys/{journey_id}")
    assert _undelete(journey_id).status_code == 200
    after = _read(token).json()

    assert after == before, {
        "only_in_before": sorted(set(before) - set(after)),
        "only_in_after": sorted(set(after) - set(before)),
    }


def test_ord0490_bin_restore_bin_is_three_answers_and_the_third_is_a_refusal_again():
    """The refusal is a reading of the column, not a verdict this door hands out once.

    Asked in this order it also rules out the two shapes a screen could be lied to by: an answer that
    sticks on 409 after the 找回 (the link is dead for ever), and an answer that sticks on 200 after
    the trip goes back in (a traveller keeps reading a trip its owner has just thrown away again).
    """
    journey_id = _trip_with_plan()
    token = _grant(journey_id)

    client.delete(f"/api/v1/journeys/{journey_id}")
    assert _read(token).status_code == 409
    assert _undelete(journey_id).status_code == 200
    assert _read(token).status_code == 200
    assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204

    back_in = _read(token)
    assert back_in.status_code == 409, back_in.text[:300]
    assert _reason_code(back_in) == "SHARE_GRANT_JOURNEY_IN_RECYCLE_BIN"


def test_ord0490_the_round_trip_writes_nothing_to_the_grant():
    """The link is not re-minted, re-issued, re-scoped or silently revoked on the way through.

    This is the half the traveller cannot see: a restored read that answered 200 while the grant row
    had moved (`revoked_at` set, `plan_version_id` cleared) would mean the next read refuses, and
    nobody would be able to say which of the two writes did it.  `token` is included for the reason
    ORD-0465 records at `backend/app/main.py` -- the column is matched with BINARY collation, so
    it is the one thing that must not drift.
    """
    journey_id = _trip_with_plan()
    token = _grant(journey_id)
    before = _grant_row(token)

    client.delete(f"/api/v1/journeys/{journey_id}")
    _read(token)
    _undelete(journey_id)
    _read(token)

    assert _grant_row(token) == before
    with SessionLocal() as session:
        grants = session.scalars(
            select(JourneyShareGrant).where(JourneyShareGrant.journey_id == journey_id)
        ).all()
        assert len(grants) == 1, [grant.id for grant in grants]


def test_ord0490_a_restore_that_was_not_needed_leaves_the_link_exactly_where_it_was():
    """`JOURNEY_NOT_DELETED` (409) is a refusal that changes nothing, and it must not cost a link.

    The owner's screen can tap 找回 twice; the second tap refuses (that is ORD-0399's own rule,
    `backend/app/main.py`).  What this order needs from that door is the fact that a
    refused tap left the trip readable -- otherwise a double tap would look like a way to break a
    share link.
    """
    journey_id = _trip_with_plan()
    token = _grant(journey_id)
    before = _read(token).json()

    refused = _undelete(journey_id)
    assert refused.status_code == 409, refused.text[:300]
    assert _reason_code(refused) == "JOURNEY_NOT_DELETED"

    after = _read(token)
    assert after.status_code == 200
    assert after.json() == before


def test_ord0490_a_version_scoped_link_still_reads_its_version_after_the_trip_comes_back():
    """ORD-0466's scope survives the bin in both directions.

    A grant cut for one plan version reads that version (`backend/app/main.py`).  Nothing about
    the bin touches `plan_version_id`, so the scoped answer must be the same one before, and after --
    and the refusal in between must not have quietly downgraded the link to a journey-level read.
    """
    journey_id = _trip_with_plan()
    with SessionLocal() as session:
        plan_id = session.get(Journey, journey_id).active_plan_version_id
    token = _grant(journey_id, scoped_to_plan=True)

    before = _read(token).json()
    assert before["grant_scope"] == "PLAN_VERSION"
    assert before["shared_plan"]["id"] == plan_id

    client.delete(f"/api/v1/journeys/{journey_id}")
    assert _reason_code(_read(token)) == "SHARE_GRANT_JOURNEY_IN_RECYCLE_BIN"
    assert _undelete(journey_id).status_code == 200

    after = _read(token)
    assert after.status_code == 200, after.text[:300]
    assert after.json() == before


def test_ord0490_the_refusal_names_the_bin_moment_and_the_restored_read_carries_no_stale_one():
    """What each of the two answers is allowed to say about the bin.

    The refusal carries `deleted_at` in `error.details` (`backend/app/main.py`) because a person
    asking "when did this go in" is entitled to the stored answer.  The restored read must carry no
    such value anywhere: `Journey` has no `deleted_at` field (`backend/app/schemas.py`), and a
    stale moment on a body that says the trip is back would be a second, contradicting clock.
    """
    journey_id = _trip_with_plan()
    token = _grant(journey_id)
    client.delete(f"/api/v1/journeys/{journey_id}")

    refused = json.loads(_read(token).text)
    details = refused["error"]["details"]
    assert details["deleted_at"], details
    assert _undelete(journey_id).status_code == 200

    body = _read(token)
    assert body.status_code == 200
    assert "deleted_at" not in body.text, body.text[:400]


def test_ord0490_the_word_for_bringing_it_back_is_the_one_the_recycle_bin_uses():
    """The screen's sentence and the door's behaviour have to be about the same action.

    `shared.refusedInRecycleBin` tells a traveller that only the owner can 找回 the trip, from the
    owner's own recycle bin (`frontend/src/i18n/LocaleContext.tsx`).  That sentence is only true
    while the door that undoes the bin is the owner's `POST /journeys/{id}/undelete` and while nothing
    on the share side can bring a trip back.  Both halves are measured: the owner's door is the one
    that restores this trip, and the token read itself is a GET that cannot (see
    `backend/tests/test_qoder_ord0465_shared_landing.py::test_ord0465_the_read_writes_nothing`).
    """
    journey_id = _trip_with_plan()
    token = _grant(journey_id)
    client.delete(f"/api/v1/journeys/{journey_id}")

    # The share door is read-only: an attempt to use it as the way back changes nothing.
    for method in ("post", "put", "patch", "delete"):
        answer = getattr(client, method)(TOKEN_PATH.format(token=token))
        assert answer.status_code == 405, (method, answer.status_code, answer.text[:200])
    assert _read(token).status_code == 409

    assert _undelete(journey_id).status_code == 200
    assert _read(token).status_code == 200


def test_ord0490_a_link_revoked_while_the_trip_was_in_the_bin_stays_revoked_when_it_comes_back():
    """Two refusals, and the older one still wins after the trip returns.

    The grant's state is decided before the bin's (`backend/app/main.py`), so a withdrawal made
    while the trip sat in the bin does not become unreadable-then-readable: it is `SHARE_GRANT_REVOKED`
    on both sides of the 找回.  The risk this pins is an implementation that reports the bin, restores
    the trip, and finds itself answering 200 to a link its owner revoked in the meantime.
    """
    journey_id = _trip_with_plan()
    token = _grant(journey_id)
    with SessionLocal() as session:
        grant_id = session.scalars(
            select(JourneyShareGrant).where(JourneyShareGrant.token == token)
        ).first().id

    client.delete(f"/api/v1/journeys/{journey_id}")
    revoked = client.post(
        f"/api/v1/journeys/{journey_id}/share-grants/{grant_id}/revoke")
    assert revoked.status_code == 200, revoked.text[:300]

    assert _reason_code(_read(token)) == "SHARE_GRANT_REVOKED"
    assert _undelete(journey_id).status_code == 200
    still_dead = _read(token)
    assert still_dead.status_code == 409, still_dead.text[:300]
    assert _reason_code(still_dead) == "SHARE_GRANT_REVOKED"


def test_ord0490_the_restored_answer_is_still_the_guest_shape_and_not_the_owner_one():
    """Restoring a trip must not turn a share link into the owner's own read.

    The absence rules are ORD-0465's (`backend/app/schemas.py` and
    `test_ord0465_success_says_the_owner_flags_and_position_are_not_on_this_answer`); they belong to
    the answer, not to the bin.  A round trip that re-computed the body through a different path --
    the owner's projection, which carries a live position and the lock flags -- would keep this
    screen working and leak the traveller's location out of it.
    """
    journey_id = _trip_with_plan()
    token = _grant(journey_id)
    client.delete(f"/api/v1/journeys/{journey_id}")
    assert _undelete(journey_id).status_code == 200

    body = _read(token).json()
    plan = body["active_plan"]
    assert "user_location" not in plan
    for stop in plan["stops"]:
        assert set(stop) == {
            "id", "sequence_no", "title", "place_name", "activity_type",
            "start_at", "end_at", "coordinate",
        }, sorted(stop)
        assert "locked" not in stop and "metadata" not in stop


def test_ord0490_a_trip_binned_and_restored_keeps_the_day_it_was_planned_into():
    """The dates are the fragile part of a restored read.

    A stop's day on this screen is derived from its `start_at` in the trip's own timezone
    (`frontend/src/features/shared/SharedJourneyPanel.tsx`), so a restored answer whose instants
    came back in a different shape would put the same stop on a different day -- and the day heading
    would be the only thing on the screen that said so.  Checked against the pre-bin reading rather
    than against a literal, so the assertion is about the round trip and not about a format.
    """
    journey_id = _trip_with_plan(stops=2)
    token = _grant(journey_id)
    before = _read(token).json()
    client.delete(f"/api/v1/journeys/{journey_id}")
    assert _undelete(journey_id).status_code == 200
    after = _read(token).json()

    assert before["timezone"] == after["timezone"] == "Asia/Shanghai"
    assert [item["start_at"] for item in after["active_plan"]["stops"]] == [
        item["start_at"] for item in before["active_plan"]["stops"]
    ]
    assert [item["coordinate"] for item in after["active_plan"]["stops"]] == [
        item["coordinate"] for item in before["active_plan"]["stops"]
    ]
