"""ORD-0465 -- the door a share link is opened at answers four named states, and one honest success.

What was measured on the authoritative tip `9110db281ca7706bb78e8e1f2dbad04a08705d88` before this file
existed (all of it read off `backend/app/main.py` at that commit, not inferred):

  * `read_shared_journey` returned `journey_schema(journey)` (`backend/app/main.py` at that tip).
    A `Journey` carries id, title, city, timezone, dates, budget and status -- and no stops.  So the
    landing screen this door feeds could name a trip but could not name a single day of it.
  * Nothing in this door read `journeys.deleted_at`.  `journey_schema` does not emit that column
    (`backend/app/schemas.py`, confirmed by ORD-0402 slice 2), so a trip its owner had put in the
    recycle bin answered `200` with its full body.  That is the fourth state being absent, not being
    mis-named: there was no word for it because there was no reading of it.
  * The three grant states the order asks about were already named and are pinned here rather than
    assumed: `SHARE_GRANT_UNKNOWN` (404), `SHARE_GRANT_REVOKED` (409), `SHARE_GRANT_EXPIRED` (409),
    all carried in `error.reason_code` inside the one ORD-0055 envelope.

The rule these tests hold the door to: a rejection reaches the screen as a code the server produced.
A screen that guessed "expired" from a timestamp it could read itself would be right until the day the
server changed its mind, and there is no way to tell from the screen which happened.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.anywhere_capability import CAPABILITY_STATES
from app.db.models import Activity, Journey, JourneyShareGrant, Place, PlanVersion, Traveler
from app.db.session import SessionLocal
from app.main import app, _shared_plan_projection
from app.schemas import SharedActivityCoordinate, SharedPlan, SharedStop
from app.stay_mobility import activity_coordinate

client = TestClient(app, raise_server_exceptions=False)

TOKEN_PATH = "/api/v1/shared/journeys/{token}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _journey_with_plan(*, stops: int = 2) -> tuple[str, str]:
    """A trip, a plan of `stops` stops, and the place rows those stops name.

    The places are built with coordinates on purpose: an activity whose place row is missing answers
    `UNAVAILABLE`/`COVERAGE_INCOMPLETE`, which is a different claim from the one this fixture is for.
    Returns `(journey_id, first_place_id)`.
    """
    moment = _now()
    # `places` carries UNIQUE(provider_key, external_id) (`app/db/models.py`), and every test in
    # this module seeds its own trip into the same run's database -- so the place rows have to be
    # named apart from each other or the second test dies on that constraint instead of testing
    # anything.
    batch = str(uuid4())
    with SessionLocal() as session:
        traveler = Traveler(
            id=str(uuid4()),
            display_name="ORD-0465 probe",
            email=None,
            locale="zh-CN",
            timezone="Asia/Shanghai",
            created_at=moment,
            updated_at=moment,
        )
        session.add(traveler)
        session.flush()
        journey = Journey(
            id=str(uuid4()),
            traveler_id=traveler.id,
            title="沿海一周",
            city_code="CN-XIAMEN",
            timezone="Asia/Shanghai",
            start_at="2030-05-01T00:00:00+00:00",
            end_at="2030-05-03T00:00:00+00:00",
            budget_amount_minor=120000,
            budget_currency="CNY",
            status="PLANNED",
            created_at=moment,
            updated_at=moment,
        )
        session.add(journey)
        plan = PlanVersion(
            id=str(uuid4()),
            journey_id=journey.id,
            version_no=3,
            planning_mode="balanced",
            status="ACTIVE",
            source="INITIAL",
            total_cost_minor=40000,
            currency="CNY",
            estimated_duration_min=600,
            created_at=moment,
        )
        session.add(plan)
        place_ids = []
        for index in range(1, stops + 1):
            place = Place(
                id=str(uuid4()),
                provider_key="ord0465-probe",
                external_id=f"ord0465-{batch}-place-{index}",
                city_code="CN-XIAMEN",
                name=f"鼓浪屿轮渡码头 {index}",
                category="ferry",
                latitude=24.45 + index / 1000,
                longitude=118.07 + index / 1000,
                opening_hours_json="{}",
                tags_json="{}",
                metadata_json="{}",
                updated_at=moment,
            )
            session.add(place)
            place_ids.append(place.id)
        # The places have to be on the row-side of the transaction before anything names them:
        # SQLite enforces `activities.place_id` at insert, and the unit of work does not order a
        # string-assigned FK for you (`tests/test_snapshot_j_backend_truth_gaps.py` flushes for
        # the same reason).
        session.flush()
        for index, place_id in enumerate(place_ids, start=1):
            session.add(
                Activity(
                    id=str(uuid4()),
                    plan_version_id=plan.id,
                    sequence_no=index,
                    activity_type="visit",
                    # A planner-typed title, deliberately NOT the place's own name: the screen's rule
                    # is the place name first, so the two must be distinguishable in the payload.
                    title=f"stop-{index}",
                    place_id=place_id,
                    city_service_id=None,
                    start_at=f"2030-05-0{index}T01:00:00+00:00",
                    end_at=f"2030-05-0{index}T02:00:00+00:00",
                    travel_from_prev_min=10,
                    estimated_cost_minor=0,
                    currency="CNY",
                    status="PLANNED",
                    locked=index == 2,
                    metadata_json="{}",
                    created_at=moment,
                    updated_at=moment,
                )
            )
        journey.active_plan_version_id = plan.id
        session.commit()
        return journey.id, place_ids[0]


def _grant(journey_id: str, *, expires_at: str | None = None) -> str:
    payload: dict[str, object] = {"visibility": "LINK_READ", "created_by": "ord0465-probe"}
    if expires_at is not None:
        payload["expires_at"] = expires_at
    answer = client.post(
        f"/api/v1/journeys/{journey_id}/share-grants", json=payload
    )
    assert answer.status_code == 201, (answer.status_code, answer.text[:300])
    return answer.json()["token"]


def _grant_that_has_aged(journey_id: str) -> str:
    """A grant minted alive and then overtaken by its own deadline.

    It cannot be minted dead: ORD-0460 made the write door refuse an `expires_at` already in the past
    (`SHARE_GRANT_EXPIRY_IN_PAST`, measured `422` at this door when this file was written).  So the
    fixture mints a future deadline and moves it back afterwards, which is the only shape the state
    exists in on a real line: the clock passed it, nobody wrote a spent token.
    """
    deadline = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    token = _grant(journey_id, expires_at=deadline)
    with SessionLocal() as session:
        grant = session.scalars(
            select(JourneyShareGrant).where(JourneyShareGrant.token == token)
        ).first()
        grant.expires_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        session.commit()
    return token


def _reason_code(answer) -> str | None:
    """The one channel this screen is allowed to name a refusal by: the body's `error.reason_code`."""
    body = json.loads(answer.text)
    error = body.get("error", body)
    return error.get("reason_code")


# ------------------------------------------------------------------ the four named refusals


def test_ord0465_unknown_token_is_named_by_the_server():
    """A token no grant carries is `SHARE_GRANT_UNKNOWN`, and it is a 404 -- not the 409 the states
    below answer with, because "never existed" and "stopped working" are two facts."""
    answer = client.get(TOKEN_PATH.format(token="nobody-minted-this-one"))
    assert answer.status_code == 404, answer.text[:300]
    assert _reason_code(answer) == "SHARE_GRANT_UNKNOWN"


def test_ord0465_revoked_grant_is_named_and_carries_the_withdrawal_instant():
    """Two sentences, because two things happened to the link: somebody took it back, or its date
    passed. One code for both would let the screen say "the owner withdrew this" about a link nobody
    withdrew."""
    journey_id, _ = _journey_with_plan()
    token = _grant(journey_id)
    with SessionLocal() as session:
        grant = session.scalars(
            select(JourneyShareGrant).where(JourneyShareGrant.token == token)
        ).first()
        grant_id = grant.id
    revoked = client.post(
        f"/api/v1/journeys/{journey_id}/share-grants/{grant_id}/revoke"
    )
    assert revoked.status_code == 200, revoked.text[:300]

    answer = client.get(TOKEN_PATH.format(token=token))
    assert answer.status_code == 409, answer.text[:300]
    assert _reason_code(answer) == "SHARE_GRANT_REVOKED"
    details = json.loads(answer.text).get("error", {}).get("details") or {}
    assert details.get("state") == "REVOKED", details
    assert details.get("revoked_at"), details


def test_ord0465_expired_grant_is_named_and_is_not_the_revoked_word():
    journey_id, _ = _journey_with_plan()
    token = _grant_that_has_aged(journey_id)
    deadline = None
    with SessionLocal() as session:
        deadline = session.scalars(
            select(JourneyShareGrant).where(JourneyShareGrant.token == token)
        ).first().expires_at

    answer = client.get(TOKEN_PATH.format(token=token))
    assert answer.status_code == 409, answer.text[:300]
    assert _reason_code(answer) == "SHARE_GRANT_EXPIRED"
    details = json.loads(answer.text).get("error", {}).get("details") or {}
    assert details.get("state") == "EXPIRED", details
    assert details.get("expires_at") == deadline, details


def test_ord0465_binned_journey_refuses_the_read_by_name():
    """The fourth state. Before this door looked at `journeys.deleted_at` the same request answered
    200 with the trip's title, so a share holder read a journey its owner had thrown away."""
    journey_id, _ = _journey_with_plan()
    token = _grant(journey_id)
    deleted = client.delete(f"/api/v1/journeys/{journey_id}")
    # 204 is what this door answers with (`backend/app/main.py`, the DELETE beside the undelete door);
    # the trip is binned, and there is no body to return.
    assert deleted.status_code == 204, deleted.text[:300]

    answer = client.get(TOKEN_PATH.format(token=token))
    assert answer.status_code == 409, answer.text[:300]
    assert _reason_code(answer) == "SHARE_GRANT_JOURNEY_IN_RECYCLE_BIN"
    assert journey_id not in answer.text, "the refused body must not carry the trip it is refusing"
    assert "沿海一周" not in answer.text


def test_ord0465_a_dead_grant_is_still_named_dead_when_the_trip_was_also_binned():
    """Order matters and it is a promise, not a detail: the grant's own state is decided first, so a
    link that expired says `EXPIRED` even about a binned trip. The owner's bin is not the reason this
    reader was stopped."""
    journey_id, _ = _journey_with_plan()
    token = _grant_that_has_aged(journey_id)
    assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204

    answer = client.get(TOKEN_PATH.format(token=token))
    assert _reason_code(answer) == "SHARE_GRANT_EXPIRED", answer.text[:300]


# ------------------------------------------------------------------ the success shape


def test_ord0465_success_carries_the_stops_the_landing_screen_has_to_draw():
    journey_id, place_id = _journey_with_plan(stops=2)
    token = _grant(journey_id)

    answer = client.get(TOKEN_PATH.format(token=token))
    assert answer.status_code == 200, answer.text[:400]
    body = answer.json()
    assert body["id"] == journey_id
    assert body["title"] == "沿海一周"
    assert body["timezone"] == "Asia/Shanghai"
    assert body["plan_state"] == "PLAN_PRESENT"
    assert body["active_plan"]["version_no"] == 3
    assert body["active_plan"]["source"] == "INITIAL"
    stops = body["active_plan"]["stops"]
    assert [item["sequence_no"] for item in stops] == [1, 2]
    # The place's own name, not the planner's typed title: the screen prefers it, and can only do so
    # if the door sends both.
    assert stops[0]["place_name"] == "鼓浪屿轮渡码头 1"
    assert stops[0]["title"] == "stop-1"
    assert stops[0]["coordinate"]["state"] == "READY"
    assert stops[0]["coordinate"]["latitude"] == 24.451
    assert stops[0]["coordinate"]["coordinate_system"] == "WGS84"


def test_ord0465_success_says_the_owner_flags_and_position_are_not_on_this_answer():
    """Absence has to be a decision somebody wrote down, because these are the four things a screen
    would otherwise help itself to: the owner's live position, the owner's lock flags, the store's own
    transition record, and the place identities behind the names."""
    journey_id, _ = _journey_with_plan(stops=2)
    body = client.get(TOKEN_PATH.format(token=_grant(journey_id))).json()
    stop = body["active_plan"]["stops"][0]
    for absent in ("locked", "protected", "metadata", "place_id", "city_service_id"):
        assert absent not in stop, absent
    for absent in ("user_location", "disruptions", "legs", "cursor_status"):
        assert absent not in body["active_plan"], absent


def test_ord0465_no_plan_is_named_and_not_left_to_be_inferred_from_a_null():
    """`active_plan: null` alone is two different facts wearing one shape: this trip never had a plan,
    and this trip points at a plan row that is not there.  `plan_state` is why the field exists."""
    journey_id, _ = _journey_with_plan(stops=1)
    with SessionLocal() as session:
        session.get(Journey, journey_id).active_plan_version_id = None
        session.commit()
    body = client.get(TOKEN_PATH.format(token=_grant(journey_id))).json()
    assert body["plan_state"] == "NO_PLAN"
    assert body["active_plan"] is None


def test_ord0465_a_dangling_plan_pointer_fails_closed_rather_than_answering_a_plan():
    """The third `plan_state`, and the reason it is tested on the function rather than through HTTP.

    `journeys.active_plan_version_id` is a foreign key to `plan_versions` (`app/db/models.py`), so
    the store will not hold a pointer to a row that is not there: measured, writing one through the
    same session answers `sqlite3.IntegrityError: FOREIGN KEY constraint failed`.  That is why no
    request in this file can reach this branch -- and why the branch is still here.  A column with no
    enforced reference in some future store, or a row deleted underneath a connection that turned
    enforcement off, must produce a named "the plan this trip points at is gone", never a plan list
    assembled from nothing.
    """
    from types import SimpleNamespace

    with SessionLocal() as session:
        stub = SimpleNamespace(active_plan_version_id=str(uuid4()), id="stub")
        assert _shared_plan_projection(session, stub) == ("PLAN_ROW_MISSING", None)


def test_ord0465_a_stop_whose_place_row_was_deleted_names_which_nothing_it_is():
    """Reachable, because the store does it for you: `activities.place_id` is
    `ForeignKey("places.id", ondelete="SET NULL")` (`app/db/models.py`), so deleting the place
    clears the identity rather than leaving a dangling reference.  The stop is then "an activity with
    no canonical identity", which is what the frozen reason token says, and the screen draws no pin
    for it."""
    journey_id, place_id = _journey_with_plan(stops=1)
    with SessionLocal() as session:
        session.delete(session.get(Place, place_id))
        session.commit()
    body = client.get(TOKEN_PATH.format(token=_grant(journey_id))).json()
    stop = body["active_plan"]["stops"][0]
    assert stop["coordinate"]["state"] == "UNAVAILABLE"
    assert stop["coordinate"]["reason"] == "NO_CANONICAL_IDENTITY"
    assert stop["coordinate"]["latitude"] is None
    assert stop["place_name"] is None


def test_ord0465_an_identity_the_catalog_does_not_hold_says_coverage_not_identity():
    """The other half of "nothing": the activity still names a place, and the place is not in what was
    read.  Those two read differently to a traveller -- one is "this stop has no address", the other is
    "we have not catalogued this city that far" -- and the pair is the same one the owner's map reads
    (`app/stay_mobility.py`).

    On the function, not through HTTP, for the reason in the test above: the store's own foreign key
    makes `place_id` NULL rather than let it name a missing row.
    """
    coordinate = activity_coordinate("a-place-nobody-read", {})
    assert coordinate["state"] == "UNAVAILABLE"
    assert coordinate["reason"] == "COVERAGE_INCOMPLETE"


# ------------------------------------------------------------------ what this read must never do


def test_ord0465_the_read_writes_nothing():
    """The order's first rule, and this project has the history to justify pinning it:
    `GET /journeys/{id}/experience` writes while it answers and 500s when it loses the lock.  A share
    link opening must not be able to do that to a stranger's device."""
    journey_id, _ = _journey_with_plan(stops=2)
    token = _grant(journey_id)
    with SessionLocal() as session:
        before_updated = session.get(Journey, journey_id).updated_at
        before_audit = len(session.execute(text("SELECT 1 FROM audit_logs")).all())
    answer = client.get(TOKEN_PATH.format(token=token))
    assert answer.status_code == 200, answer.text[:300]
    with SessionLocal() as session:
        assert session.get(Journey, journey_id).updated_at == before_updated
        after_audit = len(session.execute(text("SELECT 1 FROM audit_logs")).all())
    assert after_audit == before_audit


def test_ord0465_the_word_naming_the_generation_is_the_same_word_the_plan_itself_uses():
    """`SharedPlan.source` copies an enumeration out of `PlanVersion`, and a copy is exactly the thing
    that drifts.  Pinned against the model for the same column rather than against a list written in
    this file, so widening a `plan_versions.source` value fails here before it can reach a screen."""
    import typing

    from app.schemas import PlanVersion as PlanVersionSchema
    from app.schemas import SharedPlan

    mine = set(typing.get_args(SharedPlan.model_fields["source"].annotation))
    theirs = set(typing.get_args(PlanVersionSchema.model_fields["source"].annotation))
    assert mine == theirs, mine ^ theirs


def test_ord0465_every_word_this_answer_offers_has_a_label_the_traveller_reads():
    """The rule this order keeps having to relearn: a machine token that reaches a Chinese screen is a
    defect no matter how correct it is in the database.

    Every enumeration the share answer carries is therefore checked against the table the front uses
    to say it -- `PLAN_SOURCE` for the generation, `CAPABILITY_STATES` for a coordinate -- and the
    plan's own `status` is asserted ABSENT rather than labelled, because two of its five values
    (`SELECTED`, `SUPERSEDED`) have no entry in `STATUS_LABELS`
    (`frontend/src/i18n/travelerLabels.ts`) and the screen would have nothing to do with them but
    echo them.  The tables are read out of the TypeScript rather than restated here: a label list
    copied into a Python test would drift in exactly the way it is supposed to catch."""
    import re
    import typing
    from pathlib import Path

    from app.schemas import SharedPlan

    front = Path(__file__).resolve().parents[2] / "frontend" / "src"

    def table_keys(file_name: str, table: str) -> set[str]:
        """The keys of one `const TABLE: Record<string, Label> = { KEY: { zh: … }, … }` block, read off
        the TypeScript the front actually ships."""
        text = (front / file_name).read_text(encoding="utf-8")
        start = text.index(f"const {table}")
        body = text[start : text.index("};", start)]
        return set(re.findall(r"^\s{2}([A-Z_]+):", body, re.M))

    assert set(typing.get_args(SharedPlan.model_fields["source"].annotation)) == table_keys(
        "lib/displayVocabulary.ts", "PLAN_SOURCE"
    ), "a `source` value with no label would be echoed to a traveller as a machine word"

    assert "status" not in SharedPlan.model_fields, (
        "an unlabelled token must not be on this answer"
    )
    unlabelled = {"SELECTED", "SUPERSEDED"} - table_keys("i18n/travelerLabels.ts", "STATUS_LABELS")
    assert unlabelled == {"SELECTED", "SUPERSEDED"}, (
        f"if the front gains these labels, `status` may come back: {unlabelled}"
    )


def test_ord0465_a_version_scoped_grant_carries_both_reads_and_neither_one_is_inferred():
    """The merge test, not a courtesy one.

    ORD-0466 landed on the authoritative line while this order was being written and put a plan on the
    same answer under different names.  The two are different questions -- what the trip's own days
    are, and what this particular grant covers -- so the door now answers both, and this pins that
    adding one did not quietly change the other: a version-scoped grant still says which version it is
    scoped to, and the guest's day list is still the journey's active plan rather than the scoped one.
    Asserted together because the failure mode of a merge like this is one half being read as if it
    answered the other."""
    journey_id, _ = _journey_with_plan(stops=2)
    with SessionLocal() as session:
        plan_id = session.get(Journey, journey_id).active_plan_version_id

    scoped = client.post(
        f"/api/v1/journeys/{journey_id}/share-grants",
        json={
            "visibility": "LINK_READ",
            "created_by": "ord0465-probe",
            "plan_version_id": plan_id,
        },
    )
    assert scoped.status_code == 201, scoped.text[:300]
    body = client.get(TOKEN_PATH.format(token=scoped.json()["token"])).json()

    assert body["grant_scope"] == "PLAN_VERSION", body.get("grant_scope")
    assert body["shared_plan"] is not None and body["shared_plan"]["id"] == plan_id
    assert body["plan_state"] == "PLAN_PRESENT"
    assert [item["sequence_no"] for item in body["active_plan"]["stops"]] == [1, 2]

    # And a journey-level grant says so, rather than leaving a null for the screen to interpret.
    # `shared_plan` is ABSENT there, not null: that is ORD-0466's own rule and this lane does not
    # soften it (`test_qoder_ord0466_version_share.py::test_v1_...`).
    plain = client.get(TOKEN_PATH.format(token=_grant(journey_id))).json()
    assert plain["grant_scope"] == "JOURNEY", plain.get("grant_scope")
    assert "shared_plan" not in plain
    assert plain["plan_state"] == "PLAN_PRESENT"


def test_ord0465_the_projection_does_not_reach_a_routing_provider():
    """Legs are absent from this answer (see `SharedPlan`), so a stop's position can only have come
    from the place row -- which means this read cannot fail because a provider is down, and cannot
    invent a route the server has no evidence for."""
    journey_id, _ = _journey_with_plan(stops=2)
    body = client.get(TOKEN_PATH.format(token=_grant(journey_id))).json()
    assert "legs" not in body["active_plan"]
    assert all(item["coordinate"]["state"] == "READY" for item in body["active_plan"]["stops"])


# ------------------------------------------------------------------ contract pins


def test_ord0465_the_answer_is_a_superset_of_the_journey_the_owner_reads():
    """The share answer must be the accepted `Journey` body plus the shared-read fields -- not a
    re-spelling of it.  Checked between two live bodies rather than between two models, because this
    door has no response model to check: a screen that reads `title` from this door and from
    `/journeys/{id}` has to get the same field both times.

    Four fields are added, and each is attributed.  `plan_state` and `active_plan` are this order's;
    `grant_scope` and `shared_plan` are ORD-0466's, which landed on the authoritative line while this
    file was being written.  The set is asserted exactly, so a fifth field added by anybody has to be
    attributed here the same way."""
    journey_id, _ = _journey_with_plan(stops=1)
    token = _grant(journey_id)
    shared = client.get(TOKEN_PATH.format(token=token)).json()
    owner = client.get(f"/api/v1/journeys/{journey_id}").json()

    added = set(shared) - set(owner)
    assert added == {"plan_state", "active_plan", "grant_scope"}, added
    assert set(owner) <= set(shared), sorted(set(owner) - set(shared))
    # `shared_plan` is present only on a version-scoped grant -- ORD-0466 pins that absence itself in
    # `test_qoder_ord0466_version_share.py::test_v1_...`, and this file does not re-litigate it.
    assert "shared_plan" not in shared
    assert SharedStop.model_fields["coordinate"].annotation is SharedActivityCoordinate


def test_ord0465_coordinate_states_are_the_frozen_four_and_no_more():
    """`SharedActivityCoordinate` re-declares the vocabulary because a contract needs the enumeration;
    this test is what stops the re-declaration from becoming a second truth.  It pins the field's
    own type rather than a copy of it, so renaming a state in `anywhere_capability.py` fails here."""
    import typing

    annotated = typing.get_args(
        SharedActivityCoordinate.model_fields["state"].annotation
    )
    assert set(annotated) == set(CAPABILITY_STATES), annotated


def test_ord0465_the_contract_the_client_is_generated_from_carries_every_field_this_door_sends():
    """`contracts/openapi.yaml` is hand-written and `frontend/src/generated/api.ts` is built from it,
    while FastAPI's own `/openapi.json` is built from a `response_model` this door deliberately does
    not have.  So the only contract a client can be checked against on this side is the YAML one, and
    this reads it directly: every field the door puts on a 200 has to be in the arm the client is
    generated from, or the screen type-checks against a shape nobody promised.

    The gap this test does NOT close, named rather than papered over: with no response model,
    `/openapi.json` describes this body as a bare object, so the two OpenAPI artifacts on this tree
    still disagree about this door.  Attaching a model would write a `null` into `shared_plan` on a
    journey-level grant, which is the one thing ORD-0466's own gate forbids -- so the choice is left
    for the commander, and this lane's single-question block carries it."""
    from pathlib import Path

    import yaml

    spec = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "contracts" / "openapi.yaml").read_text(
            encoding="utf-8"
        )
    )
    arm = spec["paths"]["/shared/journeys/{token}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["allOf"][1]
    assert set(arm["properties"]) == {
        "grant_scope", "shared_plan", "plan_state", "active_plan",
    }, sorted(arm["properties"])
    assert set(arm["properties"]["plan_state"]["enum"]) == {
        "PLAN_PRESENT", "NO_PLAN", "PLAN_ROW_MISSING",
    }, arm["properties"]["plan_state"]["enum"]
    assert arm["properties"]["grant_scope"]["enum"] == ["JOURNEY", "PLAN_VERSION"]
    assert set(arm["required"]) == {"grant_scope", "plan_state"}, arm["required"]

    journey_id, _ = _journey_with_plan(stops=1)
    body = client.get(TOKEN_PATH.format(token=_grant(journey_id))).json()
    owner = client.get(f"/api/v1/journeys/{journey_id}").json()
    beyond_the_journey = set(body) - set(owner)
    assert beyond_the_journey <= set(arm["properties"]), sorted(
        beyond_the_journey - set(arm["properties"])
    )

    coordinate = spec["components"]["schemas"]["SharedActivityCoordinate"]["properties"]["state"]
    assert set(coordinate["enum"]) == set(CAPABILITY_STATES), coordinate
